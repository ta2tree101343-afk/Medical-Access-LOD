"""pySHACL による RDF 検証。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pyshacl import validate
from rdflib import Graph


def _default_shapes_path() -> Path:
    """shapes.ttl の既定パスを解決する。

    - Lambda コンテナ: `/var/task/ontology/shapes.ttl` (Dockerfile が COPY する)。
      Lambda ランタイムは `LAMBDA_TASK_ROOT=/var/task` を注入するのでそれを使う。
    - ローカル開発: repo ルート直下の `ontology/shapes.ttl` を辿る。
      __file__ = `<repo>/src/medical_access_lod/infrastructure/rdf/shacl_validator.py`
      parents[3] = `<repo>/src`、その .parent = `<repo>`。
    """
    lambda_root = os.environ.get("LAMBDA_TASK_ROOT")
    if lambda_root:
        return Path(lambda_root) / "ontology" / "shapes.ttl"
    return Path(__file__).resolve().parents[3].parent / "ontology" / "shapes.ttl"


SHAPES_PATH = _default_shapes_path()


@dataclass(frozen=True)
class ValidationResult:
    conforms: bool

    report_graph: Graph

    report_text: str


def validate_graph(data_graph: Graph, shapes_path: Path | None = None) -> ValidationResult:

    shapes = Graph().parse(source=shapes_path or SHAPES_PATH, format="turtle")

    conforms, report_graph, report_text = validate(
        data_graph=data_graph,
        shacl_graph=shapes,
        inference="none",
        abort_on_first=False,
        allow_infos=False,
        allow_warnings=False,
        meta_shacl=False,
        advanced=False,
        js=False,
        debug=False,
    )

    return ValidationResult(
        conforms=bool(conforms), report_graph=report_graph, report_text=report_text
    )
