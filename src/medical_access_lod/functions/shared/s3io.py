from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3


@dataclass(frozen=True)
class S3Ref:
    bucket: str
    key: str

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


def s3_client() -> Any:
    return boto3.client("s3")


def download_prefix(bucket: str, prefix: str, dest_dir: Path) -> list[Path]:
    """S3 の prefix 配下を dest_dir へ全て取得する。"""
    client = s3_client()
    dest_dir.mkdir(parents=True, exist_ok=True)
    paginator = client.get_paginator("list_objects_v2")
    out: list[Path] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(prefix):].lstrip("/") if key.startswith(prefix) else key
            if not rel:
                continue
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            out.append(target)
    return out


def upload_file(path: Path, bucket: str, key: str, content_type: str | None = None) -> None:
    extra: dict[str, Any] = {}
    if content_type:
        extra["ContentType"] = content_type
    s3_client().upload_file(str(path), bucket, key, ExtraArgs=extra or None)


def put_json(obj: Any, bucket: str, key: str) -> None:
    body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    s3_client().put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


def get_json(bucket: str, key: str) -> Any:
    response = s3_client().get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))
