from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import boto3
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.typing import LambdaContext
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from medical_access_lod.domain.values.medical_specialty import resolve_specialty
from medical_access_lod.functions.shared.observability import logger, metrics, tracer
from medical_access_lod.functions.shared.s3io import get_json

app = APIGatewayHttpResolver()


def _table_name() -> str:
    return os.environ.get("READ_MODEL_TABLE", "")


def _dist_bucket() -> str:
    return os.environ.get("DIST_BUCKET", "")


def _table() -> Any:
    return boto3.resource("dynamodb").Table(_table_name())


def _active_generation() -> str | None:
    """公開 manifest が指す読み取りモデル世代を取得する。

    manifest がまだ作成されていない移行期間だけは None を返し、従来キーを読む。
    manifest が存在するのに壊れている場合は、旧世代へ暗黙にフォールバックせず失敗する。
    """

    bucket = _dist_bucket()
    if not bucket:
        raise RuntimeError("DIST_BUCKET is not configured")
    try:
        manifest = get_json(bucket, "latest/manifest.json")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404"}:
            return None
        raise

    if not isinstance(manifest, dict):
        raise RuntimeError("latest/manifest.json must contain a JSON object")
    if manifest.get("schema_version") != 1:
        raise RuntimeError("latest/manifest.json has an unsupported schema_version")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise RuntimeError("latest/manifest.json is missing a non-empty run_id")
    snapshot_date = manifest.get("snapshot_date")
    if not isinstance(snapshot_date, str) or not snapshot_date.strip():
        raise RuntimeError("latest/manifest.json is missing a non-empty snapshot_date")
    release_prefix = f"releases/{snapshot_date}/{quote(run_id, safe='-_.')}/"
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RuntimeError("latest/manifest.json is missing artifacts")
    for name in ("turtle", "jsonld"):
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, dict):
            raise RuntimeError(f"latest/manifest.json is missing artifacts.{name}")
        key = descriptor.get("key")
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError(
                f"latest/manifest.json is missing a non-empty artifacts.{name}.key"
            )
        if not key.startswith(release_prefix):
            raise RuntimeError(
                f"latest/manifest.json artifacts.{name}.key is outside its release"
            )
        size = descriptor.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise RuntimeError(
                f"latest/manifest.json has an invalid artifacts.{name}.size"
            )
        for field in ("etag", "content_type"):
            value = descriptor.get(field)
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(
                    f"latest/manifest.json is missing artifacts.{name}.{field}"
                )
    return run_id


def _facility_pk(facility_id: str, generation: str | None) -> str:
    if generation is None:
        return f"FACILITY#{facility_id}"
    return f"GENERATION#{generation}#FACILITY#{facility_id}"


def _city_pk(city: str, generation: str | None) -> str:
    if generation is None:
        return f"CITY#{city}"
    return f"GENERATION#{generation}#CITY#{city}"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "table": _table_name()}


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

    generation = _active_generation()
    response = _table().query(
        IndexName="GSI1_CityBySpecialty",
        KeyConditionExpression=Key("GSI1PK").eq(_city_pk(city, generation))
        & Key("GSI1SK").begins_with(f"SPECIALTY#{specialty}#"),
    )
    items = response.get("Items", [])
    return {"items": items, "count": len(items)}


@app.get("/facilities/<facility_id>")
def get_facility(facility_id: str) -> dict[str, Any]:
    generation = _active_generation()
    response = _table().query(
        KeyConditionExpression=Key("PK").eq(_facility_pk(facility_id, generation)),
        ConsistentRead=True,
    )
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
