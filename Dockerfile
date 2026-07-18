FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}

RUN pip install --no-cache-dir uv==0.11.24

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY ontology ./ontology
COPY queries ./queries
COPY queries-real ./queries-real

RUN uv sync --frozen --no-dev --no-editable

ENV PYTHONPATH=${LAMBDA_TASK_ROOT}/src \
    POWERTOOLS_SERVICE_NAME=medical-access-lod \
    POWERTOOLS_METRICS_NAMESPACE=MedicalAccessLOD

USER 993

CMD ["medical_access_lod.functions.build_rdf.handler.lambda_handler"]
