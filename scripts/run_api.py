#!/usr/bin/env python3
"""Run the PharmaSignal API (dev server).

Usage:
    python scripts/run_api.py                 # 127.0.0.1:5000
    PORT=8080 python scripts/run_api.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import create_app  # noqa: E402


def main() -> int:
    app = create_app()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"[api] starting on http://{host}:{port} "
          f"(db={app.config['DATABASE_URL']})")
    app.run(host=host, port=port, debug=debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
