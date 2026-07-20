"""読み取りモデル世代の PK/SK インベントリを S3 に書き出し/読み出しする。

Cleanup Lambda が BatchWriteItem で世代データを削除する際に、削除対象の
PK/SK 一覧を得るためのソース。DynamoDB Scan は PK が `GENERATION#<run_id>#...`
のように run_id をキー先頭に含むため Query では列挙できず、大規模テーブル
での Scan は高コストになる。BuildReadModel 実行時に PK/SK を分割 gzip JSONL
として書き出しておくことで、Cleanup を Scan せずに実行できるようにする。

フォーマット
-----------
- Prefix: 呼び出し側が指定 (通常 `generations/<run_id>/inventory/`)
- ファイル名: `chunk-NNNNNN.jsonl.gz` (0 埋め 6 桁の連番でソート可能)
- 1 行 = `{"PK": "...", "SK": "..."}` の JSON
- 空 chunk は生成しない (0 件のときは MANIFEST のみ)
- MANIFEST: `<prefix>MANIFEST.json` に総件数と chunk 数を記録し、
  Cleanup 側が「全 chunk 揃った状態」を判定できるようにする
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
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """PK/SK ペアのイテレータを gzip 分割 JSONL として S3 へ書き出す。

    Returns
    -------
    Manifest の内容を dict で返す (呼び出し側が catalog / logger に載せられる)。
    """

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not prefix.endswith("/"):
        raise ValueError(f"inventory prefix must end with '/': {prefix!r}")

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
        "schema_version": 1,
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


def read_inventory_keys(bucket: str, prefix: str) -> Iterator[tuple[str, str]]:
    """MANIFEST 経由でインベントリを列挙して (PK, SK) を yield する。

    MANIFEST が chunk 一覧の唯一の真とする (S3 ListObjects の結果整合性を避ける)。
    chunk が MANIFEST に列挙されていて S3 に無い場合は明示的に例外化する。
    """

    if not prefix.endswith("/"):
        raise ValueError(f"inventory prefix must end with '/': {prefix!r}")

    client = s3_client()
    manifest_key = f"{prefix}{MANIFEST_FILENAME}"
    manifest_response = client.get_object(Bucket=bucket, Key=manifest_key)
    manifest = json.loads(manifest_response["Body"].read())

    for chunk_info in manifest.get("chunks", []):
        chunk_key = chunk_info["key"]
        chunk_response = client.get_object(Bucket=bucket, Key=chunk_key)
        raw = chunk_response["Body"].read()
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as gz:
            for line in gz:
                record = json.loads(line)
                yield record["PK"], record["SK"]
