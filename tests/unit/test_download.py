from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from medical_access_lod.application.download_source import (
    HttpClient,
    SourceError,
    download,
)


@dataclass
class FakeHttp(HttpClient):
    payload: bytes

    def get_bytes(self, url: str) -> bytes:

        return self.payload


def _make_zip(entries: dict[str, bytes]) -> bytes:

    buf = io.BytesIO()

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)

    return buf.getvalue()


def _make_zip_with_symlink() -> bytes:

    buf = io.BytesIO()

    with zipfile.ZipFile(buf, mode="w") as zf:
        info = zipfile.ZipInfo("link_to_etc")

        info.external_attr = (0o120777 & 0xFFFF) << 16

        zf.writestr(info, "/etc/passwd")

    return buf.getvalue()


def test_downloads_and_extracts_csv(tmp_path: Path) -> None:

    payload = _make_zip(
        {
            "hospital_facility.csv": b"facility_id,name\n01,A\n",
            "hospital_schedule.csv": b"facility_id,day,opens,closes\n01,MON,09:00,17:00\n",
            "readme.pdf": b"%PDF",
        }
    )

    result = download(
        tmp_path,
        source_url="https://example.test/z.zip",
        snapshot_date="2025-12-01",
        client=FakeHttp(payload),
    )

    assert not result.skipped

    assert (result.raw_dir / "hospital_facility.csv").exists()

    assert (result.raw_dir / "hospital_schedule.csv").exists()

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert manifest["sha256"] == result.sha256

    assert manifest["snapshot_date"] == "2025-12-01"

    assert manifest["license"].startswith("PDL")

    assert "hospital_facility.csv" in manifest["extracted_files"]


def test_idempotent_on_same_sha(tmp_path: Path) -> None:

    payload = _make_zip({"a.csv": b"x,y\n1,2\n"})

    first = download(
        tmp_path,
        source_url="https://example.test/z.zip",
        snapshot_date="2025-12-01",
        client=FakeHttp(payload),
    )

    second = download(
        tmp_path,
        source_url="https://example.test/z.zip",
        snapshot_date="2025-12-01",
        client=FakeHttp(payload),
    )

    assert not first.skipped

    assert second.skipped

    assert first.sha256 == second.sha256


def test_rejects_path_traversal(tmp_path: Path) -> None:

    payload = _make_zip({"../evil.csv": b"nope"})

    with pytest.raises(SourceError, match="path traversal"):
        download(
            tmp_path,
            source_url="https://example.test/z.zip",
            snapshot_date="2025-12-01",
            client=FakeHttp(payload),
        )


def test_rejects_symlink(tmp_path: Path) -> None:

    payload = _make_zip_with_symlink()

    with pytest.raises(SourceError, match="symlink"):
        download(
            tmp_path,
            source_url="https://example.test/z.zip",
            snapshot_date="2025-12-01",
            client=FakeHttp(payload),
        )


def test_only_whitelisted_extensions_extracted(tmp_path: Path) -> None:

    payload = _make_zip(
        {
            "keep.csv": b"a,b\n",
            "definition.pdf": b"%PDF",
            "skip.sh": b"echo pwned",
            "skip.exe": b"MZ",
        }
    )

    result = download(
        tmp_path,
        source_url="https://example.test/z.zip",
        snapshot_date="2025-12-01",
        client=FakeHttp(payload),
    )

    assert (result.raw_dir / "keep.csv").exists()

    assert (result.raw_dir / "definition.pdf").exists()

    assert not (result.raw_dir / "skip.sh").exists()

    assert not (result.raw_dir / "skip.exe").exists()
