from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from aws_lambda_powertools.utilities.typing import LambdaContext

from medical_access_lod.application.download_source import download
from medical_access_lod.functions.shared.events import DownloadEvent
from medical_access_lod.functions.shared.observability import logger, metrics, tracer
from medical_access_lod.functions.shared.s3io import s3_client


@tracer.capture_method
def _upload_dir(local_dir: Path, bucket: str, prefix: str) -> list[str]:
    client = s3_client()
    uploaded: list[str] = []
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir).as_posix()
        key = f"{prefix.rstrip('/')}/{rel}"
        client.upload_file(str(path), bucket, key)
        uploaded.append(key)
    return uploaded


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics(capture_cold_start_metric=True)
@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    request = DownloadEvent.model_validate(event)
    logger.append_keys(run_id=request.run_id, snapshot_date=request.snapshot_date)

    with tempfile.TemporaryDirectory() as tmp:
        result = download(
            Path(tmp),
            source_url=request.source_url,
            snapshot_date=request.snapshot_date,
        )
        prefix = f"snapshots/{request.snapshot_date}"
        uploaded = _upload_dir(result.raw_dir, request.raw_bucket, prefix)

    metrics.add_metric(name="SourceRecords", unit="Count", value=len(uploaded))
    logger.info("download completed", extra={"files": len(uploaded), "sha256": result.sha256})

    return {
        "run_id": request.run_id,
        "snapshot_date": request.snapshot_date,
        "raw_bucket": request.raw_bucket,
        "raw_prefix": prefix,
        "sha256": result.sha256,
        "files": uploaded,
    }
