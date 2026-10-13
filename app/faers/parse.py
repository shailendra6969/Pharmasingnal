"""FAERS file discovery and parsing (spec §7).

Header-driven: files are read as tab-delimited latin-1 text; column names are
resolved through alias tables so minor header drift across years does not
break the pipeline. Parsing never silently drops rows — counts are reported
by the validation stage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from app.faers import columns as cols

QUARTER_RE = re.compile(r"^(\d{4})q([1-4])$", re.IGNORECASE)

# File prefix -> logical dataset name
DATASET_PREFIXES = {"DEMO": "demo", "DRUG": "drug", "REAC": "reac",
                    "OUTC": "outc", "THER": "ther", "INDI": "indi"}


def normalize_quarter(quarter: str) -> str:
    m = QUARTER_RE.match(quarter.strip())
    if not m:
        raise ValueError(f"Invalid quarter '{quarter}': expected e.g. 2024q1")
    return f"{m.group(1)}q{m.group(2)}"


@dataclass(frozen=True)
class ParsedFile:
    dataset: str          # demo | drug | reac | ...
    quarter: str          # 2024q1
    path: Path
    raw_rows: int
    frame: pd.DataFrame   # all columns as raw strings (dtype=str, no NA coercion)
    field_map: dict[str, str | None]  # logical -> physical column


def discover_quarter_files(quarter_dir: Path, quarter: str) -> dict[str, Path]:
    """Find the DEMO/DRUG/REAC... files for a quarter inside a directory."""
    q = normalize_quarter(quarter)
    # Archives name files like DEMO24q1.txt (yyqN); also accept full 2024q1.
    tags = {q.upper(), q[2:].upper()}
    found: dict[str, Path] = {}
    for path in sorted(quarter_dir.rglob("*.txt")):
        name = path.name.upper()
        for prefix, dataset in DATASET_PREFIXES.items():
            if name.startswith(prefix) and any(tag in name for tag in tags):
                found.setdefault(dataset, path)
    if not found:
        # Fallback: accept files whose prefix matches even if quarter tag differs
        # (some archives name files without the year prefix), e.g. DEMO.txt
        for path in sorted(quarter_dir.rglob("*.txt")):
            name = path.name.upper()
            for prefix, dataset in DATASET_PREFIXES.items():
                if name.startswith(prefix) and dataset not in found:
                    found.setdefault(dataset, path)
    return found


def sniff_delimiter(path: Path, sample_bytes: int = 65536) -> str:
    """Detect the field separator: '$' (2024+ FIS exports), tab (legacy), or '|'."""
    with path.open("rb") as fh:
        head = fh.read(sample_bytes)
    text = head.decode("latin-1", errors="replace")
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    candidates = {"$": first.count("$"), "\t": first.count("\t"), "|": first.count("|")}
    best = max(candidates, key=candidates.get)
    if candidates[best] == 0:
        raise ValueError(f"Cannot detect delimiter for {path}: no '$', tab, or '|' in header")
    return best


def parse_file(path: Path, dataset: str, quarter: str) -> ParsedFile:
    """Parse one FAERS ASCII file into a raw-string DataFrame."""
    aliases = getattr(cols, dataset.upper())
    sep = sniff_delimiter(path)
    df = pd.read_csv(
        path,
        sep=sep,
        dtype=str,
        keep_default_na=False,
        na_filter=False,
        encoding="latin-1",
        quoting=3,  # csv.QUOTE_NONE: FAERS text can contain stray quotes
        engine="c",
    )
    field_map = resolve_field_map(df, aliases)
    return ParsedFile(
        dataset=dataset,
        quarter=normalize_quarter(quarter),
        path=path,
        raw_rows=len(df),
        frame=df,
        field_map=field_map,
    )


def resolve_field_map(df: pd.DataFrame, aliases: dict[str, tuple[str, ...]]) -> dict[str, str | None]:
    header = list(df.columns)
    cleaned = {cols.clean_header(h): h for h in header}
    out: dict[str, str | None] = {}
    for field, candidates in aliases.items():
        found = None
        for cand in candidates:
            if cand in cleaned:
                found = cleaned[cand]
                break
        out[field] = found
    return out


def parse_quarter(quarter_dir: Path, quarter: str, datasets: tuple[str, ...] = ("demo", "drug", "reac")) -> dict[str, ParsedFile]:
    q = normalize_quarter(quarter)
    files = discover_quarter_files(quarter_dir, q)
    missing = [d for d in datasets if d not in files]
    if missing:
        raise FileNotFoundError(
            f"Quarter {q}: missing required file(s) {missing} in {quarter_dir} "
            f"(found: {sorted(files)})"
        )
    return {d: parse_file(files[d], d, q) for d in datasets}
