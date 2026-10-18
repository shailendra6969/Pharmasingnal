"""Repo-root conftest: bare `pytest` must run under the project venv.

pytest loads this file before collecting any test module, which matters
because several test modules `import psycopg2` at module scope — without the
bootstrap, a system interpreter missing psycopg2 would abort collection with
ImportErrors before tests/conftest.py could help. Re-running the suite under
.venv/ here means a plain `pytest` (or `python -m pytest`) works with or
without activating .venv/.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.bootstrap import ensure_venv  # noqa: E402

ensure_venv()
