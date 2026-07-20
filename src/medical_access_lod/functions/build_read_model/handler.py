from __future__ import annotations

from typing import Any

import boto3
from aws_lambda_powertools.utilities.typing import LambdaContext

from medical_access_lod.functions.shared import generation_catalog, generation_inventory
from medical_access_lod.functions.shared.events import BuildReadModelEvent
from medical_access_lod.functions.shared.observability import logger, metrics, tracer
from medical_access_lod.functions.shared.pipeline_lock import (
    acquire_pipeline_lock,
    release_pipeline_lock,
)
from medical_access_lod.functions.shared.s3io import get_json


def _inventory_prefix(run_id: str) -> str:
    """Cleanup Lambda が読む世代 inventory の S3 prefix。

    catalog エントリと実 S3 の間で prefix 命名を推測させないよう、
    唯一の真としてここで決める。
    """

    return f"generations/{run_id}/inventory/"

# 各アイテムに付与する "generation" 属性のキー。値は現在の run_id。
# API は公開 manifest が指す generation のみを読む。旧世代を残すことで、
# 更新途中や Publish 失敗時にも現在公開中の世代を継続して参照できる。
_GENERATION_ATTR = "generation"


@tracer.capture_method
def _write_items(table_name: str, items: list[dict[str, Any]]) -> int:
    table = boto3.resource("dynamodb").Table(table_name)
    with table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for item in items:
            batch.put_item(Item=item)
    return len(items)


def _build_items(payload: dict[str, Any], generation: str) -> list[dict[str, Any]]:
    facilities = {f["facility_id"]: f for f in payload["facilities"]}
    labels = payload.get("specialty_labels", {})
    items: list[dict[str, Any]] = []

    for f in payload["facilities"]:
        pk = f"GENERATION#{generation}#FACILITY#{f['facility_id']}"
        items.append(
            {
                "PK": pk,
                "SK": "METADATA",
                _GENERATION_ATTR: generation,
                "facility_id": f["facility_id"],
                "name": f["name"],
                "facility_type": f["facility_type"],
                "prefecture": f["address"]["prefecture"],
                "city": f["address"]["city"],
                "street_address": f["address"]["street_address"],
                "GSI1PK": f"GENERATION#{generation}#CITY#{f['address']['city']}",
            }
        )

    for svc in payload["services"]:
        fid = svc["facility_id"]
        code = svc["specialty_code"]
        label = labels.get(code, code)
        facility = facilities.get(fid, {})
        city = facility.get("address", {}).get("city", "")
        items.append(
            {
                "PK": f"GENERATION#{generation}#FACILITY#{fid}",
                "SK": f"SERVICE#{code}",
                _GENERATION_ATTR: generation,
                "specialty_code": code,
                "specialty_label": label,
                "GSI1PK": f"GENERATION#{generation}#CITY#{city}",
                "GSI1SK": f"SPECIALTY#{code}#FACILITY#{fid}",
            }
        )

    for sched in payload["schedules"]:
        fid = sched["facility_id"]
        code = sched["specialty_code"]
        day = sched["day_of_week"]
        opens = sched["opens"]
        closes = sched["closes"]
        items.append(
            {
                "PK": f"GENERATION#{generation}#FACILITY#{fid}",
                "SK": f"SCHEDULE#{code}#{day}#{opens}",
                _GENERATION_ATTR: generation,
                "specialty_code": code,
                "day_of_week": day,
                "opens": opens,
                "closes": closes,
                "GSI2PK": f"GENERATION#{generation}#SPECIALTY#{code}#DAY#{day}",
                "GSI2SK": f"OPEN#{opens}#FACILITY#{fid}",
            }
        )

    return items


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics(capture_cold_start_metric=True)
@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    request = BuildReadModelEvent.model_validate(event)
    logger.append_keys(run_id=request.run_id)

    lock_expires_at = acquire_pipeline_lock(request.read_model_table, request.run_id)
    try:
        payload = get_json(request.normalized_bucket, request.normalized_key)
        items = _build_items(payload, generation=request.run_id)
        inventory_prefix = _inventory_prefix(request.run_id)
        # 書き込みより前に STAGED を登録することで、部分書き込みで落ちた世代も
        # catalog 上で「未完了」として観測できる (COMMITTED に上がらない)。
        generation_catalog.register_staged(
            request.read_model_table,
            request.run_id,
            snapshot_date=request.snapshot_date,
            inventory_prefix=inventory_prefix,
            item_count=len(items),
        )
        written = _write_items(request.read_model_table, items)
        # items 書き込み成功後に inventory (PK/SK 一覧) を S3 に永続化する。
        # ここで失敗した場合、DynamoDB には世代データがあるが Cleanup 対象と
        # ならないので Publish (mark_committed) は走らせない。
        # (register_staged で STAGED のままとなり、Cleanup の retention 判定は
        # COMMITTED のみを対象にするため実害無し)
        inventory_manifest = generation_inventory.write_inventory(
            request.build_bucket,
            inventory_prefix,
            ((item["PK"], item["SK"]) for item in items),
        )
    except Exception:
        try:
            release_pipeline_lock(request.read_model_table, request.run_id)
        except Exception:
            logger.exception("failed to release pipeline lock after read model failure")
        raise

    metrics.add_metric(name="ReadModelItems", unit="Count", value=written)
    metrics.add_metric(
        name="InventoryChunks",
        unit="Count",
        value=int(inventory_manifest["chunk_count"]),
    )
    logger.info(
        "build_read_model completed",
        extra={
            "items": written,
            "lock_expires_at": lock_expires_at,
            "inventory_prefix": inventory_prefix,
            "inventory_bucket": request.build_bucket,
            "inventory_chunks": inventory_manifest["chunk_count"],
        },
    )

    return {
        "run_id": request.run_id,
        "read_model_table": request.read_model_table,
        "items_written": written,
        "inventory_bucket": request.build_bucket,
        "inventory_prefix": inventory_prefix,
        "inventory_chunks": inventory_manifest["chunk_count"],
        "lock_owner": request.run_id,
        "lock_expires_at": lock_expires_at,
    }
