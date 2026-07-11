"""
mcp-fs — sandboxed read-only filesystem access for the corpus.
Lists filenames and reads text under FS_SANDBOX_ROOT only. No writes, no escape
above the sandbox root (path is resolved and checked).
STATUS: minimal working list/read; extend with globbing/metadata as needed.
"""
import os, pathlib
from mcp.server.fastmcp import FastMCP
ROOT = pathlib.Path(os.environ.get("FS_SANDBOX_ROOT", "/data/corpus")).resolve()
mcp = FastMCP("fs", host="0.0.0.0", port=8103)

def _safe(rel: str) -> pathlib.Path:
    p = (ROOT / rel).resolve()
    if not str(p).startswith(str(ROOT)):
        raise PermissionError("path escapes sandbox")
    return p

@mcp.tool()
def list_files(subdir: str = "") -> dict:
    """List file names (recursively) under the sandbox root or a subdir of it."""
    base = _safe(subdir)
    files = [str(p.relative_to(ROOT)) for p in base.rglob("*") if p.is_file()]
    return {"root": str(ROOT), "count": len(files), "files": files[:5000]}

@mcp.tool()
def read_text(path: str, max_bytes: int = 200_000) -> dict:
    """Read a UTF-8 text file (truncated to max_bytes) from within the sandbox."""
    p = _safe(path)
    data = p.read_bytes()[:max_bytes]
    return {"path": path, "truncated": p.stat().st_size > max_bytes,
            "text": data.decode("utf-8", errors="replace")}

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
