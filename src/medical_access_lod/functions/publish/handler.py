from __future__ import annotations

from typing import Any

from aws_lambda_powertools.utilities.typing import LambdaContext

from medical_access_lod.functions.shared.events import PublishEvent
from medical_access_lod.functions.shared.observability import logger, metrics, tracer
from medical_access_lod.functions.shared.s3io import s3_client


@tracer.capture_method
def _copy(src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
    s3_client().copy_object(
        CopySource={"Bucket": src_bucket, "Key": src_key},
        Bucket=dst_bucket,
        Key=dst_key,
    )


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics(capture_cold_start_metric=True)
@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    request = PublishEvent.model_validate(event)
    logger.append_keys(run_id=request.run_id)

    published: list[str] = []
    for key in [request.ttl_key, request.jsonld_key]:
        filename = key.rsplit("/", 1)[-1]
        dst_key = f"latest/{filename}"
        _copy(request.build_bucket, key, request.dist_bucket, dst_key)
        published.append(dst_key)

    metrics.add_metric(name="PipelineSuccess", unit="Count", value=1)
    logger.info("publish completed", extra={"published": published})

    return {
        "run_id": request.run_id,
        "dist_bucket": request.dist_bucket,
        "published_files": published,
    }
