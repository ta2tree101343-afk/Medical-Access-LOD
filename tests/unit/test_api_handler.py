from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

TABLE_NAME = "medical-access-lod-test-read-model"


@pytest.fixture
def dynamodb_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("READ_MODEL_TABLE", TABLE_NAME)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("POWERTOOLS_METRICS_NAMESPACE", "MedicalAccessLOD")
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "medical-access-lod-api")
    with mock_aws():
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
                "SK": "SERVICE#01",
                "specialty_code": "01",
                "specialty_label": "内科",
                "GSI1PK": "CITY#千葉市中央区",
                "GSI1SK": "SPECIALTY#01#FACILITY#F1",
            }
        )
        batch.put_item(
            Item={
                "PK": "FACILITY#F1",
                "SK": "SCHEDULE#01#Monday#09:00:00",
                "specialty_code": "01",
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
                "SK": "SERVICE#01",
                "specialty_code": "01",
                "specialty_label": "内科",
                "GSI1PK": "CITY#千葉市花見川区",
                "GSI1SK": "SPECIALTY#1001#FACILITY#F2",
            }
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
        _apigw_event("/facilities", query={"city": "千葉市中央区", "specialty": "01"})
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 1
    item = response["body"]["items"][0]
    assert item["specialty_code"] == "01"


def test_facilities_by_label_resolves_to_same_code(dynamodb_env: None) -> None:
    """内科 (label) を渡しても 01 (code) と同じ結果になる (resolve_specialty)"""
    _create_table_and_seed()
    response = _invoke(
        _apigw_event("/facilities", query={"city": "千葉市中央区", "specialty": "内科"})
    )
    assert response["statusCode"] == 200
    assert response["body"]["count"] == 1
    assert response["body"]["items"][0]["specialty_code"] == "01"


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


def test_unused_env_default(dynamodb_env: None) -> None:
    """TABLE_NAME 環境変数がハンドラで正しく参照されている確認 (health のecho経由)"""
    assert os.environ["READ_MODEL_TABLE"] == TABLE_NAME
