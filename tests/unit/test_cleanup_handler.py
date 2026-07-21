"""Cleanup Lambda ハンドラの moto ベース統合テスト。"""
from __future__ import annotations

import json
from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from medical_access_lod.functions.shared import generation_catalog, generation_inventory

TABLE = "medical-access-lod-test-cleanup"
BUILD_BUCKET = "medical-access-lod-test-cleanup-build"
DIST_BUCKET = "medical-access-lod-test-cleanup-dist"
REGION = "ap-northeast-1"


class _FakeLambdaContext:
    function_name = "medical-access-lod-cleanup"
    function_version = "$LATEST"
    invoked_function_arn = (
        f"arn:aws:lambda:{REGION}:111111111111:function:medical-access-lod-cleanup"
    )
    memory_limit_in_mb = 512
    aws_request_id = "cleanup-req"
    log_group_name = "/aws/lambda/medical-access-lod-cleanup"
    log_stream_name = "cleanup"


@pytest.fixture
def cleanup_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("POWERTOOLS_METRICS_NAMESPACE", "MedicalAccessLOD")
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "medical-access-lod-cleanup-test")
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (BUILD_BUCKET, DIST_BUCKET):
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
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


def _put_manifest(active_run_id: str) -> None:
    s3 = boto3.client("s3", region_name=REGION)
    s3.put_object(
        Bucket=DIST_BUCKET,
        Key="latest/manifest.json",
        Body=json.dumps({"schema_version": 1, "run_id": active_run_id}).encode(),
        ContentType="application/json",
    )


def _seed_generation(run_id: str, keys: list[tuple[str, str]]) -> None:
    """DDB に世代データを書き、inventory も S3 に置き、catalog に COMMITTED 登録する。"""
    ddb_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    with ddb_table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for pk, sk in keys:
            batch.put_item(Item={"PK": pk, "SK": sk, "generation": run_id})
    prefix = f"generations/{run_id}/inventory/"
    generation_inventory.write_inventory(BUILD_BUCKET, prefix, iter(keys), run_id=run_id)
    generation_catalog.register_staged(
        TABLE,
        run_id,
        snapshot_date="2025-12-01",
        inventory_prefix=prefix,
        item_count=len(keys),
    )
    generation_catalog.mark_committed(TABLE, run_id)


def _generation_keys(run_id: str, n: int) -> list[tuple[str, str]]:
    return [(f"GENERATION#{run_id}#FACILITY#F{i}", "METADATA") for i in range(n)]


def _invoke(event: dict[str, object]) -> dict[str, object]:
    from medical_access_lod.functions.cleanup.handler import lambda_handler
    return lambda_handler(event, _FakeLambdaContext())  # type: ignore[arg-type]


def _basic_event(trigger_run_id: str) -> dict[str, object]:
    return {
        "trigger_run_id": trigger_run_id,
        "read_model_table": TABLE,
        "inventory_bucket": BUILD_BUCKET,
        "dist_bucket": DIST_BUCKET,
    }


def test_cleanup_never_deletes_active_generation(cleanup_env: None) -> None:
    _seed_generation("active-run", _generation_keys("active-run", 3))
    _put_manifest("active-run")

    result = _invoke(_basic_event("active-run"))
    assert result["deleted_generations"] == 0

    entry = generation_catalog.get(TABLE, "active-run")
    assert entry is not None and entry["status"] == "COMMITTED"

    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    scan = table.scan(FilterExpression="begins_with(PK, :p)",
                      ExpressionAttributeValues={":p": "GENERATION#active-run#"})
    assert scan["Count"] == 3


def test_cleanup_never_runs_when_manifest_missing(cleanup_env: None) -> None:
    """manifest が未commit のときは削除しない (安全側倒し)."""
    _seed_generation("old-run", _generation_keys("old-run", 2))

    result = _invoke(_basic_event("trigger"))
    assert result["deleted_generations"] == 0
    entry = generation_catalog.get(TABLE, "old-run")
    assert entry is not None and entry["status"] == "COMMITTED"


def test_cleanup_deletes_old_generations_beyond_retention(
    cleanup_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keep_last_n=1 (テスト用) の状態で古い世代を削除する。"""
    _seed_generation("old-run", _generation_keys("old-run", 4))
    _seed_generation("active-run", _generation_keys("active-run", 3))
    _put_manifest("active-run")

    # 本番デフォルトは keep_last_n=6 / min_age=365 日。env 経由で緩める。
    monkeypatch.setenv("RETENTION_KEEP_LAST_N", "1")
    monkeypatch.setenv("RETENTION_MIN_AGE_DAYS", "0")

    result = _invoke(_basic_event("active-run"))
    assert result["deleted_generations"] == 1
    assert result["deleted_run_ids"] == ["old-run"]
    assert result["deleted_items"] == 4

    # 旧世代の catalog は DELETED tombstone
    entry = generation_catalog.get(TABLE, "old-run")
    assert entry is not None and entry["status"] == "DELETED"

    # 旧世代の実データは全て消えている
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    remaining_old = table.scan(
        FilterExpression="begins_with(PK, :p)",
        ExpressionAttributeValues={":p": "GENERATION#old-run#"},
    )
    assert remaining_old["Count"] == 0

    # active 世代は無傷
    remaining_active = table.scan(
        FilterExpression="begins_with(PK, :p)",
        ExpressionAttributeValues={":p": "GENERATION#active-run#"},
    )
    assert remaining_active["Count"] == 3


def test_cleanup_accepts_sqs_records(cleanup_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """SQS メッセージ (Records) 形式でも動く。"""
    _seed_generation("gen-A", _generation_keys("gen-A", 1))
    _put_manifest("gen-A")
    monkeypatch.setenv("RETENTION_KEEP_LAST_N", "1")
    monkeypatch.setenv("RETENTION_MIN_AGE_DAYS", "0")

    sqs_event = {
        "Records": [
            {"body": json.dumps(_basic_event("gen-A"))},
        ]
    }
    result = _invoke(sqs_event)
    assert result["active_run_id"] == "gen-A"
    assert result["deleted_generations"] == 0


def test_cleanup_resumes_interrupted_deleting_generation(cleanup_env: None) -> None:
    """途中で落ちて DELETING で残った世代を、retention に関係なく再削除する。"""
    _seed_generation("crashed-run", _generation_keys("crashed-run", 2))
    _seed_generation("active-run", _generation_keys("active-run", 1))
    _put_manifest("active-run")
    generation_catalog.mark_deleting(TABLE, "crashed-run")

    result = _invoke(_basic_event("active-run"))
    assert "crashed-run" in result["deleted_run_ids"]  # type: ignore[operator]
    entry = generation_catalog.get(TABLE, "crashed-run")
    assert entry is not None and entry["status"] == "DELETED"


class _TimedContext(_FakeLambdaContext):
    """get_remaining_time_in_millis を返すコンテキスト。呼ばれるたびに値が減る。"""

    def __init__(self, initial_ms: int, decrement_per_call_ms: int = 0) -> None:
        self._remaining = initial_ms
        self._decrement = decrement_per_call_ms

    def get_remaining_time_in_millis(self) -> int:
        value = self._remaining
        self._remaining -= self._decrement
        return value


def test_cleanup_exits_early_when_lambda_time_budget_runs_out(
    cleanup_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lambda 残時間が閾値を下回ったら chunk 途中でも早期終了し、
    cursor を DynamoDB に残す。次の SQS 配信で続きから再開できる。"""
    from medical_access_lod.functions.cleanup import handler as cleanup_handler

    # 6 chunk 分 (chunk_size=1) の inventory を作る。1 chunk 消化ごとに残時間確認。
    keys = _generation_keys("big-run", 6)
    ddb_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    with ddb_table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for pk, sk in keys:
            batch.put_item(Item={"PK": pk, "SK": sk, "generation": "big-run"})
    from medical_access_lod.functions.shared import generation_inventory
    prefix = "generations/big-run/inventory/"
    generation_inventory.write_inventory(
        BUILD_BUCKET, prefix, iter(keys), run_id="big-run", chunk_size=1
    )
    generation_catalog.register_staged(
        TABLE, "big-run", snapshot_date="2025-12-01",
        inventory_prefix=prefix, item_count=6,
    )
    generation_catalog.mark_committed(TABLE, "big-run")

    _seed_generation("active-run", _generation_keys("active-run", 1))
    _put_manifest("active-run")
    monkeypatch.setenv("RETENTION_KEEP_LAST_N", "1")
    monkeypatch.setenv("RETENTION_MIN_AGE_DAYS", "0")

    # 3 chunk 分だけ処理できる時間を渡す (初回 90秒 → 呼ぶたびに 30秒減少)。
    # 閾値 30_000ms を切ったら早期終了する。
    ctx = _TimedContext(initial_ms=90_000, decrement_per_call_ms=30_000)
    result = cleanup_handler.lambda_handler(_basic_event("active-run"), ctx)  # type: ignore[arg-type]

    # 完了せず incomplete で返り、catalog は DELETING のまま cursor が進んでいる
    assert "big-run" not in result["deleted_run_ids"]  # type: ignore[operator]
    assert "big-run" in result["incomplete_run_ids"]  # type: ignore[operator]
    entry = generation_catalog.get(TABLE, "big-run")
    assert entry is not None
    assert entry["status"] == "DELETING"
    cursor = int(entry.get("deletion_cursor", 0))
    assert 0 < cursor < 6

    # 続き: 十分な残時間で再走すると cursor から再開して完了する
    ctx2 = _TimedContext(initial_ms=600_000)
    result2 = cleanup_handler.lambda_handler(_basic_event("active-run"), ctx2)  # type: ignore[arg-type]
    assert "big-run" in result2["deleted_run_ids"]  # type: ignore[operator]
    entry2 = generation_catalog.get(TABLE, "big-run")
    assert entry2 is not None and entry2["status"] == "DELETED"

    # 実データが全て消えていること
    scan = ddb_table.scan(
        FilterExpression="begins_with(PK, :p)",
        ExpressionAttributeValues={":p": "GENERATION#big-run#"},
    )
    assert scan["Count"] == 0


def test_cleanup_refuses_when_inventory_count_disagrees_with_catalog(
    cleanup_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """catalog は item_count>0 だが inventory の item_count が食い違う場合、
    削除に進まず catalog も DELETED にしない (残骸を隠さない)。"""
    from medical_access_lod.functions.shared import generation_inventory

    _seed_generation("corrupt-run", _generation_keys("corrupt-run", 3))
    _put_manifest("active-run")
    _seed_generation("active-run", _generation_keys("active-run", 1))

    # inventory を上書きして item_count を故意に 0 に。実データ (3件) は残す。
    prefix = "generations/corrupt-run/inventory/"
    generation_inventory.write_inventory(
        BUILD_BUCKET, prefix, iter([]), run_id="corrupt-run"
    )

    monkeypatch.setenv("RETENTION_KEEP_LAST_N", "1")
    monkeypatch.setenv("RETENTION_MIN_AGE_DAYS", "0")

    with pytest.raises(generation_inventory.InventoryValidationError):
        _invoke(_basic_event("active-run"))

    # catalog は DELETING のまま (DELETED tombstone は付かない)
    entry = generation_catalog.get(TABLE, "corrupt-run")
    assert entry is not None
    assert entry["status"] == "DELETING"

    # 実データも残っている
    ddb_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    scan = ddb_table.scan(
        FilterExpression="begins_with(PK, :p)",
        ExpressionAttributeValues={":p": "GENERATION#corrupt-run#"},
    )
    assert scan["Count"] == 3
