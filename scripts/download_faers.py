#!/usr/bin/env python3
"""Download FAERS ASCII quarterly archives (spec §7).

Usage:
    python scripts/download_faers.py --quarters 2024q1,2024q2
    python scripts/download_faers.py --force          # re-download everything
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.faers.download import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
