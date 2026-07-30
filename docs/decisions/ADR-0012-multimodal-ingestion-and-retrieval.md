# ADR-0012 — Multimodal ingestion & retrieval: structured parsers, image OCR/caption/CLIP
Author: David Kubicek (david.kubicek@eywo.cz)

Status: PROPOSED (2026-07-30). Two accepted directions, phased. **Parsers:** adopt
layout-aware document parsers (Docling primary, MinerU for complex PDFs, PaddleOCR-VL
for OCR) as *libraries* called from the ingester prose path, to close the
undertested dense DOCX/PDF gap and the current hard gap where **scanned PDFs are
skipped entirely**. **Images:** a three-pass ingest (OCR + VL caption + CLIP embed)
and a cross-modal retrieval branch fused by RRF. **Phase 1** (caption-then-embed —
images become text in the existing pipeline) is low-risk and reuses everything;
**Phase 2** (native CLIP text↔image + a VL reranker as a parallel visual branch) is
the higher-fidelity follow-on. Promote to ACCEPTED per-phase as each lands and is
measured against a labelled eval set.
Date: 2026-07-30
Builds on: ADR-0002 (BGE-M3 dense+sparse embedder; images add a *new* vector space,
they do not touch BGE-M3), ADR-0006 (content-addressed manifest/sync — images and
their derived text/vectors ride it, content-addressed like any file), ADR-0007
(section-aware prose chunking — better parser output feeds the *same* chunker),
ADR-0009 (vision engine: Qwen3-VL on the one `llama-server`; llama.cpp already ships
the CLIP graph at `tools/mtmd/clip.cpp` — the VL caption/OCR passes reuse this
engine), ADR-0010 (dual-branch + late RRF fusion — the visual branch is one more
branch hung off the same fusion).
Scope: the **ingest** side (`services/ingester/ingester.py` prose path + a new image
route) and a **new retrieval branch** for images. The XLSX table path
(`xlsx_tables.py`, frozen), the embedder service, the manifest layer, and the
existing text retrieval are unchanged except for the additive image collection/branch.

## Context

Three concrete gaps, two of them already visible in the code:

1. **Scanned PDFs are dropped.** `ingester.py`'s own header: PDFs are text-layer
   extraction and *"scanned PDFs with no text layer are skipped with a warning."*
   Any scanned document in the corpus is simply absent from retrieval today.
2. **The dense DOCX/PDF path is undertested and probably parser-limited.** Retrieval
   quality on prose DOCX/PDF is weaker than on the labelled tables, and the likely
   root cause is as much *parsing* as embedding: bad chunk boundaries cap dense recall
   no matter how good BGE-M3 is. The current prose extraction is plain text-layer;
   it loses table structure and reading order, so the chunker (ADR-0007) is fed
   degraded input. The table corpus (sparse, labelled) masks this — it doesn't
   exercise dense retrieval hard, so the weakness is under-measured.
3. **No image retrieval at all.** With a VL model now in the stack (ADR-0009), the box
   can *read* images, but nothing ingests or retrieves them. The target use includes
   both "OCR this scanned doc into the corpus" and "find the photo of me standing on a
   beer barrel" — two different capabilities often conflated.

"Steal from OWUI" here means **adopt the same underlying libraries OWUI adopted**,
called directly from our ingester — not wrap OWUI's pipeline. OWUI's content-
extraction options (Docling, MinerU, Mistral OCR, PaddleOCR-VL, Tika) are the signal
for which libraries are worth using; we call the good ones ourselves.

## Decision

### 1. Layout-aware parsers as ingest libraries (prose path)

- **Docling — primary** for DOCX/PDF. Layout-aware extraction with real table-
  structure recovery and reading order, output as clean markdown — which is exactly
  what makes dense chunks retrievable. This is the single highest-leverage fix for
  gap 2: better structure ⇒ better chunk boundaries ⇒ better dense recall, feeding the
  *same* ADR-0007 chunker.
- **MinerU** for complex/formula-heavy PDFs Docling handles poorly.
- **PaddleOCR-VL** for OCR — the bridge that closes gap 1 (scanned PDFs) and does
  double duty in the image path below.
- These are standalone libraries the ingester calls; no OWUI runtime dependency. The
  prose route gains a parser-selection step; the chunker, embedder, and Qdrant upsert
  downstream are unchanged.

### 2. Three-pass image ingest — self-selecting, no hard bucketing

MIME-split at ingest (an `image/*` file routes to the image path). Rather than
classify "scan vs photo" upfront, run **all three passes on every image** and let the
scorer sort it out:

- **OCR (PaddleOCR-VL)** — text *in* the image: scanned pages, signs, whiteboards,
  screenshots.
- **VL caption (Qwen3-VL, ADR-0009 engine)** — scene semantics ("a man standing on a
  beer barrel").
- **CLIP embed** — a vector for visual similarity (image↔image and text↔image).

A scanned document scores high on OCR text, ~zero on scene caption; a photo the
reverse; a whiteboard photo on both. No image is ever mis-bucketed because nothing
buckets — the passes self-select, and a photo containing a sign is found by both its
scene *and* its text. Ingest is offline batch, so three passes per image cost nothing
we care about (and the VL forward passes are where the box's compute is well used,
unlike bandwidth-bound decode — see `docs/hardware/NVIDIA-Spark.md`).

### 3. Retrieval — CLIP is bidirectional; fuse cross-modally by RRF

- **Text query** fans out to: (a) the **caption branch** (query text vs stored
  captions — ordinary text retrieval, reranked by the text cross-encoder) **and**
  (b) **CLIP text→image** (query text vs stored image vectors). CLIP's headline
  ability is text→image *without a caption* — reserving CLIP for image queries leaves
  its best trick on the table. The two are complementary: captions encode named
  context CLIP doesn't know ("the Řezník warehouse"); CLIP catches visual detail the
  caption omitted.
- **Image query** (user submits an image for "find similar"): **CLIP image→image**,
  reranked by a **VL reranker**. The caption branch doesn't apply.
- **Cross-modal fusion by RRF, not score-merging.** Caption hits (text-reranker
  scores) and CLIP hits (CLIP/VL scores) are on **different, incomparable scales** —
  a 0.8 from one is not a 0.8 from the other. RRF is **rank-based**, so it fuses the
  two ranked lists without ever comparing raw scores across modalities. We already run
  RRF (ADR-0007/0010); this is the same mechanism extended by a branch, and it is the
  clean answer to the modality score-scale mismatch.
- **VL reranker** is the quality lever for text→image (same role the text cross-
  encoder plays for prose); for image→image it is optional polish (embedding
  similarity is already a reasonable relevance proxy there).

### 4. Storage

- Each image produces **two index entries**: a **CLIP vector** (visual search) and a
  **VL caption + OCR text written into Qdrant** (text search) — so one image is
  reachable by both paths.
- Image vectors live either as a **new named vector** on the corpus collection or a
  **separate `images` collection** (decide in implementation; separate collection is
  cleaner for a distinct VL reranker and distinct branch cost caps). Payload carries
  the **image reference** (content-addressed key/path), the caption, the OCR text, and
  EXIF/date/source.
- **Content-addressed via the ADR-0006 manifest** — images are checksummed and
  manifested like any corpus file, so visual data is a first-class citizen in corpus
  sync, not a side store that drifts.

### 5. Phasing

- **Phase 1 — caption-then-embed (now).** VL caption + OCR → text into Qdrant → the
  existing text pipeline finds images by their description. "Barrel of beer" works
  because the caption says so. Reuses everything (chunker, embedder, retrieval,
  reranker); zero new vector space; immediately useful. Ceiling: caption quality.
- **Phase 2 — native visual branch (later).** CLIP text↔image + image↔image with the
  VL reranker, as a parallel branch fused by RRF (§3). Adds the new vector space and
  reranker; earns higher fidelity for visual-similarity queries not describable in
  words. Gate the go-decision on the eval (below).

## Eval (how we decide Phase 2 is worth it)

Gap 2 and the Phase-1→2 decision are both measurement questions, and the table corpus
won't answer them:

1. Build a **dense-eval corpus** of exactly the prose DOCX/MD/PDF we're unsure about,
   plus a **200-image** set for the visual path, each with a labelled
   query→relevant-item set.
2. Measure precision@k / recall@k: (a) dense retrieval **before vs after** Docling
   (does layout-aware parsing lift recall?); (b) Phase-1 caption retrieval vs Phase-2
   CLIP retrieval on the image set (does native CLIP beat captions enough to justify
   the branch?).
3. This is a see-it-run exercise on the box, ~half a day, and it decides parser choice
   and the Phase-2 go/no-go with data instead of assertion.

## Consequences

Positive:
- Scanned PDFs enter the corpus (gap 1 closed); dense DOCX/PDF recall lifts from
  better chunk boundaries (gap 2, pending eval).
- Image retrieval by description (Phase 1) and by visual similarity (Phase 2), both
  fused into the existing pipeline — one query path returns docs *and* images.
- RRF makes cross-modal fusion principled without score calibration across modalities.
- Everything rides the existing manifest and (mostly) the existing retrieval; the
  visual branch composes with ADR-0010 rather than re-architecting.

Negative / cost:
- New ingest dependencies (Docling / MinerU / PaddleOCR-VL) and their model weights;
  ingest gets heavier (offline, acceptable).
- Phase 2 adds a CLIP vector space + a VL reranker service — more to serve, and a
  cross-modal branch whose cost must be capped like the LightRAG branch (ADR-0010).
- Caption/OCR quality bounds Phase-1 recall; VL caption is non-deterministic
  (ADR-0009), so re-ingesting an image can yield a different caption — acceptable for
  a descriptive index, noted.

Neutral / open: see below.

## Open questions

1. **Named vector vs separate `images` collection.** Leaning separate (distinct
   reranker, distinct branch caps, no schema churn on the text collection); confirm at
   implementation.
2. **CLIP model choice** and whether the llama.cpp CLIP graph (ADR-0009,
   `tools/mtmd/clip.cpp`) is directly reusable for standalone image embedding, or a
   separate CLIP embedder service is cleaner.
3. **VL reranker** — which model, and whether it's worth standing up for image→image
   or only text→image.
4. **Parser fallback order.** Docling → MinerU → plain text-layer as graceful
   degradation per document; define the cascade so one parser's failure on an odd file
   doesn't drop it silently (the gap-1 mistake, repeated).

## References
- `services/ingester/ingester.py` — prose path (add parser selection), new `image/*`
  route; `xlsx_tables.py` frozen.
- ADR-0009 (vision engine, llama.cpp CLIP graph, mmproj) — the caption/OCR engine.
- ADR-0007 (chunker fed by parser output), ADR-0010 (RRF late fusion the visual
  branch joins), ADR-0006 (manifest the images ride).
- Docling / MinerU / PaddleOCR-VL as the parser/OCR reference libraries (the ones
  worth taking from OWUI's extraction set).
- `docs/hardware/NVIDIA-Spark.md` — why VL forward passes (caption/OCR) suit the box.
