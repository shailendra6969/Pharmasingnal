"""Transform validated FAERS frames into normalized load records (spec §7).

Pipeline stage: validate -> normalize -> (loader) load. Each transformation
increments a TransformStats counter so the quality report can show exactly
what happened to every row (spec §7: "Never silently discard data").

Implementation note: columns are extracted as Python lists (never via
namedtuple field names, which pandas mangles for non-identifier headers).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.faers import normalize as nz
from app.faers.parse import ParsedFile
from app.faers.validate import ValidationResult, validate_file

UNKNOWN_DRUG = "(UNKNOWN)"


@dataclass
class TransformStats:
    quarter: str
    demo: ValidationResult | None = None
    drug: ValidationResult | None = None
    reac: ValidationResult | None = None
    drugs_unknown_name: int = 0
    drugs_normalized: int = 0
    ages_unparseable: int = 0
    ages_missing: int = 0
    sexes_unknown: int = 0
    events_normalized: int = 0
    drug_rows_orphan: int = 0      # drug rows whose primaryid not in accepted demo
    event_rows_orphan: int = 0     # event rows whose primaryid not in accepted demo
    duplicate_drug_rows: int = 0   # same (primaryid, drug_seq/name) seen twice
    duplicate_event_rows: int = 0  # same (primaryid, pt) seen twice

    def as_dict(self) -> dict:
        return {
            "quarter": self.quarter,
            "demo": self.demo.as_dict() if self.demo else None,
            "drug": self.drug.as_dict() if self.drug else None,
            "reac": self.reac.as_dict() if self.reac else None,
            "drugs_unknown_name": self.drugs_unknown_name,
            "drugs_normalized": self.drugs_normalized,
            "ages_unparseable": self.ages_unparseable,
            "ages_missing": self.ages_missing,
            "sexes_unknown": self.sexes_unknown,
            "events_normalized": self.events_normalized,
            "drug_rows_orphan": self.drug_rows_orphan,
            "event_rows_orphan": self.event_rows_orphan,
            "duplicate_drug_rows": self.duplicate_drug_rows,
            "duplicate_event_rows": self.duplicate_event_rows,
        }


def _columns(df: pd.DataFrame, field_map: dict[str, str | None],
             fields: list[str]) -> dict[str, list[str] | None]:
    """Return {field: list-of-values} for fields present, else None."""
    out: dict[str, list[str] | None] = {}
    for f in fields:
        name = field_map.get(f)
        out[f] = df[name].tolist() if name else None
    return out


def transform_quarter(parsed: dict[str, ParsedFile]) -> tuple[dict[str, list[tuple]], TransformStats]:
    """Validate + normalize one quarter.

    Returns ({"cases": [...], "case_drugs": [...], "case_events": [...]},
             TransformStats).
    """
    stats = TransformStats(quarter=parsed["demo"].quarter)

    demo_df, demo_res = validate_file(parsed["demo"])
    drug_df, drug_res = validate_file(parsed["drug"])
    reac_df, reac_res = validate_file(parsed["reac"])
    stats.demo, stats.drug, stats.reac = demo_res, drug_res, reac_res

    # ---------------- cases ----------------
    dm = parsed["demo"].field_map
    c = _columns(demo_df, dm, ["primaryid", "caseid", "event_dt", "fda_dt",
                               "fda_version", "age", "age_cod", "sex",
                               "reporter_type", "reporter_country",
                               "occr_country"])
    n = len(demo_df)
    cases: list[tuple] = []
    for i in range(n):
        primaryid = c["primaryid"][i].strip()
        case_id = c["caseid"][i].strip() if c["caseid"] else primaryid

        age_raw = c["age"][i] if c["age"] else ""
        age_cod_raw = c["age_cod"][i] if c["age_cod"] else None
        if not str(age_raw).strip():
            stats.ages_missing += 1
        age_years = nz.normalize_age(age_raw, age_cod_raw)
        if str(age_raw).strip() and age_years is None:
            stats.ages_unparseable += 1

        sex = nz.normalize_sex(c["sex"][i] if c["sex"] else None)
        if sex is None:
            stats.sexes_unknown += 1

        country = (nz.normalize_country(c["reporter_country"][i] if c["reporter_country"] else None)
                   or nz.normalize_country(c["occr_country"][i] if c["occr_country"] else None))

        fda_dt = c["fda_dt"][i] if c["fda_dt"] else None
        fda_version = c["fda_version"][i] if c["fda_version"] else None

        cases.append((
            primaryid,
            case_id,
            nz.normalize_date(c["event_dt"][i] if c["event_dt"] else None),
            nz.normalize_date(fda_dt),
            age_years,
            sex,
            country,
            nz.collapse_ws(str(c["reporter_type"][i] if c["reporter_type"] else "")).upper() or None,
            str(fda_version).strip() if fda_version and str(fda_version).strip() else None,
            nz.normalize_date(fda_dt),  # version marker used by dedupe
            parsed["demo"].quarter,
        ))
    valid_primaryids = {row[0] for row in cases}
    pid_to_caseid = {row[0]: row[1] for row in cases}

    # ---------------- case_drugs ----------------
    gm = parsed["drug"].field_map
    g = _columns(drug_df, gm, ["primaryid", "drug_seq", "drugname", "role_cod",
                               "route", "dose_vbm"])
    drugs: list[tuple] = []
    seen_drugs: set[tuple[str, str]] = set()
    for i in range(len(drug_df)):
        primaryid = g["primaryid"][i].strip()
        if primaryid not in valid_primaryids:
            stats.drug_rows_orphan += 1
            continue
        raw_name = g["drugname"][i] if g["drugname"] else ""
        drug_seq = (g["drug_seq"][i].strip() if g["drug_seq"] else "") or None
        key = (primaryid, drug_seq or nz.normalize_drug_name(raw_name) or "")
        if key in seen_drugs:
            stats.duplicate_drug_rows += 1
            continue
        seen_drugs.add(key)
        name = nz.normalize_drug_name(raw_name)
        if name is None:
            stats.drugs_unknown_name += 1
            name = UNKNOWN_DRUG
        else:
            stats.drugs_normalized += 1
        drugs.append((
            primaryid,
            pid_to_caseid[primaryid],
            drug_seq,
            name,
            nz.collapse_ws(str(raw_name)).upper() or None,
            nz.collapse_ws(str(g["role_cod"][i] if g["role_cod"] else "")).upper() or None,
            nz.collapse_ws(str(g["route"][i] if g["route"] else "")).upper() or None,
            nz.collapse_ws(str(g["dose_vbm"][i] if g["dose_vbm"] else "")) or None,
        ))

    # ---------------- case_events ----------------
    rm = parsed["reac"].field_map
    r = _columns(reac_df, rm, ["primaryid", "pt"])
    events: list[tuple] = []
    seen_events: set[tuple[str, str]] = set()
    for i in range(len(reac_df)):
        primaryid = r["primaryid"][i].strip()
        if primaryid not in valid_primaryids:
            stats.event_rows_orphan += 1
            continue
        term = nz.normalize_event_term(r["pt"][i] if r["pt"] else None)
        if term is None:
            continue
        key = (primaryid, term)
        if key in seen_events:
            stats.duplicate_event_rows += 1
            continue
        seen_events.add(key)
        stats.events_normalized += 1
        events.append((primaryid, pid_to_caseid[primaryid], term))

    return {"cases": cases, "case_drugs": drugs, "case_events": events}, stats
