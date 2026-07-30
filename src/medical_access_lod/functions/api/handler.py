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

from medical_access_lod.domain.values.day_of_week import DayOfWeek
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


def _normalize_time(raw: str) -> str:
    """`HH:MM` / `HH:MM:SS` を `HH:MM:SS` に正規化する。SPARQL の時刻比較と同じ表現に揃える。

    range を検証しない実装だと `"30:00"` が `"30:00:00"` として通り、以降の
    文字列比較で常に false → API が空結果を返す silent 失敗になる。
    受診時刻として意味のある `0 <= hh <= 23`, `0 <= mm/ss <= 59` に強制する。
    """
    parts = raw.strip().split(":")
    if len(parts) == 2:
        hh, mm = parts
        ss = "00"
    elif len(parts) == 3:
        hh, mm, ss = parts
    else:
        raise ValueError(f"invalid time: {raw!r}")
    if not (hh.isdigit() and mm.isdigit() and ss.isdigit()):
        raise ValueError(f"invalid time: {raw!r}")
    h, m, s = int(hh), int(mm), int(ss)
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        raise ValueError(f"time out of range: {raw!r}")
    return f"{h:02d}:{m:02d}:{s:02d}"


@app.get("/facilities")
@tracer.capture_method
def list_facilities() -> dict[str, Any]:
    city = app.current_event.get_query_string_value(name="city", default_value=None)
    specialty_raw = app.current_event.get_query_string_value(name="specialty", default_value=None)
    day_raw = app.current_event.get_query_string_value(name="day", default_value=None)
    time_raw = app.current_event.get_query_string_value(name="time", default_value=None)

    if not city or not specialty_raw:
        return {
            "items": [],
            "count": 0,
            "hint": "specify ?city= and ?specialty= (code or label); optional ?day= and ?time=HH:MM",
        }

    try:
        specialty = str(resolve_specialty(specialty_raw))
    except ValueError:
        specialty = specialty_raw

    day: str | None = None
    if day_raw:
        try:
            day = DayOfWeek.from_source(day_raw).value  # "Monday" 等の schema.org ローカル名
        except ValueError:
            return {"items": [], "count": 0, "error": f"invalid day: {day_raw!r}"}

    time_norm: str | None = None
    if time_raw:
        try:
            time_norm = _normalize_time(time_raw)
        except ValueError:
            return {"items": [], "count": 0, "error": f"invalid time: {time_raw!r}"}

    generation = _active_generation()
    table = _table()

    # 基本の候補集合 (city × specialty) は GSI1 で取る。
    base = table.query(
        IndexName="GSI1_CityBySpecialty",
        KeyConditionExpression=Key("GSI1PK").eq(_city_pk(city, generation))
        & Key("GSI1SK").begins_with(f"SPECIALTY#{specialty}#"),
    )
    items: list[dict[str, Any]] = base.get("Items", [])

    if day is not None:
        # day 指定時は GSI2 (SPECIALTY#code#DAY#day) を **一発** で叩く。
        # SCHEDULE items に city / facility_id が明示属性として乗っているので、
        # 施設ごとに追加 Query を撃つ N+1 を回避できる。
        gsi2_pk = _specialty_day_pk(specialty, day, generation)
        sched_resp = table.query(
            IndexName="GSI2_SpecialtyByDay",
            KeyConditionExpression=Key("GSI2PK").eq(gsi2_pk),
        )
        matched_fids: set[str] = set()
        for sched in sched_resp.get("Items", []):
            # `city` は A1 で追加した明示属性。旧世代 (attribute 未書込) は None を
            # 返す。この時点で GSI1 側 (基本の候補集合) が既に city で絞り込まれて
            # おり、facility_id の交差で最終フィルタするので、None は「city ミス
            # マッチではなく単に旧世代」として素通しさせる。
            # 一方で city 属性が存在するのに値が違うものは、確実にクロスシティ流入
            # なので排除する (GSI2PK は city を含まないため必要な防御)。
            sched_city = sched.get("city")
            if sched_city is not None and sched_city != city:
                continue
            if time_norm is not None:
                opens = sched.get("opens")
                closes = sched.get("closes")
                if not (isinstance(opens, str) and isinstance(closes, str)):
                    continue
                # 時刻比較は SPARQL §13.3 と同じ文字列 (STR) 比較。
                # HH:MM:SS 24 時制なら辞書順 == 時刻順。
                if not (opens <= time_norm < closes):
                    continue
            fid = sched.get("facility_id")
            if isinstance(fid, str):
                matched_fids.add(fid)
        items = [it for it in items if _facility_id_from_pk(it.get("PK")) in matched_fids]

    elif time_norm is not None:
        # time のみ指定時は day を絞れないため候補ごとに SCHEDULE を追加取得する。
        # 通常 UI は day+time で来るので N+1 になるのはこのパスのみ。
        filtered: list[dict[str, Any]] = []
        for item in items:
            pk = item.get("PK")
            if not isinstance(pk, str):
                continue
            resp = table.query(
                KeyConditionExpression=Key("PK").eq(pk)
                & Key("SK").begins_with(f"SCHEDULE#{specialty}#"),
            )
            for sched in resp.get("Items", []):
                opens = sched.get("opens")
                closes = sched.get("closes")
                if not (isinstance(opens, str) and isinstance(closes, str)):
                    continue
                if opens <= time_norm < closes:
                    filtered.append(item)
                    break
        items = filtered

    return {"items": items, "count": len(items)}


def _specialty_day_pk(specialty: str, day: str, generation: str | None) -> str:
    if generation is None:
        return f"SPECIALTY#{specialty}#DAY#{day}"
    return f"GENERATION#{generation}#SPECIALTY#{specialty}#DAY#{day}"


def _facility_id_from_pk(pk: Any) -> str | None:
    """PK (`GENERATION#<gen>#FACILITY#<fid>` または `FACILITY#<fid>`) から fid を取り出す。"""
    if not isinstance(pk, str):
        return None
    marker = "FACILITY#"
    idx = pk.rfind(marker)
    if idx < 0:
        return None
    return pk[idx + len(marker):]


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
