# Ingestion pipeline — mixed docx / xlsx corpus

This specifies how `services/ingester/ingester.py` turns the corpus into Qdrant
points. The agent must follow this; it is the contract step 04's gate checks
against. The corpus is ~10–30 files, mostly Czech + English, split between
table-structured xlsx (host → IP → VLAN → KVM-host assignments) and prose docx
(descriptions in both languages).

The guiding principle: **xlsx and docx have different information shapes and must
be chunked differently.** A table row is a self-contained record; a paragraph is
part of a flowing argument. One chunker for both would wreck retrieval on at
least one of them.

## 1. File routing
Walk `CORPUS_PATH` recursively. Route by extension:
- `.xlsx`, `.xls`, `.csv` → table path (§2)
- `.docx` → prose path (§3)
- anything else → skip with a logged warning (do not guess)

## 2. Table path (xlsx / csv) — one row per chunk
Tables are lookup data. The retrieval need is "give me the row for host X" or
"which hosts are on VLAN 203" — precise, record-level. So:

- Read each sheet. Treat the **first non-empty row as the header** (the keys).
  If a sheet has no sensible header, log and skip it rather than guessing.
- For each data row, build a flat, self-describing string joining each cell to
  its header key:
  ```
  host: prod-kvm-03, ip: 10.20.1.43, vlan: 203, kvm_host: node-cz-brno-4, role: db-primary
  ```
  - Skip empty cells. Normalise whitespace. Keep values verbatim — do NOT
    lowercase or reformat IPs/hostnames/VLAN IDs; exact tokens are the whole point.
  - This serialization is what makes the sparse vector useful: the literal tokens
    `prod-kvm-03`, `10.20.1.43`, `203` end up in the lexical index for exact match.
- **One Qdrant point per row.** Never embed a whole sheet or a whole table as one
  chunk — that destroys row-level precision and overflows context.
- Payload: `source_file`, `sheet`, `row_index`, `kind: "table_row"`,
  `lang: "n/a"` (table rows are language-neutral key:value), and the raw
  serialized string as `text`.
- Multi-sheet workbooks: include the sheet name in the serialized string's
  context if sheets represent different domains (e.g. prefix `sheet: vlans, ...`).

### Wide tables
If a table has many columns, the serialized row may exceed a sensible length.
BGE-M3 handles up to 8192 tokens so truncation is not a concern, but a 40-column
row is noisy. If a sheet is very wide, prefer emitting the most identifying
columns first (host/ip/name/id) so the lexical signal is front-loaded. Keep all
columns — just order them sensibly.

## 3. Prose path (docx) — semantic chunks with overlap
Descriptions are flowing text where meaning spans sentences. Retrieval need is
semantic ("how do we back up a KVM host", in Czech or English). So:

- Extract text in document order (python-docx). Preserve heading structure: a
  heading starts a new logical section.
- Chunk to **~256–384 tokens** with **~15% overlap** between adjacent chunks.
  - Split on paragraph / heading boundaries; never split mid-sentence.
  - Overlap preserves context across chunk boundaries so a query landing on the
    seam still retrieves coherent text.
  - 256–384 is chosen so a chunk holds a full idea but stays focused; it is well
    under BGE-M3's limit, so no truncation, but small enough for precise hits.
- Tables embedded *inside* docx files: extract them and route through the table
  path (§2), not the prose chunker. A table is a table wherever it lives.
- Best-effort language detection per chunk (e.g. `langdetect`) → `lang: "cs"` |
  `"en"` | `"other"`. This is metadata only; BGE-M3 is multilingual so retrieval
  does not depend on it, but it is useful for debugging and optional filtering.
- Payload: `source_file`, `heading` (nearest enclosing heading if any),
  `chunk_index`, `kind: "doc_text"`, `lang`, and the chunk `text`.

## 4. Embedding + storage
For each chunk (from either path):
1. Call the step-03 embedder `POST /embed` with `kind="passage"` and a batch of
   chunk texts (batch for throughput; the service handles a list).
2. Receive `dense` (1024-dim) and `sparse` (token→weight map) per chunk.
3. Upsert one Qdrant point with BOTH named vectors:
   - `dense`: the 1024-dim vector, cosine distance
   - `sparse`: the sparse vector, into Qdrant's sparse index
   plus the payload from §2/§3.

### Collection schema (created on first run, `--recreate` to rebuild)
```
vectors:
  dense:  { size: 1024, distance: Cosine }
sparse_vectors:
  sparse: {}                      # Qdrant sparse index
```
Collection name: `corpus`.

## 5. Idempotency and re-runs
- Use a deterministic point ID: hash of `source_file + sheet/heading + index`.
  Re-ingesting the same file overwrites its points rather than duplicating them.
- `--recreate` drops and rebuilds the collection (use when the schema or model
  changes — e.g. this BGE-M3 switch). Without it, ingestion upserts incrementally.
- On a model change (dim or model id differs from what `MODEL.md` records), refuse
  to append into an existing collection built with the old model — require
  `--recreate`. Mixing vectors from two models in one collection is silently wrong.

## 6. Why hybrid (dense + sparse) for this corpus specifically
- **Dense** carries semantics and crosses languages: a Czech query finds the
  relevant English description and vice versa. This is the half the old
  English-only NPU build could never do.
- **Sparse** carries exact lexical matches: querying `prod-kvm-03` or `10.20.1.43`
  hits the precise table row even though those tokens have no "semantics" to
  embed. Pure dense retrieval is unreliable for identifiers; sparse fixes it.
- Step 07's `search_corpus` fuses both with RRF (Qdrant Query API), so one tool
  call serves both "which VLAN is host X on" (lexical) and "how do we handle
  host maintenance" (semantic, either language).

## 7. Verification (feeds step 04's gate)
After ingesting a 2–3 file subset:
- Collection `corpus` exists, point count > 0, schema shows `dense` (1024) +
  `sparse`.
- A query for a known hostname returns its exact table row in the top hits
  (sparse path works).
- A Czech semantic query returns a relevant docx chunk (multilingual dense works).
Both must pass — together they prove the whole reason for the BGE-M3/CPU redo.
