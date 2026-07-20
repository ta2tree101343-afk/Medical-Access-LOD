"""読み取りモデルの旧世代を非同期で GC する Lambda。

トリガ
------
- SFN の Publish 完了後に SendMessage された SQS メッセージ
- EventBridge の補助スケジュール (SQS が best-effort になった場合の保険)

処理フロー
---------
1. 公開 manifest (`latest/manifest.json`) から現行世代の run_id を得る
2. catalog を list (COMMITTED / DELETING) で列挙
3. retention policy で削除計画を決める (`plan_deletions`)
4. 各対象世代について:
   a. `mark_deleting` で COMMITTED → DELETING に遷移
   b. inventory (S3) を辿って PK/SK を BatchWriteItem で削除
   c. UnprocessedItems を指数バックオフで再試行
   d. `mark_deleted` で DELETING → DELETED に遷移

権限
----
Cleanup Lambda は SYSTEM#PIPELINE (lock) に触れない。Publish の最小権限を
維持したままで、Cleanup 側は SYSTEM#GENERATION (catalog) と GENERATION#*
(実データ) だけを操作する (IAM は pipeline-stack.ts で分離)。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import boto3
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError

from medical_access_lod.functions.shared import (
    generation_catalog,
    generation_inventory,
    generation_retention,
)
from medical_access_lod.functions.shared.events import CleanupEvent
from medical_access_lod.functions.shared.observability import logger, metrics, tracer
from medical_access_lod.functions.shared.s3io import s3_client

# DynamoDB BatchWriteItem は最大 25 件/リクエスト。安全側で 25 未満に留める。
_BATCH_WRITE_MAX = 25
_MAX_RETRIES = 8
_INITIAL_BACKOFF_SECONDS = 0.5


def _load_retention_policy() -> generation_retention.RetentionPolicy:
    """環境変数から retention policy を組み立てる (未指定時は default)。

    テスト時に細かく制御するため env 経由にする。本番では CDK が default 値の
    まま (もしくは stack ごとに固定値を注入する) を想定。
    """
    keep_last_n = int(
        os.environ.get("RETENTION_KEEP_LAST_N")
        or generation_retention.DEFAULT_KEEP_LAST_N
    )
    min_age_days = int(
        os.environ.get("RETENTION_MIN_AGE_DAYS")
        or str(generation_retention.DEFAULT_MIN_AGE_DAYS)
    )
    return generation_retention.RetentionPolicy(
        keep_last_n=keep_last_n,
        min_age_days=min_age_days,
    )


@tracer.capture_method
def _read_active_run_id(dist_bucket: str) -> str | None:
    """公開 manifest から現行世代の run_id を取得する。"""
    try:
        response = s3_client().get_object(Bucket=dist_bucket, Key="latest/manifest.json")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None
        raise
    try:
        manifest = json.loads(response["Body"].read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("published manifest is not valid JSON; treating as absent")
        return None
    active = manifest.get("run_id") if isinstance(manifest, dict) else None
    return active if isinstance(active, str) and active else None


def _chunks(items: list[dict[str, str]], size: int) -> list[list[dict[str, str]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


@tracer.capture_method
def _batch_delete(
    table_name: str,
    keys: list[tuple[str, str]],
    *,
    sleep: Any = time.sleep,
) -> int:
    """PK/SK ペアを BatchWriteItem で削除する。UnprocessedItems は指数バックオフ再試行。

    戻り値は削除完了した件数 (再試行含む)。全リトライで残った UnprocessedItems は
    RuntimeError にする (呼び出し側で SQS 再配信/DLQ に任せる)。
    """
    if not keys:
        return 0

    client = boto3.client("dynamodb")
    deleted = 0
    for chunk in _chunks(
        [{"PK": pk, "SK": sk} for pk, sk in keys],
        _BATCH_WRITE_MAX,
    ):
        request_items: dict[str, list[dict[str, Any]]] = {
            table_name: [
                {
                    "DeleteRequest": {
                        "Key": {"PK": {"S": item["PK"]}, "SK": {"S": item["SK"]}},
                    }
                }
                for item in chunk
            ]
        }
        backoff = _INITIAL_BACKOFF_SECONDS
        for _attempt in range(_MAX_RETRIES):
            response = client.batch_write_item(RequestItems=request_items)
            unprocessed = response.get("UnprocessedItems", {}).get(table_name, [])
            deleted += len(request_items[table_name]) - len(unprocessed)
            if not unprocessed:
                break
            request_items = {table_name: unprocessed}
            sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        else:
            remaining = len(request_items.get(table_name, []))
            raise RuntimeError(
                f"batch_write_item failed to drain UnprocessedItems after "
                f"{_MAX_RETRIES} retries ({remaining} keys left)"
            )
    return deleted


@tracer.capture_method
def _delete_generation(
    request: CleanupEvent,
    entry: dict[str, Any],
    *,
    sleep: Any = time.sleep,
) -> int:
    run_id = entry["run_id"]
    inventory_prefix = entry.get("inventory_prefix")
    if not isinstance(inventory_prefix, str) or not inventory_prefix:
        raise ValueError(
            f"generation {run_id!r} has no inventory_prefix; cannot delete safely"
        )

    generation_catalog.mark_deleting(request.read_model_table, run_id)
    keys = list(
        generation_inventory.read_inventory_keys(request.inventory_bucket, inventory_prefix)
    )
    deleted = _batch_delete(request.read_model_table, keys, sleep=sleep)
    generation_catalog.mark_deleted(request.read_model_table, run_id)
    logger.info(
        "generation cleanup completed",
        extra={"run_id": run_id, "deleted_items": deleted},
    )
    return deleted


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics(capture_cold_start_metric=True)
@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    # SQS 経由 (Records) と直接 invoke (payload そのまま) の両方をサポートする。
    if isinstance(event.get("Records"), list) and event["Records"]:
        first_record = event["Records"][0]
        body = first_record.get("body")
        payload = json.loads(body) if isinstance(body, str) else body
    else:
        payload = event
    request = CleanupEvent.model_validate(payload)
    logger.append_keys(trigger_run_id=request.trigger_run_id)

    active_run_id = _read_active_run_id(request.dist_bucket)
    committed = generation_catalog.list_by_status(
        request.read_model_table, generation_catalog.GenerationStatus.COMMITTED
    )
    deleting = generation_catalog.list_by_status(
        request.read_model_table, generation_catalog.GenerationStatus.DELETING
    )
    plan = generation_retention.plan_deletions(
        committed + deleting,
        active_run_id=active_run_id,
        policy=_load_retention_policy(),
    )
    logger.info(
        "cleanup plan",
        extra={
            "active_run_id": active_run_id,
            "to_delete": plan.to_delete,
            "keep_reasons": plan.keep_reasons,
        },
    )

    entries_by_run: dict[str, dict[str, Any]] = {
        e["run_id"]: e for e in (committed + deleting) if isinstance(e.get("run_id"), str)
    }

    total_deleted_generations = 0
    total_deleted_items = 0
    for run_id in plan.to_delete:
        entry = entries_by_run.get(run_id)
        if entry is None:
            logger.warning("skipping unknown run_id in plan", extra={"run_id": run_id})
            continue
        deleted = _delete_generation(request, entry)
        total_deleted_items += deleted
        total_deleted_generations += 1

    metrics.add_metric(name="DeletedGenerations", unit="Count", value=total_deleted_generations)
    metrics.add_metric(name="DeletedItems", unit="Count", value=total_deleted_items)

    return {
        "trigger_run_id": request.trigger_run_id,
        "active_run_id": active_run_id,
        "deleted_generations": total_deleted_generations,
        "deleted_items": total_deleted_items,
        "deleted_run_ids": plan.to_delete,
    }
