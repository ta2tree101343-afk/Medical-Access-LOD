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
   b. inventory MANIFEST を読み、run_id / prefix / item_count を catalog エントリと
      照合する (別世代の inventory を読んで現行データを消さないための多層防御)
   c. `deletion_cursor` (前回の中断位置) から chunk 単位で処理し、各 chunk 完了時に
      cursor を DynamoDB に保存する
   d. Lambda 残時間が閾値を下回ったら早期終了 (SQS 再配信で次の invoke が続きを行う)
   e. 全 chunk 完了時にのみ `mark_deleted` で DELETING → DELETED に遷移

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
from decimal import Decimal
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
# Lambda 残時間がこの閾値を下回ったら chunk 途中でも早期終了する。
# BatchWriteItem 1 リクエスト (最悪ケースでリトライ 8 回 * 30 秒) の余裕を確保。
_MIN_REMAINING_MILLIS = 30_000
# get_remaining_time_in_millis が使えない環境 (テスト等) では時間制約なしとみなす。
_INFINITE_REMAINING_MILLIS = 10**9


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


def _remaining_millis(context: Any) -> int:
    """Lambda 残時間 (ms) を返す。取得できない環境 (テスト等) では制約なしを返す。"""
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(getter):
        return _INFINITE_REMAINING_MILLIS
    try:
        return int(getter())
    except Exception:
        return _INFINITE_REMAINING_MILLIS


def _validate_inventory_matches_catalog(
    manifest: dict[str, Any],
    entry: dict[str, Any],
) -> None:
    """catalog エントリの item_count と inventory の item_count が一致することを確認する。

    ここで弾けば、inventory が空 (0 chunk) なのに catalog に item_count > 0 の
    残骸を残したまま DELETED tombstone を付けてしまう事故を防げる。
    """
    catalog_count = entry.get("item_count")
    inventory_count = manifest.get("item_count")
    # DynamoDB は数値属性を Decimal で返すため int キャストで正規化する。
    # DynamoDB は数値属性を Decimal で返すため int/float/Decimal を受け付ける。
    if not isinstance(catalog_count, (int, float, Decimal)) or isinstance(catalog_count, bool):
        raise generation_inventory.InventoryValidationError(
            f"catalog entry missing valid item_count: run_id={entry.get('run_id')!r}"
        )
    if not isinstance(inventory_count, (int, float, Decimal)) or isinstance(inventory_count, bool):
        raise generation_inventory.InventoryValidationError(
            f"inventory manifest missing valid item_count for "
            f"run_id={entry.get('run_id')!r}"
        )
    if int(catalog_count) != int(inventory_count):
        raise generation_inventory.InventoryValidationError(
            f"inventory item_count ({int(inventory_count)}) does not match catalog "
            f"item_count ({int(catalog_count)}) for run_id={entry.get('run_id')!r}"
        )


@tracer.capture_method
def _delete_generation(
    request: CleanupEvent,
    entry: dict[str, Any],
    context: Any,
    *,
    sleep: Any = time.sleep,
) -> tuple[int, bool]:
    """世代を chunk 単位で削除する。

    Returns
    -------
    (deleted_items, completed):
      - deleted_items: 本 invoke で削除した件数
      - completed: 全 chunk を消化して mark_deleted まで到達したか
        False の場合は SQS 再配信で続きを行う (catalog の deletion_cursor で再開)
    """
    run_id = entry["run_id"]
    inventory_prefix = entry.get("inventory_prefix")
    if not isinstance(inventory_prefix, str) or not inventory_prefix:
        raise ValueError(
            f"generation {run_id!r} has no inventory_prefix; cannot delete safely"
        )

    # mark_deleting は冪等。DELETING で再入しても失敗しない。
    generation_catalog.mark_deleting(request.read_model_table, run_id)
    # mark_deleting 後の最新エントリを取り直す (deletion_cursor / item_count 更新反映)
    fresh_entry = generation_catalog.get(request.read_model_table, run_id) or entry

    manifest = generation_inventory.read_inventory_manifest(
        request.inventory_bucket,
        inventory_prefix,
        expected_run_id=run_id,
    )
    _validate_inventory_matches_catalog(manifest, fresh_entry)

    chunks = manifest.get("chunks", [])
    total_chunks = len(chunks)
    cursor_raw = fresh_entry.get("deletion_cursor", 0)
    try:
        start_index = max(0, int(cursor_raw))
    except (TypeError, ValueError):
        start_index = 0

    deleted = 0
    for index in range(start_index, total_chunks):
        # BatchWriteItem 1 リクエスト分の余裕を確保。閾値割ったら早期終了。
        if _remaining_millis(context) < _MIN_REMAINING_MILLIS:
            logger.info(
                "cleanup exiting early due to low remaining time",
                extra={
                    "run_id": run_id,
                    "chunk_index": index,
                    "total_chunks": total_chunks,
                },
            )
            return deleted, False

        chunk_info = chunks[index]
        chunk_keys = generation_inventory.read_inventory_chunk(
            request.inventory_bucket,
            chunk_info["key"],
            expected_run_id=run_id,
        )
        deleted += _batch_delete(request.read_model_table, chunk_keys, sleep=sleep)
        # 次回再開位置を進める。DELETING でない場合は Conflict になる (別 invoke が
        # 完了させて DELETED 済み、など) が、その場合は次の catalog list で既に
        # DELETED として観測されるので黙って抜ける。
        try:
            generation_catalog.update_deletion_cursor(
                request.read_model_table, run_id, index + 1
            )
        except generation_catalog.GenerationCatalogConflictError:
            logger.info(
                "cleanup cursor update raced with another invoke; stopping",
                extra={"run_id": run_id, "chunk_index": index},
            )
            return deleted, False

    generation_catalog.mark_deleted(request.read_model_table, run_id)
    logger.info(
        "generation cleanup completed",
        extra={"run_id": run_id, "deleted_items": deleted, "chunks": total_chunks},
    )
    return deleted, True


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
    incomplete_run_ids: list[str] = []
    for run_id in plan.to_delete:
        entry = entries_by_run.get(run_id)
        if entry is None:
            logger.warning("skipping unknown run_id in plan", extra={"run_id": run_id})
            continue
        # Lambda 残時間が閾値を下回ったら、この世代自体を試みずに終了する
        # (次の SQS 配信で続きを行う)。1 世代でも中断が発生したら残りの run_id も
        # 同様に持ち越す。
        if _remaining_millis(context) < _MIN_REMAINING_MILLIS:
            incomplete_run_ids.append(run_id)
            continue
        deleted, completed = _delete_generation(request, entry, context)
        total_deleted_items += deleted
        if completed:
            total_deleted_generations += 1
        else:
            incomplete_run_ids.append(run_id)

    metrics.add_metric(name="DeletedGenerations", unit="Count", value=total_deleted_generations)
    metrics.add_metric(name="DeletedItems", unit="Count", value=total_deleted_items)
    metrics.add_metric(
        name="IncompleteGenerations", unit="Count", value=len(incomplete_run_ids)
    )

    completed_run_ids = [
        run_id for run_id in plan.to_delete if run_id not in incomplete_run_ids
    ]
    return {
        "trigger_run_id": request.trigger_run_id,
        "active_run_id": active_run_id,
        "deleted_generations": total_deleted_generations,
        "deleted_items": total_deleted_items,
        # 完了世代 (mark_deleted 到達) と、途中で中断された世代 (次の SQS 配信で
        # 続きから再開) を分けて返す。plan.to_delete は今回の対象全体。
        "deleted_run_ids": completed_run_ids,
        "incomplete_run_ids": incomplete_run_ids,
    }
