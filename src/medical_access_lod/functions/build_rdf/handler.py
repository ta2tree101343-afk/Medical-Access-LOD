from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from aws_lambda_powertools.utilities.typing import LambdaContext

from medical_access_lod.application.build_rdf import build_rdf
from medical_access_lod.application.normalize_data import NormalizedDataset
from medical_access_lod.domain.models.clinical_service import ClinicalService
from medical_access_lod.domain.models.facility import Facility
from medical_access_lod.domain.models.schedule import Schedule
from medical_access_lod.functions.shared.events import BuildRdfEvent
from medical_access_lod.functions.shared.observability import logger, metrics, tracer
from medical_access_lod.functions.shared.s3io import get_json, upload_file


def _hydrate(payload: dict[str, Any]) -> NormalizedDataset:
    return NormalizedDataset(
        facilities=[Facility.model_validate(x) for x in payload["facilities"]],
        services=[ClinicalService.model_validate(x) for x in payload["services"]],
        schedules=[Schedule.model_validate(x) for x in payload["schedules"]],
        specialty_labels=payload.get("specialty_labels", {}),
    )


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics(capture_cold_start_metric=True)
@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    request = BuildRdfEvent.model_validate(event)
    logger.append_keys(run_id=request.run_id)

    payload = get_json(request.normalized_bucket, request.normalized_key)
    dataset = _hydrate(payload)

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        result = build_rdf(dataset, out_dir)
        ttl_key = f"builds/{request.run_id}/medical-access-lod.ttl"
        jsonld_key = f"builds/{request.run_id}/medical-access-lod.jsonld"
        upload_file(result.turtle_path, request.build_bucket, ttl_key, "text/turtle; charset=utf-8")
        upload_file(result.jsonld_path, request.build_bucket, jsonld_key, "application/ld+json")

    metrics.add_metric(name="GeneratedTriples", unit="Count", value=result.triples)
    logger.info("build completed", extra={"triples": result.triples})

    return {
        "run_id": request.run_id,
        "build_bucket": request.build_bucket,
        "ttl_key": ttl_key,
        "jsonld_key": jsonld_key,
        "triples": result.triples,
    }
