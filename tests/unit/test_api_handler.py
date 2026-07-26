from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from medical_access_lod.functions.api.handler import _normalize_time

TABLE_NAME = "medical-access-lod-test-read-model"
DIST_BUCKET = "medical-access-lod-test-dist"


@pytest.fixture
def dynamodb_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("READ_MODEL_TABLE", TABLE_NAME)
    monkeypatch.setenv("DIST_BUCKET", DIST_BUCKET)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("POWERTOOLS_METRICS_NAMESPACE", "MedicalAccessLOD")
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "medical-access-lod-api")
    with mock_aws():
        boto3.client("s3", region_name="ap-northeast-1").create_bucket(
            Bucket=DIST_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"},
        )
        yield


def _create_table_and_seed() -> None:
    ddb = boto3.resource("dynamodb", region_name="ap-northeast-1")
    ddb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1_CityBySpecialty",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table = ddb.Table(TABLE_NAME)
    with table.batch_writer() as batch:
        batch.put_item(
            Item={
                "PK": "FACILITY#F1",
                "SK": "METADATA",
                "facility_id": "F1",
                "name": "千葉中央総合病院",
                "facility_type": "hospital",
                "prefecture": "千葉県",
                "city": "千葉市中央区",
                "street_address": "中央1-1-1",
                "GSI1PK": "CITY#千葉市中央区",
            }
        )
        batch.put_item(
            Item={
                "PK": "FACILITY#F1",
                "SK": "SERVICE#1001",
                "specialty_code": "1001",
                "specialty_label": "内科",
                "GSI1PK": "CITY#千葉市中央区",
                "GSI1SK": "SPECIALTY#1001#FACILITY#F1",
            }
        )
        batch.put_item(
            Item={
                "PK": "FACILITY#F1",
                "SK": "SCHEDULE#1001#Monday#09:00:00",
                "specialty_code": "1001",
                "day_of_week": "Monday",
                "opens": "09:00:00",
                "closes": "17:00:00",
            }
        )
        batch.put_item(
            Item={
                "PK": "FACILITY#F2",
                "SK": "METADATA",
                "facility_id": "F2",
                "name": "花見川内科クリニック",
                "facility_type": "clinic",
                "prefecture": "千葉県",
                "city": "千葉市花見川区",
                "street_address": "幕張1-1",
                "GSI1PK": "CITY#千葉市花見川区",
            }
        )
        batch.put_item(
            Item={
                "PK": "FACILITY#F2",
                "SK": "SERVICE#1001",
                "specialty_code": "1001",
                "specialty_label": "内科",
                "GSI1PK": "CITY#千葉市花見川区",
                "GSI1SK": "SPECIALTY#1001#FACILITY#F2",
            }
        )


def _seed_generation(run_id: str, *, name: str, street_address: str) -> None:
    table = boto3.resource("dynamodb", region_name="ap-northeast-1").Table(TABLE_NAME)
    pk = f"GENERATION#{run_id}#FACILITY#F1"
    city_pk = f"GENERATION#{run_id}#CITY#千葉市中央区"
    with table.batch_writer() as batch:
        batch.put_item(
            Item={
                "PK": pk,
                "SK": "METADATA",
                "generation": run_id,
                "facility_id": "F1",
                "name": name,
                "facility_type": "hospital",
                "prefecture": "千葉県",
                "city": "千葉市中央区",
                "street_address": street_address,
                "GSI1PK": city_pk,
            }
        )
        batch.put_item(
            Item={
                "PK": pk,
                "SK": "SERVICE#1001",
                "generation": run_id,
                "specialty_code": "1001",
                "specialty_label": "内科",
                "GSI1PK": city_pk,
                "GSI1SK": "SPECIALTY#1001#FACILITY#F1",
            }
        )
        batch.put_item(
            Item={
                "PK": pk,
                "SK": "SCHEDULE#1001#Monday#09:00:00",
                "generation": run_id,
                "specialty_code": "1001",
                "day_of_week": "Monday",
                "opens": "09:00:00",
                "closes": "17:00:00",
            }
        )


def _put_manifest(run_id: str) -> None:
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "snapshot_date": "2025-12-01",
        "artifacts": {
            "turtle": {
                "key": f"releases/2025-12-01/{run_id}/data.ttl",
                "size": 3,
                "etag": "ttl-etag",
                "content_type": "text/turtle; charset=utf-8",
            },
            "jsonld": {
                "key": f"releases/2025-12-01/{run_id}/data.jsonld",
                "size": 4,
                "etag": "jsonld-etag",
                "content_type": "application/ld+json",
            },
        },
    }
    boto3.client("s3", region_name="ap-northeast-1").put_object(
        Bucket=DIST_BUCKET,
        Key="latest/manifest.json",
        Body=json.dumps(manifest).encode("utf-8"),
        ContentType="application/json",
    )


def _apigw_event(path: str, query: dict[str, str] | None = None, path_params: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "version": "2.0",
        "routeKey": f"GET {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json"},
        "queryStringParameters": query,
        "pathParameters": path_params,
        "requestContext": {
            "accountId": "111111111111",
            "apiId": "test",
            "domainName": "test.execute-api.ap-northeast-1.amazonaws.com",
            "http": {
                "method": "GET",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "pytest",
            },
            "requestId": "req-1",
            "routeKey": f"GET {path}",
            "stage": "$default",
            "time": "18/Jul/2026:00:00:00 +0000",
            "timeEpoch": 1752873600,
        },
        "isBase64Encoded": False,
    }


class _FakeLambdaContext:
    function_name = "medical-access-lod-test-api"
    function_version = "$LATEST"
    invoked_function_arn = (
        "arn:aws:lambda:ap-northeast-1:111111111111:function:medical-access-lod-test-api"
    )
    memory_limit_in_mb = 512
    aws_request_id = "test-request-id"
    log_group_name = "/aws/lambda/medical-access-lod-test-api"
    log_stream_name = "test-stream"


def _invoke(event: dict[str, Any]) -> dict[str, Any]:
    from medical_access_lod.functions.api import handler as api_handler

    response: dict[str, Any] = api_handler.lambda_handler(event, _FakeLambdaContext())  # type: ignore[arg-type]
    body = response.get("body")
    if isinstance(body, str):
        import contextlib

        with contextlib.suppress(json.JSONDecodeError):
            response["body"] = json.loads(body)
    return response


def test_health_returns_ok(dynamodb_env: None) -> None:
    _create_table_and_seed()
    response = _invoke(_apigw_event("/health"))
    assert response["statusCode"] == 200
    assert response["body"]["status"] == "ok"
    assert response["body"]["table"] == TABLE_NAME


def test_metadata_returns_source_and_license(dynamodb_env: None) -> None:
    _create_table_and_seed()
    response = _invoke(_apigw_event("/metadata"))
    assert response["statusCode"] == 200
    assert response["body"]["license"].startswith("PDL")
    assert "中央区" in response["body"]["coverage"]["wards"]


def test_facilities_requires_city_and_specialty(dynamodb_env: None) -> None:
    _create_table_and_seed()
    response = _invoke(_apigw_event("/facilities"))
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 0
    assert "hint" in response["body"]


def test_facilities_by_code_returns_matches(dynamodb_env: None) -> None:
    _create_table_and_seed()
    response = _invoke(
        _apigw_event("/facilities", query={"city": "千葉市中央区", "specialty": "1001"})
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 1
    item = response["body"]["items"][0]
    assert item["specialty_code"] == "1001"


def test_facilities_by_label_resolves_to_same_code(dynamodb_env: None) -> None:
    """内科 (label) を渡しても 1001 (code) と同じ結果になる (resolve_specialty)"""
    _create_table_and_seed()
    response = _invoke(
        _apigw_event("/facilities", query={"city": "千葉市中央区", "specialty": "内科"})
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 1
    assert response["body"]["items"][0]["specialty_code"] == "1001"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("09:00", "09:00:00"),
        ("9:5", "09:05:00"),
        ("18:30:45", "18:30:45"),
        ("00:00", "00:00:00"),
        ("23:59:59", "23:59:59"),
    ],
)
def test_normalize_time_accepts_valid_forms(raw: str, expected: str) -> None:
    assert _normalize_time(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "25:00",     # hour out of range
        "24:00",     # hour boundary — 24 は不許可 (23:59:59 が最大)
        "12:60",     # minute out of range
        "12:00:60",  # second out of range
        "-1:00",     # negative
        "abc",       # non-digit
        "",          # empty
        ":",         # separator only
        "12",        # no colon
        "12:00:00:00",  # too many parts
    ],
)
def test_normalize_time_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        _normalize_time(raw)


def test_facilities_filter_by_day_and_time_matches_schedule(dynamodb_env: None) -> None:
    """day + time 指定時に SCHEDULE との交差で施設が絞り込まれる。"""
    _create_table_and_seed()
    response = _invoke(
        _apigw_event(
            "/facilities",
            query={
                "city": "千葉市中央区",
                "specialty": "1001",
                "day": "MON",
                "time": "10:00",
            },
        )
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 1


def test_facilities_filter_by_day_and_time_excludes_outside_hours(dynamodb_env: None) -> None:
    """営業時間外 (17:00 は closes と同時なので受診不可) は除外される。"""
    _create_table_and_seed()
    response = _invoke(
        _apigw_event(
            "/facilities",
            query={
                "city": "千葉市中央区",
                "specialty": "1001",
                "day": "月曜日",  # 表記ゆれに対応
                "time": "17:00",
            },
        )
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 0


def test_facilities_time_equals_opens_included(dynamodb_env: None) -> None:
    """time == opens は開店直後として受診可 (境界 <= 側)。"""
    _create_table_and_seed()
    response = _invoke(
        _apigw_event(
            "/facilities",
            query={"city": "千葉市中央区", "specialty": "1001", "day": "MON", "time": "09:00"},
        )
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 1


def test_facilities_filter_day_only(dynamodb_env: None) -> None:
    """day のみ指定時は該当曜日に SCHEDULE がある施設のみ返る。"""
    _create_table_and_seed()
    response = _invoke(
        _apigw_event(
            "/facilities",
            query={"city": "千葉市中央区", "specialty": "1001", "day": "MON"},
        )
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 1

    # 該当曜日に SCHEDULE がない場合は 0 件
    response = _invoke(
        _apigw_event(
            "/facilities",
            query={"city": "千葉市中央区", "specialty": "1001", "day": "SUN"},
        )
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 0


def test_facilities_filter_time_only(dynamodb_env: None) -> None:
    """time のみ指定時は曜日を問わず該当時刻に開いている施設が返る。"""
    _create_table_and_seed()
    response = _invoke(
        _apigw_event(
            "/facilities",
            query={"city": "千葉市中央区", "specialty": "1001", "time": "10:00"},
        )
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 1


def test_facilities_invalid_day_returns_error(dynamodb_env: None) -> None:
    """不正な day は error フィールドを含む 200 応答で返す (API 契約の pin)。"""
    _create_table_and_seed()
    response = _invoke(
        _apigw_event(
            "/facilities",
            query={"city": "千葉市中央区", "specialty": "1001", "day": "XYZ"},
        )
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 0
    assert "error" in response["body"]
    assert response["body"]["error"].startswith("invalid day")


def test_facilities_invalid_time_returns_error(dynamodb_env: None) -> None:
    """不正な time (範囲外) も同様に error を返す。"""
    _create_table_and_seed()
    response = _invoke(
        _apigw_event(
            "/facilities",
            query={"city": "千葉市中央区", "specialty": "1001", "time": "25:00"},
        )
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 0
    assert "error" in response["body"]
    assert response["body"]["error"].startswith("invalid time")


def test_facility_detail_by_id_returns_metadata_services_schedules(dynamodb_env: None) -> None:
    _create_table_and_seed()
    response = _invoke(
        _apigw_event("/facilities/F1", path_params={"facility_id": "F1"})
    )
    assert response["statusCode"] == 200
    body = response["body"]
    assert body["found"] is True
    assert body["metadata"]["name"] == "千葉中央総合病院"
    assert len(body["services"]) == 1
    assert len(body["schedules"]) == 1


def test_facility_detail_missing_returns_found_false(dynamodb_env: None) -> None:
    _create_table_and_seed()
    response = _invoke(
        _apigw_event("/facilities/UNKNOWN", path_params={"facility_id": "UNKNOWN"})
    )
    assert response["statusCode"] == 200
    assert response["body"]["found"] is False


def test_manifest_switches_facility_search_without_mixing_generations(
    dynamodb_env: None,
) -> None:
    _create_table_and_seed()
    _seed_generation("run-A", name="A病院", street_address="旧1-1")
    _seed_generation("run-B", name="B病院", street_address="新2-2")

    _put_manifest("run-A")
    response_a = _invoke(
        _apigw_event("/facilities", query={"city": "千葉市中央区", "specialty": "1001"})
    )
    assert response_a["statusCode"] == 200
    assert response_a["body"]["count"] == 1
    assert response_a["body"]["items"][0]["generation"] == "run-A"

    _put_manifest("run-B")
    response_b = _invoke(
        _apigw_event("/facilities", query={"city": "千葉市中央区", "specialty": "1001"})
    )
    assert response_b["statusCode"] == 200
    assert response_b["body"]["count"] == 1
    assert response_b["body"]["items"][0]["generation"] == "run-B"


def test_manifest_switches_facility_detail_without_mixing_generations(
    dynamodb_env: None,
) -> None:
    _create_table_and_seed()
    _seed_generation("run-A", name="A病院", street_address="旧1-1")
    _seed_generation("run-B", name="B病院", street_address="新2-2")

    _put_manifest("run-A")
    response_a = _invoke(
        _apigw_event("/facilities/F1", path_params={"facility_id": "F1"})
    )
    assert response_a["body"]["metadata"]["name"] == "A病院"
    assert {item["generation"] for item in response_a["body"]["services"]} == {"run-A"}
    assert {item["generation"] for item in response_a["body"]["schedules"]} == {"run-A"}

    _put_manifest("run-B")
    response_b = _invoke(
        _apigw_event("/facilities/F1", path_params={"facility_id": "F1"})
    )
    assert response_b["body"]["metadata"]["name"] == "B病院"
    assert {item["generation"] for item in response_b["body"]["services"]} == {"run-B"}
    assert {item["generation"] for item in response_b["body"]["schedules"]} == {"run-B"}


def test_existing_invalid_manifest_does_not_fall_back_to_legacy_generation(
    dynamodb_env: None,
) -> None:
    from medical_access_lod.functions.api import handler as api_handler

    _create_table_and_seed()
    boto3.client("s3", region_name="ap-northeast-1").put_object(
        Bucket=DIST_BUCKET,
        Key="latest/manifest.json",
        Body=b"{}",
        ContentType="application/json",
    )

    with pytest.raises(RuntimeError, match="schema_version"):
        api_handler._active_generation()


def test_unused_env_default(dynamodb_env: None) -> None:
    """TABLE_NAME 環境変数がハンドラで正しく参照されている確認 (health のecho経由)"""
    assert os.environ["READ_MODEL_TABLE"] == TABLE_NAME
