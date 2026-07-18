from __future__ import annotations

from pathlib import Path

from rdflib import Graph

from medical_access_lod.infrastructure.rdf.shacl_validator import (
    ValidationResult,
    validate_graph,
)


def validate_turtle(turtle_path: Path, shapes_path: Path | None = None) -> ValidationResult:

    data = Graph().parse(source=turtle_path, format="turtle")

    return validate_graph(data, shapes_path)
