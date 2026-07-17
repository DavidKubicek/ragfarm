# ADR-0007 — Retrieval quality: section-aware chunking, hybrid+MMR rerank, client-side context management

Status: PROPOSED (work in progress — decisions are implemented and live, but still
being tuned/validated against the corpus; promote to ACCEPTED once the eval settles)
Date: 2026-07-17
Builds on: ADR-0001 (engine split; iGPU llama.cpp is the interactive LLM),
ADR-0002 (BGE-M3 dense+sparse embedder), ADR-0003 (rag-retrieval owns corpus RAG in
the MCP layer; retrieval is the durable layer), ADR-0006 (content-addressed corpus
sync; the parse/embed primitives and the XLSX table path are frozen).
Scope note: this ADR changes the **chunking policy** inside `ingester.py`'s prose
path and the **ranking/return policy** inside `rag-retrieval/server.py`. The XLSX
table path (`xlsx_tables.py`), the manifest/sync layer, and the embedder are
untouched.

## Context

The PoC retrieved and answered, but answers were large and noisy — often a whole
page of unrelated infra with the relevant fact buried inside. Root-causing it split
into three coupled problems.

1. **Chunks were page-sized, not section-sized.** `ingester.py` targeted ~256–384
   tokens but only ever split *between* blank-line paragraphs, and detected headings
   only from Markdown `#` or Word "Heading" *styles*. The primary corpus doc
   (`ŠA Hosting Notes.docx`) uses **bold body text** as headings, so no boundary was
   ever found: the whole document collapsed into one section, and a single long
   "paragraph" became one enormous chunk. PDFs (parsed with `markdown=False`) hit the
   same wall. Consequences, in order:
   - **Embeddings became a semantic average.** A 2-page chunk embeds to a vector that
     smears EPC + LDAP + Zabbix + OpenNebula together, so a query like
     `Jak se přihlásím do EPC?` matches the slab on aggregate topicality while the
     precise login lines inside contribute little.
   - **RRF rewarded density, not precision.** With huge chunks, fusion favors the
     long chunk that mentions many related terms over the short chunk that actually
     answers the question.
   - **Noise is fatal downstream.** With deterministic decoding, native tool-calling,
     and strict JSON, handing the model a page of unrelated text degrades extraction
     and tool reliability.

2. **Retrieval had no redundancy control and returned verbatim-diluted text.** One
   dense, on-topic slab (or several near-duplicates) could occupy the whole top-k.
   Retrieval also returned exactly the embedded text, conflating "what we embed" with
   "what we show the model".

3. **Context management shifted under us.** Upstream llama.cpp is removing
   `--context-shift` (trimming is declared the client's responsibility), and the
   Open WebUI we run replaced message *trimming* with LLM *compaction*
   (summarization). Blind server-side token eviction and naive summarization both
   corrupt tool-calling: they can sever a `tool_call` from its `tool_result` or drop
   a tool schema, which shows up as "tool calls go crazy after a while".

## Decision

### 1. Chunk by semantic section, never by page (`ingester.py`, prose path)

- **Heading-aware sectioning with a bold-run fallback.** Detect section boundaries
  from Markdown `#`, from Word Heading/Title *styles*, **and** from short, standalone,
  fully-bold docx paragraphs (`_docx_heading_level`). This is the single fix that
  turns the problem doc from one slab into its true sections.
- **Keep a coherent section whole; split only when forced.** A section at or under
  the hard ceiling (`CHUNK_MAX_WORDS`, ~600 model tokens) is emitted as **one chunk**
  — never split merely to hit the ~300-word target. Larger sections are split, but
  only at **sentence boundaries**, with ~15% sentence overlap; a lone sentence over
  the ceiling is emitted whole. **No chunk ever cuts a sentence**, so retrieval can
  never hand the model a truncated instruction (the explicit "no partial answers"
  requirement).
- **Two texts per chunk.** `text_clean` (Markdown decoration stripped, URLs/tokens
  kept) is what we **embed** — syntax stops polluting the dense vector. `text` /
  `text_raw` (verbatim) is what retrieval **returns** to the model for verbatim
  quoting. For non-Markdown sources the two coincide.
- **Citation metadata per chunk.** `section_title`, `subsection_title`,
  `chunk_start_line`, `chunk_end_line` (docx uses paragraph ordinal as the line
  proxy). These identify the exact provenance of every chunk.

Chunk-size units are whitespace tokens (words), a deterministic proxy for model
tokens; the constants are calibrated to keep chunks well under ~600 model tokens.

### 2. Broad-in / narrow-out retrieval (`rag-retrieval/server.py`)

- **Fuse a broad candidate pool, then re-rank.** Hybrid RRF (dense + sparse) over a
  broad pool (`RAG_CANDIDATES`, default 20) fetched **with dense vectors**, then
  **MMR** selection (`RAG_MMR_LAMBDA`, default 0.3): relevance is the fused RRF score,
  redundancy is dense cosine against already-picked chunks. Near-duplicate dense
  slabs contribute one representative instead of monopolizing the top-k. Return only
  `k` (default 5).
- **Bounded same-section window expansion.** Each returned hit may be widened to a
  few neighbor chunks **in the same `source_file` + `section_title`**
  (`RAG_EXPAND_NEIGHBORS`, default 1 each side), capped by `RAG_EXPAND_MAX_WORDS`
  (default 600). This reunites a split section without dragging in unrelated
  sections; the ~15% boundary overlap is de-duped **verbatim** (`_concat_dedup`
  drops the repeated substring, never re-flows text). Whole-section chunks have no
  such neighbors and pass through unchanged.
- **Return `text_raw` + provenance.** Results carry `section_title`,
  `subsection_title`, and the `[start,end]` line span alongside the verbatim text.

### 3. Client-side context management, not server-side eviction

- **Compaction lives in Open WebUI, tuned conservatively.** `compact_token_threshold`
  = 24000 (well under the 32k llama context), so short/medium conversations are never
  summarized and tool-heavy exchanges stay coherent.
- **Native (schema-side) function calling.** Tool schemas ride the OpenAI `tools`
  field, outside the compactable text, so compaction can't eat them.
- **Full determinism**, matched between the UI path and the raw endpoint: greedy
  decode (temp 0, top_k 1, top_p/min_p 0), fixed seed, penalties + mirostat
  neutralized. Captured in `setup_openwebui.py` so a re-run reproduces the exact
  model preset.
- **Keep llama.cpp's `--context-shift` as a backstop only.** We do not adopt any
  `--truncate-input` fork: forking costs us upstream Vulkan/iGPU tracking (ADR-0001)
  for a crude truncation that has the *same* tool-pair-severing failure mode. Stay on
  the pinned build whose `--context-shift` degrades an overflow to a shift instead of
  a hard error; solve real overflow at the client.
- **System prompt re-affirms live-state tool use:** call the tool again on every
  request whose answer depends on live state, even a repeat already answered earlier
  in the conversation — never answer from cached conversation context.

## Consequences

Positive:
- Chunks are section-scoped and small (verified on the problem docx: one slab → 8
  clean per-section chunks); embeddings represent a subsection, not a page average.
- MMR + broad-in/narrow-out stops a single dense slab from dominating the top-k.
- Provenance is precise (file + section + line span); the human citation panel in
  OWUI gets richer sources immediately.
- Determinism + native tool calling + conservative compaction give reliable tool
  calls even after multiple RAG searches (owner-observed, no degradation).

Negative / cost:
- A corpus rebuild (`--recreate`) is required whenever chunking changes.
- MMR re-orders results, so returned order no longer tracks raw score (intended, but
  can surprise).
- Window expansion adds a Qdrant scroll per doc_text hit.

Neutral:
- All rerank/window knobs are env-tunable (`RAG_CANDIDATES`, `RAG_MMR_LAMBDA`,
  `RAG_EXPAND_NEIGHBORS`, `RAG_EXPAND_MAX_WORDS`; see `.env.example`) and default to
  the values above, so behavior is unchanged unless deliberately swept.

## Known limitations / open questions (why this stays PROPOSED)

1. **Embedding/phrasing gap.** A purely semantic Czech query
   (`Jak se přihlásím do EPC?`) still may not surface the operationally-worded login
   section top-k — dense BGE-M3 doesn't bridge that phrasing, and it is not something
   MMR or windowing fixes. Candidate next levers: query expansion, or a cross-encoder
   re-ranker over the fused pool. Not yet decided.
2. **The new chunk metadata is under-used by the *model*.** `section_title` and the
   line span are returned, but nothing in the system prompt instructs the model to
   cite them; today they mostly benefit the human-facing OWUI citation panel. A
   one-line RULE 2 addition ("cite source_file + section_title + line range") would
   realize their intended purpose. Proposed, not yet applied.
3. **MMR λ is unswept.** 0.3 (diversity-leaning) is the owner's starting point; the
   relevance/diversity balance needs eval data on the real corpus.
4. **Operational lesson — mcpo boot-race.** Restarting a RAG backend severs mcpo's
   streamable-http MCP session (anyio cancel-scope bug), leaving tools unmounted
   ("Session terminated", empty aggregate spec). `scripts/mcpo-heal.sh` handles this
   on boot; any ad-hoc `rag-retrieval` restart must be followed by an mcpo restart.
