from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from medical_access_lod.functions.shared import pipeline_lock

TABLE_NAME = "medical-access-lod-test-pipeline-lock"


@pytest.fixture
def lock_table(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="ap-northeast-1").create_table(
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
        yield table


def _read_lock(table: object) -> dict[str, object] | None:
    response = table.get_item(  # type: ignore[attr-defined]
        Key={"PK": pipeline_lock.LOCK_PK, "SK": pipeline_lock.LOCK_SK},
        ConsistentRead=True,
    )
    return response.get("Item")


def test_acquire_allows_absent_lock_and_same_owner_reentry(
    lock_table: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_lock.time, "time", lambda: 1_000)
    assert pipeline_lock.acquire_pipeline_lock(TABLE_NAME, "run-A", lease=100) == 1_100

    monkeypatch.setattr(pipeline_lock.time, "time", lambda: 1_050)
    assert pipeline_lock.acquire_pipeline_lock(TABLE_NAME, "run-A", lease=100) == 1_150
    assert _read_lock(lock_table) == {
        "PK": pipeline_lock.LOCK_PK,
        "SK": pipeline_lock.LOCK_SK,
        "owner": "run-A",
        "expires_at": 1_150,
    }


def test_acquire_rejects_another_owner_while_lease_is_valid(
    lock_table: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_lock.time, "time", lambda: 2_000)
    pipeline_lock.acquire_pipeline_lock(TABLE_NAME, "run-A", lease=100)

    monkeypatch.setattr(pipeline_lock.time, "time", lambda: 2_050)
    with pytest.raises(pipeline_lock.PipelineLockConflictError, match="run-A"):
        pipeline_lock.acquire_pipeline_lock(TABLE_NAME, "run-B", lease=100)
    assert _read_lock(lock_table)["owner"] == "run-A"  # type: ignore[index]


def test_acquire_allows_another_owner_after_expiration(
    lock_table: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_lock.time, "time", lambda: 3_000)
    pipeline_lock.acquire_pipeline_lock(TABLE_NAME, "run-A", lease=10)

    monkeypatch.setattr(pipeline_lock.time, "time", lambda: 3_010)
    assert pipeline_lock.acquire_pipeline_lock(TABLE_NAME, "run-B", lease=20) == 3_030
    assert _read_lock(lock_table)["owner"] == "run-B"  # type: ignore[index]


def test_renew_extends_only_the_current_owners_valid_lock(
    lock_table: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_lock.time, "time", lambda: 3_500)
    pipeline_lock.acquire_pipeline_lock(TABLE_NAME, "run-A", lease=100)

    monkeypatch.setattr(pipeline_lock.time, "time", lambda: 3_550)
    assert pipeline_lock.renew(TABLE_NAME, "run-A", lease=200) == 3_750
    assert _read_lock(lock_table)["expires_at"] == 3_750  # type: ignore[index]

    with pytest.raises(pipeline_lock.PipelineLockConflictError, match="run-A"):
        pipeline_lock.renew(TABLE_NAME, "run-B", lease=200)
    assert _read_lock(lock_table)["owner"] == "run-A"  # type: ignore[index]


def test_renew_rejects_expired_or_missing_lock(
    lock_table: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_lock.time, "time", lambda: 3_800)
    pipeline_lock.acquire_pipeline_lock(TABLE_NAME, "run-A", lease=10)

    monkeypatch.setattr(pipeline_lock.time, "time", lambda: 3_810)
    with pytest.raises(pipeline_lock.PipelineLockConflictError, match="expired"):
        pipeline_lock.renew(TABLE_NAME, "run-A")

    pipeline_lock.release(TABLE_NAME, "run-A")
    with pytest.raises(pipeline_lock.PipelineLockMissingError, match="missing"):
        pipeline_lock.renew(TABLE_NAME, "run-A")


def test_release_is_owner_conditional_and_does_not_delete_another_owner(
    lock_table: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_lock.time, "time", lambda: 4_000)
    pipeline_lock.acquire_pipeline_lock(TABLE_NAME, "run-A")

    with pytest.raises(pipeline_lock.PipelineLockOwnershipError, match="run-A"):
        pipeline_lock.release(TABLE_NAME, "run-B")
    assert _read_lock(lock_table)["owner"] == "run-A"  # type: ignore[index]

    pipeline_lock.release(TABLE_NAME, "run-A")
    assert _read_lock(lock_table) is None
    pipeline_lock.release(TABLE_NAME, "run-A")


@pytest.mark.parametrize(("owner", "lease"), [("", 10), ("run-A", 0), ("run-A", -1)])
def test_acquire_rejects_invalid_arguments(
    lock_table: object,
    owner: str,
    lease: int,
) -> None:
    with pytest.raises(ValueError):
        pipeline_lock.acquire_pipeline_lock(TABLE_NAME, owner, lease)
