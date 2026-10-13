"""Normalizers (spec §7): drug names, age, sex, event terms, countries.

All normalizers are pure functions: same input -> same output, testable.
Nothing normalizes to an invented value; unparseable values become None and
are counted upstream.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

_WS_RE = re.compile(r"\s+")

# Explicit alias table: raw upper-form name -> canonical name.
# Conservative on purpose: only well-documented salt/form equivalences.
_DEFAULT_ALIASES_PATH = Path(__file__).with_name("drug_aliases.json")

_ALIAS_CACHE: dict[str, str] | None = None


def load_aliases(path: Path | None = None) -> dict[str, str]:
    global _ALIAS_CACHE
    p = path or _DEFAULT_ALIASES_PATH
    if _ALIAS_CACHE is None or path is not None:
        data = json.loads(p.read_text(encoding="utf-8"))
        if path is None:
            _ALIAS_CACHE = data
        return data
    return _ALIAS_CACHE


def collapse_ws(value: str) -> str:
    return _WS_RE.sub(" ", value).strip()


def normalize_drug_name(raw: str | None, aliases: dict[str, str] | None = None) -> str | None:
    """Uppercase, deaccent, collapse whitespace, apply exact-match alias table.

    Returns None for blank input (counted as unknown downstream).
    """
    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", str(raw))
    text = collapse_ws(text).upper()
    if not text:
        return None
    table = aliases if aliases is not None else load_aliases()
    return table.get(text, text)


# Age normalization -> years
_AGE_UNIT_FACTORS = {
    "YR": 1.0, "YEAR": 1.0, "YEARS": 1.0, "DEC": 1.0,
    "MO": 1.0 / 12.0, "MON": 1.0 / 12.0, "MONTH": 1.0 / 12.0,
    "WK": 1.0 / 52.0, "WEEK": 1.0 / 52.0,
    "DY": 1.0 / 365.25, "DAY": 1.0 / 365.25,
    "HR": 1.0 / (365.25 * 24.0), "HOUR": 1.0 / (365.25 * 24.0),
}


def normalize_age(raw_age: str | None, raw_age_cod: str | None) -> float | None:
    """Convert FAERS age + AGE_COD to years. None if unparseable.

    DEC (decimal years) and YR are already years. Values are rounded to 4
    decimals; out-of-range (<0 or >130) values are rejected as invalid.
    """
    if raw_age is None:
        return None
    text = str(raw_age).strip()
    if not text:
        return None
    try:
        value = float(text.replace("<", "").replace(">", "").replace("=", ""))
    except ValueError:
        return None
    if value < 0 or value > 130:
        return None
    unit = str(raw_age_cod or "").strip().upper()
    if not unit:
        unit = "YR"  # FAERS default when AGE present without code
    factor = _AGE_UNIT_FACTORS.get(unit)
    if factor is None:
        return None
    years = value * factor
    if years > 130:
        return None
    return round(years, 4)


def normalize_sex(raw: str | None) -> str | None:
    """Map FAERS sex codes to 'M'/'F'; anything else -> None (unknown)."""
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if text in {"M", "MALE"}:
        return "M"
    if text in {"F", "FEMALE"}:
        return "F"
    return None


def normalize_event_term(raw: str | None) -> str | None:
    """Uppercase + collapse whitespace for MedDRA PT terms."""
    if raw is None:
        return None
    text = collapse_ws(str(raw)).upper()
    return text or None


def normalize_country(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = collapse_ws(str(raw)).upper()
    return text or None


def normalize_date(raw: str | None) -> str | None:
    """Normalize FAERS partial dates (YYYYMMDD, YYYYMM, YYYY) to ISO-ish text.

    Preserves granularity instead of inventing missing components. Returns
    None for blank/unparseable. Full dates become YYYY-MM-DD.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "null"}:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) == 8 and digits != "00000000":
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) == 6 and digits != "000000":
        return f"{digits[0:4]}-{digits[4:6]}"
    if len(digits) == 4 and digits != "0000":
        return digits
    return None
