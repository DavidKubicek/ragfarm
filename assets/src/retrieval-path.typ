// retrieval-path.typ — what one grounded question actually costs.
// Source of truth; regenerate with assets/src/build.sh.
// Timings measured on the Spark; the CPU-rerank figure is the AMD-era comparison
// that justified moving the reranker onto the GPU (ADR-0008).

#set page(width: auto, height: auto, margin: 10pt, fill: white)
#set text(font: "DejaVu Sans", size: 8.5pt)

#let stage(n, name, detail, ms, fill) = box(
  width: 380pt, fill: fill, stroke: 0.6pt + luma(90), radius: 3pt, inset: 6pt,
  grid(columns: (16pt, 1fr, 52pt), align: (left, left, right),
    text(weight: "bold", fill: luma(110), str(n)),
    [#text(weight: "bold", name) #h(4pt) #text(size: 7.5pt, fill: luma(90), detail)],
    text(weight: "bold", size: 8pt, ms),
  )
)
#let arrow = align(center, text(fill: luma(130), size: 11pt)[↓])

#align(center, text(size: 12pt, weight: "bold")[Query-time retrieval path])
#v(2pt)
#align(center, text(size: 8pt, fill: luma(100))[
  one `search_corpus` call · hybrid sparse+dense · measured 2026-08-15, five warm runs
])
#v(8pt)

#stage(1, "embed the query", "BGE-M3 on CUDA — dense 1024-dim AND sparse, one pass", "13 ms", rgb("#dae8fc"))
#arrow
#stage(2, "Qdrant two-branch prefetch", "dense branch by cosine, sparse branch by lexical overlap", "incl.", rgb("#d5e8d4"))
#arrow
#stage(3, "RRF fusion", "reciprocal rank fusion of both branches → 40 candidates", "15 ms", rgb("#d5e8d4"))
#arrow
#stage(4, "cross-encoder rerank", "bge-reranker-v2-m3, full quality, scores every (query, passage) pair", "250 ms", rgb("#f8cecc"))
#arrow
#stage(5, "floor + Kneedle gate", "40 in → 8 out; floor_drop 0, kneedle_cut 14", "<1 ms", rgb("#fff2cc"))
#arrow
#stage(6, "same-section expansion", "pull neighbouring chunks of a surviving passage back in", "<1 ms", rgb("#fff2cc"))
#arrow
#stage(7, "verbatim text + source", "the model receives the ORIGINAL line, never a paraphrase", "—", rgb("#e1d5e7"))

#v(10pt)
#align(center, box(stroke: 0.6pt + luma(170), radius: 3pt, inset: 7pt, width: 380pt, text(size: 7.5pt)[
  #text(weight: "bold")[Stage 4 is the expensive one, and it stays that way.] \
  Warm. The first call after an idle period costs ~1.6 s.
  It decides whether the model sees the right passages at all, so it is never
  shrunk to buy latency — that trades retrieval precision for speed, the wrong way
  round. Moving it off the CPU took the same batch from #text(weight: "bold")[36 s] to
  #text(weight: "bold")[250 ms] (ADR-0008). It runs at lowered scheduling priority so
  the interactive model wins contention.
]))

#v(6pt)
#align(center, text(size: 7pt, fill: luma(110))[
  `search_corpus` returns `_timing_ms` per stage plus the gate decision
  (`floor_drop`, `kneedle_cut`, `kneedle_d`) \
  generated from assets/src/retrieval-path.typ
])
