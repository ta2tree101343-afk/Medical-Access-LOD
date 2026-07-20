from __future__ import annotations

import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

LOCK_PK = "SYSTEM#PIPELINE"
LOCK_SK = "READ_MODEL_LOCK"


class PipelineLockError(RuntimeError):
    """読み取りモデル更新用ロックの操作に失敗した。"""


class PipelineLockConflictError(PipelineLockError):
    """有効なロックを別の owner が保持している。"""


class PipelineLockMissingError(PipelineLockConflictError):
    """更新対象のロックが存在しない。"""


class PipelineLockOwnershipError(PipelineLockError):
    """別の owner が保持するロックを解放しようとした。"""


def _table(table: str) -> Any:
    return boto3.resource("dynamodb").Table(table)


def _current_lock(table: str) -> dict[str, Any] | None:
    response: dict[str, Any] = _table(table).get_item(
        Key={"PK": LOCK_PK, "SK": LOCK_SK},
        ConsistentRead=True,
    )
    item = response.get("Item")
    return item if isinstance(item, dict) else None


def _current_lock_description(table: str) -> str:
    item = _current_lock(table)
    if not item:
        return "lock is missing"
    return f"owner={item.get('owner')!r}, expires_at={item.get('expires_at')!r}"


def acquire_pipeline_lock(table: str, owner: str, lease: int = 7200) -> int:
    """読み取りモデル更新用ロックを取得し、有効期限の epoch 秒を返す。

    ロックが存在しない、期限切れ、または同一 owner による再入の場合のみ取得する。
    同一 owner の再入では lease を現在時刻から延長する。
    """

    if not owner:
        raise ValueError("pipeline lock owner must not be empty")
    if lease <= 0:
        raise ValueError("pipeline lock lease must be positive")

    now = int(time.time())
    expires_at = now + lease
    try:
        _table(table).put_item(
            Item={
                "PK": LOCK_PK,
                "SK": LOCK_SK,
                "owner": owner,
                "expires_at": expires_at,
            },
            ConditionExpression=(
                "attribute_not_exists(PK) OR #expires_at <= :now OR #owner = :owner"
            ),
            ExpressionAttributeNames={
                "#expires_at": "expires_at",
                "#owner": "owner",
            },
            ExpressionAttributeValues={
                ":now": now,
                ":owner": owner,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        description = _current_lock_description(table)
        raise PipelineLockConflictError(
            f"read model pipeline lock is already held by another execution: {description}"
        ) from exc
    return expires_at


def renew_pipeline_lock(table: str, owner: str, lease: int = 7200) -> int:
    """同一 owner が保持する有効なロックだけを延長する。

    lock不存在、期限切れ、別ownerの場合は更新しない。遅延した旧実行がロックを
    再取得して新しい世代のmanifestを巻き戻すことを防ぐため、acquireとは分離する。
    """

    if not owner:
        raise ValueError("pipeline lock owner must not be empty")
    if lease <= 0:
        raise ValueError("pipeline lock lease must be positive")

    now = int(time.time())
    expires_at = now + lease
    try:
        _table(table).update_item(
            Key={"PK": LOCK_PK, "SK": LOCK_SK},
            UpdateExpression="SET #expires_at = :expires_at",
            ConditionExpression="#owner = :owner AND #expires_at > :now",
            ExpressionAttributeNames={
                "#expires_at": "expires_at",
                "#owner": "owner",
            },
            ExpressionAttributeValues={
                ":expires_at": expires_at,
                ":now": now,
                ":owner": owner,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        current = _current_lock(table)
        if current is None:
            raise PipelineLockMissingError(
                "cannot renew read model pipeline lock because it is missing"
            ) from exc
        description = (
            f"owner={current.get('owner')!r}, expires_at={current.get('expires_at')!r}"
        )
        raise PipelineLockConflictError(
            "cannot renew read model pipeline lock because it is expired or owned by "
            f"another execution: {description}"
        ) from exc
    return expires_at


def renew(table: str, owner: str, lease: int = 7200) -> int:
    """Publish側から利用する短い名前のlease更新。"""

    return renew_pipeline_lock(table, owner, lease)


def release_pipeline_lock(table: str, owner: str) -> bool:
    """owner が保持するロックを解放する。

    ロックが既に無い場合は成功として扱う。同一 owner の再試行を冪等にしつつ、
    別 owner が取得したロックは条件付き Delete により保護する。
    """

    if not owner:
        raise ValueError("pipeline lock owner must not be empty")

    try:
        _table(table).delete_item(
            Key={"PK": LOCK_PK, "SK": LOCK_SK},
            ConditionExpression="attribute_not_exists(PK) OR #owner = :owner",
            ExpressionAttributeNames={"#owner": "owner"},
            ExpressionAttributeValues={":owner": owner},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        description = _current_lock_description(table)
        raise PipelineLockOwnershipError(
            f"cannot release read model pipeline lock owned by another execution: {description}"
        ) from exc
    return True


def release(table: str, owner: str) -> bool:
    """Publish側から利用する短い名前の所有者条件付きrelease。"""

    return release_pipeline_lock(table, owner)
