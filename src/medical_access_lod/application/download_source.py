"""元データ取得。

出典: 厚生労働省 医療情報ネットのオープンデータ (PDL 1.0)
        https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/newpage_43373.html

処理:
    1. HTTP GET でスナップショット ZIP を取得
    2. SHA-256 を計算し、既存 manifest と一致するならスキップ (冪等)
    3. ZIP を data/raw/<snapshot_date>/ に安全展開 (path traversal / zip bomb 対策)
    4. manifest.json を書き出す

正規化 (千葉市抽出) は normalize_data.py 側で行う。ここでは元データを改変しない。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

DEFAULT_SOURCE_URL = (
    "https://data.e-gov.go.jp/data/dataset/321fdf20-5f6a-49e5-bcab-35d81d652c65/"
    "resource/af88450b-049c-4deb-8dc9-327312d877e1/download/e-gov20251201.zip"
)

DEFAULT_SNAPSHOT_DATE = "2025-12-01"

DEFAULT_LICENSE = "PDL 1.0 (Public Data License 1.0)"

DEFAULT_ATTRIBUTION = "厚生労働省 医療情報ネット"


MAX_ZIP_BYTES = 500 * 1024 * 1024

MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024

HTTP_TIMEOUT_SECONDS = 120.0


class SourceError(RuntimeError):
    """出典データ取得中の異常。"""


class HttpClient(Protocol):
    """テスト差し替え用の HTTP クライアント Protocol。"""

    def get_bytes(self, url: str) -> bytes: ...


class HttpxClient:
    """httpx ベースの実装。"""

    def __init__(self, timeout: float = HTTP_TIMEOUT_SECONDS) -> None:

        self._timeout = timeout

    def get_bytes(self, url: str) -> bytes:

        with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
            response = client.get(url)

            response.raise_for_status()

            content = response.content

            if len(content) > MAX_ZIP_BYTES:
                raise SourceError(f"ZIP size {len(content)} exceeds MAX_ZIP_BYTES={MAX_ZIP_BYTES}")

            return content


@dataclass(frozen=True)
class DownloadResult:
    raw_dir: Path

    manifest_path: Path

    sha256: str

    skipped: bool

    extracted_files: list[str]


def _sha256_hex(data: bytes) -> str:

    return hashlib.sha256(data).hexdigest()


def _read_existing_sha(manifest_path: Path) -> str | None:

    if not manifest_path.exists():
        return None

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    except (OSError, json.JSONDecodeError):
        return None

    value = payload.get("sha256")

    return value if isinstance(value, str) else None


def _safe_extract(archive: zipfile.ZipFile, dest_dir: Path) -> list[str]:
    """ZIP を安全に展開する。

    - パストラバーサル (`..`, 絶対パス) を持つエントリは拒否
    - シンボリックリンクは拒否
    - 展開後の合計サイズが MAX_EXTRACTED_BYTES を超えたら中断
    - CSV / 定義書系のみを対象にする (実行ファイル等は展開しない)
    """

    dest_dir = dest_dir.resolve()

    dest_dir.mkdir(parents=True, exist_ok=True)

    extracted: list[str] = []

    total = 0

    for info in archive.infolist():
        if info.is_dir():
            continue

        mode = info.external_attr >> 16

        if mode & 0o120000 == 0o120000:
            raise SourceError(f"symlink in archive not allowed: {info.filename}")

        candidate = (dest_dir / info.filename).resolve()

        if dest_dir not in candidate.parents and candidate != dest_dir:
            raise SourceError(f"path traversal attempt: {info.filename!r}")

        declared = info.file_size

        if declared < 0:
            raise SourceError(f"invalid declared size: {info.filename}")

        total += declared

        if total > MAX_EXTRACTED_BYTES:
            raise SourceError("extracted size exceeds MAX_EXTRACTED_BYTES")

        suffix = candidate.suffix.lower()

        if suffix not in {".csv", ".txt", ".pdf", ".xlsx"}:
            continue

        candidate.parent.mkdir(parents=True, exist_ok=True)

        with archive.open(info) as src, candidate.open("wb") as dst:
            written = 0

            while chunk := src.read(1024 * 1024):
                written += len(chunk)

                if written > declared:
                    raise SourceError(f"actual size exceeds declared for {info.filename}")

                dst.write(chunk)

        extracted.append(str(candidate.relative_to(dest_dir)))

    return extracted


def download(
    dest_root: Path,
    *,
    source_url: str = DEFAULT_SOURCE_URL,
    snapshot_date: str = DEFAULT_SNAPSHOT_DATE,
    license_text: str = DEFAULT_LICENSE,
    attribution: str = DEFAULT_ATTRIBUTION,
    client: HttpClient | None = None,
) -> DownloadResult:
    """指定 URL からスナップショット ZIP を取得し、dest_root/<snapshot_date>/ に展開する。"""

    raw_dir = dest_root / snapshot_date

    raw_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = raw_dir / "manifest.json"

    http = client or HttpxClient()

    content = http.get_bytes(source_url)

    sha256 = _sha256_hex(content)

    existing = _read_existing_sha(manifest_path)

    if existing == sha256:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))

        extracted = list(payload.get("extracted_files", []))

        return DownloadResult(
            raw_dir=raw_dir,
            manifest_path=manifest_path,
            sha256=sha256,
            skipped=True,
            extracted_files=extracted,
        )

    zip_path = raw_dir / "source.zip"

    zip_path.write_bytes(content)

    with zipfile.ZipFile(zip_path) as archive:
        extracted = _safe_extract(archive, raw_dir)

    manifest = {
        "source": attribution,
        "source_url": source_url,
        "snapshot_date": snapshot_date,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "license": license_text,
        "sha256": sha256,
        "zip_size_bytes": len(content),
        "extracted_files": extracted,
    }

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    return DownloadResult(
        raw_dir=raw_dir,
        manifest_path=manifest_path,
        sha256=sha256,
        skipped=False,
        extracted_files=extracted,
    )
