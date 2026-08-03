# ADR-0002: Embedding model runs on CPU (BGE-M3), not the NPU
Author: David Kubicek (david.kubicek@eywo.cz)

Status: Accepted (2026-06-15) — supersedes the original NPU/Quark decision below.
**Amended by ADR-0013** (2026-08-03): the BGE-M3 model choice and the rejection of
the NPU/Quark path both stand unchanged; only the *host* moves, CPU → CUDA, on the
DGX Spark. NOTE for readers arriving from the filename: this ADR **rejects** Quark,
it does not adopt it.

## Context
The original plan (ADR-0001 and the first version of this ADR) put the embedding
model on the NPU, quantized via AMD Quark and served through the RyzenAI VitisAI
EP. An NPU embedder was built and technically passed a generic gate
(bge-small-en-v1.5, INT8/QDQ, static batch=1/seq=128, 384-dim).

That build was then found invalid **for this corpus**, on two independent grounds:

1. **Language.** The corpus is mixed Czech + English (infra docs, descriptions).
   bge-small-en-v1.5 is English-only; Czech text embeds with no meaningful
   semantics. The NPU constraint forced an English-only model because the
   multilingual alternatives do not fit the NPU.
2. **Shape.** VitisAI's vaiml compiler hard-crashes on dynamic shapes
   (`hasStaticShape()` assertion in `libvaiml.so`), forcing a static export at
   seq=128 (~90 words). The corpus is dominated by wide structured table rows
   (host → IP → VLAN → KVM host) and longer docx prose; seq=128 silently
   truncates them, corrupting embeddings in a way that is hard to debug later.

The NPU's advantage is high-throughput repeated inference. This workload is the
opposite: a one-time ingest of 10–30 docx/xlsx files and low-frequency retrieval
driven by an agent. There is no throughput pressure to justify the NPU's
compilation complexity or its model/shape constraints.

## Decision
**Run the embedder on the CPU. Use BAAI/BGE-M3.** Do not use the NPU or Quark for
embeddings.

Rationale for BGE-M3 specifically:
- Multilingual: 100+ languages incl. Czech, in a shared semantic space, so
  cross-lingual retrieval works (Czech query against English doc and vice versa).
- Long context: up to 8192 tokens — table rows and docx chunks fit without the
  seq=128 truncation that broke the NPU build.
- Hybrid from one model: emits dense (1024-dim) AND sparse (lexical) vectors in a
  single pass. The sparse vectors give exact-token matching for hostnames, IPs,
  and VLAN IDs — critical for this corpus — without a separate BM25 stage.
- CPU cost is acceptable: 568M params, run at ingest time and for occasional
  queries, not in a hot serving loop.

## Consequences
- The engine split (ADR-0001) is updated: **iGPU** = generative LLM
  (llama.cpp+Vulkan); **CPU** = BGE-M3 embedder (ONNX/FlagEmbedding) + Qdrant +
  MCP services. The NPU is no longer on the critical path for RAG.
- The NPU remains brought-up and available (step 01 stands) for possible future
  use — e.g. an encoder-only reranker or a Whisper encoder — but nothing in the
  RAG path depends on it now.
- Quark is no longer used anywhere in the project. GGUF (Q4_K_M via llama.cpp)
  remains the quantization path for the iGPU LLM; the embedder is served at
  native/CPU precision.
- Qdrant collections use named vectors `dense` + `sparse`; retrieval is hybrid
  (RRF). See `docs/ingestion-pipeline.md`.

## Superseded: original NPU/Quark decision (2026-06-10)
The original ADR chose AMD Quark to quantize a BF16/INT8 encoder for the NPU via
the RyzenAI ONNX EP. This is retained for history only. It was the right call
under the original assumption of a throughput-bound, English-tolerant embedding
workload; it does not survive contact with a Czech-inclusive, table-heavy corpus
on a low-frequency ingest pattern. The account-gated NPU/XRT install notes from
that version now live in `docs/hardware/bios-f5x.md` and `infra/npu/`.
