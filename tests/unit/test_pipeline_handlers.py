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


def _publish_event(run_id: str = "test-005") -> dict[str, str]:
    return {
        "run_id": run_id,
        "build_bucket": BUILD_BUCKET,
        "ttl_key": "builds/r/medical-access-lod.ttl",
        "jsonld_key": "builds/r/medical-access-lod.jsonld",
        "dist_bucket": DIST_BUCKET,
        "read_model_table": "read-model",
        "lock_owner": run_id,
        "snapshot_date": "2025-12-01",
    }


def _put_build_artifacts() -> None:
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    s3.put_object(
        Bucket=BUILD_BUCKET,
        Key="builds/r/medical-access-lod.ttl",
        Body=b"ttl",
        ContentType="text/turtle; charset=utf-8",
    )
    s3.put_object(
        Bucket=BUILD_BUCKET,
        Key="builds/r/medical-access-lod.jsonld",
        Body=b"json",
        ContentType="application/ld+json",
    )


def test_publish_handler_commits_release_with_single_manifest(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medical_access_lod.functions.publish import handler as publish_handler

    _put_build_artifacts()
    renew_calls: list[tuple[str, str]] = []
    release_calls: list[tuple[str, str]] = []

    def renew(table: str, owner: str) -> int:
        renew_calls.append((table, owner))
        return 1

    def failing_release(table: str, owner: str) -> bool:
        release_calls.append((table, owner))
        raise RuntimeError("release failed")

    monkeypatch.setattr(publish_handler.pipeline_lock, "renew", renew)
    monkeypatch.setattr(publish_handler.pipeline_lock, "release", failing_release)
    # generation catalog は Publish 側の関心事ではないため、mark_committed も
    # 明示的に mock する。実 catalog 遷移は test_generation_catalog + E2E で検証。
    mark_committed_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        publish_handler.generation_catalog,
        "mark_committed",
        lambda table, run_id: mark_committed_calls.append((table, run_id)) or 1,
    )

    event = _publish_event("test/005")
    response = publish_handler.lambda_handler(
        event,
        _FakeLambdaContext(),  # type: ignore[arg-type]
    )
    release_prefix = "releases/2025-12-01/test%2F005"
    ttl_key = f"{release_prefix}/medical-access-lod.ttl"
    jsonld_key = f"{release_prefix}/medical-access-lod.jsonld"
    assert response["published_files"] == [ttl_key, jsonld_key, "latest/manifest.json"]
    assert response["manifest_key"] == "latest/manifest.json"

    s3 = boto3.client("s3", region_name="ap-northeast-1")
    ttl_head = s3.head_object(Bucket=DIST_BUCKET, Key=ttl_key)
    jsonld_head = s3.head_object(Bucket=DIST_BUCKET, Key=jsonld_key)
    manifest_obj = s3.get_object(Bucket=DIST_BUCKET, Key="latest/manifest.json")
    manifest = json.loads(manifest_obj["Body"].read())
    assert manifest == {
        "schema_version": 1,
        "run_id": "test/005",
        "snapshot_date": "2025-12-01",
        "artifacts": {
            "turtle": {
                "key": ttl_key,
                "size": 3,
                "etag": ttl_head["ETag"].strip('"'),
                "content_type": "text/turtle; charset=utf-8",
            },
            "jsonld": {
                "key": jsonld_key,
                "size": 4,
                "etag": jsonld_head["ETag"].strip('"'),
                "content_type": "application/ld+json",
            },
        },
    }
    assert manifest_obj["ContentType"] == "application/json"
    latest = s3.list_objects_v2(Bucket=DIST_BUCKET, Prefix="latest/").get("Contents", [])
    assert [obj["Key"] for obj in latest] == ["latest/manifest.json"]
    # release失敗はcommit済みの公開結果をエラーに戻さない。
    assert renew_calls == [("read-model", "test/005"), ("read-model", "test/005")]
    assert release_calls == [("read-model", "test/005")]


def test_manifest_put_uses_compare_and_swap_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    from medical_access_lod.functions.publish import handler as publish_handler

    requests: list[dict[str, object]] = []

    class _PutRecorder:
        def put_object(self, **kwargs: object) -> None:
            requests.append(kwargs)

    recorder = _PutRecorder()
    monkeypatch.setattr(publish_handler, "s3_client", lambda: recorder)
    publish_handler._put_manifest("dist", {"run_id": "first"}, None)
    publish_handler._put_manifest("dist", {"run_id": "next"}, '"old-etag"')

    assert requests[0]["IfNoneMatch"] == "*"
    assert "IfMatch" not in requests[0]
    assert requests[1]["IfMatch"] == '"old-etag"'
    assert "IfNoneMatch" not in requests[1]


def test_manifest_compare_and_swap_rejects_different_run(
    aws_env: None,
) -> None:
    from medical_access_lod.functions.publish import handler as publish_handler

    s3 = boto3.client("s3", region_name="ap-northeast-1")
    s3.put_object(Bucket=DIST_BUCKET, Key="latest/manifest.json", Body=b'{"run_id":"old"}')
    _, old_etag = publish_handler._read_manifest_state(DIST_BUCKET)
    newer_manifest = {
        "schema_version": 1,
        "run_id": "newer-run",
        "snapshot_date": "2025-12-01",
        "artifacts": {},
    }
    s3.put_object(
        Bucket=DIST_BUCKET,
        Key="latest/manifest.json",
        Body=json.dumps(newer_manifest).encode(),
    )
    staged_manifest = {
        "schema_version": 1,
        "run_id": "delayed-run",
        "snapshot_date": "2025-12-01",
        "artifacts": {},
    }

    with pytest.raises(
        publish_handler.ManifestCommitConflictError,
        match="different pipeline execution",
    ):
        publish_handler._commit_manifest(DIST_BUCKET, staged_manifest, old_etag)

    stored_manifest = s3.get_object(Bucket=DIST_BUCKET, Key="latest/manifest.json")
    assert json.loads(stored_manifest["Body"].read()) == newer_manifest


def test_manifest_compare_and_swap_accepts_identical_committed_run(
    aws_env: None,
) -> None:
    from medical_access_lod.functions.publish import handler as publish_handler

    s3 = boto3.client("s3", region_name="ap-northeast-1")
    s3.put_object(Bucket=DIST_BUCKET, Key="latest/manifest.json", Body=b'{"run_id":"old"}')
    _, old_etag = publish_handler._read_manifest_state(DIST_BUCKET)
    staged_manifest = {
        "schema_version": 1,
        "run_id": "same-run",
        "snapshot_date": "2025-12-01",
        "artifacts": {
            "turtle": {
                "key": "releases/same/data.ttl",
                "size": 3,
                "etag": "ttl-etag",
                "content_type": "text/turtle; charset=utf-8",
            },
            "jsonld": {
                "key": "releases/same/data.jsonld",
                "size": 4,
                "etag": "jsonld-etag",
                "content_type": "application/ld+json",
            },
        },
    }
    # 同じrunの別Lambdaが先に全artifactをcommitした状態を作る。
    s3.put_object(
        Bucket=DIST_BUCKET,
        Key="latest/manifest.json",
        Body=json.dumps(staged_manifest, sort_keys=True).encode(),
    )

    published, committed = publish_handler._commit_manifest(
        DIST_BUCKET,
        staged_manifest,
        old_etag,
    )
    assert published == staged_manifest
    assert committed is False


def test_publish_copy_failure_keeps_previous_manifest_and_original_error(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medical_access_lod.functions.publish import handler as publish_handler

    _put_build_artifacts()
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    previous_manifest = {
        "schema_version": 1,
        "run_id": "previous-run",
        "snapshot_date": "2025-06-01",
        "artifacts": {
            "turtle": {"key": "releases/previous/data.ttl"},
            "jsonld": {"key": "releases/previous/data.jsonld"},
        },
    }
    s3.put_object(
        Bucket=DIST_BUCKET,
        Key="latest/manifest.json",
        Body=json.dumps(previous_manifest).encode(),
        ContentType="application/json",
    )
    s3.put_object(Bucket=DIST_BUCKET, Key="latest/data.ttl", Body=b"old-ttl")
    s3.put_object(Bucket=DIST_BUCKET, Key="latest/data.jsonld", Body=b"old-jsonld")

    monkeypatch.setattr(publish_handler.pipeline_lock, "renew", lambda _table, _owner: 1)
    release_calls: list[tuple[str, str]] = []

    def failing_release(table: str, owner: str) -> bool:
        release_calls.append((table, owner))
        raise RuntimeError("release failed")

    monkeypatch.setattr(publish_handler.pipeline_lock, "release", failing_release)
    original_copy = publish_handler._copy
    copy_count = 0

    def fail_second_copy(
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        content_type: str,
    ) -> None:
        nonlocal copy_count
        copy_count += 1
        if copy_count == 2:
            raise RuntimeError("second artifact copy failed")
        original_copy(src_bucket, src_key, dst_bucket, dst_key, content_type)

    monkeypatch.setattr(publish_handler, "_copy", fail_second_copy)

    with pytest.raises(RuntimeError, match="second artifact copy failed"):
        publish_handler.lambda_handler(
            _publish_event("failed-run"),
            _FakeLambdaContext(),  # type: ignore[arg-type]
        )

    stored_manifest = s3.get_object(Bucket=DIST_BUCKET, Key="latest/manifest.json")
    assert json.loads(stored_manifest["Body"].read()) == previous_manifest
    assert s3.get_object(Bucket=DIST_BUCKET, Key="latest/data.ttl")["Body"].read() == b"old-ttl"
    assert (
        s3.get_object(Bucket=DIST_BUCKET, Key="latest/data.jsonld")["Body"].read()
        == b"old-jsonld"
    )
    # release失敗で元のcopy例外を上書きせず、必ずreleaseを試みる。
    assert release_calls == [("read-model", "failed-run")]


def test_delayed_publish_cannot_replace_manifest_after_lock_owner_changes(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medical_access_lod.functions.publish import handler as publish_handler

    _put_build_artifacts()
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    newer_manifest = {
        "schema_version": 1,
        "run_id": "newer-run",
        "snapshot_date": "2025-12-01",
        "artifacts": {},
    }
    s3.put_object(
        Bucket=DIST_BUCKET,
        Key="latest/manifest.json",
        Body=json.dumps(newer_manifest).encode(),
    )

    renew_count = 0

    def owner_changes_before_commit(_table: str, _owner: str) -> int:
        nonlocal renew_count
        renew_count += 1
        if renew_count == 2:
            raise publish_handler.pipeline_lock.PipelineLockConflictError(
                "lock is owned by newer-run"
            )
        return 1

    monkeypatch.setattr(publish_handler.pipeline_lock, "renew", owner_changes_before_commit)
    monkeypatch.setattr(publish_handler.pipeline_lock, "release", lambda _table, _owner: True)

    with pytest.raises(
        publish_handler.pipeline_lock.PipelineLockConflictError,
        match="newer-run",
    ):
        publish_handler.lambda_handler(
            _publish_event("delayed-run"),
            _FakeLambdaContext(),  # type: ignore[arg-type]
        )

    stored_manifest = s3.get_object(Bucket=DIST_BUCKET, Key="latest/manifest.json")
    assert json.loads(stored_manifest["Body"].read()) == newer_manifest


def test_publish_without_lock_is_idempotent_only_for_committed_run(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medical_access_lod.functions.publish import handler as publish_handler

    s3 = boto3.client("s3", region_name="ap-northeast-1")
    completed_manifest = {
        "schema_version": 1,
        "run_id": "completed-run",
        "snapshot_date": "2025-12-01",
        "artifacts": {
            "turtle": {
                "key": "releases/2025-12-01/completed-run/medical-access-lod.ttl",
                "size": 3,
                "etag": "ttl-etag",
                "content_type": "text/turtle; charset=utf-8",
            },
            "jsonld": {
                "key": "releases/2025-12-01/completed-run/medical-access-lod.jsonld",
                "size": 4,
                "etag": "jsonld-etag",
                "content_type": "application/ld+json",
            },
        },
    }
    s3.put_object(
        Bucket=DIST_BUCKET,
        Key="latest/manifest.json",
        Body=json.dumps(completed_manifest).encode(),
    )

    def missing_lock(_table: str, _owner: str) -> int:
        raise publish_handler.pipeline_lock.PipelineLockMissingError("lock is missing")

    monkeypatch.setattr(publish_handler.pipeline_lock, "renew", missing_lock)
    monkeypatch.setattr(publish_handler.pipeline_lock, "release", lambda _table, _owner: True)
    # idempotent 復帰でも catalog は mark_committed される (test_publish_idempotent_retry_
    # still_marks_catalog_committed が意味を検証)。ここでは実 DDB を触らないよう mock。
    monkeypatch.setattr(
        publish_handler.generation_catalog,
        "mark_committed",
        lambda _table, _run_id: 1,
    )

    def unexpected_copy(*_args: str) -> None:
        raise AssertionError("completed retry must not copy artifacts")

    monkeypatch.setattr(publish_handler, "_copy", unexpected_copy)
    response = publish_handler.lambda_handler(
        _publish_event("completed-run"),
        _FakeLambdaContext(),  # type: ignore[arg-type]
    )
    assert response["published_files"] == [
        "releases/2025-12-01/completed-run/medical-access-lod.ttl",
        "releases/2025-12-01/completed-run/medical-access-lod.jsonld",
        "latest/manifest.json",
    ]


def test_publish_idempotent_retry_still_marks_catalog_committed(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """manifest CAS 成功後に catalog 更新だけ落ちた場合、次回の idempotent 復帰でも
    mark_committed を必ず呼ぶ。呼ばないと公開済み世代が catalog 上は STAGED のまま
    残り、Cleanup Lambda がこの世代を GC 対象外として永久に残す。"""

    from medical_access_lod.functions.publish import handler as publish_handler

    s3 = boto3.client("s3", region_name="ap-northeast-1")
    completed_manifest = {
        "schema_version": 1,
        "run_id": "resumed-run",
        "snapshot_date": "2025-12-01",
        "artifacts": {
            "turtle": {
                "key": "releases/2025-12-01/resumed-run/medical-access-lod.ttl",
                "size": 3,
                "etag": "ttl-etag",
                "content_type": "text/turtle; charset=utf-8",
            },
            "jsonld": {
                "key": "releases/2025-12-01/resumed-run/medical-access-lod.jsonld",
                "size": 4,
                "etag": "jsonld-etag",
                "content_type": "application/ld+json",
            },
        },
    }
    s3.put_object(
        Bucket=DIST_BUCKET,
        Key="latest/manifest.json",
        Body=json.dumps(completed_manifest).encode(),
    )

    def missing_lock(_table: str, _owner: str) -> int:
        raise publish_handler.pipeline_lock.PipelineLockMissingError("lock is missing")

    monkeypatch.setattr(publish_handler.pipeline_lock, "renew", missing_lock)
    monkeypatch.setattr(publish_handler.pipeline_lock, "release", lambda _t, _o: True)

    mark_committed_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        publish_handler.generation_catalog,
        "mark_committed",
        lambda table, run_id: mark_committed_calls.append((table, run_id)) or 1,
    )

    publish_handler.lambda_handler(
        _publish_event("resumed-run"),
        _FakeLambdaContext(),  # type: ignore[arg-type]
    )
    assert mark_committed_calls == [("read-model", "resumed-run")]


def test_build_read_model_failure_is_not_masked_when_lock_release_fails(
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medical_access_lod.functions.build_read_model import handler as read_model_handler

    monkeypatch.setattr(
        read_model_handler,
        "acquire_pipeline_lock",
        lambda _table, _owner: 1,
    )

    def fail_read(_bucket: str, _key: str) -> dict[str, object]:
        raise ValueError("normalized payload is invalid")

    def fail_release(_table: str, _owner: str) -> bool:
        raise RuntimeError("DynamoDB release failed")

    monkeypatch.setattr(read_model_handler, "get_json", fail_read)
    monkeypatch.setattr(read_model_handler, "release_pipeline_lock", fail_release)

    with pytest.raises(ValueError, match="normalized payload is invalid"):
        read_model_handler.lambda_handler(
            {
                "run_id": "failed-read-model",
                "normalized_bucket": NORM_BUCKET,
                "normalized_key": "normalized/invalid.json",
                "read_model_table": "read-model",
                "build_bucket": BUILD_BUCKET,
                "snapshot_date": "2025-12-01",
            },
            _FakeLambdaContext(),  # type: ignore[arg-type]
        )


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
            "build_bucket": BUILD_BUCKET,
            "snapshot_date": "2025-12-01",
        },
        _FakeLambdaContext(),  # type: ignore[arg-type]
    )
    assert response["items_written"] == 3
    assert response["lock_owner"] == "test-006"
    assert isinstance(response["lock_expires_at"], int)
    assert response["inventory_prefix"] == "generations/test-006/inventory/"
    assert response["inventory_bucket"] == BUILD_BUCKET
    assert response["inventory_chunks"] == 1

    # inventory chunk + MANIFEST が S3 に存在
    s3_check = boto3.client("s3", region_name="ap-northeast-1")
    inventory_objs = s3_check.list_objects_v2(
        Bucket=BUILD_BUCKET, Prefix="generations/test-006/inventory/"
    ).get("Contents", [])
    inventory_keys = sorted(obj["Key"] for obj in inventory_objs)
    assert inventory_keys == [
        "generations/test-006/inventory/MANIFEST.json",
        "generations/test-006/inventory/chunk-000000.jsonl.gz",
    ]

    # generation catalog に STAGED で登録されていること
    from medical_access_lod.functions.shared import generation_catalog
    entry = generation_catalog.get(table_name, "test-006")
    assert entry is not None
    assert entry["status"] == "STAGED"
    assert entry["snapshot_date"] == "2025-12-01"
    assert entry["inventory_prefix"] == "generations/test-006/inventory/"
    assert int(entry["item_count"]) == 3

    table = ddb.Table(table_name)
    data_items = [
        item for item in table.scan()["Items"] if item["PK"].startswith("GENERATION#")
    ]
    assert {(item["PK"], item["SK"]) for item in data_items} == {
        ("GENERATION#test-006#FACILITY#F1", "METADATA"),
        ("GENERATION#test-006#FACILITY#F1", "SERVICE#01"),
        ("GENERATION#test-006#FACILITY#F1", "SCHEDULE#01#Monday#09:00:00"),
    }


def test_build_read_model_keeps_previous_generation_until_manifest_switch(
    aws_env: None,
) -> None:
    """公開manifest切替前に現行世代を壊さないよう、新旧世代が共存する。"""
    from medical_access_lod.functions.build_read_model.handler import lambda_handler

    table_name = "medical-access-lod-test-read-model-stale"
    ddb = boto3.resource("dynamodb", region_name="ap-northeast-1")
    table = ddb.create_table(
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
    # 上流で削除された想定の "旧世代" レコードを事前投入
    table.put_item(
        Item={
            "PK": "GENERATION#old-run#FACILITY#DELETED",
            "SK": "METADATA",
            "generation": "old-run",
            "facility_id": "DELETED",
            "name": "廃業した診療所",
        }
    )
    table.put_item(
        Item={
            "PK": "GENERATION#old-run#FACILITY#F1",
            "SK": "SERVICE#99",
            "generation": "old-run",
            "specialty_code": "99",
        }
    )

    payload = {
        "facilities": [
            {
                "facility_id": "F1",
                "name": "存続する病院",
                "facility_type": "hospital",
                "address": {
                    "prefecture": "千葉県",
                    "city": "千葉市中央区",
                    "street_address": "1-1-1",
                },
            }
        ],
        "services": [{"facility_id": "F1", "specialty_code": "01"}],
        "schedules": [],
        "specialty_labels": {"01": "内科"},
    }
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    s3.put_object(
        Bucket=NORM_BUCKET,
        Key="normalized/stale-test.json",
        Body=json.dumps(payload).encode("utf-8"),
    )

    response = lambda_handler(
        {
            "run_id": "new-run-001",
            "normalized_bucket": NORM_BUCKET,
            "normalized_key": "normalized/stale-test.json",
            "read_model_table": table_name,
            "build_bucket": BUILD_BUCKET,
            "snapshot_date": "2025-12-01",
        },
        _FakeLambdaContext(),  # type: ignore[arg-type]
    )

    assert response["items_written"] == 2
    assert response["lock_owner"] == "new-run-001"
    assert isinstance(response["lock_expires_at"], int)

    # lockアイテムを除くと、旧世代2件と新世代2件が共存する。
    scan = table.scan()
    surviving = {
        (item["PK"], item["SK"])
        for item in scan["Items"]
        if item["PK"].startswith("GENERATION#")
    }
    assert surviving == {
        ("GENERATION#old-run#FACILITY#DELETED", "METADATA"),
        ("GENERATION#old-run#FACILITY#F1", "SERVICE#99"),
        ("GENERATION#new-run-001#FACILITY#F1", "METADATA"),
        ("GENERATION#new-run-001#FACILITY#F1", "SERVICE#01"),
    }


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
