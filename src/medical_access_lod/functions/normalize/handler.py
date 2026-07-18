from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from aws_lambda_powertools.utilities.typing import LambdaContext

from medical_access_lod.application.normalize_mhlw import normalize_mhlw
from medical_access_lod.functions.shared.events import NormalizeEvent
from medical_access_lod.functions.shared.observability import logger, metrics, tracer
from medical_access_lod.functions.shared.s3io import download_prefix, put_json


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics(capture_cold_start_metric=True)
@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    request = NormalizeEvent.model_validate(event)
    logger.append_keys(run_id=request.run_id)

    with tempfile.TemporaryDirectory() as tmp:
        raw_dir = Path(tmp) / "raw"
        download_prefix(request.raw_bucket, request.raw_prefix, raw_dir)
        dataset = normalize_mhlw(raw_dir)

    payload = {
        "facilities": [f.model_dump(mode="json") for f in dataset.facilities],
        "services": [s.model_dump(mode="json") for s in dataset.services],
        "schedules": [s.model_dump(mode="json") for s in dataset.schedules],
        "specialty_labels": dataset.specialty_labels,
    }
    normalized_key = f"normalized/{request.run_id}.json"
    put_json(payload, request.normalized_bucket, normalized_key)

    metrics.add_metric(name="NormalizedRecords", unit="Count", value=len(dataset.schedules))
    logger.info(
        "normalize completed",
        extra={
            "facilities": len(dataset.facilities),
            "services": len(dataset.services),
            "schedules": len(dataset.schedules),
        },
    )

    return {
        "run_id": request.run_id,
        "normalized_bucket": request.normalized_bucket,
        "normalized_key": normalized_key,
        "facilities": len(dataset.facilities),
        "services": len(dataset.services),
        "schedules": len(dataset.schedules),
    }
