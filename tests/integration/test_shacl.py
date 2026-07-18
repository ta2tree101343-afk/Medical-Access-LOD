from __future__ import annotations

from pathlib import Path

from rdflib import Literal, URIRef
from rdflib.namespace import RDF

from medical_access_lod.application.normalize_data import normalize
from medical_access_lod.infrastructure.rdf.graph_builder import build_graph
from medical_access_lod.infrastructure.rdf.shacl_validator import validate_graph
from medical_access_lod.infrastructure.rdf.uri_factory import EX, SCHEMA

FIXTURES = Path(__file__).parent.parent.parent / "data" / "fixtures"

SHAPES = Path(__file__).parent.parent.parent / "ontology" / "shapes.ttl"


def _build_valid_graph():

    dataset = normalize(
        FIXTURES / "facilities.csv",
        FIXTURES / "services.csv",
        FIXTURES / "schedules.csv",
    )

    return build_graph(dataset)


def test_valid_fixture_conforms() -> None:

    graph = _build_valid_graph()

    result = validate_graph(graph, SHAPES)

    assert result.conforms, result.report_text


def test_missing_facility_id_is_violation() -> None:

    graph = _build_valid_graph()

    facility = URIRef("https://example.org/medical-access/resource/facility/1210000002")

    graph.remove((facility, EX.facilityId, None))

    result = validate_graph(graph, SHAPES)

    assert not result.conforms


def test_missing_address_is_violation() -> None:

    graph = _build_valid_graph()

    facility = URIRef("https://example.org/medical-access/resource/facility/1210000002")

    graph.remove((facility, SCHEMA.address, None))

    result = validate_graph(graph, SHAPES)

    assert not result.conforms


def test_targetclass_is_concrete_not_upper() -> None:
    """MedicalOrganization のみ (具象クラスなし) では 0 件マッチ = 適合と誤判定されないよう
    Shapes 側が Hospital / MedicalClinic を明示していることを確認する。"""

    from rdflib import Graph

    graph = Graph()

    facility = URIRef("https://example.org/medical-access/resource/facility/X")

    graph.add((facility, RDF.type, SCHEMA.MedicalOrganization))

    graph.add((facility, EX.facilityId, Literal("X")))

    result = validate_graph(graph, SHAPES)

    assert result.conforms
