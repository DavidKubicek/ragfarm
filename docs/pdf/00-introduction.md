# Introduction — what this document is and why it exists

This bundle is the single authoritative reference for the **ragfarm** proof-of-concept: a fully on-prem retrieval-augmented generation (RAG) assistant with infrastructure-control capabilities, running today on a small AMD Ryzen AI 9 HX 370 mini-PC with a Radeon 890M integrated GPU, and portable, unchanged, to production NVIDIA hardware.

It is generated on demand from the repository's live source-of-truth documents (README, deployment guide, architecture decision records, prompt library, model records, tracing tool docs, and the current progress ledger). No content in this PDF exists only here — every chapter has a stable home in the repo tree, and this bundle only concatenates and typesets them into one printable, offline-readable artifact. When any of those source documents change, the PDF is regenerated from `docs/pdf/build.sh` in a few seconds.

## Why on-prem RAG matters right now

Two forces meet at this point in time. The first is the practical maturity of open-weight, apache-licensed transformer models in the 4–30 billion-parameter range: Qwen 2.5, Qwen 3, and Qwen 3-VL specifically. These models — quantized to Q4_K_M and served through llama.cpp — are competent enough for structured document retrieval, tabular data extraction, tool-driven infrastructure operations, and, in the vision variants, direct optical understanding of scanned pages, screenshots, diagrams and photographs. They fit on hardware operators already own.

The second force is the regulatory and business tension around cloud AI. Corporate documentation, employee contacts, network topology, credentials, and operational runbooks either cannot legally leave the enterprise perimeter (energy, finance, healthcare, defense, NIS2) or represent competitive information whose loss to a third-party model provider is unacceptable. Cloud AI trades ease of setup for a permanent flow of internal data through vendors' servers, permanent per-token cost per query, and a permanent lock-in to whichever provider best matches this quarter's price/quality trade-off.

**ragfarm is the concrete demonstration that the trade is optional.** The same natural-language interface — the same OpenAPI-compatible chat, the same tool-calling, the same vision capabilities — can run entirely inside the enterprise perimeter on hardware bought once, with no per-token cost, no data egress, no vendor dependency for the working system.

## What is genuinely custom about this build

Most open-source LLM stacks are collections of upstream projects glued together with wiring code. This one is honestly the same, and the wiring is deliberately thin so the pieces stay independently upgradeable. But there are five areas where genuinely custom design work produced measurable improvements over the out-of-the-box configurations. These are the pieces to highlight if you are evaluating whether the effort behind this system is worth reproducing.

**1. Engine split — decided empirically, not by wishful thinking.** The HX 370 chip carries three compute planes: 12 Zen 5 CPU cores, a Radeon 890M iGPU on the RDNA 3.5 architecture, and an XDNA 2 NPU rated at 50 TOPS INT8. AMD's initial guidance suggested a hybrid single-model split across NPU+iGPU, driven by the RyzenAI software stack. **We measured and rejected it** for this class of models. On Linux specifically, the hybrid flow is Windows-only; the NPU-alone flow reaches only ~17 tokens/second decode, unacceptable for chat; and llama.cpp — the mature multi-backend inference server — cannot reach the NPU at all under any configuration. So the split settled at: iGPU runs both generative LLM and cross-encoder reranker (both bandwidth-bound, both benefit from Vulkan compute), CPU runs Qdrant and MCP services, NPU stays quiet for now. Rationale in ADR-0001; measured numbers throughout the deployment guide.

**2. Retrieval pipeline tuning — from stock BGE-M3 to a domain-adapted hybrid.** The retrieval stack is BGE-M3 dense (1024-dim) plus its native sparse output, fused with Reciprocal Rank Fusion, then re-ranked by BAAI's bge-reranker-v2-m3 cross-encoder. Nothing exotic. What is custom: the reranker was moved from CPU to GPU/Vulkan (ADR-0008) after CPU-side reranking of 40–50 candidates measured ~36 seconds per query; on the same iGPU the reranker completes the same batch in ~1.9 seconds. That single change turns retrieval from "unusable" to "snappier than most cloud SaaS RAG." Also custom: a broad-in, narrow-out chunking strategy tuned specifically for structured XLSX and Confluence-style prose (ADR-0007); a domain-tuned XLSX table parser that recovers headers from multi-row banded formats where standard tools return zero rows.

**3. Grounding system prompt as a five-rule contract.** The default LLM behavior on a bare llama-server is to answer everything from its own knowledge, hallucinate when it lacks that knowledge, drop columns from tables, and produce prose where structured output was asked for. The grounding system prompt in `infra/openwebui/setup_openwebui.py` is five explicit rules — tools-first-silently, answer-only-from-tool-results, always-render-tables-full-column, diagrams-in-chosen-syntax, and coding-with-execute-then-benchmark. Every rule is present because a specific failure mode was observed live and needed to be fenced off. The proof is in the nine screenshots at the top of this document: every one of them is a category the model failed at with a shorter prompt.

**4. Two co-existing model presets — text-greedy and vision-non-greedy — with a scripted swap between them.** The text preset drives Qwen 2.5-7B under fully greedy sampling (temperature 0, top_k 1, fixed seed 42) so demonstrations are bit-for-bit reproducible across runs. The vision preset drives Qwen 3-VL-8B-Thinking under Qwen's own required non-greedy sampling (temperature 0.6, other shape knobs unset) because Thinking-class models loop under greedy decode. Both presets share the same tool set. The swap is a single command (`scripts/activate-llm.sh --dir …`) followed by an auto-restart, and the wrapper detects the model type (text vs vision) automatically from the on-disk filename; the OWUI base_model_id updates itself. Rationale in ADR-0009.

**5. Draw.io in-chat rendering, air-gap-safe.** Vision models can regenerate a scanned architecture diagram as either mermaid (native OWUI render) or draw.io (interactive canvas). The draw.io path required (a) discovering that OWUI's HTML preview iframe CSP is env-configurable rather than code-hardcoded, (b) hosting a full local mirror of the 151 MB draw.io webapp (all stencil and shape libraries) through a small nginx container, so no runtime request leaves the box at demo time. Rationale in ADR-0009 → "Why we run our own nginx."

Beyond those five, there is a full instrumentation and tracing toolkit for characterizing exactly where a chat turn's latency goes — prefill, decode, tool call, decision phase — and how retrieval candidate pools evolve through the pipeline. These are in `tests/tracing/`, catalogued in the "Debug & measurement" section of the deployment guide.

## What this bundle contains and how to read it

The chapters that follow are the source-of-truth documents from the repository, in the order most useful for a first read:

1. **README** — architectural summary, working chat-example gallery (nine screenshots), network/proxy setup.
2. **Deployment guide** — currently-running services, ports, systemd units, scripts, OWUI configuration, vision engine walk-through, debugging tools.
3. **Prompt library** — every prompt from the working chat-example gallery in Section A, plus the verified vision-preset prompts for image understanding in Section B, with observed outputs.
4. **Ingestion pipeline** — how documents flow from `/data/corpus` into Qdrant, the chunking strategy, the autonomous watcher.
5. **Component READMEs** — llama.cpp build, BGE-M3 embedder service.
6. **Configuration reference** — every environment variable across the stack, cross-referenced to where it takes effect.
7. **Model records** — pinned revisions and fetch recipes for LLM, embedder, reranker.
8. **Instrumentation and tracing** — the seven-tool toolkit for profiling any layer, plus context-blowup diagnosis playbooks.
9. **Build progress** — the linear ledger of every completed and pending step; the operational status of the PoC on the day this PDF was built.

For a technical audience: start with the deployment guide (Chapter 2) and treat the rest as backing material. For a business audience: this introduction plus the README (Chapters 1 and 2) are sufficient. For operators inheriting the system: read every chapter once, then keep the deployment guide open.
