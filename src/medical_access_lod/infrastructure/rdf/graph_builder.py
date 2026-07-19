"""正規化済みデータから RDF グラフを構築する。"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from medical_access_lod.application.normalize_data import NormalizedDataset
from medical_access_lod.domain.models.facility import Facility, FacilityType
from medical_access_lod.domain.values.day_of_week import DayOfWeek
from medical_access_lod.domain.values.medical_specialty import SpecialtyCode
from medical_access_lod.infrastructure.rdf.uri_factory import (
    BASE,
    EX,
    SCHEMA,
    address_uri,
    day_of_week_uri,
    facility_uri,
    schedule_uri,
    service_uri,
    specialty_concept_uri,
)

_FACILITY_CLASS: dict[FacilityType, URIRef] = {
    FacilityType.HOSPITAL: SCHEMA.Hospital,
    FacilityType.CLINIC: SCHEMA.MedicalClinic,
    FacilityType.DENTIST: SCHEMA.Dentist,
}


def _bind_prefixes(graph: Graph) -> None:

    graph.bind("ex", EX)

    graph.bind("schema", SCHEMA)

    graph.bind("skos", SKOS)

    graph.bind("xsd", XSD)

    graph.bind("rdfs", RDFS)

    graph.bind("owl", OWL)

    graph.base = URIRef(BASE)


def build_graph(dataset: NormalizedDataset, *, include_ontology: bool = True) -> Graph:
    """NormalizedDataset から RDF グラフを構築する。"""

    graph = Graph()

    _bind_prefixes(graph)

    if include_ontology:
        _add_ontology(graph)

    _add_specialty_scheme(graph, dataset)

    for facility in dataset.facilities:
        _add_facility(graph, facility)

    services_by_facility: dict[str, list[tuple[str, URIRef]]] = defaultdict(list)

    for service in dataset.services:
        service_ref = service_uri(service.facility_id, service.specialty_code)

        services_by_facility[str(service.facility_id)].append(
            (str(service.specialty_code), service_ref)
        )

        graph.add((service_ref, RDF.type, EX.ClinicalService))

        graph.add(
            (
                service_ref,
                EX.medicalSpecialty,
                specialty_concept_uri(service.specialty_code),
            )
        )

    for facility_id_str, pairs in services_by_facility.items():
        f_ref = URIRef(f"{BASE}resource/facility/{facility_id_str}")

        for _code, s_ref in pairs:
            graph.add((f_ref, EX.offersClinicalService, s_ref))

    _add_schedules(graph, dataset)

    return graph


def _add_ontology(graph: Graph) -> None:

    ont = URIRef(f"{BASE}ontology/medical-access")

    graph.add((ont, RDF.type, OWL.Ontology))

    graph.add((ont, RDFS.label, Literal("Medical Access Ontology", lang="en")))

    graph.add((ont, RDFS.label, Literal("地域医療アクセスオントロジー", lang="ja")))


def _add_specialty_scheme(graph: Graph, dataset: NormalizedDataset) -> None:

    scheme = URIRef(f"{BASE}concept/specialty")

    graph.add((scheme, RDF.type, SKOS.ConceptScheme))

    graph.add((scheme, SKOS.prefLabel, Literal("標榜診療科", lang="ja")))

    used_codes = {str(s.specialty_code) for s in dataset.services}

    for code in sorted(used_codes):
        concept = specialty_concept_uri(SpecialtyCode(code))

        graph.add((concept, RDF.type, SKOS.Concept))

        graph.add((concept, SKOS.inScheme, scheme))

        graph.add((concept, SKOS.notation, Literal(code)))

        label = dataset.specialty_labels.get(code)
        if label:
            graph.add((concept, SKOS.prefLabel, Literal(label, lang="ja")))


def _add_facility(graph: Graph, facility: Facility) -> None:

    f_ref = facility_uri(facility.facility_id)

    graph.add((f_ref, RDF.type, _FACILITY_CLASS[facility.facility_type]))

    graph.add((f_ref, EX.facilityId, Literal(str(facility.facility_id))))

    graph.add((f_ref, SCHEMA.name, Literal(facility.name, lang="ja")))

    a_ref = address_uri(facility.facility_id)

    graph.add((f_ref, SCHEMA.address, a_ref))

    graph.add((a_ref, RDF.type, SCHEMA.PostalAddress))

    graph.add((a_ref, SCHEMA.addressRegion, Literal(facility.address.prefecture, lang="ja")))

    graph.add((a_ref, SCHEMA.addressLocality, Literal(facility.address.city, lang="ja")))

    graph.add((a_ref, SCHEMA.streetAddress, Literal(facility.address.street_address, lang="ja")))

    if facility.geo is not None:
        from rdflib import BNode

        geo_node = BNode()
        graph.add((f_ref, SCHEMA.geo, geo_node))
        graph.add((geo_node, RDF.type, SCHEMA.GeoCoordinates))
        graph.add((geo_node, SCHEMA.latitude, Literal(facility.geo.latitude, datatype=XSD.double)))
        graph.add((geo_node, SCHEMA.longitude, Literal(facility.geo.longitude, datatype=XSD.double)))


def _add_schedules(graph: Graph, dataset: NormalizedDataset) -> None:

    counter: dict[tuple[str, str, DayOfWeek], int] = defaultdict(int)

    for schedule in dataset.schedules:
        key = (str(schedule.facility_id), str(schedule.specialty_code), schedule.day_of_week)

        counter[key] += 1

        sched_ref = schedule_uri(
            schedule.facility_id,
            schedule.specialty_code,
            schedule.day_of_week,
            counter[key],
        )

        service_ref = service_uri(schedule.facility_id, schedule.specialty_code)

        graph.add((service_ref, EX.hasSchedule, sched_ref))

        graph.add((sched_ref, RDF.type, SCHEMA.OpeningHoursSpecification))

        graph.add((sched_ref, SCHEMA.dayOfWeek, day_of_week_uri(schedule.day_of_week)))

        graph.add((sched_ref, SCHEMA.opens, Literal(schedule.opens, datatype=XSD.time)))

        graph.add((sched_ref, SCHEMA.closes, Literal(schedule.closes, datatype=XSD.time)))


def serialize_turtle(graph: Graph, path: Path) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)

    graph.serialize(destination=path, format="turtle")


def serialize_jsonld(graph: Graph, path: Path) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)

    graph.serialize(destination=path, format="json-ld")


_ = Namespace
