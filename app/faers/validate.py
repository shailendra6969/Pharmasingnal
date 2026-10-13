"""Structural + row-level validation (spec §7).

Two levels:
1. File-level: required logical columns must resolve, otherwise the file is
   rejected with an explicit error (never silently skipped).
2. Row-level: rows missing critical identifiers are counted as rejected with
   a reason. Nothing is discarded without being counted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.faers import columns as cols
from app.faers.parse import ParsedFile


class FileValidationError(Exception):
    pass


@dataclass
class ValidationResult:
    dataset: str
    quarter: str
    raw_rows: int = 0
    accepted_rows: int = 0
    rejected_rows: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    missing_columns: list[str] = field(default_factory=list)
    missingness: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str, count: int = 1) -> None:
        self.rejected_rows += count
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + count

    def as_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "quarter": self.quarter,
            "raw_rows": self.raw_rows,
            "accepted_rows": self.accepted_rows,
            "rejected_rows": self.rejected_rows,
            "rejection_reasons": dict(self.rejection_reasons),
            "missing_columns": list(self.missing_columns),
            "missingness": dict(self.missingness),
        }


def validate_file(parsed: ParsedFile) -> tuple[pd.DataFrame, ValidationResult]:
    """Validate one parsed file; return accepted rows + a ValidationResult."""
    required = cols.REQUIRED_COLUMNS.get(parsed.dataset, ("primaryid",))
    result = ValidationResult(dataset=parsed.dataset, quarter=parsed.quarter,
                              raw_rows=parsed.raw_rows)

    missing = [f for f in required if not parsed.field_map.get(f)]
    if missing:
        result.missing_columns = missing
        raise FileValidationError(
            f"{parsed.path}: missing required column(s) {missing} "
            f"(logical fields; header had: {list(parsed.frame.columns)[:20]})"
        )

    df = parsed.frame
    keep = pd.Series(True, index=df.index)

    pid_col = parsed.field_map["primaryid"]
    blank_pid = df[pid_col].str.strip() == ""
    if blank_pid.any():
        result.reject("missing_primaryid", int(blank_pid.sum()))
        keep &= ~blank_pid

    if parsed.dataset == "demo":
        cid_col = parsed.field_map.get("caseid")
        if cid_col:
            blank_cid = df[cid_col].str.strip() == ""
            if blank_cid.any():
                result.reject("missing_caseid", int(blank_cid.sum()))
                keep &= ~blank_cid
    elif parsed.dataset == "drug":
        dn = parsed.field_map.get("drugname")
        if dn:
            blank = df[dn].str.strip() == ""
            if blank.any():
                # Rows kept (drug name is not a hard identifier) but counted:
                # they become "(UNKNOWN)" after normalization.
                result.missingness["blank_drugname"] = int(blank.sum())
    elif parsed.dataset == "reac":
        pt = parsed.field_map.get("pt")
        if pt:
            blank = df[pt].str.strip() == ""
            if blank.any():
                result.reject("missing_pt", int(blank.sum()))
                keep &= ~blank

    accepted = df[keep].copy()
    result.accepted_rows = len(accepted)

    # Missingness for reportable demographics
    if parsed.dataset == "demo":
        for logical, label in (("age", "missing_age"), ("sex", "missing_sex")):
            col = parsed.field_map.get(logical)
            if col:
                result.missingness[label] = int((accepted[col].str.strip() == "").sum())
        if not result.raw_rows:
            pass

    return accepted, result
