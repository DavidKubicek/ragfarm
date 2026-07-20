# Ingestion pipeline — mixed docx / xlsx / pdf corpus

This is the design reference for how `services/ingester/ingester.py` (+
`xlsx_tables.py`, `corpus_manifest.py`) turns the corpus into Qdrant points. Those
files are **frozen and regression-locked** (see BUILD_STATE step 04): treat this
doc as the explanation of *what* the parser does and *why*, and the **code as the
source of truth where they differ**. Do not edit the parser to match this doc —
raise a blocker instead. The corpus is ~10–30 files, mostly Czech + English, split
between table-structured xlsx/csv (host → IP → VLAN → KVM-host assignments and
contact sheets) and prose docx/pdf (descriptions in both languages).

The guiding principle: **tables and prose have different information shapes and are
chunked differently.** A table row is a self-contained record; a paragraph is part
of a flowing argument. One chunker for both would wreck retrieval on at least one.

Relevant ADRs: **ADR-0002** (BGE-M3 dense+sparse embedder, CPU), **ADR-0006**
(content-addressed corpus sync — manifest, alias, watcher), **ADR-0007**
(section-aware prose chunking; the `text`/`text_clean` split).

## 1. File routing
Walk the corpus root recursively (`scan_disk`). Route by extension (`iter_file`):
- `.xlsx`, `.xls` → **table path**, delegated to `xlsx_tables.iter_xlsx` (§2)
- `.csv` → **table path**, single-header CSV (`iter_csv`, §2)
- `.docx` → **docx path**: embedded tables → table path, body → prose path (§3)
- `.txt` → **prose path**, plain (`markdown=False`)
- `.md`, `.markdown` → **prose path**, Markdown headings honoured (`markdown=True`)
- `.pdf` → **prose path**, text-layer extraction (`pdfplumber`); a scanned PDF with
  no text layer is skipped with a warning
- anything else → skip with a logged warning (do not guess)

## 2. Table path (xlsx / csv / docx-tables) — one row per chunk
Tables are lookup data. The retrieval need is "give me the row for host X", "which
hosts are on VLAN 203", or "the contact for the project lead" — precise,
record-level. So:

- **One Qdrant point per data row.** Never embed a whole sheet or table as one
  chunk — that destroys row-level precision and overflows context.
- Each row is serialized to a flat, self-describing string joining each cell to its
  **header key** (`_serialize_kv`), sheet-prefixed for multi-sheet/table sources:
  ```
  sheet: CutOver kontakty, Řízení projektu Jméno: Marek Česal, Řízení projektu Firma: EPC, Tel: 739223474, E-mail: marek.cesal@epcommodities.cz
  ```
  - Empty cells skipped; whitespace normalised. **Values kept verbatim** — no
    lowercasing/reformatting of IPs/hostnames/VLAN IDs; exact tokens are the point.
  - This serialization is what makes the sparse vector useful: literal tokens
    (`prod-kvm-03`, `10.20.1.43`, `203`) land in the lexical index for exact match.
- **Payload** (per row): `source_file`, `sheet` (sheet name, `"csv"`, or
  `"table<n>"` for docx tables), `row_index`, `kind: "table_row"`, `lang: "n/a"`
  (rows are language-neutral key:value), and the serialized string as both `text`
  and `text_clean` (rows have no decoration to strip).
- **CSV** (`iter_csv`): first non-empty row is the header; a sheet with no sensible
  header is logged and skipped rather than guessed.

### Messy real-world XLSX
All non-trivial XLSX structure — multi-table sheets, stacked/banded headers,
multi-row title+header blocks, carry-forward and vertical-merge grouping,
headerless data, trailing totals/notes trimming — lives in **`xlsx_tables.py`** and
is locked by the offline regression (`tests/fixtures`,
`test_xlsx_tables.py` → `ALL PASS`). This doc does not restate that logic; the code
and its fixtures are authoritative. BGE-M3 handles 8192 tokens so a wide row is not
a truncation risk; the parser front-loads identifying columns so the lexical signal
leads.

## 3. Prose path (docx / pdf / txt / md) — section-aware chunks (ADR-0007)
Descriptions are flowing text where meaning spans sentences, and the primary docx
uses **bold body text as headings** (not real Heading styles). The earlier
paragraph-splitting chunker collapsed such a document into one page-sized slab whose
embedding was a semantic average; ADR-0007 replaced it with section-aware chunking.

- **Section detection** (`_sections_from_lines`, `_docx_heading_level`): a new
  section starts at a Markdown `#`…`######` heading, a Word `Heading N`/`Title`
  *style*, **or** a short standalone fully-bold docx paragraph (≤12 words, ≤80
  chars — the bold-run fallback). Top-level heading opens a section and clears the
  subsection; a deeper heading becomes the subsection.
- **Keep a coherent section whole; split only when forced** (`_emit_sections`).
  Sizes are measured in **whitespace tokens (words)**, a deterministic proxy for
  model tokens:
  - `CHUNK_MAX_WORDS = 480` (~600 model tokens) — a section at or under this is
    emitted as **one chunk**, never split just to hit a target (no partial answers).
  - `CHUNK_TARGET_WORDS = 300` — packing target when a larger section *is* split.
  - Splitting happens at **sentence boundaries only** (`_SENT_SPLIT`); a chunk never
    cuts a sentence, so retrieval can't hand the model a truncated instruction. A
    lone sentence over the ceiling is emitted whole.
  - `OVERLAP_FRAC = 0.15` — trailing whole sentences (~15% of target) are carried
    into the next chunk so a topic straddling a split appears in both.
- **Two texts per chunk** (ADR-0007): `text` / `text_raw` is **verbatim** (returned
  to the LLM for citation/quoting); `text_clean` is Markdown-decoration-stripped
  (`_strip_md`) and is the **embedding input** so syntax stops polluting the dense
  vector. Link/URL tokens and hostnames are *kept* in `text_clean` (the sparse
  branch needs them); only scaffolding (`**`, `#`, list/quote markers, code fences,
  pipes) is removed. For non-Markdown sources the two coincide.
- **Line-span citation metadata**: `chunk_start_line` / `chunk_end_line`. docx has
  no source lines, so the **paragraph ordinal** (1-based over all paragraphs) is the
  line proxy; pdf uses extracted-text line numbers.
- **Language** (`detect_lang` via `langdetect` on `text_clean`): `cs` | `en` |
  `other` | `unknown`. Metadata only — BGE-M3 is multilingual so retrieval does not
  depend on it, but it aids debugging/optional filtering.
- **Payload** (per chunk): `source_file`, `section_title`, `subsection_title`,
  `heading` (= section_title, used as retrieval's `location`), `chunk_index`,
  `chunk_start_line`, `chunk_end_line`, `kind: "doc_text"`, `lang`, `text`,
  `text_clean`.

## 4. Embedding + storage
For each chunk (either path), batched (`BATCH = 64`):
1. Call the embedder `POST /embed` with `kind="passage"` on the chunk's
   **`text_clean`** (table rows fall back to their verbatim `text`).
2. Receive `dense` (1024-dim) and `sparse` (token→weight map) per chunk.
3. Upsert one Qdrant point with BOTH named vectors and the §2/§3 payload:
   - `dense`: 1024-dim, cosine distance
   - `sparse`: into Qdrant's sparse index
   - the exact embedded string is mirrored back into the payload as `text_clean`.

**Point IDs are opaque `uuid4`.** The parser also computes a deterministic per-row
hash, but it is *ignored* on upsert — identity and pruning live in the checksum
manifest (§5, ADR-0006), not the point ID. Old and new versions of a file never
share an ID, which is what makes blind delete-by-recorded-UUID safe.

### Collection schema + alias (created on first run)
```
vectors:
  dense:  { size: 1024, distance: Cosine }
sparse_vectors:
  sparse: {}                      # Qdrant sparse index
```
Physical collections are named `corpus_<timestamp>_<uuid8>`; **retrieval targets the
alias `corpus`** (`QDRANT_COLLECTION`), never a physical name. `--recreate` builds a
new physical collection alongside the live one and atomically repoints the alias.

## 5. Sync, idempotency & re-runs (ADR-0006 — content-addressed)
Identity is the **file content checksum** (`blake2b`), tracked in a SQLite manifest
(`corpus_manifest.py`, `manifest.db`) mapping `checksum → {uuids, path,
departed_at}`. Identical-content files collapse to one checksum (intended dedup).
Three modes (`main`, mutually exclusive, each under a pass lock so a manual run and
the watcher never collide):

- **default — incremental** (`incremental`, grace=None): in-place sync of the live
  collection against disk. Order is **embed-before-prune** so no file's data is ever
  missing mid-pass: (1) add new checksums, (2) reconcile still-present content —
  cancel stale departures, and on a pure rename just refresh `source_file` in the
  payload (no re-embed), (3) prune departed checksums immediately.
- **`--recreate`** (`recreate`): full rebuild into a fresh `corpus_<ts>_<uuid8>`,
  embed everything, then **atomically switch the alias** and drop the old physical
  collection — **zero retrieval blackout**. The one-time exception is migrating a
  legacy pre-ADR-0006 physical `corpus` collection (a single momentary switch).
  Use `--recreate` whenever the schema, embedder model, or chunking changes.
- **`--watch`** (`watch`): autonomous debounced watcher (the
  `ragfarm-ingester-watcher` service). Debounce/quiescence (`INGEST_DEBOUNCE`) so a
  half-written file is never read and event storms coalesce; a full re-scan every
  `INGEST_SCAN_INTERVAL` as the missed-event backstop; **deletes are grace-gated**
  (`INGEST_DELETE_GRACE`) — a vanished file is only *marked*, and reappearing before
  the grace expires cancels the delete (rides out atomic-save / rsync windows).
  Read-only filesystem events (the watcher's own checksum reads) are ignored to
  avoid a self-feeding loop.

A model change (dim or model id) mixed into an existing collection is silently
wrong; the alias/`--recreate` design makes the correct action (rebuild + switch) the
easy one. Keep `models/embeddings/MODEL.md` in step with what a collection was built
against.

## 6. Why hybrid (dense + sparse) for this corpus specifically
- **Dense** carries semantics and crosses languages: a Czech query finds the
  relevant English description and vice versa (the half the old English-only NPU
  build could never do). It embeds `text_clean`, so Markdown syntax doesn't skew it.
- **Sparse** carries exact lexical matches: querying `prod-kvm-03` or `10.20.1.43`
  hits the precise table row even though those tokens have no "semantics" to embed.
  Pure dense retrieval is unreliable for identifiers; sparse fixes it — which is why
  `text_clean` deliberately keeps URL/host tokens.
- `search_corpus` (rag-retrieval) fuses both with RRF over a broad candidate pool,
  then re-ranks with a cross-encoder (ADR-0008), so one tool call serves both "which
  VLAN is host X on" (lexical) and "how do we handle host maintenance" (semantic,
  either language).

## 7. Verification (feeds step 04's gate)
After ingesting the corpus (or a 2–3 file subset):
- The offline parser regression passes: `FIXTURES=tests/fixtures python
  services/ingester/test_xlsx_tables.py` → `ALL PASS`.
- Alias `corpus` resolves to a physical collection with point count > 0 and a schema
  showing `dense` (1024) + `sparse`.
- A query for a known hostname returns its exact table row in the top hits (sparse
  path works).
- A Czech semantic query returns a relevant doc chunk (multilingual dense works).
Together these prove the whole reason for the BGE-M3/CPU redo and the ADR-0006/0007
rework.
