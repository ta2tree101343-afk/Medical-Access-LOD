"""generation_inventory の gzip JSONL 書き出しと round-trip テスト。"""
from __future__ import annotations

import gzip
import io
import json
from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from medical_access_lod.functions.shared import generation_inventory

BUCKET = "medical-access-lod-test-inventory"
REGION = "ap-northeast-1"


@pytest.fixture
def s3_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield BUCKET


def _list_keys(bucket: str, prefix: str) -> list[str]:
    s3 = boto3.client("s3", region_name=REGION)
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return sorted(obj["Key"] for obj in response.get("Contents", []))


def test_write_inventory_produces_manifest_and_single_chunk(s3_env: str) -> None:
    prefix = "generations/run-A/inventory/"
    keys = [("GENERATION#run-A#F1", "METADATA"), ("GENERATION#run-A#F1", "SERVICE#01")]
    manifest = generation_inventory.write_inventory(s3_env, prefix, iter(keys))

    assert manifest["item_count"] == 2
    assert manifest["chunk_count"] == 1
    written = _list_keys(s3_env, prefix)
    assert written == [
        f"{prefix}MANIFEST.json",
        f"{prefix}chunk-000000.jsonl.gz",
    ]

    round_tripped = list(generation_inventory.read_inventory_keys(s3_env, prefix))
    assert round_tripped == keys


def test_write_inventory_splits_across_chunks_by_chunk_size(s3_env: str) -> None:
    prefix = "generations/run-B/inventory/"
    keys = [(f"GENERATION#run-B#F{i}", "METADATA") for i in range(2500)]
    manifest = generation_inventory.write_inventory(s3_env, prefix, iter(keys), chunk_size=1000)

    assert manifest["item_count"] == 2500
    assert manifest["chunk_count"] == 3  # 1000 + 1000 + 500
    written = _list_keys(s3_env, prefix)
    assert written == [
        f"{prefix}MANIFEST.json",
        f"{prefix}chunk-000000.jsonl.gz",
        f"{prefix}chunk-000001.jsonl.gz",
        f"{prefix}chunk-000002.jsonl.gz",
    ]

    counts = [chunk["item_count"] for chunk in manifest["chunks"]]
    assert counts == [1000, 1000, 500]

    round_tripped = list(generation_inventory.read_inventory_keys(s3_env, prefix))
    assert round_tripped == keys


def test_write_inventory_with_zero_keys_writes_only_manifest(s3_env: str) -> None:
    prefix = "generations/run-empty/inventory/"
    manifest = generation_inventory.write_inventory(s3_env, prefix, iter([]))

    assert manifest["item_count"] == 0
    assert manifest["chunk_count"] == 0
    written = _list_keys(s3_env, prefix)
    assert written == [f"{prefix}MANIFEST.json"]

    round_tripped = list(generation_inventory.read_inventory_keys(s3_env, prefix))
    assert round_tripped == []


def test_write_inventory_chunk_files_are_valid_gzip_jsonl(s3_env: str) -> None:
    prefix = "generations/run-C/inventory/"
    keys = [
        ("GENERATION#run-C#F1", "METADATA"),
        ("GENERATION#run-C#F1", "SCHEDULE#01#Monday#09:00:00"),
    ]
    generation_inventory.write_inventory(s3_env, prefix, iter(keys))

    s3 = boto3.client("s3", region_name=REGION)
    chunk = s3.get_object(Bucket=s3_env, Key=f"{prefix}chunk-000000.jsonl.gz")
    assert chunk["ContentType"] == "application/x-ndjson"
    assert chunk["ContentEncoding"] == "gzip"

    with gzip.GzipFile(fileobj=io.BytesIO(chunk["Body"].read()), mode="rb") as gz:
        lines = gz.read().decode("utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert records == [
        {"PK": "GENERATION#run-C#F1", "SK": "METADATA"},
        {"PK": "GENERATION#run-C#F1", "SK": "SCHEDULE#01#Monday#09:00:00"},
    ]


def test_write_inventory_rejects_prefix_without_trailing_slash(s3_env: str) -> None:
    with pytest.raises(ValueError, match="must end with '/'"):
        generation_inventory.write_inventory(s3_env, "generations/x/inventory", iter([]))


def test_write_inventory_rejects_non_positive_chunk_size(s3_env: str) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        generation_inventory.write_inventory(s3_env, "generations/x/", iter([]), chunk_size=0)


def test_read_inventory_keys_rejects_prefix_without_trailing_slash(s3_env: str) -> None:
    with pytest.raises(ValueError, match="must end with '/'"):
        list(generation_inventory.read_inventory_keys(s3_env, "generations/x/inventory"))


def test_write_inventory_output_is_deterministic(s3_env: str) -> None:
    """同じ入力を 2 回書いても chunk のバイト列が同一になる (gzip mtime=0)."""
    prefix = "generations/run-det/inventory/"
    keys = [("GENERATION#run-det#F1", "METADATA")]
    generation_inventory.write_inventory(s3_env, prefix, iter(keys))

    s3 = boto3.client("s3", region_name=REGION)
    first = s3.get_object(Bucket=s3_env, Key=f"{prefix}chunk-000000.jsonl.gz")["Body"].read()

    generation_inventory.write_inventory(s3_env, prefix, iter(keys))
    second = s3.get_object(Bucket=s3_env, Key=f"{prefix}chunk-000000.jsonl.gz")["Body"].read()

    assert first == second
