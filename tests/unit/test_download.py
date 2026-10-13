from __future__ import annotations

import zipfile

import pytest

from app.faers.download import URL_PATTERNS, archive_url, download_quarter


def test_archive_url_uses_current_pattern():
    url = archive_url("2024q1")
    assert url == "https://fis.fda.gov/content/Exports/faers_ascii_2024q1.zip"
    legacy = archive_url("2024q1", pattern=1)
    assert legacy == "https://fis.fda.gov/content/Exports/ASCII-2024q1.zip"
    assert len(URL_PATTERNS) >= 2


def test_download_quarter_uses_local_archive_without_network(fixture_q1, tmp_path):
    """If a valid archive already exists, no network access is attempted."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir(parents=True)
    archive = archive_dir / "ASCII-2099q1.zip"  # downloader's local archive name

    # Build a fake archive containing this quarter's files, named for 2099q1
    with zipfile.ZipFile(archive, "w") as zf:
        for src in fixture_q1.glob("*.txt"):
            name = src.name.replace("24q1", "99q1")
            zf.writestr(name, src.read_text(encoding="utf-8"))

    class _NoNetwork:
        def get(self, *a, **k):
            raise AssertionError("network access attempted but not needed")

    res = download_quarter("2099q1", tmp_path, session=_NoNetwork())
    assert res.skipped_existing is False
    assert (tmp_path / "2099q1").is_dir()
    names = {n.upper() for n in res.extracted}
    assert any(n.startswith("DEMO") for n in names)
    assert any(n.startswith("DRUG") for n in names)
    assert any(n.startswith("REAC") for n in names)


def test_invalid_quarter_raises():
    with pytest.raises(ValueError):
        archive_url("not-a-quarter")
