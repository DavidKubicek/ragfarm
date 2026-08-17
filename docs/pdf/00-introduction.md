# Introduction — what this document is and why it exists

This bundle is the single authoritative reference for **ragfarm**: a fully
on-prem retrieval-augmented generation (RAG) assistant with infrastructure-control
capabilities, running today on an **NVIDIA DGX Spark (GB10 Grace Blackwell)** and
portable, unchanged, to larger NVIDIA hardware.

It is generated on demand from the repository's live source-of-truth documents.
No content exists only here — every chapter has a stable home in the repo tree,
and this bundle concatenates and typesets them into one printable, offline
artifact. When a source document changes, `docs/pdf/build.sh` regenerates the PDF
in seconds.

> **A note on the historical material.** The project began on an AMD Ryzen AI
> mini-PC and moved to the Spark in August 2026. Where old numbers appear — 8
> tokens/second decode, three-minute Farsi OCR, the hardware ladder's Tier 0 —
> they are kept deliberately and labelled. They are the bottom rung, and the only
> tier where we have lived with the consequences of being under-provisioned.

## Why on-prem RAG matters right now

Two forces meet at this moment. The first is the practical maturity of
open-weight transformer models in the 8-30 billion-parameter range —
specifically the **mixture-of-experts** generation, where a 30-billion-parameter
model activates only about 3 billion per token. These are now competent at
structured document retrieval, tabular extraction, tool-driven infrastructure
operations, and, in the vision variants, direct optical understanding of scanned
pages, screenshots and diagrams. They fit on hardware an operator can buy once.

The second is the regulatory and commercial tension around cloud AI. Corporate
documentation, employee contacts, network topology, credentials and operational
runbooks either cannot legally leave the enterprise perimeter (energy, finance,
healthcare, defence, NIS2) or represent competitive information whose loss to a
third-party provider is unacceptable. Cloud AI trades ease of setup for a
permanent flow of internal data through someone else's servers, a permanent
per-token cost, and lock-in to whichever vendor best matches this quarter's
price/quality trade-off.

**ragfarm demonstrates that the trade is optional.** The same natural-language
interface — the same OpenAI-compatible chat, the same tool calling, the same
vision — runs entirely inside the perimeter, with no per-token cost, no data
egress, and no vendor dependency for the working system.

## What is genuinely custom about this build

Most open-source LLM stacks are upstream projects glued together with wiring
code. This one is honestly the same, and the wiring is deliberately thin so the
pieces stay independently upgradeable. Six areas produced measurable improvement
over the out-of-the-box configuration. These are the parts worth reproducing.

**1. The engine split, decided by measurement.** Generation runs on **vLLM** with
an NVFP4 mixture-of-experts checkpoint; the cross-encoder reranker stays on
**llama.cpp** at lowered scheduling priority so the interactive model wins
contention; the BGE-M3 embedder runs on CUDA; Qdrant and the MCP services run on
CPU. All three GPU consumers share one memory pipe, which is why every model
choice is argued against bandwidth rather than parameter count (ADR-0013).

The sm_121 FP4 backend choice deserves its own warning: a misconfigured NVFP4 MoE
on GB10 fails **silently** — the model loads cleanly and then emits streams of
`!!!!!`. Garbage output here is a kernel problem first and a model problem
second.

**2. A measured hardware-sizing model, not a vendor slide.** Decode is
memory-bandwidth-bound, so tokens/second is bandwidth divided by bytes read per
token, and bytes read follows *active* parameters. We back-solved the constant
from our own two models: a 30B-A3B MoE at 68 tok/s and a dense 32B at 5.9 tok/s
both imply **~200 GB/s effective, 73% of specification**. The same size class,
**11.5× apart**. That single ratio is the argument for buying bandwidth and MoE
rather than parameter count, and Chapter 4 turns it into priced options.

**3. Retrieval tuned from stock BGE-M3 to a domain-adapted hybrid.** Dense
(1024-dim) plus native sparse, fused with Reciprocal Rank Fusion, re-ranked by a
`bge-reranker-v2-m3` cross-encoder, then cut by a floor plus a Kneedle
chord-distance gate. The reranker is the most expensive stage at ~250 ms and
decides whether the model receives the right passages at all — which is why it
is never shrunk to buy latency. Also custom: broad-in/narrow-out chunking tuned
for structured XLSX and Confluence-style prose, and an XLSX table parser that
recovers headers from multi-row banded layouts where standard tools return zero
rows (ADR-0007, ADR-0008, ADR-0010).

**4. The grounding prompt as a seven-rule contract — and the discovery that it
is the whole specification.** RULE 0 through RULE 6 in
`infra/openwebui/setup_openwebui.py` define when a tool is required, how much to
retrieve, when a table is the answer and when one record is, how images are
handled, which diagram format to emit, and that only Python is executed.

The important finding is methodological. Every "the model is stupid" symptom
investigated in August turned out to be **an instruction defect, not a model
defect**: a rule saying "call the tool once, do not refine" was generalised into
*be minimal*, so the model set `k=1` and starved its own retrieval. Fixing the
prompt took a model from 2/5 to 9/10 on grounded questions and shrank its
reasoning from 16,805 to 2,788 characters. That is a larger gain than any
hardware upgrade on this page, and it cost nothing.

Because the prompt is that load-bearing, it now has a **regression suite**
(Chapters 7 and 8): the prompt library is machine-readable, replayed against the
live model, and judged by a second model call.

**5. Slots — two models resident at once, switchable mid-conversation.** vLLM
serves one model per process, so two resident models means two instances with a
shared, non-coordinating memory budget. The tooling derives each slot's share
from the verified checkpoint size and refuses any binding that would exceed the
ceiling. The payoff is diagnostic: asking a second model the same question with
the same history separates "the model cannot do this" from "our prompt told it
not to" (Chapter 6).

**6. draw.io rendered in-chat, air-gap-safe.** A vision model can regenerate a
scanned architecture diagram as mermaid or as an interactive draw.io canvas,
served from a full local mirror so nothing leaves the box at demo time. Getting
there required fixing four independent faults that all presented as the same
blank white rectangle — a missing mirror, a loopback URL that a remote browser
resolves to itself, a content-security policy blocking the corrected URL, and an
XML hand-off form the viewer does not read. Every one of them failed **silently**.
That story is in the deployment guide, and it is the best illustration in this
bundle of why "it returned 200" is not evidence of anything.

## What this bundle contains and how to read it

| chapters | what |
|---|---|
| 1-4 | This introduction, the README, vision demonstrations, and the hardware ladder with the sizing formula |
| 5-6 | Deployment guide, then the LLM life-cycle — models, slots, and running the stack |
| 7-8 | The prompt library, then how it doubles as a regression suite |
| 9-10 | Ingestion pipeline and component READMEs |
| 11-12 | Configuration reference and model records |
| 13 | Instrumentation and tracing: the tools, and what they taught us |
| 14 | Build progress — the operational ledger on the day this PDF was built |

**For a technical audience:** start with the deployment guide and the life-cycle
chapter, and treat the rest as backing material.

**For a business audience:** this introduction plus Chapter 4 (hardware) are
sufficient, and Chapter 4 is where the money question is answered.

**For operators inheriting the system:** read every chapter once, then keep the
deployment guide and `man docs/man1/stack.1` open.
