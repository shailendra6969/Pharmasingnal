"""Data quality report (spec §7, §38).

Aggregates per-quarter TransformStats + database counts into a single JSON
report written to data/processed/ and persisted to `data_quality_reports`.
Every important transformation is measurable; nothing is silently discarded.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.faers.transform import TransformStats


@dataclass
class QualityReport:
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    quarters: list[str] = field(default_factory=list)
    stage_totals: dict[str, int] = field(default_factory=dict)
    per_quarter: list[dict] = field(default_factory=list)
    database_counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # 'partial' while a run is still in flight (persisted per quarter so a
    # killed client cannot lose completed quarters' stats); 'complete' after
    # the final database counts land.
    status: str = "complete"

    def add_stats(self, stats: TransformStats) -> None:
        self.quarters.append(stats.quarter)
        self.per_quarter.append(stats.as_dict())
        d = stats.as_dict()
        totals = self.stage_totals
        for key in (
            "drugs_normalized", "drugs_unknown_name", "ages_missing",
            "ages_unparseable", "sexes_unknown", "events_normalized",
            "drug_rows_orphan", "event_rows_orphan",
            "duplicate_drug_rows", "duplicate_event_rows",
        ):
            totals[key] = totals.get(key, 0) + int(d[key])
        for stage in ("demo", "drug", "reac"):
            sd = d.get(stage)
            if not sd:
                continue
            for k in ("raw_rows", "accepted_rows", "rejected_rows"):
                key = f"{stage}_{k}"
                totals[key] = totals.get(key, 0) + int(sd[k])
            for reason, cnt in sd.get("rejection_reasons", {}).items():
                key = f"{stage}_rejected_{reason}"
                totals[key] = totals.get(key, 0) + int(cnt)

    def set_database_counts(self, counts: dict[str, int]) -> None:
        self.database_counts = dict(counts)

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "quarters": self.quarters,
            "stage_totals": self.stage_totals,
            "database_counts": self.database_counts,
            "per_quarter": self.per_quarter,
            "notes": self.notes,
            "status": self.status,
        }

    def write(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = out_dir / f"data_quality_report_{stamp}.json"
        path.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=False),
                        encoding="utf-8")
        # Also maintain a stable pointer for the dashboard.
        latest = out_dir / "data_quality_report_latest.json"
        latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        return path
