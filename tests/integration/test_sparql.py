from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph

from medical_access_lod.application.normalize_data import normalize
from medical_access_lod.infrastructure.rdf.graph_builder import build_graph

FIXTURES = Path(__file__).parent.parent.parent / "data" / "fixtures"

QUERIES = Path(__file__).parent.parent.parent / "queries"


@pytest.fixture(scope="module")
def graph() -> Graph:

    dataset = normalize(
        FIXTURES / "facilities.csv",
        FIXTURES / "services.csv",
        FIXTURES / "schedules.csv",
    )

    return build_graph(dataset)


def _load(name: str) -> str:

    return (QUERIES / name).read_text(encoding="utf-8")


def test_facilities_by_specialty_internal(graph: Graph) -> None:

    results = list(graph.query(_load("facilities_by_specialty.rq")))

    names = sorted(str(row[1]) for row in results)

    assert names == ["千葉中央総合病院", "花見川内科クリニック"]


def test_saturday_pediatrics(graph: Graph) -> None:

    results = list(graph.query(_load("saturday_pediatrics.rq")))

    assert len(results) == 1

    row = results[0]

    assert str(row[1]) == "こばやし小児科クリニック"

    assert str(row[2]) == "09:00:00"

    assert str(row[3]) == "13:00:00"


def test_weekday_dermatology_open_at_18(graph: Graph) -> None:

    results = list(graph.query(_load("weekday_dermatology_open_at_18.rq")))

    assert len(results) == 2

    names_days = {(str(r[1]), str(r[2]).rsplit("/", 1)[-1]) for r in results}

    assert names_days == {
        ("みなと皮膚科医院", "Monday"),
        ("みなと皮膚科医院", "Friday"),
    }


def test_09_to_18_facility_is_not_matched_as_open_at_18(graph: Graph) -> None:
    """closes = 18:00:00 (排他) は 18時受診可としてヒットさせない。
    fixture の 1210000004 (内科 月 09:00-18:00) が皮膚科クエリに乗らないのは当然として、
    STR比較の境界を独立に確認する。"""

    q = """
    BASE <https://example.org/medical-access/>
    PREFIX ex: <https://example.org/medical-access/>
    PREFIX schema: <https://schema.org/>
    SELECT ?f ?opens ?closes WHERE {
        ?f ex:offersClinicalService ?s .
        ?s ex:hasSchedule ?sched .
        ?sched schema:opens ?opens ; schema:closes ?closes .
        FILTER(STR(?opens) <= "18:00:00" && STR(?closes) > "18:00:00")
    }
    """

    results = list(graph.query(q))

    for _f, opens, closes in results:
        assert str(opens) <= "18:00:00"

        assert str(closes) > "18:00:00"

    facility_uris = {str(r[0]) for r in results}

    assert "https://example.org/medical-access/resource/facility/1210000004" not in facility_uris


def test_no_results_for_impossible_condition(graph: Graph) -> None:

    q = """
    BASE <https://example.org/medical-access/>
    PREFIX ex: <https://example.org/medical-access/>
    SELECT ?f WHERE {
        ?f ex:offersClinicalService ?s .
        ?s ex:medicalSpecialty <concept/specialty/99> .
    }
    """

    results = list(graph.query(q))

    assert results == []
