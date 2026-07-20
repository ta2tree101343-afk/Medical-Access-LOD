from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError

from medical_access_lod.functions.shared import generation_catalog, pipeline_lock
from medical_access_lod.functions.shared.events import PublishEvent
from medical_access_lod.functions.shared.observability import logger, metrics, tracer
from medical_access_lod.functions.shared.s3io import s3_client

MANIFEST_KEY = "latest/manifest.json"
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
MANIFEST_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"


class ManifestCommitConflictError(RuntimeError):
    """manifest が読み取り後に別の公開処理によって更新された。"""


@tracer.capture_method
def _copy(
    src_bucket: str,
    src_key: str,
    dst_bucket: str,
    dst_key: str,
    content_type: str,
) -> None:
    s3_client().copy_object(
        CopySource={"Bucket": src_bucket, "Key": src_key},
        Bucket=dst_bucket,
        Key=dst_key,
        ContentType=content_type,
        CacheControl=IMMUTABLE_CACHE_CONTROL,
        MetadataDirective="REPLACE",
    )


@tracer.capture_method
def _head_artifact(bucket: str, key: str) -> dict[str, Any]:
    response = s3_client().head_object(Bucket=bucket, Key=key)
    return {
        "key": key,
        "size": response["ContentLength"],
        "etag": response["ETag"].strip('"'),
        "content_type": response["ContentType"],
    }


@tracer.capture_method
def _put_manifest(
    bucket: str,
    manifest: dict[str, Any],
    expected_etag: str | None,
) -> None:
    body = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    request: dict[str, Any] = {
        "Bucket": bucket,
        "Key": MANIFEST_KEY,
        "Body": body,
        "ContentType": "application/json",
        "CacheControl": MANIFEST_CACHE_CONTROL,
    }
    if expected_etag is None:
        request["IfNoneMatch"] = "*"
    else:
        request["IfMatch"] = expected_etag

    try:
        s3_client().put_object(**request)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if error.get("Code") in {"PreconditionFailed", "ConditionalRequestConflict"} or status in {
            409,
            412,
        }:
            raise ManifestCommitConflictError(
                "latest manifest changed before the conditional commit"
            ) from exc
        raise


def _read_manifest_state(bucket: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        response = s3_client().get_object(Bucket=bucket, Key=MANIFEST_KEY)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None, None
        raise

    etag = response["ETag"]
    try:
        manifest = json.loads(response["Body"].read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("published manifest is not valid JSON")
        return None, etag
    return (manifest if isinstance(manifest, dict) else None), etag


def _artifact_descriptor_is_complete(descriptor: dict[str, Any]) -> bool:
    size = descriptor.get("size")
    return (
        isinstance(descriptor.get("key"), str)
        and bool(descriptor["key"])
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size >= 0
        and isinstance(descriptor.get("etag"), str)
        and bool(descriptor["etag"])
        and isinstance(descriptor.get("content_type"), str)
        and bool(descriptor["content_type"])
    )


def _manifest_is_complete(
    manifest: dict[str, Any],
    request: PublishEvent,
    artifacts: dict[str, dict[str, str]],
) -> bool:
    if (
        manifest.get("schema_version") != 1
        or manifest.get("run_id") != request.run_id
        or manifest.get("snapshot_date") != request.snapshot_date
    ):
        return False

    manifest_artifacts = manifest.get("artifacts")
    if not isinstance(manifest_artifacts, dict):
        return False
    for name, expected in artifacts.items():
        descriptor = manifest_artifacts.get(name)
        if not isinstance(descriptor, dict):
            return False
        if (
            not _artifact_descriptor_is_complete(descriptor)
            or descriptor.get("key") != expected["key"]
            or descriptor.get("content_type") != expected["content_type"]
        ):
            return False
    return True


def _manifest_matches_staged(
    published: dict[str, Any],
    staged: dict[str, Any],
) -> bool:
    """CAS競合時、同じrunの同じ成果物が既にcommit済みかを厳密に確認する。"""
    if any(
        published.get(field) != staged.get(field)
        for field in ("schema_version", "run_id", "snapshot_date")
    ):
        return False
    published_artifacts = published.get("artifacts")
    staged_artifacts = staged.get("artifacts")
    if not isinstance(published_artifacts, dict) or not isinstance(staged_artifacts, dict):
        return False
    for name in ("turtle", "jsonld"):
        published_descriptor = published_artifacts.get(name)
        staged_descriptor = staged_artifacts.get(name)
        if (
            not isinstance(published_descriptor, dict)
            or not isinstance(staged_descriptor, dict)
            or not _artifact_descriptor_is_complete(published_descriptor)
            or not _artifact_descriptor_is_complete(staged_descriptor)
            or published_descriptor != staged_descriptor
        ):
            return False
    return True


def _commit_manifest(
    bucket: str,
    manifest: dict[str, Any],
    expected_etag: str | None,
) -> tuple[dict[str, Any], bool]:
    try:
        _put_manifest(bucket, manifest, expected_etag)
    except ManifestCommitConflictError as exc:
        published, _ = _read_manifest_state(bucket)
        if published is not None and _manifest_matches_staged(published, manifest):
            return published, False
        raise ManifestCommitConflictError(
            "latest manifest was committed by a different pipeline execution"
        ) from exc
    return manifest, True


def _read_manifest_if_exists(bucket: str) -> dict[str, Any] | None:
    manifest, _ = _read_manifest_state(bucket)
    return manifest


def _renew_or_get_completed_manifest(
    request: PublishEvent,
    artifacts: dict[str, dict[str, str]],
) -> dict[str, Any] | None:
    """lease を延長する。lock 不在の完了済み再試行だけは成功として扱う。"""
    try:
        pipeline_lock.renew(request.read_model_table, request.lock_owner)
    except pipeline_lock.PipelineLockMissingError:
        manifest = _read_manifest_if_exists(request.dist_bucket)
        if manifest is not None and _manifest_is_complete(manifest, request, artifacts):
            return manifest
        raise
    return None


def _response(request: PublishEvent, manifest: dict[str, Any]) -> dict[str, Any]:
    manifest_artifacts = manifest.get("artifacts")
    published: list[str] = []
    if isinstance(manifest_artifacts, dict):
        for name in ("turtle", "jsonld"):
            artifact = manifest_artifacts.get(name)
            if isinstance(artifact, dict) and isinstance(artifact.get("key"), str):
                published.append(artifact["key"])
    published.append(MANIFEST_KEY)
    return {
        "run_id": request.run_id,
        "dist_bucket": request.dist_bucket,
        "published_files": published,
        "manifest_key": MANIFEST_KEY,
    }


def _release_lock(request: PublishEvent) -> None:
    """所有中のロックだけを解放し、公開結果や元の例外を上書きしない。"""
    try:
        released = pipeline_lock.release(request.read_model_table, request.lock_owner)
        if released is False:
            logger.warning(
                "pipeline lock was not released because ownership changed",
                extra={"lock_owner": request.lock_owner},
            )
    except Exception:
        logger.exception(
            "failed to release pipeline lock",
            extra={"lock_owner": request.lock_owner},
        )


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics(capture_cold_start_metric=True)
@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    request = PublishEvent.model_validate(event)
    logger.append_keys(run_id=request.run_id)

    try:
        # 成果物は run_id ごとの不変 prefix に配置する。同一日付の再実行も
        # 別 prefix になるため、過去に公開したリリースを上書きしない。
        release_prefix = (
            f"releases/{request.snapshot_date}/{quote(request.run_id, safe='-_.')}"
        )
        artifacts = {
            "turtle": {
                "source_key": request.ttl_key,
                "key": f"{release_prefix}/{request.ttl_key.rsplit('/', 1)[-1]}",
                "content_type": "text/turtle; charset=utf-8",
            },
            "jsonld": {
                "source_key": request.jsonld_key,
                "key": f"{release_prefix}/{request.jsonld_key.rsplit('/', 1)[-1]}",
                "content_type": "application/ld+json",
            },
        }

        # Lambda応答消失後などの同一run再試行は、既にcommit済みなら何も書かず成功する。
        # 別runがlockを保持している場合やlease切れの場合はrenewが例外になり、旧実行は停止する。
        completed_manifest = _renew_or_get_completed_manifest(request, artifacts)
        if completed_manifest is not None:
            logger.info("publish already completed; returning idempotently")
            return _response(request, completed_manifest)
        _, expected_manifest_etag = _read_manifest_state(request.dist_bucket)

        for artifact in artifacts.values():
            _copy(
                request.build_bucket,
                artifact["source_key"],
                request.dist_bucket,
                artifact["key"],
                artifact["content_type"],
            )

        # 2 形式が揃っていることを確認してから、単一オブジェクトの manifest を
        # 最後に更新する。この PutObject だけが公開世代の commit point になる。
        manifest_artifacts = {
            name: _head_artifact(request.dist_bucket, artifact["key"])
            for name, artifact in artifacts.items()
        }
        manifest = {
            "schema_version": 1,
            "run_id": request.run_id,
            "snapshot_date": request.snapshot_date,
            "artifacts": manifest_artifacts,
        }

        # コピー中にleaseが切れたり後続runへ所有権が移ったりしていないことを
        # commit直前に再確認する。失敗時は既存manifestを一切変更しない。
        completed_manifest = _renew_or_get_completed_manifest(request, artifacts)
        if completed_manifest is not None:
            logger.info("publish completed by another retry; skipping manifest update")
            return _response(request, completed_manifest)
        committed_manifest, committed = _commit_manifest(
            request.dist_bucket,
            manifest,
            expected_manifest_etag,
        )
        if committed:
            metrics.add_metric(name="PipelineSuccess", unit="Count", value=1)
            logger.info(
                "publish completed",
                extra={
                    "published": [artifact["key"] for artifact in artifacts.values()],
                    "manifest_key": MANIFEST_KEY,
                },
            )
        else:
            logger.info("publish completed by another retry during manifest commit")

        # Manifest commit が成功した後にのみ generation catalog を COMMITTED へ
        # 遷移させる。以降 Cleanup Lambda はこの世代を "公開済み" として扱える。
        # STAGED から呼ばれても COMMITTED から呼ばれても冪等成功 (再試行対応)。
        try:
            generation_catalog.mark_committed(
                request.read_model_table,
                request.run_id,
            )
        except generation_catalog.GenerationCatalogMissingError:
            # 移行期の manifest (旧 pipeline が catalog を書かずに publish した
            # 場合など) を retry で通したケース。commit 自体は成功しているので
            # ここで失敗させない。
            logger.warning(
                "generation catalog entry missing during mark_committed; "
                "publish succeeded but Cleanup will skip this generation"
            )
        return _response(request, committed_manifest)
    finally:
        _release_lock(request)
