"""Reproducible FAERS quarterly downloader (spec §7).

Sources: FDA FIS ASCII quarterly export archives
(https://fis.fda.gov/content/Exports/ASCII-YYYYqN.zip).

Safety properties:
- Raw archives are stored byte-for-byte under data/raw/FAERS/ (never committed).
- Extracts only the datasets the pipeline consumes (DEMO/DRUG/REAC by default).
- Skips files that already exist unless --force.
- Verifies the zip opens and contains the expected dataset prefixes before
  extracting (a truncated download never silently becomes "data").
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from app.faers.parse import DATASET_PREFIXES, normalize_quarter

# Current FIS export pattern (verified 2026-10), with the legacy pattern kept
# as a fallback: some mirrors/archives still use ASCII-YYYYqN.zip.
URL_PATTERNS = (
    "https://fis.fda.gov/content/Exports/faers_ascii_{quarter}.zip",
    "https://fis.fda.gov/content/Exports/ASCII-{quarter}.zip",
)
CHUNK = 1024 * 1024


@dataclass
class DownloadResult:
    quarter: str
    archive: Path
    extracted_dir: Path
    extracted: list[str]
    skipped_existing: bool
    archive_sha256: str | None


def archive_url(quarter: str, pattern: int = 0) -> str:
    return URL_PATTERNS[pattern].format(quarter=normalize_quarter(quarter))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def download_quarter(
    quarter: str,
    raw_root: Path,
    datasets: tuple[str, ...] = ("demo", "drug", "reac"),
    force: bool = False,
    session: requests.Session | None = None,
    timeout: int = 120,
) -> DownloadResult:
    q = normalize_quarter(quarter)
    raw_root = Path(raw_root)
    archive_dir = raw_root / "archives"
    extract_dir = raw_root / q
    archive_dir.mkdir(parents=True, exist_ok=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    archive = archive_dir / f"ASCII-{q}.zip"
    sess = session or requests.Session()

    needed_prefixes = {p.upper() for d, p in
                       ((d, d.upper()) for d in datasets)}

    # Skip everything if the expected datasets are already extracted
    if not force:
        existing = {p.name.upper() for p in extract_dir.glob("*.txt")}
        if existing and all(any(name.startswith(pref) for name in existing)
                            for pref in needed_prefixes):
            return DownloadResult(q, archive, extract_dir, sorted(existing),
                                  True, None)

    if force or not archive.exists():
        tmp = archive.with_suffix(".zip.part")
        attempts: list[str] = []
        downloaded = False
        for idx, pattern in enumerate(URL_PATTERNS):
            url = archive_url(q, pattern=idx)
            for attempt in range(1, 4):
                try:
                    print(f"[{q}] downloading {url} (attempt {attempt})", flush=True)
                    with sess.get(url, stream=True,
                                  timeout=(30, max(timeout, 300))) as resp:
                        if resp.status_code != 200:
                            attempts.append(f"{url}: HTTP {resp.status_code}")
                            break  # 404 etc. will not improve on retry
                        written = 0
                        next_mb = 10 * CHUNK
                        with tmp.open("wb") as fh:
                            for block in resp.iter_content(CHUNK):
                                if block:
                                    fh.write(block)
                                    written += len(block)
                                    if written >= next_mb:
                                        print(f"[{q}] {written // CHUNK} MB...",
                                              flush=True)
                                        next_mb += 10 * CHUNK
                    if not zipfile.is_zipfile(tmp):
                        tmp.unlink(missing_ok=True)
                        attempts.append(f"{url}: not a valid zip archive")
                        continue
                    tmp.replace(archive)
                    downloaded = True
                    break
                except Exception as exc:
                    attempts.append(f"{url} (attempt {attempt}): {exc}")
                    tmp.unlink(missing_ok=True)
            if downloaded:
                break
        if not downloaded:
            raise RuntimeError(f"Download failed for {q}: " + "; ".join(attempts))

    extracted: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        for member in names:
            base = Path(member).name.upper()
            if not base.endswith(".TXT"):
                continue
            if any(base.startswith(pref) for pref in needed_prefixes):
                target = extract_dir / Path(member).name
                if force or not target.exists():
                    with zf.open(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                extracted.append(target.name)

    if not extracted:
        raise RuntimeError(
            f"Archive for {q} contains none of the expected datasets "
            f"{sorted(needed_prefixes)}; members: {names[:20]}"
        )

    return DownloadResult(q, archive, extract_dir, sorted(extracted), False,
                          _sha256(archive))


def main(argv: list[str] | None = None) -> int:
    import argparse

    from app.config import settings

    parser = argparse.ArgumentParser(description="Download FAERS ASCII quarterly archives")
    parser.add_argument("--quarters", default=",".join(settings.faers_quarters()),
                        help="Comma-separated quarters, e.g. 2024q1,2024q2")
    parser.add_argument("--raw-root", default=str(settings.faers_data_path()))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    failures = 0
    for q in [x.strip() for x in args.quarters.split(",") if x.strip()]:
        try:
            res = download_quarter(q, Path(args.raw_root), force=args.force)
            state = "exists (skipped)" if res.skipped_existing else "downloaded"
            print(f"[{q}] {state}: {len(res.extracted)} file(s) -> {res.extracted_dir}")
        except Exception as exc:  # report per-quarter, keep going
            failures += 1
            print(f"[{q}] FAILED: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
