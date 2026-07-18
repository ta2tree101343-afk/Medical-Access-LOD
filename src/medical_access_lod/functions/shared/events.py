from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BaseEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str = Field(min_length=1, max_length=64)


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


class BuildReadModelEvent(BaseEvent):
    normalized_bucket: str = Field(min_length=3)
    normalized_key: str = Field(min_length=1)
    read_model_table: str = Field(min_length=1)
