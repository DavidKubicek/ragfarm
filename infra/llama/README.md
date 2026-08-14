# llama.cpp — CUDA build, for the reranker only

**Scope, because this used to say something else.** llama.cpp is no longer the
generation engine. ADR-0013 moved generation to vLLM, and every LLM in this
deployment is served by `ragfarm-vllm@N` on the OpenAI-compatible API. What
survives on llama.cpp is exactly one service:

> **`ragfarm-reranker`** — `bge-reranker-v2-m3` on `llama-server --reranking`,
> `127.0.0.1:8081/reranking`. A cross-encoder, not a generator. See ADR-0008.

Everything about AMD, Vulkan, iGPU offload, `--mmproj` vision projectors and GGUF
generation models has been removed from this file. It described the retired box
and was actively misleading next to a CUDA deployment. Git history has it if you
ever need the provenance.

Why keep llama.cpp at all: `--reranking` gives a small, well-behaved
cross-encoder server with no Python runtime, negligible memory, and no
competition for vLLM's memory budget. Replacing it with a second vLLM instance
would cost more GPU than the reranker itself uses.

## Build

CUDA, not Vulkan. The target is a GB10 Grace Blackwell, compute capability
**sm_121**.

```bash
git clone https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release
cmake --build "$LLAMA_DIR/build" --config Release -j "${MAX_JOBS:-8}"
```

Two things that bite:

- **`MAX_JOBS` is load-bearing.** Unbounded, ninja runs `nproc+2` parallel CUDA
  compiles and the OOM killer takes the build at ~98 GB. Same lesson as the vLLM
  build; a different budget from `--gpu-memory-utilization` entirely.
- **`$LLAMA_DIR`** is where the rest of the tooling expects to find it.
  `scripts/deploy.sh` and `scripts/fetch-encoder.sh` both check for
  `$LLAMA_DIR/build/bin/llama-server` and refuse to continue without it. The
  reranker unit currently hardcodes its path rather than reading `LLAMA_DIR` —
  a known inconsistency, noted in `PROGRESS.md`.

Verify:

```bash
"$LLAMA_DIR/build/bin/llama-server" --version
scripts/stack.sh status          # reranker row should read [OK]
```

## Running it

You should not need to. `ragfarm-reranker.service` owns it, and
`scripts/stack.sh` starts, stops and health-checks it with everything else. The
unit runs at a mildly lowered scheduling priority so the interactive LLM wins any
contention — the reranker is the most expensive stage of retrieval (~297 ms) but
it is not the one a human is waiting on keystroke by keystroke.

By hand, for debugging only:

```bash
"$LLAMA_DIR/build/bin/llama-server" --reranking \
    -m "$RERANK_MODEL_PATH" --host 127.0.0.1 --port 8081
```

```bash
curl -s http://127.0.0.1:8081/health
```

## See also

- `models/reranker/MODEL.md` — which checkpoint, and why full quality
- `docs/decisions/ADR-0008` — why a cross-encoder reranker at all
- `docs/decisions/ADR-0013` — the engine split that retired llama.cpp for generation
- `man docs/man1/stack.1` — lifecycle and health checking
