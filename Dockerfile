FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}

RUN pip install --no-cache-dir uv==0.11.24

COPY pyproject.toml uv.lock ./

RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt \
        > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt --target "${LAMBDA_TASK_ROOT}"

COPY src/medical_access_lod ./medical_access_lod
COPY ontology ./ontology
COPY queries ./queries
COPY queries-real ./queries-real

ENV POWERTOOLS_SERVICE_NAME=medical-access-lod \
    POWERTOOLS_METRICS_NAMESPACE=MedicalAccessLOD

USER 993

CMD ["medical_access_lod.functions.build_rdf.handler.lambda_handler"]
