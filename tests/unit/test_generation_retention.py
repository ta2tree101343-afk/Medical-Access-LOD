"""世代 retention ポリシーの単体テスト (純関数、AWS 不要)。"""
from __future__ import annotations

import pytest

from medical_access_lod.functions.shared.generation_retention import (
    DEFAULT_KEEP_LAST_N,
    RetentionPolicy,
    plan_deletions,
)

NOW = 2_000_000_000  # 十分未来の固定 epoch 秒


def _committed(run_id: str, committed_at: int) -> dict[str, object]:
    return {
        "run_id": run_id,
        "status": "COMMITTED",
        "committed_at": committed_at,
        "snapshot_date": "2025-12-01",
    }


def _staged(run_id: str) -> dict[str, object]:
    return {"run_id": run_id, "status": "STAGED", "snapshot_date": "2025-12-01"}


def _deleting(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "status": "DELETING",
        "committed_at": NOW - 999_999,
        "snapshot_date": "2025-12-01",
    }


def _deleted(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "status": "DELETED",
        "committed_at": NOW - 999_999,
        "snapshot_date": "2025-12-01",
    }


def test_active_manifest_run_is_never_deleted() -> None:
    entries = [
        _committed("active", committed_at=NOW - 10),
        _committed("very-old", committed_at=NOW - 999_999_999),
    ]
    plan = plan_deletions(
        entries,
        active_run_id="active",
        policy=RetentionPolicy(keep_last_n=1, min_age_days=0),
        now=NOW,
    )
    assert plan.to_delete == ["very-old"]
    assert plan.keep_reasons["active"] == "active manifest"


def test_missing_manifest_means_no_deletion() -> None:
    """manifest が未commit のときは絶対に削除しない (安全側倒し)."""
    entries = [
        _committed("run-A", committed_at=NOW - 999_999_999),
        _committed("run-B", committed_at=NOW - 999_999_999),
    ]
    plan = plan_deletions(entries, active_run_id=None, now=NOW)
    assert plan.to_delete == []
    assert set(plan.keep_reasons.keys()) == {"run-A", "run-B"}


def test_keep_last_n_preserves_recent_committed_generations() -> None:
    entries = [
        _committed(f"gen-{i}", committed_at=NOW - i * 3600)
        for i in range(10)
    ]
    plan = plan_deletions(
        entries,
        active_run_id="gen-0",
        policy=RetentionPolicy(keep_last_n=3, min_age_days=0),
        now=NOW,
    )
    # gen-0 は active、gen-1, gen-2 は keep_last_n 内 (rank 1, 2)
    # gen-3 以降は削除対象
    assert set(plan.to_delete) == {f"gen-{i}" for i in range(3, 10)}
    assert plan.keep_reasons["gen-0"] == "active manifest"


def test_min_age_days_preserves_recent_by_time() -> None:
    """committed_at からの経過が min_age 未満なら残す。"""
    entries = [
        _committed("new-one", committed_at=NOW - 10 * 86_400),  # 10日前
        _committed("old-one", committed_at=NOW - 400 * 86_400),  # 400日前
        _committed("active", committed_at=NOW),
    ]
    plan = plan_deletions(
        entries,
        active_run_id="active",
        policy=RetentionPolicy(keep_last_n=1, min_age_days=365),
        now=NOW,
    )
    assert plan.to_delete == ["old-one"]
    assert "within keep_last_n" not in plan.keep_reasons["new-one"]
    assert "age" in plan.keep_reasons["new-one"]


def test_staged_is_never_deleted() -> None:
    entries = [_staged("stuck"), _committed("active", committed_at=NOW)]
    plan = plan_deletions(
        entries,
        active_run_id="active",
        policy=RetentionPolicy(keep_last_n=1, min_age_days=0),
        now=NOW,
    )
    assert "stuck" not in plan.to_delete
    assert plan.keep_reasons["stuck"] == "status=STAGED (in-flight)"


def test_deleted_is_never_re_deleted() -> None:
    entries = [_deleted("tombstoned"), _committed("active", committed_at=NOW)]
    plan = plan_deletions(entries, active_run_id="active", now=NOW)
    assert "tombstoned" not in plan.to_delete
    assert plan.keep_reasons["tombstoned"] == "status=DELETED (already tombstoned)"


def test_deleting_is_resumed_regardless_of_retention() -> None:
    """前回途中で落ちた DELETING は retention に関係無く再削除する。"""
    entries = [_deleting("interrupted"), _committed("active", committed_at=NOW)]
    plan = plan_deletions(
        entries,
        active_run_id="active",
        policy=RetentionPolicy(keep_last_n=100, min_age_days=100_000),
        now=NOW,
    )
    assert "interrupted" in plan.to_delete


def test_default_policy_is_conservative() -> None:
    """N=6, min_age=365 日で、9 世代あるうち古い 3 世代のみ削除。"""
    entries = [
        _committed(f"g{i}", committed_at=NOW - i * 400 * 86_400)  # 400日刻み
        for i in range(9)
    ]
    plan = plan_deletions(entries, active_run_id="g0", now=NOW)
    assert DEFAULT_KEEP_LAST_N == 6
    # g0 (active), g1-g5 (keep_last_n 内で rank 1-5)、g6-g8 が削除候補
    assert set(plan.to_delete) == {"g6", "g7", "g8"}


def test_retention_policy_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="keep_last_n"):
        RetentionPolicy(keep_last_n=0)
    with pytest.raises(ValueError, match="min_age_days"):
        RetentionPolicy(min_age_days=-1)
