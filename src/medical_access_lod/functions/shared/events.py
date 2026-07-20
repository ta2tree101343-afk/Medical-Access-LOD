from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BaseEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    # AWS Step Functions の Execution.Name (最大 80 文字) を run_id に流用するため 128 まで許容
    run_id: str = Field(min_length=1, max_length=128)


class DownloadEvent(BaseEvent):
    source_url: str = Field(min_length=8)
    snapshot_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    raw_bucket: str = Field(min_length=3)


class NormalizeEvent(BaseEvent):
    raw_bucket: str = Field(min_length=3)
    raw_prefix: str = Field(min_length=1)
    normalized_bucket: str = Field(min_length=3)


class BuildRdfEvent(BaseEvent):
    normalized_bucket: str = Field(min_length=3)
    normalized_key: str = Field(min_length=1)
    build_bucket: str = Field(min_length=3)


class ValidateEvent(BaseEvent):
    build_bucket: str = Field(min_length=3)
    ttl_key: str = Field(min_length=1)


class PublishEvent(BaseEvent):
    build_bucket: str = Field(min_length=3)
    ttl_key: str = Field(min_length=1)
    jsonld_key: str = Field(min_length=1)
    dist_bucket: str = Field(min_length=3)
    read_model_table: str = Field(min_length=1)
    lock_owner: str = Field(min_length=1, max_length=128)
    # 不変 release (`releases/<snapshot_date>/<run_id>/`) の prefix に使う。
    snapshot_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def lock_must_belong_to_run(self) -> Self:
        if self.lock_owner != self.run_id:
            raise ValueError("lock_owner must match run_id")
        return self


class BuildReadModelEvent(BaseEvent):
    normalized_bucket: str = Field(min_length=3)
    normalized_key: str = Field(min_length=1)
    read_model_table: str = Field(min_length=1)
    # 世代 inventory (PK/SK 一覧) の書き出し先。Cleanup Lambda はここから
    # BatchWriteItem 削除対象を読み取る。
    build_bucket: str = Field(min_length=3)
    # generation catalog に STAGED で登録する際に必要。Cleanup 時の保持ポリシー
    # (直近 N 世代 + 最低保持期間) の判定にも使う。
    snapshot_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
