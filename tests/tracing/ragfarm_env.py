"""Shim so `from ragfarm_env import ...` works when a tracing tool is run directly
(`python tests/tracing/foo.py`), where sys.path[0] is this directory rather than
the repo root.

The canonical resolver is `ragfarm_env.py` at the REPO ROOT — edit that one. This
file only makes it importable from here, so there is still exactly one definition
of every variable name.
"""
from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load the root module under a private name, then re-export its public surface,
# so `import ragfarm_env` from this directory and from the repo root yield the
# same values rather than two independently-resolved copies.
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("_ragfarm_env_root", _ROOT / "ragfarm_env.py")
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

__all__ = list(_mod.__all__)
globals().update({name: getattr(_mod, name) for name in _mod.__all__})

if __name__ == "__main__":
    print(describe())  # noqa: F821 - injected above
