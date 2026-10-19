"""PharmaSignal application package.

Importing anything from this package first runs the interpreter bootstrap:
if the interpreter in use is missing a pinned dependency (e.g. a bare
`pytest` resolving to Anaconda base, which has no psycopg2), the same command
re-executes under .venv/ instead of failing with ModuleNotFoundError. See
app/bootstrap.py — it is a strict no-op when the current interpreter is
complete, so CI, Docker and activated virtualenvs are unaffected.

The bootstrap is loaded here by file path under a private module name rather
than `from app.bootstrap import ensure_venv`. The plain import would register
the name `app.bootstrap` in sys.modules, and `python -m app.bootstrap --check`
would then make runpy warn — "found in sys.modules after import of package
'app', but prior to execution ... may result in unpredictable behaviour" — on
stderr, which is noise on the one command whose job is to report cleanly.
Keeping the canonical name free costs nothing: bootstrap.py is stdlib-only,
has no import-time side effects, and ensure_venv() is one-shot.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_BOOTSTRAP_PATH = Path(__file__).with_name("bootstrap.py")
_spec = importlib.util.spec_from_file_location("app._bootstrap_init", _BOOTSTRAP_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"interpreter bootstrap not loadable from {_BOOTSTRAP_PATH}")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

ensure_venv = _module.ensure_venv

ensure_venv()
