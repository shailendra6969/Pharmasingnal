"""PharmaSignal application package.

Importing anything from this package first runs the interpreter bootstrap:
if the interpreter in use is missing a pinned dependency (e.g. a bare
`pytest` resolving to Anaconda base, which has no psycopg2), the same command
re-executes under .venv/ instead of failing with ModuleNotFoundError. See
app/bootstrap.py — it is a strict no-op when the current interpreter is
complete, so CI, Docker and activated virtualenvs are unaffected.
"""
from app.bootstrap import ensure_venv

ensure_venv()
