"""Version handling / deduplication (spec §7).

FDA FAERS rule: reports sharing a CASEID are successive versions of the same
case. Exactly one version per CASEID is "current":

    winner = max by (explicit FDA_VERSION if present, else FDA_DT,
                     then PRIMARYID as tie-break)

Design notes:
- The rule is implemented ONCE, here, as a pure function over simple records
  so it is unit-testable and deterministic.
- Data is never discarded: every loaded version row is kept and flagged
  (is_current); losers are flagged, not deleted.
- The decision is computed by streaming DEMO records (memory: one small
  tuple per CASEID), then applied to the database in one UPDATE.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class VersionRecord:
    case_id: str
    primaryid: str
    fda_dt: str | None = None       # YYYYMMDD or None
    fda_version: str | None = None  # explicit version number if the file has one


def _version_key(rec: VersionRecord) -> tuple[int, int, int]:
    """Sort key for 'most recent version'. All components ascending; max wins.

    Priority: explicit fda_version (int) > fda_dt (YYYYMMDD int) > primaryid.
    Non-numeric values are treated as 0 (never crash on dirty data; the
    primaryid tie-break still makes the outcome deterministic).
    """
    version = 0
    if rec.fda_version:
        try:
            version = int(str(rec.fda_version).strip())
        except ValueError:
            version = 0
    date = 0
    if rec.fda_dt:
        digits = "".join(c for c in str(rec.fda_dt) if c.isdigit())
        if digits:
            try:
                date = int(digits[:8])
            except ValueError:
                date = 0
    try:
        pid = int(str(rec.primaryid).strip())
    except ValueError:
        pid = 0
    return (version, date, pid)


def select_current_versions(records: Iterable[VersionRecord]) -> dict[str, str]:
    """Return {case_id: winning primaryid} for a stream of version records.

    Deterministic: identical input in any order yields identical output.
    """
    best: dict[str, VersionRecord] = {}
    for rec in records:
        if not rec.case_id or not rec.primaryid:
            continue
        current = best.get(rec.case_id)
        if current is None:
            best[rec.case_id] = rec
            continue
        # Strict > so ties resolve on primaryid and never flip on input order.
        if _version_key(rec) > _version_key(current):
            best[rec.case_id] = rec
    return {case_id: rec.primaryid for case_id, rec in best.items()}
