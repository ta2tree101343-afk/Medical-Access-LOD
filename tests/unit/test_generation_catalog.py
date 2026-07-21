"""generation_catalog モジュールの状態遷移テスト。"""
from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from medical_access_lod.functions.shared import generation_catalog

TABLE_NAME = "medical-access-lod-test-catalog"
REGION = "ap-northeast-1"


@pytest.fixture
def catalog_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE_NAME,
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
        yield TABLE_NAME


def _register(table: str, run_id: str, item_count: int = 42) -> None:
    generation_catalog.register_staged(
        table,
        run_id,
        snapshot_date="2025-12-01",
        inventory_prefix=f"generations/{run_id}/inventory/",
        item_count=item_count,
    )


def test_register_staged_records_all_metadata(catalog_env: str) -> None:
    _register(catalog_env, "run-A", item_count=1234)
    entry = generation_catalog.get(catalog_env, "run-A")
    assert entry is not None
    assert entry["status"] == "STAGED"
    assert entry["run_id"] == "run-A"
    assert entry["snapshot_date"] == "2025-12-01"
    assert entry["inventory_prefix"] == "generations/run-A/inventory/"
    assert entry["item_count"] == 1234
    # DynamoDB は数値属性を Decimal で返すため、int() で正規化して型を確認する
    assert int(entry["staged_at"]) > 0


def test_register_staged_is_idempotent_for_same_run(catalog_env: str) -> None:
    _register(catalog_env, "run-A", item_count=10)
    _register(catalog_env, "run-A", item_count=20)  # 再試行
    entry = generation_catalog.get(catalog_env, "run-A")
    assert entry is not None
    assert entry["item_count"] == 20


def test_register_staged_rejects_reregister_after_committed(catalog_env: str) -> None:
    _register(catalog_env, "run-A")
    generation_catalog.mark_committed(catalog_env, "run-A")
    with pytest.raises(generation_catalog.GenerationCatalogConflictError):
        _register(catalog_env, "run-A")


def test_mark_committed_transitions_and_records_timestamp(catalog_env: str) -> None:
    _register(catalog_env, "run-A")
    committed_at = generation_catalog.mark_committed(catalog_env, "run-A")
    entry = generation_catalog.get(catalog_env, "run-A")
    assert entry is not None
    assert entry["status"] == "COMMITTED"
    assert entry["committed_at"] == committed_at


def test_mark_committed_is_idempotent_and_preserves_original_timestamp(
    catalog_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(catalog_env, "run-A")
    monkeypatch.setattr(generation_catalog.time, "time", lambda: 1_000_000)
    first = generation_catalog.mark_committed(catalog_env, "run-A")
    monkeypatch.setattr(generation_catalog.time, "time", lambda: 2_000_000)
    second = generation_catalog.mark_committed(catalog_env, "run-A")
    assert first == second == 1_000_000


def test_mark_committed_raises_missing_when_not_registered(catalog_env: str) -> None:
    with pytest.raises(generation_catalog.GenerationCatalogMissingError):
        generation_catalog.mark_committed(catalog_env, "unknown-run")


def test_get_returns_none_for_unknown_run(catalog_env: str) -> None:
    assert generation_catalog.get(catalog_env, "does-not-exist") is None


def test_register_staged_rejects_empty_run_id(catalog_env: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        generation_catalog.register_staged(
            catalog_env,
            "",
            snapshot_date="2025-12-01",
            inventory_prefix="generations//inventory/",
            item_count=0,
        )


def test_mark_committed_rejects_empty_run_id(catalog_env: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        generation_catalog.mark_committed(catalog_env, "")


def test_mark_deleting_transitions_committed_to_deleting(catalog_env: str) -> None:
    _register(catalog_env, "run-A")
    generation_catalog.mark_committed(catalog_env, "run-A")
    generation_catalog.mark_deleting(catalog_env, "run-A")
    entry = generation_catalog.get(catalog_env, "run-A")
    assert entry is not None
    assert entry["status"] == "DELETING"
    assert int(entry["deleting_at"]) > 0


def test_mark_deleting_is_idempotent(catalog_env: str) -> None:
    _register(catalog_env, "run-A")
    generation_catalog.mark_committed(catalog_env, "run-A")
    generation_catalog.mark_deleting(catalog_env, "run-A")
    # 冪等: 再度呼んでも例外にならない
    generation_catalog.mark_deleting(catalog_env, "run-A")


def test_mark_deleting_rejects_staged(catalog_env: str) -> None:
    """未完了 (STAGED) の世代を Cleanup 対象にしてはならない。"""
    _register(catalog_env, "run-A")
    with pytest.raises(generation_catalog.GenerationCatalogConflictError):
        generation_catalog.mark_deleting(catalog_env, "run-A")


def test_mark_deleting_raises_missing_when_not_registered(catalog_env: str) -> None:
    with pytest.raises(generation_catalog.GenerationCatalogMissingError):
        generation_catalog.mark_deleting(catalog_env, "unknown")


def test_mark_deleted_transitions_deleting_to_deleted(catalog_env: str) -> None:
    _register(catalog_env, "run-A")
    generation_catalog.mark_committed(catalog_env, "run-A")
    generation_catalog.mark_deleting(catalog_env, "run-A")
    generation_catalog.mark_deleted(catalog_env, "run-A")
    entry = generation_catalog.get(catalog_env, "run-A")
    assert entry is not None
    assert entry["status"] == "DELETED"
    assert int(entry["deleted_at"]) > 0


def test_mark_deleted_rejects_committed_without_intermediate_deleting(catalog_env: str) -> None:
    """COMMITTED を直接 DELETED にはできない (DELETING 経由必須)."""
    _register(catalog_env, "run-A")
    generation_catalog.mark_committed(catalog_env, "run-A")
    with pytest.raises(generation_catalog.GenerationCatalogConflictError):
        generation_catalog.mark_deleted(catalog_env, "run-A")


def test_update_deletion_cursor_persists_progress_only_when_deleting(catalog_env: str) -> None:
    _register(catalog_env, "run-A")
    generation_catalog.mark_committed(catalog_env, "run-A")
    generation_catalog.mark_deleting(catalog_env, "run-A")

    generation_catalog.update_deletion_cursor(catalog_env, "run-A", 3)
    entry = generation_catalog.get(catalog_env, "run-A")
    assert entry is not None
    assert int(entry["deletion_cursor"]) == 3

    generation_catalog.update_deletion_cursor(catalog_env, "run-A", 5)
    entry = generation_catalog.get(catalog_env, "run-A")
    assert entry is not None
    assert int(entry["deletion_cursor"]) == 5


def test_update_deletion_cursor_rejects_non_deleting_state(catalog_env: str) -> None:
    _register(catalog_env, "run-A")
    generation_catalog.mark_committed(catalog_env, "run-A")
    with pytest.raises(generation_catalog.GenerationCatalogConflictError):
        generation_catalog.update_deletion_cursor(catalog_env, "run-A", 1)


def test_update_deletion_cursor_rejects_negative(catalog_env: str) -> None:
    with pytest.raises(ValueError, match="cursor"):
        generation_catalog.update_deletion_cursor(catalog_env, "run-A", -1)


def test_list_by_status_returns_only_matching_entries(catalog_env: str) -> None:
    from medical_access_lod.functions.shared.generation_catalog import GenerationStatus
    _register(catalog_env, "run-staged")
    _register(catalog_env, "run-committed")
    generation_catalog.mark_committed(catalog_env, "run-committed")
    _register(catalog_env, "run-deleted")
    generation_catalog.mark_committed(catalog_env, "run-deleted")
    generation_catalog.mark_deleting(catalog_env, "run-deleted")
    generation_catalog.mark_deleted(catalog_env, "run-deleted")

    committed = generation_catalog.list_by_status(catalog_env, GenerationStatus.COMMITTED)
    assert [e["run_id"] for e in committed] == ["run-committed"]

    deleted = generation_catalog.list_by_status(catalog_env, GenerationStatus.DELETED)
    assert [e["run_id"] for e in deleted] == ["run-deleted"]

    staged = generation_catalog.list_by_status(catalog_env, GenerationStatus.STAGED)
    assert [e["run_id"] for e in staged] == ["run-staged"]
