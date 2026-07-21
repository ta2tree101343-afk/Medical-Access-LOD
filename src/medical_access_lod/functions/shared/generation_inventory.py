"""読み取りモデル世代の PK/SK インベントリを S3 に書き出し/読み出しする。

Cleanup Lambda が BatchWriteItem で世代データを削除する際に、削除対象の
PK/SK 一覧を得るためのソース。DynamoDB Scan は PK が `GENERATION#<run_id>#...`
のように run_id をキー先頭に含むため Query では列挙できず、大規模テーブル
での Scan は高コストになる。BuildReadModel 実行時に PK/SK を分割 gzip JSONL
として書き出しておくことで、Cleanup を Scan せずに実行できるようにする。

フォーマット (schema_version=2)
------------------------------
- Prefix: 呼び出し側が指定 (通常 `generations/<run_id>/inventory/`)
- ファイル名: `chunk-NNNNNN.jsonl.gz` (0 埋め 6 桁の連番でソート可能)
- 1 行 = `{"PK": "...", "SK": "..."}` の JSON
- 空 chunk は生成しない (0 件のときは MANIFEST のみ)
- MANIFEST: `<prefix>MANIFEST.json` に run_id / prefix / 総件数 / chunk 一覧を記録
  reader は run_id と prefix を照合し、想定外の世代/場所の inventory を
  誤って読まないようにする (Cleanup による現行世代誤削除を防ぐ多層防御)。
"""
from __future__ import annotations

import gzip
import io
import json
from collections.abc import Iterable, Iterator
from typing import Any

from medical_access_lod.functions.shared.s3io import s3_client

# BatchWriteItem の 25 件上限に対して十分な余裕。1 chunk あたり ~15 KB に収まる想定。
DEFAULT_CHUNK_SIZE = 1000
MANIFEST_FILENAME = "MANIFEST.json"
SCHEMA_VERSION = 2


class InventoryValidationError(RuntimeError):
    """inventory MANIFEST の内容が期待した run_id / prefix と一致しない。"""


def _iter_chunks(items: Iterable[dict[str, Any]], chunk_size: int) -> Iterator[list[dict[str, Any]]]:
    buffer: list[dict[str, Any]] = []
    for item in items:
        buffer.append(item)
        if len(buffer) >= chunk_size:
            yield buffer
            buffer = []
    if buffer:
        yield buffer


def _encode_chunk(chunk: list[dict[str, Any]]) -> bytes:
    """chunk を gzip 圧縮した JSONL バイト列に変換する。"""

    raw = io.BytesIO()
    # mtime=0 で決定論的な出力にする (再実行での ETag 一致・S3 の
    # ConditionalPUT を将来使うため)
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        for record in chunk:
            gz.write(json.dumps(record, ensure_ascii=False).encode("utf-8"))
            gz.write(b"\n")
    return raw.getvalue()


def write_inventory(
    bucket: str,
    prefix: str,
    keys: Iterable[tuple[str, str]],
    *,
    run_id: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """PK/SK ペアのイテレータを gzip 分割 JSONL として S3 へ書き出す。

    run_id は MANIFEST に埋め込まれ、reader が「この inventory が本当に
    その世代のものか」を検証する材料になる。
    """

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not prefix.endswith("/"):
        raise ValueError(f"inventory prefix must end with '/': {prefix!r}")
    if not run_id:
        raise ValueError("run_id must not be empty")

    client = s3_client()
    records = ({"PK": pk, "SK": sk} for pk, sk in keys)

    chunk_infos: list[dict[str, Any]] = []
    total_count = 0
    for index, chunk in enumerate(_iter_chunks(records, chunk_size)):
        key = f"{prefix}chunk-{index:06d}.jsonl.gz"
        body = _encode_chunk(chunk)
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/x-ndjson",
            ContentEncoding="gzip",
        )
        chunk_infos.append(
            {"key": key, "item_count": len(chunk), "compressed_size": len(body)}
        )
        total_count += len(chunk)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "prefix": prefix,
        "item_count": total_count,
        "chunk_count": len(chunk_infos),
        "chunks": chunk_infos,
    }
    client.put_object(
        Bucket=bucket,
        Key=f"{prefix}{MANIFEST_FILENAME}",
        Body=json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )
    return manifest


def read_inventory_manifest(
    bucket: str,
    prefix: str,
    *,
    expected_run_id: str,
) -> dict[str, Any]:
    """MANIFEST を読み、schema/run_id/prefix/chunk key を検証して返す。

    invariant を満たさない場合は InventoryValidationError を送出し、
    呼び出し側は削除処理を中断する (SQS 再配信で DLQ に到達すれば運用者に届く)。
    """

    if not prefix.endswith("/"):
        raise ValueError(f"inventory prefix must end with '/': {prefix!r}")

    client = s3_client()
    manifest_key = f"{prefix}{MANIFEST_FILENAME}"
    response = client.get_object(Bucket=bucket, Key=manifest_key)
    manifest = json.loads(response["Body"].read())
    if not isinstance(manifest, dict):
        raise InventoryValidationError(f"inventory manifest is not a dict: {manifest_key!r}")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise InventoryValidationError(
            f"inventory schema_version mismatch: expected {SCHEMA_VERSION}, "
            f"got {manifest.get('schema_version')!r}"
        )
    if manifest.get("run_id") != expected_run_id:
        raise InventoryValidationError(
            f"inventory run_id mismatch: expected {expected_run_id!r}, "
            f"got {manifest.get('run_id')!r}"
        )
    if manifest.get("prefix") != prefix:
        raise InventoryValidationError(
            f"inventory prefix mismatch: expected {prefix!r}, "
            f"got {manifest.get('prefix')!r}"
        )
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list):
        raise InventoryValidationError("inventory manifest missing 'chunks' list")
    for chunk in chunks:
        chunk_key = chunk.get("key") if isinstance(chunk, dict) else None
        if not isinstance(chunk_key, str) or not chunk_key.startswith(prefix):
            raise InventoryValidationError(
                f"inventory chunk key {chunk_key!r} is outside prefix {prefix!r}"
            )
    return manifest


def read_inventory_chunk(
    bucket: str,
    chunk_key: str,
    *,
    expected_run_id: str,
) -> list[tuple[str, str]]:
    """1 chunk を読み、全 PK が期待する world_id のプレフィックスに従うか検証する。

    現行世代 (`GENERATION#<active>#...`) を含む inventory が誤って
    旧世代削除リストに紛れ込んでも、この検証が入っていれば削除は起きない。
    """

    if not expected_run_id:
        raise ValueError("expected_run_id must not be empty")
    expected_pk_prefix = f"GENERATION#{expected_run_id}#"

    response = s3_client().get_object(Bucket=bucket, Key=chunk_key)
    raw = response["Body"].read()
    keys: list[tuple[str, str]] = []
    with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as gz:
        for line in gz:
            record = json.loads(line)
            pk = record["PK"]
            sk = record["SK"]
            if not pk.startswith(expected_pk_prefix):
                raise InventoryValidationError(
                    f"inventory chunk {chunk_key!r} contains PK {pk!r} that does "
                    f"not belong to run_id {expected_run_id!r}"
                )
            keys.append((pk, sk))
    return keys


def read_inventory_keys(
    bucket: str,
    prefix: str,
    *,
    expected_run_id: str,
) -> Iterator[tuple[str, str]]:
    """MANIFEST 経由でインベントリを列挙して (PK, SK) を yield する。

    MANIFEST が chunk 一覧の唯一の真とする (S3 ListObjects の結果整合性を避ける)。
    schema/run_id/prefix/chunk key/PK prefix の全てを検証する。
    """

    manifest = read_inventory_manifest(bucket, prefix, expected_run_id=expected_run_id)
    for chunk_info in manifest.get("chunks", []):
        yield from read_inventory_chunk(
            bucket, chunk_info["key"], expected_run_id=expected_run_id
        )
