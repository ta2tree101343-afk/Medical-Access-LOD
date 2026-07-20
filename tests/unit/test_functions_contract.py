from __future__ import annotations

import pytest

from medical_access_lod.functions.api import handler as api_handler
from medical_access_lod.functions.build_rdf import handler as build_handler
from medical_access_lod.functions.build_read_model import handler as brm_handler
from medical_access_lod.functions.download import handler as download_handler
from medical_access_lod.functions.normalize import handler as normalize_handler
from medical_access_lod.functions.publish import handler as publish_handler
from medical_access_lod.functions.shared.events import (
    BuildRdfEvent,
    BuildReadModelEvent,
    DownloadEvent,
    NormalizeEvent,
    PublishEvent,
    ValidateEvent,
)
from medical_access_lod.functions.validate import handler as validate_handler

HANDLERS = [
    ("download", download_handler),
    ("normalize", normalize_handler),
    ("build_rdf", build_handler),
    ("validate", validate_handler),
    ("publish", publish_handler),
    ("build_read_model", brm_handler),
    ("api", api_handler),
]


@pytest.mark.parametrize(("name", "module"), HANDLERS)
def test_handler_module_defines_lambda_handler(name: str, module: object) -> None:
    assert callable(module.lambda_handler), f"{name} missing lambda_handler"


def test_download_event_validation() -> None:
    ok = DownloadEvent.model_validate(
        {
            "run_id": "2026-07-18-001",
            "source_url": "https://example.test/x.zip",
            "snapshot_date": "2025-12-01",
            "raw_bucket": "raw-bucket",
        }
    )
    assert ok.snapshot_date == "2025-12-01"
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DownloadEvent.model_validate({"run_id": "x", "source_url": "y-invalid", "snapshot_date": "bad", "raw_bucket": "bkt"})


def test_normalize_event_validation() -> None:
    NormalizeEvent.model_validate(
        {
            "run_id": "r",
            "raw_bucket": "raw-bucket",
            "raw_prefix": "snapshots/2025-12-01",
            "normalized_bucket": "norm-bucket",
        }
    )


def test_build_rdf_event_validation() -> None:
    BuildRdfEvent.model_validate(
        {
            "run_id": "r",
            "normalized_bucket": "norm-bucket",
            "normalized_key": "normalized/r.json",
            "build_bucket": "build-bucket",
        }
    )


def test_validate_event_validation() -> None:
    ValidateEvent.model_validate(
        {"run_id": "r", "build_bucket": "build-bucket", "ttl_key": "builds/r/x.ttl"}
    )


def test_publish_event_validation() -> None:
    PublishEvent.model_validate(
        {
            "run_id": "r",
            "build_bucket": "build-bucket",
            "ttl_key": "builds/r/x.ttl",
            "jsonld_key": "builds/r/x.jsonld",
            "dist_bucket": "dist-bucket",
            "read_model_table": "read-model",
            "lock_owner": "r",
            "snapshot_date": "2025-12-01",
        }
    )


def test_publish_event_rejects_missing_snapshot_date() -> None:
    """不変 release (releases/<snapshot_date>/<run_id>/) に日付は必須。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PublishEvent.model_validate(
            {
                "run_id": "r",
                "build_bucket": "build-bucket",
                "ttl_key": "builds/r/x.ttl",
                "jsonld_key": "builds/r/x.jsonld",
                "dist_bucket": "dist-bucket",
                "read_model_table": "read-model",
                "lock_owner": "r",
                # snapshot_date 未指定
            }
        )


@pytest.mark.parametrize("missing", ["read_model_table", "lock_owner"])
def test_publish_event_rejects_missing_lock_context(missing: str) -> None:
    from pydantic import ValidationError

    event = {
        "run_id": "r",
        "build_bucket": "build-bucket",
        "ttl_key": "builds/r/x.ttl",
        "jsonld_key": "builds/r/x.jsonld",
        "dist_bucket": "dist-bucket",
        "read_model_table": "read-model",
        "lock_owner": "r",
        "snapshot_date": "2025-12-01",
    }
    event.pop(missing)
    with pytest.raises(ValidationError):
        PublishEvent.model_validate(event)


def test_publish_event_rejects_lock_owned_by_another_run() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="lock_owner must match run_id"):
        PublishEvent.model_validate(
            {
                "run_id": "run-A",
                "build_bucket": "build-bucket",
                "ttl_key": "builds/run-A/x.ttl",
                "jsonld_key": "builds/run-A/x.jsonld",
                "dist_bucket": "dist-bucket",
                "read_model_table": "read-model",
                "lock_owner": "run-B",
                "snapshot_date": "2025-12-01",
            }
        )


def test_build_read_model_event_validation() -> None:
    BuildReadModelEvent.model_validate(
        {
            "run_id": "r",
            "normalized_bucket": "norm-bucket",
            "normalized_key": "normalized/r.json",
            "read_model_table": "read-model",
        }
    )


def test_build_read_model_items_shape() -> None:
    from medical_access_lod.functions.build_read_model.handler import _build_items

    payload = {
        "facilities": [
            {
                "facility_id": "F1",
                "name": "テスト病院",
                "facility_type": "hospital",
                "address": {
                    "prefecture": "千葉県",
                    "city": "千葉市中央区",
                    "street_address": "1-1-1",
                },
            }
        ],
        "services": [{"facility_id": "F1", "specialty_code": "01"}],
        "schedules": [
            {
                "facility_id": "F1",
                "specialty_code": "01",
                "day_of_week": "Monday",
                "opens": "09:00:00",
                "closes": "17:00:00",
            }
        ],
        "specialty_labels": {"01": "内科"},
    }
    items = _build_items(payload, generation="test-run-001")
    pks = {(i["PK"], i["SK"]) for i in items}
    assert ("GENERATION#test-run-001#FACILITY#F1", "METADATA") in pks
    assert ("GENERATION#test-run-001#FACILITY#F1", "SERVICE#01") in pks
    assert (
        "GENERATION#test-run-001#FACILITY#F1",
        "SCHEDULE#01#Monday#09:00:00",
    ) in pks
    # SKOS label should propagate
    svc = next(i for i in items if i["SK"] == "SERVICE#01")
    assert svc["specialty_label"] == "内科"
    # manifestが指す世代だけをAPIが参照できる。
    assert all(i["generation"] == "test-run-001" for i in items)
