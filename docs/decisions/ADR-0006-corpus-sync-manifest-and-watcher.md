# ADR-0006 — Content-addressed corpus sync: SQLite manifest, alias switch, watcher
Author: David Kubicek (david.kubicek@eywo.cz)

Status: ACCEPTED
Date: 2026-07-13
Builds on: ADR-0002 (BGE-M3 CPU embedder, dense+sparse), ADR-0003 (rag-retrieval
owns corpus RAG; retrieval is the durable layer), ADR-0005 (naming/layout; host
services are `ragfarm-<component>`).
Unfreezes: the step-04 "ingester is COMMITTED and FROZEN" rule in BUILD_STATE.md.
That freeze existed so a *build agent* wouldn't reimplement the hand-tuned parser;
it was never meant to bar a deliberate, owner-driven capability change. The XLSX
parser (`xlsx_tables.py`) and the parse/embed primitives in `ingester.py` remain
untouched and byte-identical; only the collection/orchestration layer changes.

## Context

Corpus updates were all-or-nothing: `ingester.py` re-embedded and re-upserted every
chunk of every file on every run, and only `--recreate` (drop + rebuild the whole
collection) could clear stale points. Two problems surface as we move toward the
production energy-sector deployment:

1. **No pruning / no true incrementality.** Deterministic point IDs
   (`hash(source_file + sheet|heading + index)`) made re-ingest idempotent for
   *overwrites*, but nothing handled deletions, renames, or chunk-reflow — a
   deleted or renamed file left orphaned points, and an edit that shifted chunk
   boundaries left stale tail chunks. The only cleanup was a full `--recreate`.
2. **`--recreate` blanks retrieval.** `delete_collection → empty → refill` is a
   retrieval blackout for the rebuild window. Invisible on the PoC; unacceptable on
   a live deployment where the assistant is answering while the corpus is rebuilt.

We also want corpus changes to take effect **without a human running a command** —
files dropped into `/data/corpus` should be picked up autonomously — while never
letting that autonomy destroy data on a transient filesystem event.

## Decision

### 1. Identity is the file's content checksum, not its name

A SQLite manifest (`services/ingester/manifest.db`, auto-initialised, gitignored)
maps **content checksum → [Qdrant point UUIDs]**, with a non-authoritative
last-seen path. Filenames never drive identity: a rename is a no-op, a same-name
edit is a genuine change, and identical bytes dedupe to one row. Implemented in
`corpus_manifest.py`.

### 2. Point IDs become opaque `uuid4`; idempotency moves up a layer

Points get random `uuid4` IDs, recorded in the manifest against their file's
checksum. Idempotency and pruning live in the **checksum gate + manifest**, not in
the ID. This is the load-bearing change: because old and new versions of a file
share **no** IDs, "delete the UUIDs a departed or changed checksum recorded" is
always safe. Deterministic IDs are abandoned precisely because a same-name edit
reused IDs across versions — pruning the old set would have deleted chunks the new
version just wrote (silent data loss on the exact edit case we care about).

### 3. Incremental sync is in-place; `--recreate` uses an alias switch

- **Retrieval targets an alias** named `corpus`; physical collections are
  `corpus_<ts>_<uuid8>`. `rag-retrieval` is unchanged — `QDRANT_COLLECTION=corpus`
  keeps working because Qdrant resolves a query against an alias.
- **Incremental (default)**: resolve the alias to the live collection and mutate it
  *in place*. Hash all on-disk files; embed checksums new to the manifest (new
  UUIDs, upsert); then prune departed checksums (delete their UUIDs). **Ordering is
  embed-before-prune**, so no file's data is ever absent mid-pass — there is no
  blackout to hide, so incremental does **not** use dual-collection. (Forcing it
  through an alias switch would require copying the whole collection on every small
  change — O(corpus) work to avoid a non-existent blackout.)
- **`--recreate`**: build a fresh `corpus_<ts>_<uuid8>` alongside the live one,
  embed everything, then **atomically** repoint the alias (single
  `update_collection_aliases` call) and drop the old physical collection. Zero
  blackout. Rebuilds the manifest from scratch. This is the *only* path that uses
  dual-collection. It also handles the one-time migration where `corpus` is still a
  physical collection (drop-then-alias — the sole momentary blackout, once ever).
  The `<uuid8>` suffix guarantees a unique name even for sub-second repeated
  recreates (a plain timestamp collides and would silently reuse the live
  collection, orphaning the prior run's points).

### 4. Delete semantics differ by caller (manual vs autonomous)

- **Manual runs** (`--recreate`, bare incremental) are human-initiated on a
  known-good tree, so departed files are pruned **immediately**.
- **The watcher never prunes on transient absence.** A checksum missing from disk
  is *marked departed with a timestamp* and only pruned once it has been absent for
  `INGEST_DELETE_GRACE` (default **120 s**). A reappearance before the grace elapses
  cancels the pending delete. This rides out atomic-save unlink/rewrite, `mv x
  x.bak` edits, and `rsync --delete` windows — an autonomous prune-on-event would
  delete production embeddings because a file blinked out for 200 ms.

### 5. `--watch`: a debounced, self-sufficient watcher (host service)

`ingester.py --watch` runs a watchdog observer over `/data/corpus` and:
- **Debounces** to quiescence (`INGEST_DEBOUNCE`, default **3 s**): a pass runs only
  after the tree has been silent, coalescing an editor's event storm into one pass
  and guaranteeing we never read a half-written file (which would parse into wrong
  sparse content and upsert corruption).
- Applies **add/update promptly, deletes grace-gated** (§4).
- Runs a **full-scan backstop** every `INGEST_SCAN_INTERVAL` (default **3600 s**)
  regardless of events, so a missed inotify event costs minutes of staleness, never
  permanent drift. 60 min is a staleness bound, not an availability one — chosen to
  avoid periodically stampeding the CPU/embedder during live inference.
- Falls back to polling via `INGEST_WATCH_POLL=1` if native events prove unreliable.

It runs **on-host** (not containerised): the watcher must see host writes to
`/data/corpus` directly — a bind-mount boundary is exactly the flaky-inotify path —
and it is a peer of the host embedder (`:8090`) and Qdrant it talks to. Deployed as
`ragfarm-ingester-watcher.service`, `WantedBy` the stack, `After=` Qdrant +
`ragfarm-embedder`. The unbuildable `infra-ingester` compose service is removed.

### 6. Cross-writer safety

A coarse whole-pass advisory lock (`fcntl.flock` on `manifest.db.lock`) serialises
writers: a manual `--recreate`/incremental takes it blocking (it should win); the
watcher takes it non-blocking and skips a cycle if a manual run holds it. So a
manual rebuild and an autonomous pass can never mutate Qdrant + the manifest
concurrently. (flock is host-only — fine, the ingester is host-only by design.)

## Runtime environment (retire the RyzenAI venv)

The ingester/watcher run under a project venv `~dave/ragfarm/.venv` on host
`python3.12`, picked up via `dave`'s profile. `/opt/ryzenai/venv` is retired: it
carried NPU packages (`onnxruntime-vitisai`, `voe`, `flexml`, a Vitis torch) that
are dead for us (ADR-0002). Populate the new venv from a curated
`services/ingester/requirements.txt` constrained to the old venv's known-good
versions (`pip install -r requirements.txt -c known-good.constraints.txt`), which
filters the NPU cruft by omission. The embedder migrates to the same venv as a
second, bounded step (strip `torch`/`onnxruntime*` from the constraints, install
CPU torch explicitly); if that fights back it may stay on the old venv temporarily
without blocking corpus-sync delivery.

## Consequences

- `ingester.py` gains `corpus_manifest.py`; its parse/embed core is unchanged. New
  entrypoints: default = in-place incremental (immediate prune), `--recreate` =
  alias-switch rebuild, `--watch` = autonomous watcher. `--corpus` still overrides.
- **First run after deploy must be `--recreate`**: it migrates the legacy physical
  `corpus` collection behind the alias and seeds the manifest. Bare incremental
  before that will refuse (no alias yet) — by design.
- `manifest.db` (+ `-wal`, `-shm`, `.lock`) and `known-good.constraints.txt` are
  gitignored derived state; never committed.
- `rag-retrieval` needs no code change; `QDRANT_COLLECTION=corpus` now names an
  alias. Verify retrieval after the first `--recreate`.
- Retrieval correctness now depends on the manifest matching Qdrant. `--recreate`
  is the reset button that re-establishes that invariant from disk truth.
- BUILD_STATE step 04's freeze note is superseded by this ADR for owner-driven work;
  the parser remains off-limits to build agents. Record the unfreeze + this change
  in PROGRESS.md.

## Alternatives considered

- **ctime/mtime change detection**: rejected as the identity signal — mtime is
  forgeable (`cp -p`, rsync, atomic saves preserve it → missed changes), ctime
  can't be read as "content changed" and fires on metadata-only ops. A content
  hash is authoritative and cheap at this scale; timestamps are at most a
  pre-filter to skip hashing once files are large.
- **Keep deterministic IDs, compute `departed_ids − live_ids` on prune**: works but
  is fragile reasoning a future change will get wrong; UUIDs make blind delete
  correct by construction.
- **Dual-collection for incremental too**: rejected — turns an O(files-changed)
  operation into an O(corpus) copy to hide a blackout incremental doesn't have.
- **Containerise the watcher**: rejected — puts a bind-mount between the host writer
  and the watcher (the flaky-inotify path) and separates it from the host services
  it calls. On-host is simpler and more reliable here.
- **Prune-on-event in the watcher**: rejected — deletes live data on transient
  absence. Grace-gated deletion is the safe autonomous form.
