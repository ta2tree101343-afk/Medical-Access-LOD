"""Cleanup Lambda が世代削除の可否を判定するためのポリシー。

方針: 保守的側に倒す。以下のいずれかに該当したら削除しない。

1. 現在の公開 manifest が指す run_id: 参照中のため絶対に削除しない
2. status が STAGED / DELETED: STAGED は未完了 (書き込み途中)、DELETED は
   既に完了しているので何もしない
3. 直近 N 世代 (default 6, 最新 committed_at の降順)
4. committed_at からの経過が最低保持期間 (default 365 日) 未満

上記全てを潜り抜けた COMMITTED / DELETING の世代のみが削除対象になる。
DELETING は "前回途中で落ちた" ものを resume するために対象に含む。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

DEFAULT_KEEP_LAST_N = 6
DEFAULT_MIN_AGE_DAYS = 365


@dataclass(frozen=True)
class RetentionPolicy:
    keep_last_n: int = DEFAULT_KEEP_LAST_N
    min_age_days: int = DEFAULT_MIN_AGE_DAYS

    def __post_init__(self) -> None:
        if self.keep_last_n < 1:
            raise ValueError("keep_last_n must be >= 1")
        if self.min_age_days < 0:
            raise ValueError("min_age_days must be >= 0")


@dataclass(frozen=True)
class DeletionPlan:
    to_delete: list[str]  # 削除する run_id 群
    keep_reasons: dict[str, str]  # 保持する run_id -> 保持理由

    def is_empty(self) -> bool:
        return not self.to_delete


def _committed_at(entry: dict[str, Any]) -> int:
    # DynamoDB は数値属性を Decimal で返すので int() で正規化する。
    return int(entry.get("committed_at") or entry.get("staged_at") or 0)


def _run_id(entry: dict[str, Any]) -> str:
    value = entry.get("run_id")
    if not isinstance(value, str):
        raise TypeError(f"catalog entry missing run_id: {entry!r}")
    return value


def plan_deletions(
    entries: list[dict[str, Any]],
    *,
    active_run_id: str | None,
    policy: RetentionPolicy | None = None,
    now: int | None = None,
) -> DeletionPlan:
    """catalog エントリ全件を受け取り、削除計画を返す。

    Parameters
    ----------
    entries:
        `generation_catalog.list_by_status` で得たすべてのエントリ (どの状態でも
        混ぜて渡してよい)。呼び出し側で status ごとに分ける必要は無い。
    active_run_id:
        `latest/manifest.json` が指す run_id。None の場合は "manifest が未commit"
        と見なし、削除は一切行わない (誤って現行を消さないための安全側倒し)。
    policy:
        保持ポリシー。None なら DEFAULT_KEEP_LAST_N / DEFAULT_MIN_AGE_DAYS。
    now:
        テスト用の現在時刻 (epoch 秒)。None の場合は time.time() を使う。
    """

    if active_run_id is None:
        return DeletionPlan(
            to_delete=[],
            keep_reasons={_run_id(e): "manifest is not committed" for e in entries},
        )

    resolved_policy = policy or RetentionPolicy()
    resolved_now = now if now is not None else int(time.time())
    min_age_seconds = resolved_policy.min_age_days * 86_400

    # COMMITTED を committed_at 降順で並べる (最新から数える)
    committed = sorted(
        [e for e in entries if e.get("status") == "COMMITTED"],
        key=_committed_at,
        reverse=True,
    )
    # DELETING は resume 対象として含めるが retention 順位には数えない
    deleting = [e for e in entries if e.get("status") == "DELETING"]

    keep_reasons: dict[str, str] = {}
    to_delete: list[str] = []

    for entry in entries:
        rid = _run_id(entry)
        status = entry.get("status")
        if status == "STAGED":
            keep_reasons[rid] = "status=STAGED (in-flight)"
        elif status == "DELETED":
            keep_reasons[rid] = "status=DELETED (already tombstoned)"

    for entry in committed:
        rid = _run_id(entry)
        if rid == active_run_id:
            keep_reasons[rid] = "active manifest"
            continue
        rank = committed.index(entry)  # 0-indexed, 0 が最新
        if rank < resolved_policy.keep_last_n:
            keep_reasons[rid] = f"within keep_last_n (rank={rank})"
            continue
        age = resolved_now - _committed_at(entry)
        if age < min_age_seconds:
            keep_reasons[rid] = f"age {age}s < min_age {min_age_seconds}s"
            continue
        to_delete.append(rid)

    # DELETING の resume: retention に関係無く常に削除を試みる
    # (既に mark_deleting されているため、実質的にはリトライ)
    for entry in deleting:
        rid = _run_id(entry)
        if rid == active_run_id:
            # 通常ここには来ない (active は COMMITTED のはず) が念のため守る
            keep_reasons[rid] = "active manifest"
            continue
        to_delete.append(rid)

    return DeletionPlan(to_delete=to_delete, keep_reasons=keep_reasons)
