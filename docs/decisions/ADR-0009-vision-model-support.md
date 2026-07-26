# ADR-0009 — Vision model support (Qwen3-VL family, dual-preset UI)
Author: David Kubicek (david.kubicek@eywo.cz)
Status: ACCEPTED (2026-07-26). Verified end-to-end against a live Qwen3-VL-8B-Thinking
GGUF on `llama-server`/Vulkan (iGPU): the openlm.ai receipt OCR example returns
correct text at ~8.13 tok/s decode. Retrieval, tools, and mermaid remain
identical to the text engine; only the generative model, sampler, and OWUI
preset differ.
Date: 2026-07-26
Builds on: ADR-0001 (engine split — llama.cpp/Vulkan on iGPU is the generative
plane; unchanged), ADR-0003 (Open WebUI + mcpo agent layer — presets and tool
routing live here), ADR-0007/0008 (retrieval + reranker — unchanged; the vision
engine reuses the same `search_corpus`).
Scope: which vision-language models we support, how they load in the *same*
`ragfarm-llama` service (mmproj auto-detect), how OWUI exposes them alongside the
text engine as a distinct preset, why the vision sampler is non-greedy, and how
draw.io diagrams render in-chat without a live internet dep.

## Context

Two Monday requirements drove this: (a) demonstrate that the same on-prem stack
can read images (receipts, scanned docs, diagram screenshots) — not just text; and
(b) support Qwen's *Thinking* variants (visible reasoning traces, better long-form
answers) alongside the *Instruct* variant we've been driving greedy for
reproducibility. Both point at the Qwen3-VL family — the smallest generation of
"thinking VL" models we can actually run under 24 GB VRAM at usable speed on the
Ryzen AI 9 iGPU.

llama.cpp added first-class Qwen3-VL support in mid-2026 (arch strings
`qwen3vl` in `src/models/qwen3vl.cpp` and the MoE variant `qwen3vlmoe.cpp`,
CLIP graph in `tools/mtmd/clip.cpp`), so no engine change is needed — the same
`llama-server` we already run loads the vision model when pointed at a VL GGUF
plus its multimodal projector (`mmproj-*.gguf`).

## Decision

**One engine, two OWUI presets.** `ragfarm-llama.service` continues to be the
single generative endpoint on `127.0.0.1:8080`; whichever GGUF is loaded (text or
VL) is served under the alias derived from its dir name (`--alias` set by
`scripts/llama-launch.sh` from `$LLM_GGUF_PATH`). Open WebUI holds two persisted
model presets side-by-side, each pinned to a different `base_model_id`:

- **`ragfarm`** — greedy sampler (temp 0, top_k 1, seed 42), full text-tuning
  from RULES 1-5 (tools/tables/mermaid/coding). Points at
  `qwen2.5-7b-instruct` (or whatever text alias is live).

- **`ragfarm-vision`** — non-greedy (temp 0.6; **`top_k`/`top_p`/`min_p`/`seed`
  intentionally OMITTED** so llama.cpp's own nucleus defaults apply — mixing an
  explicit temp with hand-set shape knobs re-introduces the very determinism
  trap this preset is meant to avoid). `base_model_id` auto-detected from
  `/v1/models` (first entry with capability `multimodal`; overridable via
  `VISION_BASE_MODEL_ID` env). Vision + file upload capabilities ON; RULE 5
  adds draw.io HTML rendering next to mermaid.

Only the preset whose `base_model_id` matches the currently-loaded alias is
usable at any given moment — the other stays as stored config waiting for the
next model swap. Both presets share the same tool set (RAG, placement,
reboot_guarded), so either engine can drive the infra.

**Model swap flow (Instruct ↔ Thinking, or text ↔ vision):**

1. `scripts/fetch-llm.sh --repo … --file …` (once per model; auto-detects and
   pulls the sibling `mmproj-*.gguf` for vision repos — no manual `--mmproj`).
2. `scripts/activate-llm.sh` (interactive list, or `--dir <slug>`) writes the
   chosen `LLM_GGUF_PATH` + `LLM_GGUF_MMPROJ` into `.env`. The mmproj var is
   *cleared* when switching to a text-only model so a stale `--mmproj` never
   leaks in.
3. `sudo systemctl restart ragfarm-llama` — the wrapper picks up the new env,
   derives `--alias` from the model's dir, and conditionally adds `--mmproj` when
   set (bash `if [ -n … ]`, not systemd's `$VAR:+…` — systemd's own substitution
   doesn't understand bash conditional expansion).
4. OWUI: the matching preset is now usable; the other is stored-but-inert.

**Vision-language file layout on HF (auto-detect heuristic).** VL repos ship a
main GGUF plus a separate `mmproj-<name>.gguf` (the multimodal projector — a
small adapter tying the vision encoder into the LM). `hf_pick_mmproj()` in
`scripts/lib-models.sh` chooses the smallest reasonable projector quant:
quant-match with the main file if available (rare), else Q8_0, else f16, else
bf16, else alphabetically first. This saves ~500 MB VRAM on repos that offer
both Q8_0 and f16 projectors (confirmed on `ggml-org/Qwen2.5-VL-7B-Instruct-GGUF`:
Q8_0 = 853 MB vs f16 = 1354 MB). The word-boundary regex in that function is
non-trivial: naive substring matching false-matched `f16` inside `bf16`.

## Instruct vs Thinking — sampler & thinking control

Qwen3 nominally exposes `/think` and `/no_think` control tokens **for the base
text template only**. The Qwen3-VL-**Thinking** chat template (baked into the
GGUF at `tokenizer.chat_template`) is different:
- No `enable_thinking` kwarg — `chat_template_kwargs.enable_thinking` is
  silently ignored (llama.cpp passes it through, but the template has no
  matching `{% if %}` gate to read it).
- The `add_generation_prompt` block ends with a hardcoded
  `<|im_start|>assistant\n<think>\n` — every assistant turn is *forced* to
  begin a reasoning block. There is no template-level switch to turn thinking
  off for a single turn.

Empirically confirmed by probing the live GGUF (2026-07-27): identical
`reasoning_content` output with and without the kwarg. `/no_think` in a user
message is likewise a no-op with this template.

**Consequence for the vision preset.** To get "no-think by default with
`/think` as opt-in", the honest options are:

1. **Swap to `Qwen3-VL-8B-Instruct`** — never thinks, no per-turn opt-in to
   turn it back on (Instruct simply doesn't do reasoning mode). Fast + quiet.
2. **Custom `--chat-template <file>` override** that grafts the text-template's
   `enable_thinking` block onto the VL template. Fragile: needs re-verification
   on every model upgrade, and subtle template drift can break vision or
   tool-calling in non-obvious ways.
3. **Post-strip `<think>…</think>` from the response** — cosmetic only, still
   pays full latency. Bad trade.

Option 1 is the recommended path for the vision preset baseline; keep the
Thinking GGUF on disk as an explicit `activate-llm.sh` swap for the rare
questions where the reasoning trace is worth the wait.

**Why the vision preset must be non-greedy (either variant).** Thinking models
loop on greedy decode — the reasoning trace can re-derive the same intermediate
step forever because the argmax never provides an escape. Qwen's own guidance
is `temperature ≈ 0.6` as a floor, and `top_k`/`top_p`/`min_p` left at defaults
so nucleus sampling picks a variety of continuations. This preset therefore
*intentionally* sacrifices bit-for-bit reproducibility — outputs vary between
runs, and that's the trade for coherent multi-step reasoning. The text preset
stays greedy for the reproducibility the tables/coding rules depend on.

**Why the vision preset must be non-greedy.** Thinking models loop on greedy
decode — the reasoning trace can re-derive the same intermediate step forever
because the argmax never provides an escape. Qwen's own guidance is
`temperature ≈ 0.6` as a floor, and `top_k`/`top_p`/`min_p` left at defaults so
nucleus sampling picks a variety of continuations. This preset therefore
*intentionally* sacrifices bit-for-bit reproducibility — outputs vary between
runs, and that's the trade for coherent multi-step reasoning. The text preset
stays greedy for the reproducibility the tables/coding rules depend on.

## draw.io in-chat rendering (RULE 5 diagrams)

OWUI's HTML preview sandbox renders any fenced ```html block as an iframe. The
draw.io viewer (`viewer-static.min.js`) auto-attaches to any `<div
class="mxgraph">` in the DOM and turns the enclosed `<mxfile>…</mxfile>` XML into
an interactive pan/zoom/lightbox canvas. The system-prompt template gives the
model the exact wrapper to output.

### Why we run our own nginx (the load-bearing "why")

The draw.io reference deployment lives at `viewer.diagrams.net` — one script
(`js/viewer-static.min.js`) plus five sibling trees the viewer XHR-loads at
runtime:

- `/styles/*.xml`      — visual themes and shape-style presets
- `/shapes/*` + `/shapes/*.js`  — the JS-side shape catalogues (BPMN, AWS, mockup, ArchiMate, ER, iOS7, …)
- `/stencils/*.xml`    — XML stencil libraries (AWS, Azure, GCP, Cisco, IBM, Oracle, Kubernetes, VeeamGP, …)
- `/math4/es5/*`       — MathJax for equations inside diagrams
- `/img/*`             — UI icons, background tiles, expand/collapse arrows

The viewer JS hardcodes these prefixes at
`window.STYLE_PATH / SHAPES_PATH / STENCIL_PATH / DRAW_MATH_URL / GRAPH_IMAGE_PATH`,
each defaulting to `https://viewer.diagrams.net/…`. If those URLs are unreachable
(air-gapped demo, corporate proxy without diagrams.net whitelisted, or just flaky
conference Wi-Fi), the diagram renders as **blank white space** the moment the
model uses any shape more advanced than a plain rectangle. This is the failure
mode we absolutely can't have live — a diagram request that visibly returns
nothing invalidates the whole vision demo.

So the vision path *depends* on a local mirror of the entire draw.io webapp,
not just the script. A stub with only `viewer-static.min.js` is exactly the
"looks fine in dev, dies on stage" trap.

### What the nginx serves

`infra/compose.yaml` runs an `nginx:alpine` container (`drawio-viewer`) mounting
`infra/drawio-viewer/` read-only on `0.0.0.0:80` — LAN-reachable so demo
attendees on their own laptops resolve the same URLs the model outputs.

Contents come from `jgraph/drawio`'s `src/main/webapp/` (~151 MB, upstream's
official tree of everything `viewer.diagrams.net` serves) — rehydrated by
`scripts/fetch-drawio-viewer.sh` (shallow + sparse-checkout clone, idempotent).
The 151 MB blob is gitignored; only two files are tracked:

- `index.html` — the landing page (see below).
- `drawio-editor.html` — the renamed upstream `index.html` (the full draw.io
  editor). Renamed to free `/` for our landing page.

The vision system prompt's RULE 5 draw.io template sets the five
`window.*_PATH` overrides to `http://127.0.0.1/…` **before** the viewer JS
loads, so no runtime fetch ever leaves the box. Verified end-to-end: `/`,
`/drawio-editor.html`, `/js/viewer-static.min.js`, `/styles/default.xml`,
`/stencils/aws4.xml`, `/shapes/mxAndroid.js` all return HTTP 200 with the
compose stack up. Air-gap-safe.

### CSP: env, not code patch

OWUI's HTML-preview iframe defaults to a strict Content-Security-Policy that
blocks all `script-src`, so even a same-origin script tag would render inert.
Investigated whether relaxing this needs a code patch (which would be a hard
no — patching an upstream image is un-supportable): it does not.
`open_webui/config.py:1696` reads `IFRAME_CSP` as a straight env var. We set it
in `infra/compose.yaml`'s `open-webui.environment` to a loose-but-scoped
policy: `script-src / style-src / img-src / connect-src / font-src` all admit
`'self'` plus `http://127.0.0.1` (with explicit `:80`); nothing else opens up.
No upstream image modification.

### Landing page — a genuine home + Eywo-styled

Since we already run an nginx to solve the diagram problem, `index.html` is a
tiny landing page rather than an empty directory listing. Styled to match the
Eywo brand (dark theme, mint accent, numbered Solutions row) so operators
landing on `http://<host>/` see something coherent, and adds a fourth Solutions
row **"04 · Custom original on-prem AI"** linking straight to OWUI on `:3000`.
Cheap; adds a real front door.

### Failure modes and their fixes

| Symptom in the OWUI HTML preview | Cause | Fix |
|----------------------------------|-------|-----|
| Blank canvas, no shapes at all   | `viewer-static.min.js` failed to load | Check `curl http://127.0.0.1/js/viewer-static.min.js` returns 200; if 404 → the mirror is a stub, run `scripts/fetch-drawio-viewer.sh` |
| Rectangles render but complex shapes are blank | Runtime shape/stencil fetch failed | Same fix — likely the mirror is partial |
| "Refused to load the script" in browser console | OWUI's CSP is still the default (strict) | `IFRAME_CSP` didn't take effect; `docker compose up -d --force-recreate open-webui` to reload env |
| Script loads but XHR to /shapes 404s | `window.*_PATH` overrides missing from the model's output | Re-run `setup_openwebui.py` — the prompt template is the source of truth for those |

## Format is user-chosen, not routed

An earlier iteration had the model auto-decide mermaid vs draw.io from keywords.
Dropped: routing is fragile on a small model, and users know which they want.
The prompt now says the model produces exactly one of the two formats — whichever
the user names — with no fallback and no both-at-once. If ambiguous, the model
asks. This trades one turn for zero confusion.

## Consequences

**Positive:**
- Two-model demo (text + vision) with no additional stack complexity —
  same `llama-server`, same tools, same UI, just a different preset selected.
- Vision path proven end-to-end: OCR of the openlm.ai Indonesian receipt at
  8.13 tok/s decode, all numeric fields recovered verbatim, no invention.
- Draw.io rendering works air-gapped (local nginx) — no demo-day internet gamble.
- Alias auto-derivation means `.env`/`activate-llm.sh` swaps update the OWUI
  base id automatically; nothing to reconfigure in OWUI on a text↔vision flip
  beyond selecting the other preset from the model dropdown.
- Sampler split (greedy for text, non-greedy for Thinking) contains the
  reproducibility trade to exactly the preset that needs the change.

**Negative / accepted:**
- **Vision decode ≈ text decode** (~8 tok/s on this iGPU — memory-bandwidth
  bound on shared LPDDR5x). Long OCR of a dense receipt takes ~40 s.
- **Vision preset is non-reproducible**. Same input → different output between
  runs. Acceptable because the text preset preserves reproducibility for the
  workflows that need it (tables, coding, regression tests).
- **draw.io stencils that live on `viewer.diagrams.net`** are missing when the
  demo box is offline. Basic shapes render fine; complex mockup sets don't.
- **Only one model loads at a time.** The alternate preset is stored but
  inert until the wrapper is restarted with a different `LLM_GGUF_PATH`. There
  is no side-by-side text-and-vision on the same GPU today (VRAM budget won't
  fit both).
- **`ctx=32768` with `-np 4` parallelism** multiplies KV allocation by ~4;
  monitored, not yet blocking. If it becomes so, drop `-np` before shrinking
  `-c` — reasoning quality suffers more from short context than from serialized
  requests.

## Open questions

1. **`/no_think` demo ergonomics.** Users won't type `/no_think` reliably.
   Should the OWUI preset expose a "fast mode" per-message toggle that prepends
   it? Not blocking Monday; add if the presentation runtime demands it.

2. **Draw.io stencil offline mirror.** Serving the whole `viewer.diagrams.net`
   `/styles` + `/shapes` tree would cover the last edge case. ~50 MB extra.
   Do it *if* a demo actually needs a mockup-family shape.

3. **Instruct variant of Qwen3-VL** (`Qwen/Qwen3-VL-8B-Instruct-GGUF`, non-Thinking)
   as a third preset — greedy-friendly VL for reproducible OCR/description
   flows. On disk if desired; the wrapper + auto-detect already handle it.

## References

- `scripts/llama-launch.sh` — wrapper (alias derivation, conditional `--mmproj`).
- `scripts/fetch-llm.sh`, `scripts/activate-llm.sh`, `scripts/lib-models.sh`
  (`hf_pick_mmproj`) — model management.
- `infra/openwebui/setup_openwebui.py` — dual-preset registration, capability
  matrix, vision system prompt with draw.io template.
- `infra/compose.yaml` — `drawio-viewer` nginx service, `IFRAME_CSP` env.
- `docs/deployment.md` → "Vision engine" section — practical usage / demo
  commands. `docs/deployment.md` → "Debug & measurement" — the tracing tools
  under `tests/tracing/`.
