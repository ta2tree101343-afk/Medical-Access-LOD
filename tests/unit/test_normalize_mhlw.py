from __future__ import annotations

from pathlib import Path

from medical_access_lod.application.normalize_mhlw import (
    CHIBA_CITY_WARDS,
    normalize_mhlw,
)
from medical_access_lod.domain.models.facility import FacilityType

SAMPLE = Path(__file__).parent.parent / "fixtures" / "mhlw_sample"


def test_normalize_mhlw_filters_chiba_only() -> None:
    ds = normalize_mhlw(SAMPLE)
    for f in ds.facilities:
        assert f.address.prefecture == "千葉県"
        assert f.address.city in CHIBA_CITY_WARDS.values()


def test_normalize_mhlw_produces_facilities_services_and_schedules() -> None:
    ds = normalize_mhlw(SAMPLE)
    assert len(ds.facilities) >= 1
    assert len(ds.services) >= 1
    assert len(ds.schedules) >= 1


def test_service_ids_are_covered_by_schedules() -> None:
    ds = normalize_mhlw(SAMPLE)
    schedule_keys = {(str(s.facility_id), str(s.specialty_code)) for s in ds.schedules}
    for svc in ds.services:
        assert (str(svc.facility_id), str(svc.specialty_code)) in schedule_keys


def test_facility_ids_are_all_present_in_services() -> None:
    ds = normalize_mhlw(SAMPLE)
    service_fids = {str(s.facility_id) for s in ds.services}
    for f in ds.facilities:
        assert str(f.facility_id) in service_fids


def test_hospital_and_clinic_types_are_used() -> None:
    ds = normalize_mhlw(SAMPLE)
    types = {f.facility_type for f in ds.facilities}
    assert FacilityType.HOSPITAL in types or FacilityType.CLINIC in types


def test_schedule_times_are_hhmmss_and_opens_before_closes() -> None:
    ds = normalize_mhlw(SAMPLE)
    for s in ds.schedules:
        assert len(s.opens) == 8
        assert len(s.closes) == 8
        assert s.opens < s.closes


def test_specialty_labels_cover_all_used_codes() -> None:
    ds = normalize_mhlw(SAMPLE)
    used_codes = {str(s.specialty_code) for s in ds.services}
    for code in used_codes:
        assert code in ds.specialty_labels, f"missing label for {code}"
        assert ds.specialty_labels[code].strip() != ""
