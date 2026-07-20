"""読み取りモデル世代 (ReadModel generation) のカタログ管理。

カタログ項目
-----------
    PK = "SYSTEM#GENERATION"
    SK = "RUN#<run_id>"
    status = "STAGED" | "COMMITTED" | "DELETING" | "DELETED"
    snapshot_date = "YYYY-MM-DD"
    inventory_prefix = "generations/<run_id>/inventory/"
    item_count = <int>
    committed_at = <epoch int> (COMMITTED 以降のみ)

用途
----
- BuildReadModel が書き込み前に STAGED で登録し、inventory prefix と件数を保存
- Publish が manifest CAS commit 成功時に COMMITTED へ遷移し committed_at を記録
- Cleanup が保持ポリシーを満たす世代を DELETING → DELETED に段階遷移させる

権限分離
--------
- BuildReadModel: STAGED 書き込みのみ
- Publish: STAGED → COMMITTED の遷移のみ
- Cleanup: COMMITTED → DELETING → DELETED の遷移のみ (別 IAM Role)
"""
from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

import boto3
from botocore.exceptions import ClientError

CATALOG_PK = "SYSTEM#GENERATION"


class GenerationStatus(StrEnum):
    STAGED = "STAGED"
    COMMITTED = "COMMITTED"
    DELETING = "DELETING"
    DELETED = "DELETED"


class GenerationCatalogError(RuntimeError):
    """generation catalog 操作の失敗。"""


class GenerationCatalogConflictError(GenerationCatalogError):
    """状態遷移の前提条件が満たされていない (現在 status が期待と違う など)。"""


class GenerationCatalogMissingError(GenerationCatalogError):
    """対象世代のエントリが catalog に存在しない。"""


def _table(table: str) -> Any:
    return boto3.resource("dynamodb").Table(table)


def _sk(run_id: str) -> str:
    return f"RUN#{run_id}"


def register_staged(
    table: str,
    run_id: str,
    *,
    snapshot_date: str,
    inventory_prefix: str,
    item_count: int,
) -> None:
    """新規世代を STAGED で登録する。

    同一 run_id で既存エントリがあり、状態が STAGED でも冪等再入を許可する
    (BuildReadModel の Lambda 再試行に耐える)。STAGED 以外の状態で存在する
    場合は Conflict とする。
    """

    if not run_id:
        raise ValueError("run_id must not be empty")

    item = {
        "PK": CATALOG_PK,
        "SK": _sk(run_id),
        "status": GenerationStatus.STAGED.value,
        "run_id": run_id,
        "snapshot_date": snapshot_date,
        "inventory_prefix": inventory_prefix,
        "item_count": item_count,
        "staged_at": int(time.time()),
    }
    try:
        _table(table).put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(PK) OR #status = :staged"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":staged": GenerationStatus.STAGED.value},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        raise GenerationCatalogConflictError(
            f"cannot register generation {run_id!r} as STAGED (already in non-STAGED state)"
        ) from exc


def mark_committed(table: str, run_id: str) -> int:
    """STAGED 状態の世代を COMMITTED へ遷移させる。

    Publish が manifest CAS commit を成功させた直後にのみ呼ぶ。COMMITTED から
    再度呼ばれた場合は、committed_at を維持したまま冪等成功とする (Publish の
    Lambda 再試行対応)。
    """

    if not run_id:
        raise ValueError("run_id must not be empty")

    now = int(time.time())
    try:
        response = _table(table).update_item(
            Key={"PK": CATALOG_PK, "SK": _sk(run_id)},
            UpdateExpression=(
                "SET #status = :committed, "
                "committed_at = if_not_exists(committed_at, :now)"
            ),
            ConditionExpression="#status IN (:staged, :committed)",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":committed": GenerationStatus.COMMITTED.value,
                ":staged": GenerationStatus.STAGED.value,
                ":now": now,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        entry = get(table, run_id)
        if entry is None:
            raise GenerationCatalogMissingError(
                f"generation {run_id!r} does not exist in catalog"
            ) from exc
        raise GenerationCatalogConflictError(
            f"cannot mark generation {run_id!r} as COMMITTED (current status={entry.get('status')!r})"
        ) from exc

    committed_at = response.get("Attributes", {}).get("committed_at")
    return int(committed_at) if committed_at is not None else now


def get(table: str, run_id: str) -> dict[str, Any] | None:
    response: dict[str, Any] = _table(table).get_item(
        Key={"PK": CATALOG_PK, "SK": _sk(run_id)},
        ConsistentRead=True,
    )
    item = response.get("Item")
    return item if isinstance(item, dict) else None


def list_by_status(table: str, status: GenerationStatus) -> list[dict[str, Any]]:
    """指定 status の catalog エントリを ConsistentRead で列挙する。

    Query で SYSTEM#GENERATION をキーにするので Scan にはならない。
    """

    result: list[dict[str, Any]] = []
    query_kwargs: dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk",
        "FilterExpression": "#status = :status",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":pk": CATALOG_PK, ":status": status.value},
        "ConsistentRead": True,
    }
    while True:
        response = _table(table).query(**query_kwargs)
        result.extend(response.get("Items", []))
        key = response.get("LastEvaluatedKey")
        if not key:
            break
        query_kwargs["ExclusiveStartKey"] = key
    return result


def mark_deleting(table: str, run_id: str) -> None:
    """COMMITTED 状態の世代を DELETING へ遷移させる。

    Cleanup Lambda が削除を開始する直前に呼ぶ。既に DELETING/DELETED である
    場合は冪等成功として扱う (SQS 再配信対応)。COMMITTED でも DELETING/DELETED
    でもない状態 (STAGED) は Cleanup の対象になってはならないため Conflict。
    """

    if not run_id:
        raise ValueError("run_id must not be empty")

    now = int(time.time())
    try:
        _table(table).update_item(
            Key={"PK": CATALOG_PK, "SK": _sk(run_id)},
            UpdateExpression=(
                "SET #status = :deleting, "
                "deleting_at = if_not_exists(deleting_at, :now)"
            ),
            ConditionExpression="#status IN (:committed, :deleting, :deleted)",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":deleting": GenerationStatus.DELETING.value,
                ":committed": GenerationStatus.COMMITTED.value,
                ":deleted": GenerationStatus.DELETED.value,
                ":now": now,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        entry = get(table, run_id)
        if entry is None:
            raise GenerationCatalogMissingError(
                f"generation {run_id!r} does not exist in catalog"
            ) from exc
        raise GenerationCatalogConflictError(
            f"cannot mark generation {run_id!r} as DELETING "
            f"(current status={entry.get('status')!r})"
        ) from exc


def mark_deleted(table: str, run_id: str) -> None:
    """DELETING 状態の世代を DELETED (tombstone) へ遷移させる。

    実データの BatchWriteItem 削除が完走したことの記録。DELETED から再度
    呼ばれた場合は冪等成功。
    """

    if not run_id:
        raise ValueError("run_id must not be empty")

    now = int(time.time())
    try:
        _table(table).update_item(
            Key={"PK": CATALOG_PK, "SK": _sk(run_id)},
            UpdateExpression=(
                "SET #status = :deleted, "
                "deleted_at = if_not_exists(deleted_at, :now)"
            ),
            ConditionExpression="#status IN (:deleting, :deleted)",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":deleted": GenerationStatus.DELETED.value,
                ":deleting": GenerationStatus.DELETING.value,
                ":now": now,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        entry = get(table, run_id)
        if entry is None:
            raise GenerationCatalogMissingError(
                f"generation {run_id!r} does not exist in catalog"
            ) from exc
        raise GenerationCatalogConflictError(
            f"cannot mark generation {run_id!r} as DELETED "
            f"(current status={entry.get('status')!r})"
        ) from exc
