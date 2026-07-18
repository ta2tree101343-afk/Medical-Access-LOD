from __future__ import annotations

import os

from aws_lambda_powertools import Logger, Metrics, Tracer

SERVICE_NAME = os.environ.get("POWERTOOLS_SERVICE_NAME", "medical-access-lod")
METRICS_NAMESPACE = os.environ.get("POWERTOOLS_METRICS_NAMESPACE", "MedicalAccessLOD")

logger = Logger(service=SERVICE_NAME)
metrics = Metrics(namespace=METRICS_NAMESPACE, service=SERVICE_NAME)
tracer = Tracer(service=SERVICE_NAME)
