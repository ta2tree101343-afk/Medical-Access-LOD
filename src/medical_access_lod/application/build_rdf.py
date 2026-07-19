"""正規化済みデータから Turtle/JSON-LD を生成する application 層。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from medical_access_lod.application.normalize_data import NormalizedDataset
from medical_access_lod.infrastructure.rdf.dataset_metadata import DatasetMetadata
from medical_access_lod.infrastructure.rdf.graph_builder import (
    build_graph,
    serialize_jsonld,
    serialize_turtle,
)


@dataclass(frozen=True)
class BuildResult:
    turtle_path: Path

    jsonld_path: Path

    triples: int


def build_rdf(
    dataset: NormalizedDataset,
    out_dir: Path,
    metadata: DatasetMetadata | None = None,
) -> BuildResult:

    graph = build_graph(dataset, metadata=metadata)

    ttl = out_dir / "medical-access-lod.ttl"

    js = out_dir / "medical-access-lod.jsonld"

    serialize_turtle(graph, ttl)

    serialize_jsonld(graph, js)

    return BuildResult(turtle_path=ttl, jsonld_path=js, triples=len(graph))
