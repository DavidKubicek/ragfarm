"""
corpus_manifest — SQLite state for content-addressed corpus sync (ADR-0006).

Identity is the file's CONTENT checksum, never its name — so a rename is a no-op
and a same-name edit is a genuine change. The manifest maps:

    checksum  ->  the list of Qdrant point UUIDs that content produced

Point IDs are opaque uuid4 (assigned at ingest, recorded here). Idempotency and
pruning live at THIS layer, not in the point ID: re-embedding uses new UUIDs, and
old points are removed by deleting the UUIDs the departed checksum recorded. That
is why blind delete-by-recorded-UUID is always safe (old and new versions of a
file share no IDs).

Two responsibilities:
  1. the manifest table itself (checksum -> uuids [+ last-seen path, grace clock]);
  2. `pass_lock` — a coarse, whole-pass advisory lock (fcntl.flock on a lockfile)
     so a manual `--recreate`/incremental run and the `--watch` loop can never
     write Qdrant + the manifest concurrently. Host-only (flock), which is fine:
     the ingester runs on-host by design (ADR-0006).

Schema (single table):
    files(
        checksum    TEXT PRIMARY KEY,   -- content identity
        uuids       TEXT NOT NULL,      -- JSON list of point UUIDs
        path        TEXT,               -- last-seen relative path (non-authoritative)
        departed_at REAL,               -- epoch when first seen missing (watch grace); NULL if present
        updated_at  REAL NOT NULL
    )

Content-dedup note: two files with identical bytes share one checksum and are
stored once (one row, one set of points). That is intended — identical content
yields identical vectors; `path` holds whichever identical file was seen last.
"""
import os
import json
import time
import fcntl
import sqlite3
import contextlib
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    checksum    TEXT PRIMARY KEY,
    uuids       TEXT NOT NULL,
    path        TEXT,
    departed_at REAL,
    updated_at  REAL NOT NULL
);
"""


class Manifest:
    """Thin, synchronous wrapper over the SQLite manifest. One instance per run."""

    def __init__(self, db_path: str | os.PathLike, busy_timeout_ms: int = 30_000):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit
        self._db.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(_SCHEMA)

    def close(self):
        with contextlib.suppress(Exception):
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- reads ---------------------------------------------------------------
    def all(self) -> dict[str, dict]:
        """checksum -> {'uuids': [...], 'path': str|None, 'departed_at': float|None}"""
        out: dict[str, dict] = {}
        for ck, uuids, path, departed in self._db.execute(
            "SELECT checksum, uuids, path, departed_at FROM files"
        ):
            out[ck] = {"uuids": json.loads(uuids), "path": path, "departed_at": departed}
        return out

    def checksums(self) -> set[str]:
        return {row[0] for row in self._db.execute("SELECT checksum FROM files")}

    def get(self, checksum: str) -> dict | None:
        row = self._db.execute(
            "SELECT uuids, path, departed_at FROM files WHERE checksum = ?", (checksum,)
        ).fetchone()
        if not row:
            return None
        return {"uuids": json.loads(row[0]), "path": row[1], "departed_at": row[2]}

    # --- writes --------------------------------------------------------------
    def put(self, checksum: str, uuids: list[str], path: str | None):
        """Insert/replace a checksum's points; clears any departed mark."""
        self._db.execute(
            "INSERT INTO files (checksum, uuids, path, departed_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?) "
            "ON CONFLICT(checksum) DO UPDATE SET "
            "  uuids=excluded.uuids, path=excluded.path, "
            "  departed_at=NULL, updated_at=excluded.updated_at",
            (checksum, json.dumps(uuids), path, time.time()),
        )

    def set_path(self, checksum: str, path: str | None):
        self._db.execute(
            "UPDATE files SET path = ?, updated_at = ? WHERE checksum = ?",
            (path, time.time(), checksum),
        )

    def delete(self, checksum: str) -> list[str]:
        """Remove a checksum; return the UUIDs it had recorded (to delete in Qdrant)."""
        row = self._db.execute(
            "SELECT uuids FROM files WHERE checksum = ?", (checksum,)
        ).fetchone()
        uuids = json.loads(row[0]) if row else []
        self._db.execute("DELETE FROM files WHERE checksum = ?", (checksum,))
        return uuids

    # --- grace (watch mode) --------------------------------------------------
    def mark_departed(self, checksum: str, ts: float):
        """Record that a checksum went missing at `ts` (only if not already marked)."""
        self._db.execute(
            "UPDATE files SET departed_at = COALESCE(departed_at, ?) WHERE checksum = ?",
            (ts, checksum),
        )

    def clear_departed(self, checksum: str):
        self._db.execute(
            "UPDATE files SET departed_at = NULL WHERE checksum = ?", (checksum,)
        )

    def departed_due(self, now: float, grace: float) -> list[tuple[str, list[str]]]:
        """Checksums marked departed at least `grace` seconds ago → (checksum, uuids)."""
        rows = self._db.execute(
            "SELECT checksum, uuids FROM files "
            "WHERE departed_at IS NOT NULL AND (? - departed_at) >= ?",
            (now, grace),
        ).fetchall()
        return [(ck, json.loads(u)) for ck, u in rows]

    # --- recreate ------------------------------------------------------------
    def replace_all(self, rows: list[tuple[str, list[str], str | None]]):
        """Atomically wipe and repopulate (used by --recreate after a full rebuild)."""
        now = time.time()
        with self._db:  # BEGIN..COMMIT
            self._db.execute("DELETE FROM files")
            self._db.executemany(
                "INSERT INTO files (checksum, uuids, path, departed_at, updated_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                [(ck, json.dumps(u), p, now) for ck, u, p in rows],
            )


@contextlib.contextmanager
def pass_lock(db_path: str | os.PathLike, blocking: bool = True):
    """
    Whole-pass advisory lock, so a `--recreate`/incremental run and the `--watch`
    loop never mutate Qdrant + manifest at the same time.

    blocking=True  → wait for the lock (manual runs: they should win, and wait if
                     the watcher is mid-pass).
    blocking=False → try once; on contention raise Locked (watcher: skip this
                     cycle and retry on the next debounce/scan tick).

    Uses fcntl.flock on `<db>.lock` (created if absent). Host-only.
    """
    lock_path = f"{db_path}.lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(fh.fileno(), flags)
        except OSError as e:
            fh.close()
            raise Locked("another ingest/watch pass holds the lock") from e
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()


class Locked(RuntimeError):
    """Raised by pass_lock(blocking=False) when the lock is already held."""
