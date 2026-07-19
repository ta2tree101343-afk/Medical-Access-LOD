from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, SKOS, XSD

from medical_access_lod.application.normalize_data import normalize
from medical_access_lod.domain.values.day_of_week import DayOfWeek
from medical_access_lod.domain.values.facility_id import FacilityId
from medical_access_lod.domain.values.medical_specialty import SpecialtyCode
from medical_access_lod.infrastructure.rdf.graph_builder import (
    build_graph,
    serialize_jsonld,
    serialize_turtle,
)
from medical_access_lod.infrastructure.rdf.uri_factory import (
    BASE,
    EX,
    SCHEMA,
    facility_uri,
    schedule_uri,
    service_uri,
)

FIXTURES = Path(__file__).parent.parent.parent / "data" / "fixtures"


@pytest.fixture(scope="module")
def graph() -> Graph:

    dataset = normalize(
        FIXTURES / "facilities.csv",
        FIXTURES / "services.csv",
        FIXTURES / "schedules.csv",
    )

    return build_graph(dataset)


def test_graph_has_triples(graph: Graph) -> None:

    assert len(graph) > 0


def test_facility_id_is_unique(graph: Graph) -> None:

    ids = [str(o) for _s, _p, o in graph.triples((None, EX.facilityId, None))]

    counts = Counter(ids)

    duplicates = {k: v for k, v in counts.items() if v > 1}

    assert not duplicates, f"施設ID重複: {duplicates}"


def test_hospital_and_clinic_are_used(graph: Graph) -> None:

    hospital_count = len(list(graph.triples((None, RDF.type, SCHEMA.Hospital))))

    clinic_count = len(list(graph.triples((None, RDF.type, SCHEMA.MedicalClinic))))

    assert hospital_count >= 1

    assert clinic_count >= 1


def test_all_facilities_have_address_and_service(graph: Graph) -> None:

    for facility in graph.subjects(RDF.type, SCHEMA.Hospital):
        assert (facility, SCHEMA.address, None) in graph

        assert (facility, EX.offersClinicalService, None) in graph

    for facility in graph.subjects(RDF.type, SCHEMA.MedicalClinic):
        assert (facility, SCHEMA.address, None) in graph

        assert (facility, EX.offersClinicalService, None) in graph


def test_services_have_specialty_and_schedule(graph: Graph) -> None:

    services = list(graph.subjects(RDF.type, EX.ClinicalService))

    assert len(services) >= 1

    for service in services:
        assert (service, EX.medicalSpecialty, None) in graph

        assert (service, EX.hasSchedule, None) in graph


def test_schedule_times_are_xsd_time(graph: Graph) -> None:

    for _s, _p, opens in graph.triples((None, SCHEMA.opens, None)):
        assert isinstance(opens, Literal)

        assert opens.datatype == XSD.time

    for _s, _p, closes in graph.triples((None, SCHEMA.closes, None)):
        assert isinstance(closes, Literal)

        assert closes.datatype == XSD.time


def test_schema_namespace_is_https(graph: Graph) -> None:

    assert str(SCHEMA).startswith("https://schema.org/")


def test_expected_sample_subset(graph: Graph) -> None:
    """設計書 §11 準拠の期待Turtle (fixture 1210000002) の主要トリプルが含まれる。"""

    fid = FacilityId("1210000002")

    facility = facility_uri(fid)

    service = service_uri(fid, SpecialtyCode("02"))

    schedule_mon = schedule_uri(fid, SpecialtyCode("02"), DayOfWeek.MON, 1)

    schedule_sat = schedule_uri(fid, SpecialtyCode("02"), DayOfWeek.SAT, 1)

    assert (facility, RDF.type, SCHEMA.MedicalClinic) in graph

    assert (facility, EX.facilityId, Literal("1210000002")) in graph

    assert (facility, EX.offersClinicalService, service) in graph

    assert (service, RDF.type, EX.ClinicalService) in graph

    assert (service, EX.hasSchedule, schedule_mon) in graph

    assert (service, EX.hasSchedule, schedule_sat) in graph

    assert (schedule_mon, SCHEMA.dayOfWeek, URIRef("https://schema.org/Monday")) in graph

    assert (schedule_mon, SCHEMA.opens, Literal("09:00:00", datatype=XSD.time)) in graph

    assert (schedule_mon, SCHEMA.closes, Literal("12:00:00", datatype=XSD.time)) in graph


def test_specialty_scheme_has_inScheme_and_notation(graph: Graph) -> None:

    scheme = URIRef(f"{BASE}concept/specialty")

    concepts = list(graph.subjects(SKOS.inScheme, scheme))

    assert len(concepts) >= 1

    for concept in concepts:
        notations = list(graph.objects(concept, SKOS.notation))

        assert len(notations) == 1

        assert str(notations[0]).isdigit()


def test_specialty_concepts_have_prefLabel(graph: Graph) -> None:
    scheme = URIRef(f"{BASE}concept/specialty")
    for concept in graph.subjects(SKOS.inScheme, scheme):
        labels = list(graph.objects(concept, SKOS.prefLabel))
        assert labels, f"no prefLabel on {concept}"


def test_facilities_have_geo_coordinates(graph: Graph) -> None:
    geos = list(graph.subject_objects(SCHEMA.geo))
    assert geos, "no schema:geo triples produced"
    for _facility, geo_node in geos:
        assert (geo_node, RDF.type, SCHEMA.GeoCoordinates) in graph
        assert (geo_node, SCHEMA.latitude, None) in graph
        assert (geo_node, SCHEMA.longitude, None) in graph


def test_dataset_has_void_and_dcat_metadata(graph: Graph) -> None:
    from medical_access_lod.infrastructure.rdf.dataset_metadata import DCAT, DCT, VOID

    ds = URIRef(f"{BASE}dataset")
    assert (ds, RDF.type, VOID.Dataset) in graph
    assert (ds, RDF.type, DCAT.Dataset) in graph
    assert list(graph.objects(ds, DCT.title))
    assert list(graph.objects(ds, DCT.license))
    assert list(graph.objects(ds, VOID.dataDump))

    distributions = list(graph.objects(ds, DCAT.distribution))
    assert len(distributions) == 2  # Turtle + JSON-LD
    for dist in distributions:
        assert (dist, RDF.type, DCAT.Distribution) in graph
        assert list(graph.objects(dist, DCAT.downloadURL))
        assert list(graph.objects(dist, DCAT.mediaType))


def test_turtle_roundtrip(graph: Graph, tmp_path: Path) -> None:

    ttl = tmp_path / "out.ttl"

    serialize_turtle(graph, ttl)

    reparsed = Graph()

    reparsed.parse(source=ttl, format="turtle")

    assert len(reparsed) == len(graph)


def test_jsonld_roundtrip(graph: Graph, tmp_path: Path) -> None:

    js = tmp_path / "out.jsonld"

    serialize_jsonld(graph, js)

    reparsed = Graph()

    reparsed.parse(source=js, format="json-ld")

    assert len(reparsed) == len(graph)
