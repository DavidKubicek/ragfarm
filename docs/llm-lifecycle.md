# LLM life-cycle — models, slots, and running the stack

How a model gets from Hugging Face onto the box, into GPU memory, and in front of
a user; and how the whole system is started, stopped and checked. Three tools and
one registry, each with a full manual page under `docs/man1/`.

```
fetch_llm.py     ->  on disk + registered      "we have it"
activate_llm.py  ->  bound to a slot, serving  "it is resident"
setup_openwebui  ->  preset bound to its alias "a human can pick it"
stack.sh         ->  the whole system up, and honestly checked
```

## What a slot is

vLLM serves exactly **one** base model per process. There is no multi-model mode:
several `--served-model-name` values are aliases for the same weights, and LoRA
adapters need a shared base. A *slot* is therefore this project's name for one
vLLM instance:

| slot | systemd unit | endpoint |
|---|---|---|
| 0 | `ragfarm-vllm@0.service` | `127.0.0.1:8080/v1` |
| 1 | `ragfarm-vllm@1.service` | `127.0.0.1:8082/v1` |

Port is `8080 + 2N`; the stride is 2 because 8081 belongs to the reranker.

**Why two.** Two occupied slots mean two models resident at once, and Open WebUI
can switch between them **mid-conversation with the context intact**. That is not
a convenience feature — it is the differential-diagnosis instrument. When an
answer is wrong, asking a second model the same question with the same history
separates "the model cannot do this" from "our prompt told it not to". Most of
the August prompt fixes were found that way.

## The memory budget, which is the whole difficulty

`--gpu-memory-utilization` is a fraction of **total** device memory, and every
vLLM instance computes it **independently** — neither slot knows the other
exists. Two slots each asking for the single-model default of 0.50 will OOM. On a
GB10 there is no separate VRAM to retreat into: 121.7 GiB is shared with the
embedder, the reranker, Qdrant, the container plane and the operating system.

So the per-slot fraction is derived, never chosen:

```
util = (weights + KV_TARGET_GIB + OVERHEAD_GIB) / TOTAL_GIB
```

with `weights` taken from the size recorded **after** the download was verified
against the Hub — never from `du`, because a half-finished checkpoint measures
small and would under-allocate the slot. The sum across slots must stay under
`BUDGET_CEILING` (0.72), leaving at least 28% for everything that is not an LLM.
Exceeding it is a hard refusal, not a warning.

Today's configuration, as reported by `activate_llm.py --status`:

```
slot  port   util    model
0     8080   0.338   Qwen3-VL-30B-A3B-Thinking-FP8   [MoE]
1     8082   0.237   Qwen3-VL-30B-A3B-Instruct-NVFP4 [MoE]

total GPU budget: 0.575 of 0.72 ceiling
```

## The registry

`models/llm/active.json` is git-tracked and answers two questions that change on
different timescales:

- **`downloaded[]`** — every model this deployment should have on disk. You fetch
  a model once.
- **`active[]`** — which of them are resident, one array position per slot, as
  **indexes into `downloaded[]`**. You re-bind slots several times a day.

Storing the binding as an index means a slot cannot reference a model that was
never registered. Each entry carries the directory name, the Hub repo, the served
alias, the Open WebUI preset id, a display label, a tuning profile and the
verified size. A machine can be rebuilt from this file with one command.

`model`, `alias` and `preset` must each be unique. The tools **refuse to run** on
a duplicate rather than disambiguating, because choosing which name wins is a
decision only a person should make — silently renaming produces a model picker
whose entries do not mean what they say.

## Everyday commands

```bash
scripts/stack.sh status                    # every service, endpoint, state
scripts/activate_llm.py --status           # slots, models, memory budget
scripts/activate_llm.py                    # interactive: activate or clear
```

```bash
scripts/activate_llm.py -s 0 -m Qwen3-VL-30B-A3B-Thinking-FP8
```

One call rewrites the slot's config, restarts its unit, waits for it, and
re-binds the Open WebUI presets.

Adding a model:

```bash
scripts/fetch_llm.py -m <hf-repo> \
    --alias <served-name> --preset <owui-preset-id> \
    --display '<label for the picker>' --profile vision-instruct
```

Uniqueness is checked **before** the transfer, so a name clash costs a typo
rather than an afternoon and 70 GB.

## Four things that will bite

**A model swap without re-binding the presets.** Presets bind by alias. Change
what a slot serves and the preset names an alias nothing serves; the picker then
offers only raw base models — no system prompt, no tools, no grounding rules.
Nothing looks broken and the answers are silently ungrounded. `activate_llm.py`
now re-binds automatically, and prunes presets whose model is gone.

**Starting slots in parallel.** vLLM profiles free GPU memory during
initialisation; if a second instance is allocating at the same moment the first
sees memory move underneath it and dies with `Error in memory profiling` or `No
available memory for the cache blocks`. Neither message names the race. The tools
serialise; `systemctl start a b c` does not.

**The reasoning parser is a property of the checkpoint.** A Thinking model needs
`--reasoning-parser qwen3`; an Instruct model must not have it. The qwen3 parser
keys on `</think>`, and finding none it files the model's *entire answer* as
reasoning — the chat message comes back empty with the whole reply inside the
collapsed thinking trace, which is convincingly mistakable for a broken
quantisation. It is written per slot from the registry's `profile` field.

**`max_tokens` shares the context window with the prompt.** vLLM does not clamp
it; it rejects the request. A generous cap works until a tool result grows the
prompt on the continuation request, and then every tool-using turn returns
nothing. With `--max-model-len 32768`, `max_tokens` 8192 leaves 24 k of prompt
headroom, which is the reason it is set there.

## Boot behaviour

**Slot 0 is enabled at boot; slot 1 deliberately is not.** Enabling both would
have systemd start them in parallel, which is exactly the race above, and the
serialisation lives in the tooling rather than in the unit. The box comes up
serving the MoE; a second model is a deliberate act.

After a reboot, this is the check:

```bash
scripts/stack.sh status
```

It probes all thirteen services and reports `[OK]`, `[NOT_OK: code]`,
`[DEGRADED]`, `[OFF]` or `[ABSENT]`. `[DEGRADED]` is the interesting one — a
service that answers correctly and is still broken, such as mcpo serving 200 with
zero tools mounted, or nginx serving its landing page while the draw.io viewer
JavaScript is absent. Both of those happened, and both were invisible to a
status-code check.

## Manual pages

Full detail, including the single-large-model workflow and the failure modes
above with their diagnostics:

```bash
man docs/man1/stack.1          # lifecycle and health
man docs/man1/activate_llm.1   # slots and the memory budget
man docs/man1/fetch_llm.1      # downloading and registering
man docs/man1/active.json.1    # the registry format
man docs/man1/setup_openwebui.1
man docs/man1/env.1            # how configuration propagates
```
