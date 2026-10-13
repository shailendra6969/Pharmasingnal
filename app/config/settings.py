"""Central configuration (spec §53): environment variables, paths, scope.

All values come from environment / .env. `.env.example` documents the contract.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv optional at import time
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_QUARTERS = (
    "2024q1,2024q2,2024q3,2024q4,2025q1,2025q2,2025q3,2025q4"
)


def _path_from_env(name: str, default: str) -> Path:
    raw = os.environ.get(name, default)
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache(maxsize=1)
def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://localhost:5432/pharmasignal")


@lru_cache(maxsize=1)
def test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", "postgresql://localhost:5432/pharmasignal_test")


@lru_cache(maxsize=1)
def faers_data_path() -> Path:
    return _path_from_env("FAERS_DATA_PATH", "data/raw/FAERS")


@lru_cache(maxsize=1)
def processed_data_path() -> Path:
    return _path_from_env("PROCESSED_DATA_PATH", "data/processed")


@lru_cache(maxsize=1)
def faers_quarters() -> tuple[str, ...]:
    raw = os.environ.get("FAERS_QUARTERS", DEFAULT_QUARTERS)
    return tuple(q.strip().lower() for q in raw.split(",") if q.strip())


@lru_cache(maxsize=1)
def focus_drugs() -> tuple[str, ...]:
    """Deep-analysis focus drugs. NEVER used as the statistical background (spec §5)."""
    raw = os.environ.get(
        "FOCUS_DRUGS", "METFORMIN,IBUPROFEN,SERTRALINE,ATORVASTATIN,OMEPRAZOLE"
    )
    return tuple(d.strip().upper() for d in raw.split(",") if d.strip())


@lru_cache(maxsize=1)
def admin_api_key() -> str:
    """Shared secret for /api/admin/* routes. Empty disables them (503)."""
    return os.environ.get("ADMIN_API_KEY", "").strip()


def environment() -> str:
    return os.environ.get("ENVIRONMENT", "development")


def is_test_env() -> bool:
    return environment() == "test" or bool(os.environ.get("PHARMASIGNAL_TEST"))
