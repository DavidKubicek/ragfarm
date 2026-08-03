"""ragfarm_env — one place the tracing tools learn where the services are.

WHY THIS EXISTS. The tracing tools were written against an early, wrong port map
(`localhost:8001` for generation, `:8002` for the reranker, `:8003` for the
embedder). None of those are real. Every tool therefore had its own hardcoded and
incorrect defaults scattered through argparse, constructors and docstrings.

`.env` at the repo root is the single source of truth for configuration. This
module loads it via python-dotenv and exposes the resolved endpoints, so a tool
does:

    from ragfarm_env import LLM_URL, RERANK_URL, MCPO_URL
    parser.add_argument("--url", default=LLM_URL)

and never hardcodes a port again. Every value falls back to the real deployed
default, so the tools work even with no `.env` present.

CONTAINERS. `load_dotenv()` is verified to work inside our service containers —
the library is importable there. What containers lack is the *file*: the repo-root
`.env` is not mounted in. To make `.env` authoritative for a containerized service,
bind-mount it read-only (`../.env:/app/.env:ro`) and call `load_dotenv("/app/.env")`.
Compose's own `.env` handling only does `${VAR}` substitution in the compose file;
it does not put the file inside the container.

THE REAL PORT MAP (also in CLAUDE.md and the Spark handoff):
    8080  LLM, OpenAI-compatible      (llama.cpp --alias / vLLM --served-model-name)
    8081  reranker /reranking         (bge-reranker-v2-m3)
    8090  embedder /embed             (BGE-M3, dense+sparse)
    6333  Qdrant
    8000  mcpo OpenAPI bridge         (tools mount under /<name>, e.g. /rag)
    8104  rag-retrieval MCP           (behind mcpo; curl it via :8000/rag/...)
    3000  Open WebUI
"""
from __future__ import annotations

import os
import pathlib

__all__ = [
    "REPO_ROOT", "ENV_FILE", "LLM_URL", "RERANK_URL", "EMBED_URL", "QDRANT_URL",
    "MCPO_URL", "RAG_MCP_URL", "OWUI_URL", "MCPO_RAG_URL", "describe",
]


def _find_repo_root() -> pathlib.Path:
    """Walk up from this file looking for the repo root (the dir holding .env or
    CLAUDE.md). Falls back to two levels up (tests/tracing/ -> repo root) so the
    module still imports from an odd cwd."""
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "CLAUDE.md").exists() or (parent / ".env").exists():
            return parent
    return here.parents[2] if len(here.parents) > 2 else here.parent


REPO_ROOT = _find_repo_root()
ENV_FILE = REPO_ROOT / ".env"

# Load .env if python-dotenv is available. Deliberately non-fatal: these are
# diagnostic tools and must not refuse to start over a missing optional dep.
# override=False so a value already exported in the shell wins over the file.
try:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=False)
    _DOTENV = True
except ImportError:  # pragma: no cover - depends on install profile
    _DOTENV = False


def _url(var: str, default: str) -> str:
    """Read `var` from the environment, trimming any trailing slash so callers can
    concatenate paths without doubling it."""
    return os.environ.get(var, default).rstrip("/")


LLM_URL = _url("LLM_URL", "http://127.0.0.1:8080")
RERANK_URL = _url("RERANK_URL", "http://127.0.0.1:8081")
EMBED_URL = _url("EMBED_URL", "http://127.0.0.1:8090")
QDRANT_URL = _url("QDRANT_URL", "http://127.0.0.1:6333")
MCPO_URL = _url("MCPO_URL", "http://127.0.0.1:8000")
RAG_MCP_URL = _url("RAG_MCP_URL", "http://127.0.0.1:8104")
OWUI_URL = _url("OWUI_URL", "http://127.0.0.1:3000")

# Convenience: the mcpo route the rag tools actually call. NOTE this is :8000/rag
# (through the bridge), NOT :8104 directly — a long-standing source of confusion.
MCPO_RAG_URL = _url("MCPO_RAG_URL", f"{MCPO_URL}/rag")


def describe() -> str:
    """One-line-per-endpoint summary, for a tool's --version/-v banner."""
    src = f".env at {ENV_FILE}" if (_DOTENV and ENV_FILE.exists()) else "defaults only"
    rows = [
        f"  llm       {LLM_URL}",
        f"  reranker  {RERANK_URL}",
        f"  embedder  {EMBED_URL}",
        f"  qdrant    {QDRANT_URL}",
        f"  mcpo      {MCPO_URL}   (rag route: {MCPO_RAG_URL})",
        f"  rag-mcp   {RAG_MCP_URL}",
        f"  open-webui {OWUI_URL}",
    ]
    return f"ragfarm endpoints (source: {src}):\n" + "\n".join(rows)


if __name__ == "__main__":
    print(describe())
