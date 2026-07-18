from __future__ import annotations

import os
from typing import Any

import boto3
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.typing import LambdaContext
from boto3.dynamodb.conditions import Key

from medical_access_lod.domain.values.medical_specialty import resolve_specialty
from medical_access_lod.functions.shared.observability import logger, metrics, tracer

app = APIGatewayHttpResolver()

TABLE_NAME = os.environ.get("READ_MODEL_TABLE", "")


def _table() -> Any:
    return boto3.resource("dynamodb").Table(TABLE_NAME)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "table": TABLE_NAME}


@app.get("/metadata")
def metadata() -> dict[str, Any]:
    return {
        "source": "厚生労働省 医療情報ネット",
        "license": "PDL 1.0",
        "coverage": {
            "prefecture": "千葉県",
            "city": "千葉市",
            "wards": ["中央区", "花見川区", "稲毛区", "若葉区", "緑区", "美浜区"],
        },
    }


@app.get("/specialties")
def specialties() -> dict[str, Any]:
    return {
        "note": "See /concept/specialty in the RDF for full SKOS scheme",
    }


@app.get("/facilities")
@tracer.capture_method
def list_facilities() -> dict[str, Any]:
    city = app.current_event.get_query_string_value(name="city", default_value=None)
    specialty_raw = app.current_event.get_query_string_value(name="specialty", default_value=None)

    if not city or not specialty_raw:
        return {"items": [], "count": 0, "hint": "specify ?city= and ?specialty= (code or label)"}

    try:
        specialty = str(resolve_specialty(specialty_raw))
    except ValueError:
        specialty = specialty_raw

    response = _table().query(
        IndexName="GSI1_CityBySpecialty",
        KeyConditionExpression=Key("GSI1PK").eq(f"CITY#{city}")
        & Key("GSI1SK").begins_with(f"SPECIALTY#{specialty}#"),
    )
    items = response.get("Items", [])
    return {"items": items, "count": len(items)}


@app.get("/facilities/<facility_id>")
def get_facility(facility_id: str) -> dict[str, Any]:
    response = _table().query(KeyConditionExpression=Key("PK").eq(f"FACILITY#{facility_id}"))
    items = response.get("Items", [])
    if not items:
        return {"facility_id": facility_id, "found": False}
    metadata_row = next((i for i in items if i["SK"] == "METADATA"), None)
    return {
        "facility_id": facility_id,
        "found": True,
        "metadata": metadata_row,
        "services": [i for i in items if i["SK"].startswith("SERVICE#")],
        "schedules": [i for i in items if i["SK"].startswith("SCHEDULE#")],
    }


@logger.inject_lambda_context(
    correlation_id_path=correlation_paths.API_GATEWAY_HTTP,
    clear_state=True,
)
@metrics.log_metrics(capture_cold_start_metric=True)
@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
