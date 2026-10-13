"""FAERS column-name aliases.

Real FAERS headers vary slightly across years and are sometimes upper/lower
case with BOMs. All matching is done on uppercased, BOM-stripped, whitespace-
collapsed header names.
"""
from __future__ import annotations

DEMO = {
    "primaryid": ("PRIMARYID", "PRIMARYID_"),
    "caseid": ("CASEID",),
    "event_dt": ("EVENT_DT",),
    "fda_dt": ("FDA_DT",),
    "fda_version": ("FDA_VERSION", "CASEVERSION", "VERSION"),
    "rept_cod": ("REPT_COD",),
    "age": ("AGE",),
    "age_cod": ("AGE_COD",),
    "sex": ("SEX",),
    "weight": ("WT", "WEIGHT"),
    "weight_cod": ("WT_COD",),
    "reporter_type": ("OCCP_COD",),
    "reporter_country": ("REPORTER_COUNTRY",),
    "occr_country": ("OCCR_COUNTRY",),
}

DRUG = {
    "primaryid": ("PRIMARYID",),
    "drug_seq": ("DRUG_SEQ",),
    "role_cod": ("ROLE_COD", "ROLE",),
    "drugname": ("DRUGNAME", "DRUG_NAME", "MEDICINALPRODUCT"),
    "prod_ai": ("PROD_AI",),
    "route": ("ROUTE", "LPET_ROUTE"),
    "dose_vbm": ("DOSE_VBM", "DOSE_AMT"),
    "dose_unit": ("DOSE_UNIT",),
    "dechal": ("DECHAL",),
    "rechal": ("RECHAL",),
    "lot_num": ("LOT_NUM",),
    "exp_dt": ("EXP_DT",),
}

REAC = {
    "primaryid": ("PRIMARYID",),
    "pt": ("PT", "MEDDRA_PT"),
}

OUTC = {
    "primaryid": ("PRIMARYID",),
    "outc_cod": ("OUTC_COD",),
}

# Logical field -> (dataset, list of physical names), used by validation.
REQUIRED_COLUMNS = {
    "demo": ("primaryid", "caseid"),
    "drug": ("primaryid", "drugname"),
    "reac": ("primaryid", "pt"),
}


def clean_header(name: str) -> str:
    return name.replace("\ufeff", "").strip().upper()


def resolve(header: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, str | None]:
    """Map logical field -> physical column name present in header (or None)."""
    cleaned = {clean_header(h): h for h in header}
    out: dict[str, str | None] = {}
    for field, candidates in aliases.items():
        found = None
        for cand in candidates:
            if cand in cleaned:
                found = cleaned[cand]
                break
        out[field] = found
    return out
