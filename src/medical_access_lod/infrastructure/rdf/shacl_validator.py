"""pySHACL による RDF 検証。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pyshacl import validate
from rdflib import Graph

SHAPES_PATH = Path(__file__).resolve().parents[3].parent / "ontology" / "shapes.ttl"


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
