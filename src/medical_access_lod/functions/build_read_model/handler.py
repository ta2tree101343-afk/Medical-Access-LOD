from __future__ import annotations

from typing import Any

import boto3
from aws_lambda_powertools.utilities.typing import LambdaContext

from medical_access_lod.functions.shared.events import BuildReadModelEvent
from medical_access_lod.functions.shared.observability import logger, metrics, tracer
from medical_access_lod.functions.shared.pipeline_lock import (
    acquire_pipeline_lock,
    release_pipeline_lock,
)
from medical_access_lod.functions.shared.s3io import get_json

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
        written = _write_items(request.read_model_table, items)
    except Exception:
        try:
            release_pipeline_lock(request.read_model_table, request.run_id)
        except Exception:
            logger.exception("failed to release pipeline lock after read model failure")
        raise

    metrics.add_metric(name="ReadModelItems", unit="Count", value=written)
    logger.info(
        "build_read_model completed",
        extra={"items": written, "lock_expires_at": lock_expires_at},
    )

    return {
        "run_id": request.run_id,
        "read_model_table": request.read_model_table,
        "items_written": written,
        "lock_owner": request.run_id,
        "lock_expires_at": lock_expires_at,
    }
