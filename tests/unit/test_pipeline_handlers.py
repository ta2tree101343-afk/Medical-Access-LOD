"""Pipeline Lambda ハンドラの moto ベース統合テスト。

download / normalize / build_rdf / validate / publish の各 handler を
実 AWS を使わずに検証する。
"""
from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

FIXTURES = Path(__file__).parent.parent / "fixtures" / "mhlw_sample"

RAW_BUCKET = "medical-access-lod-test-raw"
NORM_BUCKET = "medical-access-lod-test-normalized"
BUILD_BUCKET = "medical-access-lod-test-build"
DIST_BUCKET = "medical-access-lod-test-dist"


class _FakeLambdaContext:
    function_name = "medical-access-lod-test"
    function_version = "$LATEST"
    invoked_function_arn = (
        "arn:aws:lambda:ap-northeast-1:111111111111:function:medical-access-lod-test"
    )
    memory_limit_in_mb = 512
    aws_request_id = "test-request-id"
    log_group_name = "/aws/lambda/medical-access-lod-test"
    log_stream_name = "test-stream"


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("POWERTOOLS_METRICS_NAMESPACE", "MedicalAccessLOD")
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "medical-access-lod-test")
    with mock_aws():
        s3 = boto3.client("s3", region_name="ap-northeast-1")
        for bucket in (RAW_BUCKET, NORM_BUCKET, BUILD_BUCKET, DIST_BUCKET):
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"},
            )
        yield


def _upload_fixture_csvs(prefix: str) -> None:
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    for csv in FIXTURES.glob("*.csv"):
        s3.upload_file(str(csv), RAW_BUCKET, f"{prefix}/{csv.name}")


def test_normalize_handler_produces_normalized_json(aws_env: None) -> None:
    from medical_access_lod.functions.normalize.handler import lambda_handler

    prefix = "snapshots/2025-12-01"
    _upload_fixture_csvs(prefix)

    event = {
        "run_id": "test-001",
        "raw_bucket": RAW_BUCKET,
        "raw_prefix": prefix,
        "normalized_bucket": NORM_BUCKET,
    }
    response = lambda_handler(event, _FakeLambdaContext())  # type: ignore[arg-type]

    assert response["facilities"] >= 1
    assert response["schedules"] >= 1

    s3 = boto3.client("s3", region_name="ap-northeast-1")
    obj = s3.get_object(Bucket=NORM_BUCKET, Key=response["normalized_key"])
    payload = json.loads(obj["Body"].read())
    assert "facilities" in payload
    assert "specialty_labels" in payload
    assert isinstance(payload["facilities"], list)


def test_build_rdf_handler_uploads_ttl_and_jsonld(aws_env: None) -> None:
    from medical_access_lod.functions.build_rdf.handler import lambda_handler

    payload = {
        "facilities": [
            {
                "facility_id": "F1",
                "name": "テスト病院",
                "facility_type": "hospital",
                "address": {
                    "prefecture": "千葉県",
                    "city": "千葉市中央区",
                    "street_address": "中央1-1-1",
                },
            }
        ],
        "services": [{"facility_id": "F1", "specialty_code": "01"}],
        "schedules": [
            {
                "facility_id": "F1",
                "specialty_code": "01",
                "day_of_week": "Monday",
                "opens": "09:00:00",
                "closes": "17:00:00",
            }
        ],
        "specialty_labels": {"01": "内科"},
    }
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    s3.put_object(
        Bucket=NORM_BUCKET,
        Key="normalized/test.json",
        Body=json.dumps(payload).encode("utf-8"),
    )

    response = lambda_handler(
        {
            "run_id": "test-002",
            "normalized_bucket": NORM_BUCKET,
            "normalized_key": "normalized/test.json",
            "build_bucket": BUILD_BUCKET,
        },
        _FakeLambdaContext(),  # type: ignore[arg-type]
    )
    assert response["triples"] > 0
    ttl_obj = s3.head_object(Bucket=BUILD_BUCKET, Key=response["ttl_key"])
    js_obj = s3.head_object(Bucket=BUILD_BUCKET, Key=response["jsonld_key"])
    assert ttl_obj["ContentLength"] > 0
    assert js_obj["ContentLength"] > 0


def _valid_ttl() -> bytes:
    return (
        '@prefix ex: <https://example.org/medical-access/> .\n'
        '@prefix schema: <https://schema.org/> .\n'
        '@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n'
        '@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n'
        '@base <https://example.org/medical-access/> .\n'
        '<resource/facility/F1> a schema:MedicalClinic ;\n'
        '  ex:facilityId "F1" ;\n'
        '  schema:name "T"@ja ;\n'
        '  schema:address <resource/address/F1> ;\n'
        '  ex:offersClinicalService <resource/service/F1/01> .\n'
        '<resource/address/F1> a schema:PostalAddress ;\n'
        '  schema:addressRegion "千葉県"@ja ;\n'
        '  schema:addressLocality "千葉市中央区"@ja ;\n'
        '  schema:streetAddress "1-1-1"@ja .\n'
        '<resource/service/F1/01> a ex:ClinicalService ;\n'
        '  ex:medicalSpecialty <concept/specialty/01> ;\n'
        '  ex:hasSchedule <resource/schedule/F1/01/MON/1> .\n'
        '<concept/specialty/01> a skos:Concept ;\n'
        '  skos:notation "01" .\n'
        '<resource/schedule/F1/01/MON/1> a schema:OpeningHoursSpecification ;\n'
        '  schema:dayOfWeek schema:Monday ;\n'
        '  schema:opens "09:00:00"^^xsd:time ;\n'
        '  schema:closes "17:00:00"^^xsd:time .\n'
    ).encode()


def test_validate_handler_returns_conforms_for_valid_ttl(aws_env: None) -> None:
    from medical_access_lod.functions.validate.handler import lambda_handler

    s3 = boto3.client("s3", region_name="ap-northeast-1")
    s3.put_object(Bucket=BUILD_BUCKET, Key="builds/x/x.ttl", Body=_valid_ttl())

    response = lambda_handler(
        {
            "run_id": "test-003",
            "build_bucket": BUILD_BUCKET,
            "ttl_key": "builds/x/x.ttl",
        },
        _FakeLambdaContext(),  # type: ignore[arg-type]
    )
    assert response["conforms"] is True
    assert "report_key" not in response


def test_validate_handler_returns_violation_for_missing_facility_id(aws_env: None) -> None:
    from medical_access_lod.functions.validate.handler import lambda_handler

    broken = _valid_ttl().replace(b'ex:facilityId "F1" ;\n  ', b"")
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    s3.put_object(Bucket=BUILD_BUCKET, Key="builds/y/y.ttl", Body=broken)

    response = lambda_handler(
        {
            "run_id": "test-004",
            "build_bucket": BUILD_BUCKET,
            "ttl_key": "builds/y/y.ttl",
        },
        _FakeLambdaContext(),  # type: ignore[arg-type]
    )
    assert response["conforms"] is False
    assert response["report_key"].startswith("builds/test-004/")
    s3.head_object(Bucket=BUILD_BUCKET, Key=response["report_key"])


def test_publish_handler_copies_build_to_dist(aws_env: None) -> None:
    from medical_access_lod.functions.publish.handler import lambda_handler

    s3 = boto3.client("s3", region_name="ap-northeast-1")
    s3.put_object(Bucket=BUILD_BUCKET, Key="builds/r/medical-access-lod.ttl", Body=b"ttl")
    s3.put_object(Bucket=BUILD_BUCKET, Key="builds/r/medical-access-lod.jsonld", Body=b"json")

    response = lambda_handler(
        {
            "run_id": "test-005",
            "build_bucket": BUILD_BUCKET,
            "ttl_key": "builds/r/medical-access-lod.ttl",
            "jsonld_key": "builds/r/medical-access-lod.jsonld",
            "dist_bucket": DIST_BUCKET,
        },
        _FakeLambdaContext(),  # type: ignore[arg-type]
    )
    assert "latest/medical-access-lod.ttl" in response["published_files"]
    assert "latest/medical-access-lod.jsonld" in response["published_files"]
    s3.head_object(Bucket=DIST_BUCKET, Key="latest/medical-access-lod.ttl")
    s3.head_object(Bucket=DIST_BUCKET, Key="latest/medical-access-lod.jsonld")


def test_build_read_model_handler_writes_dynamodb_items(aws_env: None) -> None:
    from medical_access_lod.functions.build_read_model.handler import lambda_handler

    table_name = "medical-access-lod-test-read-model"
    ddb = boto3.resource("dynamodb", region_name="ap-northeast-1")
    ddb.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    payload = {
        "facilities": [
            {
                "facility_id": "F1",
                "name": "テスト病院",
                "facility_type": "hospital",
                "address": {
                    "prefecture": "千葉県",
                    "city": "千葉市中央区",
                    "street_address": "1-1-1",
                },
            }
        ],
        "services": [{"facility_id": "F1", "specialty_code": "01"}],
        "schedules": [
            {
                "facility_id": "F1",
                "specialty_code": "01",
                "day_of_week": "Monday",
                "opens": "09:00:00",
                "closes": "17:00:00",
            }
        ],
        "specialty_labels": {"01": "内科"},
    }
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    s3.put_object(
        Bucket=NORM_BUCKET,
        Key="normalized/test.json",
        Body=json.dumps(payload).encode("utf-8"),
    )

    response = lambda_handler(
        {
            "run_id": "test-006",
            "normalized_bucket": NORM_BUCKET,
            "normalized_key": "normalized/test.json",
            "read_model_table": table_name,
        },
        _FakeLambdaContext(),  # type: ignore[arg-type]
    )
    assert response["items_written"] == 3


class _StaticHttp:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def get_bytes(self, url: str) -> bytes:
        return self.payload


def test_download_handler_writes_extracted_files_to_s3(aws_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from medical_access_lod.application import download_source
    from medical_access_lod.functions.download.handler import lambda_handler

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("hello.csv", "a,b\n1,2\n")
    payload = buf.getvalue()

    monkeypatch.setattr(download_source, "HttpxClient", lambda: _StaticHttp(payload))

    response = lambda_handler(
        {
            "run_id": "test-007",
            "source_url": "https://example.test/x.zip",
            "snapshot_date": "2025-12-01",
            "raw_bucket": RAW_BUCKET,
        },
        _FakeLambdaContext(),  # type: ignore[arg-type]
    )
    assert response["files"], "no files uploaded"
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    for key in response["files"]:
        s3.head_object(Bucket=RAW_BUCKET, Key=key)
