from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from aws_lambda_powertools.utilities.typing import LambdaContext

from medical_access_lod.application.validate_rdf import validate_turtle
from medical_access_lod.functions.shared.events import ValidateEvent
from medical_access_lod.functions.shared.observability import logger, metrics, tracer
from medical_access_lod.functions.shared.s3io import s3_client, upload_file


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics(capture_cold_start_metric=True)
@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    request = ValidateEvent.model_validate(event)
    logger.append_keys(run_id=request.run_id)

    with tempfile.TemporaryDirectory() as tmp:
        ttl_local = Path(tmp) / "input.ttl"
        s3_client().download_file(request.build_bucket, request.ttl_key, str(ttl_local))
        result = validate_turtle(ttl_local)

        report_key: str | None = None
        if not result.conforms:
            report_local = Path(tmp) / "validation-report.ttl"
            result.report_graph.serialize(destination=str(report_local), format="turtle")
            report_key = f"builds/{request.run_id}/validation-report.ttl"
            upload_file(report_local, request.build_bucket, report_key, "text/turtle; charset=utf-8")

    violations = 0 if result.conforms else 1
    metrics.add_metric(name="ShaclViolations", unit="Count", value=violations)
    logger.info("validate completed", extra={"conforms": result.conforms})

    response: dict[str, Any] = {
        "run_id": request.run_id,
        "conforms": result.conforms,
    }
    if report_key:
        response["report_key"] = report_key
    return response
