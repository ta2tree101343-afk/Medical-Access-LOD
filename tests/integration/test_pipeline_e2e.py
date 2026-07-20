"""Step Functions ASL を実際に駆動する E2E テスト。

CDK が合成した pipeline-stack.ts の ASL 定義を Python の ASLSimulator で
実行し、Scheduler 入力 → InjectContext → Download → ... → Publish/ReadModel
の一連の payload マッピングが実 Lambda ハンドラで有効であることを検証する。
moto (S3 + DynamoDB モック) で完結し、実 AWS も実 Step Functions も使わない。
"""
from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from tests.integration.asl_simulator import (
    ASLSimulator,
    SynthesizedPipeline,
    synth_pipeline,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "mhlw_sample"
REGION = "ap-northeast-1"
EXPECTED_CONTEXT_PARAMETERS = {
    "raw_bucket": "medical-access-lod-dev-raw",
    "normalized_bucket": "medical-access-lod-dev-normalized",
    "build_bucket": "medical-access-lod-dev-build",
    "dist_bucket": "medical-access-lod-dev-dist",
    "read_model_table": "medical-access-lod-dev-read-model",
}


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
def aws_env(
    monkeypatch: pytest.MonkeyPatch,
    synthesized_pipeline: SynthesizedPipeline,
) -> Iterator[dict[str, str]]:
    resources = synthesized_pipeline.context_parameters
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("POWERTOOLS_METRICS_NAMESPACE", "MedicalAccessLOD")
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "medical-access-lod-e2e")
    monkeypatch.setenv("READ_MODEL_TABLE", resources["read_model_table"])
    monkeypatch.setenv("DIST_BUCKET", resources["dist_bucket"])
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (
            resources["raw_bucket"],
            resources["normalized_bucket"],
            resources["build_bucket"],
            resources["dist_bucket"],
        ):
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=resources["read_model_table"],
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
        yield resources


@pytest.fixture(scope="session")
def synthesized_pipeline() -> SynthesizedPipeline:
    """実Scheduler入力とCFN参照を含むPipelineを1回だけsynthして解決する。"""
    return synth_pipeline()


def test_synthesized_context_resolves_expected_physical_resources(
    synthesized_pipeline: SynthesizedPipeline,
) -> None:
    """ImportValueを含む各contextが、意図した物理リソースへ配線されている。"""
    assert synthesized_pipeline.context_parameters == EXPECTED_CONTEXT_PARAMETERS


def _build_handlers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    validate_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    """各 Lambda ハンドラを ASLSimulator 用に (event -> event) の形に包む。"""

    from medical_access_lod.application import download_source
    from medical_access_lod.functions.build_rdf import handler as build
    from medical_access_lod.functions.build_read_model import handler as brm
    from medical_access_lod.functions.download import handler as download
    from medical_access_lod.functions.normalize import handler as normalize
    from medical_access_lod.functions.publish import handler as publish
    from medical_access_lod.functions.validate import handler as validate

    payload = _make_zip_from_fixtures()
    monkeypatch.setattr(download_source, "HttpxClient", lambda: _StaticHttp(payload))
    ctx = _FakeLambdaContext()

    def _wrap(fn: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
        return lambda event: fn.lambda_handler(event, ctx)

    # キーはASL state名ではなく、合成LambdaのFUNCTION_KEY。Task Resourceが
    # 誤ったLambdaを指した場合はASLSimulatorが実際にそのhandlerを呼び、契約違反になる。
    handlers = {
        "Download": _wrap(download),
        "Normalize": _wrap(normalize),
        "BuildRdf": _wrap(build),
        "Validate": _wrap(validate),
        "Publish": _wrap(publish),
        "BuildReadModel": _wrap(brm),
    }

    if validate_hook is not None:
        validate_impl = handlers["Validate"]

        def _validate_with_hook(event: dict[str, Any]) -> dict[str, Any]:
            validate_hook(event)
            return validate_impl(event)

        handlers["Validate"] = _validate_with_hook

    return handlers


def test_asl_end_to_end_completes_and_publishes(
    aws_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    synthesized_pipeline: SynthesizedPipeline,
) -> None:
    """Scheduler 入力を InjectContext から流し、6 Lambda を ASL の順で全部通す。"""

    handlers = _build_handlers(monkeypatch)
    run_id = "e2e-run-001"
    sim = ASLSimulator(synthesized_pipeline.definition, execution_name=run_id)

    # 手書きの初期入力ではなく、合成されたScheduler Target.Inputをそのまま使う。
    final_state = sim.run(synthesized_pipeline.scheduler_input, handlers)

    # 各段の実行順を検証 (InjectContext → 4 タスク → Choice → ReadModel → Publish)
    task_names = [name for name, typ in sim.trace if typ == "Task"]
    assert task_names == [
        "DownloadTask",
        "NormalizeTask",
        "BuildRdfTask",
        "ValidateTask",
        "BuildReadModelTask",
        "PublishTask",
    ]
    assert sim.invocations == [
        ("DownloadTask", "Download"),
        ("NormalizeTask", "Normalize"),
        ("BuildRdfTask", "BuildRdf"),
        ("ValidateTask", "Validate"),
        ("BuildReadModelTask", "BuildReadModel"),
        ("PublishTask", "Publish"),
    ]
    assert synthesized_pipeline.task_function_keys == dict(sim.invocations)

    # 各 Lambda の戻り値が resultPath どおりに state に格納されていること
    assert final_state["download"]["files"], "download step yielded no files"
    assert final_state["normalize"]["facilities"] >= 1
    assert final_state["build_rdf"]["triples"] > 0
    assert final_state["validate"]["conforms"] is True
    assert final_state["read_model"]["items_written"] >= 3
    assert final_state["read_model"]["lock_owner"] == run_id
    assert final_state["read_model"]["lock_expires_at"] > 0

    # run_id が Execution.Name から派生していること
    assert final_state["run_id"] == run_id

    snapshot_date = synthesized_pipeline.scheduler_input["snapshot_date"]
    release_prefix = f"releases/{snapshot_date}/{quote(run_id, safe='-_.')}"
    expected_artifacts = {
        f"{release_prefix}/medical-access-lod.ttl",
        f"{release_prefix}/medical-access-lod.jsonld",
    }
    published = set(final_state["publish"]["published_files"])
    assert published == expected_artifacts | {"latest/manifest.json"}
    assert final_state["publish"]["manifest_key"] == "latest/manifest.json"

    # 2成果物が不変releaseに揃った後、単一manifestが公開世代をcommitしていること。
    s3 = boto3.client("s3", region_name=REGION)
    manifest_response = s3.get_object(
        Bucket=aws_env["dist_bucket"],
        Key="latest/manifest.json",
    )
    manifest = json.loads(manifest_response["Body"].read())
    assert manifest["schema_version"] == 1
    assert manifest["run_id"] == run_id
    assert manifest["snapshot_date"] == snapshot_date
    manifest_keys = {
        manifest["artifacts"]["turtle"]["key"],
        manifest["artifacts"]["jsonld"]["key"],
    }
    assert manifest_keys == expected_artifacts
    for artifact in manifest["artifacts"].values():
        head = s3.head_object(Bucket=aws_env["dist_bucket"], Key=artifact["key"])
        assert artifact["size"] == head["ContentLength"]
        assert artifact["etag"] == head["ETag"].strip('"')
        assert artifact["content_type"] == head["ContentType"]

    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(aws_env["read_model_table"])
    types: set[str] = set()
    data_items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, object] = {}
    while True:
        scan_response = table.scan(**scan_kwargs)
        page_items = [
            item
            for item in scan_response.get("Items", [])
            if item["PK"] != "SYSTEM#PIPELINE"
        ]
        data_items.extend(page_items)
        types.update(item["SK"].split("#", 1)[0] for item in page_items)
        key = scan_response.get("LastEvaluatedKey")
        if not key:
            break
        scan_kwargs["ExclusiveStartKey"] = key
    assert {"METADATA", "SERVICE", "SCHEDULE"}.issubset(types), types
    assert data_items
    assert all(item["PK"].startswith(f"GENERATION#{run_id}#") for item in data_items)

    # Publish完了後は所有していた期限付きlockが解放される。
    lock = table.get_item(Key={"PK": "SYSTEM#PIPELINE", "SK": "READ_MODEL_LOCK"})
    assert "Item" not in lock


def test_asl_halts_on_shacl_violation(
    aws_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    synthesized_pipeline: SynthesizedPipeline,
) -> None:
    """SHACL 違反時は Choice が Fail branch を選択し、Publish / ReadModel は呼ばれない。"""

    from rdflib import Graph, Namespace

    def _corrupt_ttl(event: dict[str, Any]) -> None:
        s3 = boto3.client("s3", region_name=REGION)
        original = s3.get_object(Bucket=event["build_bucket"], Key=event["ttl_key"])[
            "Body"
        ].read()
        ex = Namespace("https://example.org/medical-access/")
        graph = Graph().parse(data=original, format="turtle")
        graph.remove((None, ex.facilityId, None))
        broken = graph.serialize(format="turtle").encode("utf-8")
        s3.put_object(Bucket=event["build_bucket"], Key=event["ttl_key"], Body=broken)

    handlers = _build_handlers(monkeypatch, validate_hook=_corrupt_ttl)
    sim = ASLSimulator(synthesized_pipeline.definition, execution_name="e2e-run-002")

    with pytest.raises(RuntimeError, match=r"ShaclViolation|RDF validation failed"):
        sim.run(synthesized_pipeline.scheduler_input, handlers)

    # Publish / BuildReadModel は実行されていない (Choice が Fail branch へ)
    executed_tasks = [name for name, typ in sim.trace if typ == "Task"]
    assert "PublishTask" not in executed_tasks
    assert "BuildReadModelTask" not in executed_tasks

    # dist にも DDB にも何も書かれていない
    s3 = boto3.client("s3", region_name=REGION)
    dist_list = s3.list_objects_v2(Bucket=aws_env["dist_bucket"]).get("Contents", [])
    assert not dist_list, f"dist should be empty on SHACL failure: {dist_list}"

    ddb = boto3.resource("dynamodb", region_name=REGION)
    scan_response = ddb.Table(aws_env["read_model_table"]).scan(Limit=1)
    assert scan_response["Count"] == 0


def test_publish_failure_does_not_commit_manifest(
    aws_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    synthesized_pipeline: SynthesizedPipeline,
) -> None:
    """release配置後に失敗しても、公開世代のcommit pointは更新されない。"""

    from medical_access_lod.functions.publish import handler as publish

    handlers = _build_handlers(monkeypatch)

    def _fail_manifest(
        _bucket: str,
        _manifest: dict[str, Any],
        _expected_etag: str | None,
    ) -> None:
        raise RuntimeError("injected manifest failure")

    monkeypatch.setattr(publish, "_put_manifest", _fail_manifest)
    sim = ASLSimulator(synthesized_pipeline.definition, execution_name="e2e-run-failed")

    with pytest.raises(RuntimeError, match="injected manifest failure"):
        sim.run(synthesized_pipeline.scheduler_input, handlers)

    s3 = boto3.client("s3", region_name=REGION)
    with pytest.raises(ClientError) as exc_info:
        s3.head_object(Bucket=aws_env["dist_bucket"], Key="latest/manifest.json")
    assert exc_info.value.response["Error"]["Code"] in {"404", "NoSuchKey"}

    # 失敗時もfinallyでlockを解放し、後続executionを不要に待たせない。
    table = boto3.resource("dynamodb", region_name=REGION).Table(
        aws_env["read_model_table"]
    )
    lock = table.get_item(Key={"PK": "SYSTEM#PIPELINE", "SK": "READ_MODEL_LOCK"})
    assert "Item" not in lock
