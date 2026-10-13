"""Quarterly trend analysis (spec §12).

Deliberately SEPARATE from disproportionality: reporting VOLUME and signal
strength are different things and are never mixed here.

MVP methods (only what is interpretable):
- quarterly report counts (volume)
- quarterly co-occurrence counts for a drug-event pair
- rolling z-score of report volume (window w, sample SD) as a simple volume
  anomaly flag — a flag means "reporting pattern changed", NOT "risk changed";
  stimulated reporting, media attention and awareness are alternative
  explanations (spec §13).

No seasonal decomposition or change-point detection in the MVP: those are
justified only where enough quarters exist to estimate them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def rolling_z(values: list[float], window: int = 4) -> list[float | None]:
    """Rolling z-score: z[i] uses the sample mean/SD of the `window` values
    BEFORE i (exclusive), so no future data leaks into a flag.

    Returns None where the preceding window is incomplete or SD is 0.
    """
    if window < 2:
        raise ValueError("window must be >= 2")
    out: list[float | None] = [None] * len(values)
    for i in range(window, len(values)):
        prev = values[i - window:i]
        mean = sum(prev) / window
        var = sum((x - mean) ** 2 for x in prev) / (window - 1)  # sample SD
        sd = math.sqrt(var)
        if sd == 0:
            out[i] = None
        else:
            out[i] = (values[i] - mean) / sd
    return out


@dataclass
class QuarterPoint:
    quarter: str
    reports: int
    events: int
    z_score: float | None = None
    volume_flag: bool = False


def quarterly_volume(conn, quarters: list[str] | None = None) -> list[QuarterPoint]:
    """Report/event volume per quarter (current versions only)."""
    cur = conn.cursor()
    if quarters:
        cur.execute("""
            SELECT source_quarter,
                   count(*) AS reports
            FROM cases
            WHERE is_current AND source_quarter IN %s
            GROUP BY 1 ORDER BY 1
        """, (tuple(quarters),))
    else:
        cur.execute("""
            SELECT source_quarter, count(*)
            FROM cases WHERE is_current
            GROUP BY 1 ORDER BY 1
        """)
    rows = cur.fetchall()
    counts = {q: int(n) for q, n in rows}

    cur.execute("""
        SELECT c.source_quarter, count(*)
        FROM case_events e
        JOIN cases c ON c.primaryid = e.primaryid AND c.is_current
        GROUP BY 1
    """)
    event_counts = {q: int(n) for q, n in cur.fetchall()}

    ordered = sorted(counts)
    volumes = [counts[q] for q in ordered]
    zs = rolling_z([float(v) for v in volumes], window=min(4, max(2, len(volumes) - 1))) \
        if len(volumes) > 2 else [None] * len(volumes)

    points = []
    for i, q in enumerate(ordered):
        z = zs[i]
        points.append(QuarterPoint(
            quarter=q,
            reports=counts[q],
            events=event_counts.get(q, 0),
            z_score=z,
            volume_flag=bool(z is not None and abs(z) >= 2.0),
        ))
    return points


def pair_quarterly_series(conn, drug: str, event: str,
                          primary_suspect_only: bool = False) -> list[dict]:
    """Quarterly co-occurrence history for one drug-event pair.

    Returns [{'quarter', 'a', 'reports'}] with a=co-occurrence count in the
    quarter (any version row of current cases) and reports = quarter volume.
    """
    from app.statistics.engine import PRIMARY_SUSPECT_ROLES

    role_filter = ""
    params: list = [drug, event]
    if primary_suspect_only:
        placeholders = ",".join(f"'{r}'" for r in PRIMARY_SUSPECT_ROLES)
        role_filter = f"AND d.role IN ({placeholders})"
    cur = conn.cursor()
    cur.execute(f"""
        SELECT c.source_quarter, count(*)
        FROM case_drugs d
        JOIN case_events e ON e.primaryid = d.primaryid AND e.event_term = %s
        JOIN cases c ON c.primaryid = d.primaryid AND c.is_current
        WHERE d.drug_normalized = %s {role_filter}
        GROUP BY 1 ORDER BY 1
    """, (event, drug))
    series = {q: int(n) for q, n in cur.fetchall()}

    volume = {p.quarter: p.reports for p in quarterly_volume(conn)}
    quarters = sorted(set(series) | set(volume))
    return [
        {"quarter": q, "a": series.get(q, 0), "reports": volume.get(q, 0)}
        for q in quarters
    ]
