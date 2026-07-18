"""URI 生成規則。

`ex:` は語彙（クラス・プロパティ）専用。リソースは @base + 相対IRI で書くため、
本モジュールは Turtle シリアライズ後の見た目に依らず、絶対 URI を生成する。
（RDFLib は Graph 内では絶対 URI を保持し、Turtle 出力時に @base で相対化される）
"""

from __future__ import annotations

from rdflib import Namespace, URIRef

from medical_access_lod.domain.values.day_of_week import DayOfWeek
from medical_access_lod.domain.values.facility_id import FacilityId
from medical_access_lod.domain.values.medical_specialty import SpecialtyCode

BASE = "https://example.org/medical-access/"

EX = Namespace(BASE)

SCHEMA = Namespace("https://schema.org/")

SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")


def facility_uri(facility_id: FacilityId) -> URIRef:

    return URIRef(f"{BASE}resource/facility/{facility_id}")


def address_uri(facility_id: FacilityId) -> URIRef:

    return URIRef(f"{BASE}resource/address/{facility_id}")


def service_uri(facility_id: FacilityId, specialty_code: SpecialtyCode) -> URIRef:

    return URIRef(f"{BASE}resource/service/{facility_id}/{specialty_code}")


def schedule_uri(
    facility_id: FacilityId,
    specialty_code: SpecialtyCode,
    day: DayOfWeek,
    sequence: int,
) -> URIRef:

    if sequence < 1:
        raise ValueError(f"sequence must be >= 1: {sequence}")

    return URIRef(f"{BASE}resource/schedule/{facility_id}/{specialty_code}/{day.name}/{sequence}")


def specialty_concept_uri(specialty_code: SpecialtyCode) -> URIRef:

    return URIRef(f"{BASE}concept/specialty/{specialty_code}")


def day_of_week_uri(day: DayOfWeek) -> URIRef:

    return URIRef(f"{SCHEMA}{day.value}")
