#!/usr/bin/env python3
"""ragfarm_env — the canonical resolver for every ragfarm endpoint and path.

RUN IT to see the whole environment as the code actually resolves it:
    ./ragfarm_env.py

IMPORT IT so config comes from one place instead of hand-typed variable names:
    from ragfarm_env import LLM_URL, EMBED_ENDPOINT


`.env` AT THE REPO ROOT IS THE SINGLE SOURCE OF TRUTH. This module is how Python
code gets at it. Import it instead of calling `os.environ.get` with a hand-typed
variable name, and the naming stays consistent by construction.

    from ragfarm_env import LLM_URL, EMBED_ENDPOINT, RERANK_ENDPOINT

WHY IT EXISTS. The same concept was being read under different names in different
files, which quietly defeats the whole point of a single source of truth:

    LLM base URL   LLM_URL          (agent.py, deploy.sh, tracing)
                   LLAMA_URL        (setup_openwebui.py, check_toolchain.py)
    embedder       EMBED_URL        (base, deploy.sh)
                   EMBED_ENDPOINT   (full /embed path, services + units + compose)
    reranker       RERANK_URL       (base)
                   RERANK_ENDPOINT  (full /reranking path)
    weights        LLM_GGUF_PATH    (GGUF-specific — wrong once vLLM serves
                                     safetensors; ADR-0013)

CANONICAL RULE: **base URLs (scheme://host:port, no path) are the source of
truth**; full endpoints are *derived here* — `EMBED_ENDPOINT = EMBED_URL +
"/embed"` — so one place defines each route.

`.env` also spells out the derived EMBED_ENDPOINT / RERANK_ENDPOINT, because two
classes of consumer cannot import this module: the **frozen** ingester (which reads
`EMBED_ENDPOINT` directly and must not be edited) and the **containers**. That
redundancy is a footgun, so `drifts()` reports any materialized endpoint that has
fallen out of step with its base URL, and `describe()` prints the warning.

BACK-COMPAT: the legacy names are still honoured if explicitly set, so an existing
`.env`, unit or compose file keeps working. They are deprecated — prefer the base
form. `deprecations()` reports any that are in play.

PRECEDENCE (highest first):
    1. a real environment variable (shell export, systemd Environment=, compose)
    2. `.env` at the repo root
    3. the built-in default, which is the real deployed value

CONTAINERS. `load_dotenv()` is verified working inside our service containers, but
the repo-root `.env` is NOT inside the image — compose's own `.env` handling only
does `${VAR}` substitution in the compose file. Containers therefore get `.env` via
`env_file: ../.env` in `infra/compose.yaml`, which injects it as real environment
variables (precedence 1 above), so this module is not needed in the image.

THE PORT MAP (also in CLAUDE.md and the Spark handoff):
    8080  LLM, OpenAI-compatible   (llama.cpp --alias / vLLM --served-model-name)
    8081  reranker /reranking      (bge-reranker-v2-m3)
    8090  embedder /embed          (BGE-M3, dense+sparse)
    6333  Qdrant
    8000  mcpo OpenAPI bridge      (tools mount under /<name>, e.g. /rag)
    8104  rag-retrieval MCP        (behind mcpo; curl it via :8000/rag/...)
    3000  Open WebUI
"""
from __future__ import annotations

import os
import pathlib
import sys

# Run directly (`./ragfarm_env.py`) with an interpreter that lacks python-dotenv and
# we would read NO .env and print built-in defaults — output that looks fine and is
# wrong. The system python3 does not have python-dotenv; the project venv does. So
# when executed as a script, hand off to the venv interpreter if this one can't do
# the job. Importing is unaffected: callers already run under the venv.
if __name__ == "__main__":
    try:
        import dotenv as _probe  # noqa: F401
    except ImportError:
        _venv_py = pathlib.Path(__file__).resolve().parent / ".venv" / "bin" / "python"
        # Loop guard is a sentinel env var, NOT a path comparison: a venv's
        # bin/python is typically a SYMLINK to the base interpreter, so comparing
        # resolved paths makes them look identical and the hand-off never fires.
        # What distinguishes them is site-packages, which resolve() cannot see.
        if _venv_py.exists() and os.environ.get("_RAGFARM_ENV_REEXEC") != "1":
            os.environ["_RAGFARM_ENV_REEXEC"] = "1"
            os.execv(str(_venv_py), [str(_venv_py), os.path.abspath(__file__), *sys.argv[1:]])
        print("WARNING: python-dotenv is unavailable"
              f"{' and the re-exec into .venv did not resolve it' if os.environ.get('_RAGFARM_ENV_REEXEC') == '1' else ' and no project .venv was found'}.\n"
              "         .env was NOT read — the values below are built-in defaults only.\n"
              "         Fix: .venv/bin/pip install python-dotenv\n",
              file=sys.stderr)

__all__ = [
    "REPO_ROOT", "ENV_FILE",
    "LLM_URL", "RERANK_URL", "EMBED_URL", "QDRANT_URL", "MCPO_URL",
    "RAG_MCP_URL", "OWUI_URL", "MCPO_RAG_URL", "MCPO_PLACEMENT_URL",
    "EMBED_ENDPOINT", "RERANK_ENDPOINT",
    "LLM_MODEL_PATH", "LLM_MMPROJ_PATH", "EMBED_MODEL_PATH", "RERANK_MODEL_PATH",
    "QDRANT_COLLECTION", "CORPUS_PATH",
    "describe", "deprecations", "drifts",
]


def _find_repo_root() -> pathlib.Path:
    """Walk up from this file for the repo root (the dir holding CLAUDE.md/.env),
    so importing works from any cwd — systemd units, containers, ad-hoc scripts."""
    here = pathlib.Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "CLAUDE.md").exists() or (parent / ".env").exists():
            return parent
    return here.parent


REPO_ROOT = _find_repo_root()
ENV_FILE = REPO_ROOT / ".env"

# Non-fatal if python-dotenv is absent: a missing optional dep must not stop a
# service or a diagnostic from starting. override=False so a real environment
# variable (systemd/compose/shell) always beats the file.
try:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=False)
    _DOTENV = True
except ImportError:  # pragma: no cover - depends on install profile
    _DOTENV = False

_used_legacy: dict[str, str] = {}


def _base(canonical: str, default: str, legacy: str | None = None) -> str:
    """Resolve a BASE url, honouring a legacy alias, trailing slash trimmed."""
    val = os.environ.get(canonical)
    if val is None and legacy:
        val = os.environ.get(legacy)
        if val is not None:
            _used_legacy[legacy] = canonical
    return (val or default).rstrip("/")


def _path(canonical: str, default: str = "", legacy: str | None = None) -> str:
    val = os.environ.get(canonical)
    if val is None and legacy:
        val = os.environ.get(legacy)
        if val is not None:
            _used_legacy[legacy] = canonical
    return val or default


def _route(canonical_full: str, base: str, route: str) -> str:
    """Derive a full endpoint from a base URL, unless the legacy full-path variable
    is explicitly set (in which case that wins, for back-compat)."""
    explicit = os.environ.get(canonical_full)
    if explicit:
        return explicit.rstrip("/")
    return f"{base}{route}"


# ---- base URLs (what belongs in .env) --------------------------------------
LLM_URL = _base("LLM_URL", "http://127.0.0.1:8080", legacy="LLAMA_URL")
RERANK_URL = _base("RERANK_URL", "http://127.0.0.1:8081")
EMBED_URL = _base("EMBED_URL", "http://127.0.0.1:8090")
QDRANT_URL = _base("QDRANT_URL", "http://127.0.0.1:6333")
MCPO_URL = _base("MCPO_URL", "http://127.0.0.1:8000")
RAG_MCP_URL = _base("RAG_MCP_URL", "http://127.0.0.1:8104")
OWUI_URL = _base("OWUI_URL", "http://127.0.0.1:3000")

# ---- derived full endpoints (an explicit value in .env/env wins) -----------
EMBED_ENDPOINT = _route("EMBED_ENDPOINT", EMBED_URL, "/embed")
RERANK_ENDPOINT = _route("RERANK_ENDPOINT", RERANK_URL, "/reranking")
# mcpo mounts each MCP under /<name>. NOTE :8000/rag, never :8104 directly —
# a recurring trap in the tracing tools.
MCPO_RAG_URL = _route("MCPO_RAG_URL", MCPO_URL, "/rag")
MCPO_PLACEMENT_URL = _route("MCPO_PLACEMENT_URL", MCPO_URL, "/placement")

# ---- model/data paths ------------------------------------------------------
# LLM_MODEL_PATH is format-neutral: a GGUF file on llama.cpp, a safetensors
# snapshot dir on vLLM (ADR-0013). LLM_GGUF_PATH is the legacy spelling.
LLM_MODEL_PATH = _path("LLM_MODEL_PATH", legacy="LLM_GGUF_PATH")
LLM_MMPROJ_PATH = _path("LLM_MMPROJ_PATH", legacy="LLM_GGUF_MMPROJ")
EMBED_MODEL_PATH = _path("EMBED_MODEL_PATH", str(REPO_ROOT / "models/embeddings/bge-m3"))
RERANK_MODEL_PATH = _path("RERANK_MODEL_PATH", legacy="RERANK_GGUF_PATH")

QDRANT_COLLECTION = _path("QDRANT_COLLECTION", "corpus")
CORPUS_PATH = _path("CORPUS_PATH", "/data/corpus")


def drifts() -> list[str]:
    """Materialized full endpoints that disagree with their base URL.

    `.env` spells out EMBED_ENDPOINT / RERANK_ENDPOINT because the FROZEN ingester
    and the containers read those names directly and cannot import this module.
    That redundancy is a footgun: change EMBED_URL, forget EMBED_ENDPOINT, and the
    two silently disagree. This reports it instead."""
    out = []
    for full, base, route in (
        ("EMBED_ENDPOINT", EMBED_URL, "/embed"),
        ("RERANK_ENDPOINT", RERANK_URL, "/reranking"),
    ):
        explicit = os.environ.get(full)
        if explicit and explicit.rstrip("/") != f"{base}{route}":
            out.append(f"{full}={explicit} disagrees with derived {base}{route}")
    return out


def deprecations() -> dict[str, str]:
    """{legacy_name: canonical_name} for every legacy variable actually in use.
    Empty dict means the environment is fully on canonical names."""
    return dict(_used_legacy)


def describe() -> str:
    src = f".env at {ENV_FILE}" if (_DOTENV and ENV_FILE.exists()) else "defaults only"
    rows = [
        f"  llm          {LLM_URL}",
        f"  reranker     {RERANK_URL}  -> {RERANK_ENDPOINT}",
        f"  embedder     {EMBED_URL}  -> {EMBED_ENDPOINT}",
        f"  qdrant       {QDRANT_URL}  (collection: {QDRANT_COLLECTION})",
        f"  mcpo         {MCPO_URL}  (rag: {MCPO_RAG_URL})",
        f"  rag-mcp      {RAG_MCP_URL}",
        f"  open-webui   {OWUI_URL}",
        f"  llm model    {LLM_MODEL_PATH or '(unset)'}",
        f"  mmproj       {LLM_MMPROJ_PATH or '(none — text-only or vLLM native)'}",
        f"  embed model  {EMBED_MODEL_PATH or '(unset)'}",
        f"  rerank model {RERANK_MODEL_PATH or '(unset)'}",
        f"  corpus       {CORPUS_PATH}",
    ]
    out = f"ragfarm environment (source: {src}):\n" + "\n".join(rows)
    dft = drifts()
    if dft:
        out += "\n\nWARNING — materialized endpoint drifted from its base URL:\n" + "\n".join(
            f"  {d}" for d in dft
        )
    dep = deprecations()
    if dep:
        out += "\n\nDEPRECATED variable names in use (still honoured):\n" + "\n".join(
            f"  {old}  ->  use {new}" for old, new in sorted(dep.items())
        )
    return out


if __name__ == "__main__":
    print(describe())
