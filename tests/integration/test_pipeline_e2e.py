"""Step Functions と同じ順序で 6 ハンドラを連鎖させる E2E テスト。

download -> normalize -> build_rdf -> validate -> publish -> build_read_model
moto (S3 + DynamoDB モック) で完結し、実 AWS を使わない。
"""
from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

FIXTURES = Path(__file__).parent.parent / "fixtures" / "mhlw_sample"
REGION = "ap-northeast-1"
RAW_BUCKET = "medical-access-lod-e2e-raw"
NORM_BUCKET = "medical-access-lod-e2e-normalized"
BUILD_BUCKET = "medical-access-lod-e2e-build"
DIST_BUCKET = "medical-access-lod-e2e-dist"
DDB_TABLE = "medical-access-lod-e2e-read-model"


class _FakeLambdaContext:
    function_name = "medical-access-lod-e2e"
    function_version = "$LATEST"
    invoked_function_arn = (
        f"arn:aws:lambda:{REGION}:111111111111:function:medical-access-lod-e2e"
    )
    memory_limit_in_mb = 512
    aws_request_id = "e2e-req"
    log_group_name = "/aws/lambda/medical-access-lod-e2e"
    log_stream_name = "e2e"


class _StaticHttp:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def get_bytes(self, url: str) -> bytes:
        return self.payload


def _make_zip_from_fixtures() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for csv in FIXTURES.glob("*.csv"):
            zf.write(csv, arcname=csv.name)
    return buf.getvalue()


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("POWERTOOLS_METRICS_NAMESPACE", "MedicalAccessLOD")
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "medical-access-lod-e2e")
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (RAW_BUCKET, NORM_BUCKET, BUILD_BUCKET, DIST_BUCKET):
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=DDB_TABLE,
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
        yield


def test_step_functions_equivalent_flow_completes_and_publishes(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medical_access_lod.application import download_source
    from medical_access_lod.functions.build_rdf import handler as build
    from medical_access_lod.functions.build_read_model import handler as brm
    from medical_access_lod.functions.download import handler as download
    from medical_access_lod.functions.normalize import handler as normalize
    from medical_access_lod.functions.publish import handler as publish
    from medical_access_lod.functions.validate import handler as validate

    payload = _make_zip_from_fixtures()
    monkeypatch.setattr(download_source, "HttpxClient", lambda: _StaticHttp(payload))

    run_id = "e2e-run-001"
    context = _FakeLambdaContext()

    dl = download.lambda_handler(
        {
            "run_id": run_id,
            "source_url": "https://example.test/x.zip",
            "snapshot_date": "2025-12-01",
            "raw_bucket": RAW_BUCKET,
        },
        context,  # type: ignore[arg-type]
    )
    assert dl["files"], "download step yielded no files"

    norm = normalize.lambda_handler(
        {
            "run_id": run_id,
            "raw_bucket": RAW_BUCKET,
            "raw_prefix": dl["raw_prefix"],
            "normalized_bucket": NORM_BUCKET,
        },
        context,  # type: ignore[arg-type]
    )
    assert norm["facilities"] >= 1
    assert norm["schedules"] >= 1

    built = build.lambda_handler(
        {
            "run_id": run_id,
            "normalized_bucket": NORM_BUCKET,
            "normalized_key": norm["normalized_key"],
            "build_bucket": BUILD_BUCKET,
        },
        context,  # type: ignore[arg-type]
    )
    assert built["triples"] > 0

    val = validate.lambda_handler(
        {
            "run_id": run_id,
            "build_bucket": BUILD_BUCKET,
            "ttl_key": built["ttl_key"],
        },
        context,  # type: ignore[arg-type]
    )
    assert val["conforms"] is True, val

    # Choice: conforms=true -> Publish + BuildReadModel
    pub = publish.lambda_handler(
        {
            "run_id": run_id,
            "build_bucket": BUILD_BUCKET,
            "ttl_key": built["ttl_key"],
            "jsonld_key": built["jsonld_key"],
            "dist_bucket": DIST_BUCKET,
        },
        context,  # type: ignore[arg-type]
    )
    assert "latest/medical-access-lod.ttl" in pub["published_files"]

    brm_result = brm.lambda_handler(
        {
            "run_id": run_id,
            "normalized_bucket": NORM_BUCKET,
            "normalized_key": norm["normalized_key"],
            "read_model_table": DDB_TABLE,
        },
        context,  # type: ignore[arg-type]
    )
    assert brm_result["items_written"] >= 3

    # 最終状態の確認: dist に成果物、DDB に施設 METADATA が存在
    s3 = boto3.client("s3", region_name=REGION)
    s3.head_object(Bucket=DIST_BUCKET, Key="latest/medical-access-lod.ttl")
    s3.head_object(Bucket=DIST_BUCKET, Key="latest/medical-access-lod.jsonld")

    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(DDB_TABLE)
    types: set[str] = set()
    scan_kwargs: dict[str, object] = {}
    while True:
        scan_response = table.scan(**scan_kwargs)
        types.update(item["SK"].split("#", 1)[0] for item in scan_response.get("Items", []))
        key = scan_response.get("LastEvaluatedKey")
        if not key:
            break
        scan_kwargs["ExclusiveStartKey"] = key
    assert {"METADATA", "SERVICE", "SCHEDULE"}.issubset(types), types


def test_step_functions_equivalent_flow_halts_on_shacl_violation(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SHACL 違反時は Publish / BuildReadModel を呼ばずに終了する
    (SFN の Choice ブランチと同じ挙動)。"""
    from medical_access_lod.application import download_source
    from medical_access_lod.functions.build_rdf import handler as build
    from medical_access_lod.functions.download import handler as download
    from medical_access_lod.functions.normalize import handler as normalize
    from medical_access_lod.functions.validate import handler as validate

    payload = _make_zip_from_fixtures()
    monkeypatch.setattr(download_source, "HttpxClient", lambda: _StaticHttp(payload))

    run_id = "e2e-run-002"
    context = _FakeLambdaContext()

    dl = download.lambda_handler(
        {
            "run_id": run_id,
            "source_url": "https://example.test/x.zip",
            "snapshot_date": "2025-12-01",
            "raw_bucket": RAW_BUCKET,
        },
        context,  # type: ignore[arg-type]
    )
    norm = normalize.lambda_handler(
        {
            "run_id": run_id,
            "raw_bucket": RAW_BUCKET,
            "raw_prefix": dl["raw_prefix"],
            "normalized_bucket": NORM_BUCKET,
        },
        context,  # type: ignore[arg-type]
    )
    built = build.lambda_handler(
        {
            "run_id": run_id,
            "normalized_bucket": NORM_BUCKET,
            "normalized_key": norm["normalized_key"],
            "build_bucket": BUILD_BUCKET,
        },
        context,  # type: ignore[arg-type]
    )

    # わざと違反した TTL に置き換える (rdflib で facilityId プロパティを全て削除)
    from rdflib import Graph, Namespace

    s3 = boto3.client("s3", region_name=REGION)
    original = s3.get_object(Bucket=BUILD_BUCKET, Key=built["ttl_key"])["Body"].read()
    ex = Namespace("https://example.org/medical-access/")
    graph = Graph().parse(data=original, format="turtle")
    graph.remove((None, ex.facilityId, None))
    broken = graph.serialize(format="turtle").encode("utf-8")
    s3.put_object(Bucket=BUILD_BUCKET, Key=built["ttl_key"], Body=broken)

    val = validate.lambda_handler(
        {
            "run_id": run_id,
            "build_bucket": BUILD_BUCKET,
            "ttl_key": built["ttl_key"],
        },
        context,  # type: ignore[arg-type]
    )
    assert val["conforms"] is False, "SHACL should reject facilities without facilityId"
    assert val["report_key"].startswith(f"builds/{run_id}/")

    # dist にも DDB にも何も書かれていないこと (SFN の Choice が failure ブランチへ)
    dist_list = s3.list_objects_v2(Bucket=DIST_BUCKET).get("Contents", [])
    assert not dist_list, f"dist should be empty on SHACL failure: {dist_list}"

    ddb = boto3.resource("dynamodb", region_name=REGION)
    scan_response = ddb.Table(DDB_TABLE).scan(Limit=1)
    assert scan_response["Count"] == 0
