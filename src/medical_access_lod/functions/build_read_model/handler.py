from __future__ import annotations

from typing import Any

import boto3
from aws_lambda_powertools.utilities.typing import LambdaContext

from medical_access_lod.functions.shared.events import BuildReadModelEvent
from medical_access_lod.functions.shared.observability import logger, metrics, tracer
from medical_access_lod.functions.shared.s3io import get_json


@tracer.capture_method
def _write_items(table_name: str, items: list[dict[str, Any]]) -> int:
    table = boto3.resource("dynamodb").Table(table_name)
    with table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for item in items:
            batch.put_item(Item=item)
    return len(items)


def _build_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    facilities = {f["facility_id"]: f for f in payload["facilities"]}
    labels = payload.get("specialty_labels", {})
    items: list[dict[str, Any]] = []

    for f in payload["facilities"]:
        pk = f"FACILITY#{f['facility_id']}"
        items.append(
            {
                "PK": pk,
                "SK": "METADATA",
                "facility_id": f["facility_id"],
                "name": f["name"],
                "facility_type": f["facility_type"],
                "prefecture": f["address"]["prefecture"],
                "city": f["address"]["city"],
                "street_address": f["address"]["street_address"],
                "GSI1PK": f"CITY#{f['address']['city']}",
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
                "PK": f"FACILITY#{fid}",
                "SK": f"SERVICE#{code}",
                "specialty_code": code,
                "specialty_label": label,
                "GSI1PK": f"CITY#{city}",
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
                "PK": f"FACILITY#{fid}",
                "SK": f"SCHEDULE#{code}#{day}#{opens}",
                "specialty_code": code,
                "day_of_week": day,
                "opens": opens,
                "closes": closes,
                "GSI2PK": f"SPECIALTY#{code}#DAY#{day}",
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

    payload = get_json(request.normalized_bucket, request.normalized_key)
    items = _build_items(payload)
    written = _write_items(request.read_model_table, items)

    metrics.add_metric(name="ReadModelItems", unit="Count", value=written)
    logger.info("build_read_model completed", extra={"items": written})

    return {
        "run_id": request.run_id,
        "read_model_table": request.read_model_table,
        "items_written": written,
    }
