from __future__ import annotations

from pathlib import Path

import pytest

from medical_access_lod.application.normalize_data import (
    NormalizedDataset,
    normalize,
    normalize_facilities,
    normalize_schedules,
    normalize_services,
)
from medical_access_lod.domain.values.day_of_week import DayOfWeek
from medical_access_lod.domain.values.facility_id import FacilityId
from medical_access_lod.domain.values.medical_specialty import (
    SpecialtyCode,
    resolve_specialty,
)
from medical_access_lod.domain.values.time_of_day import normalize_time, split_lunch_break

FIXTURES = Path(__file__).parent.parent.parent / "data" / "fixtures"


def test_facility_id_rejects_invalid() -> None:

    with pytest.raises(ValueError):
        FacilityId("bad id!")


def test_specialty_code_and_resolver() -> None:

    assert SpecialtyCode("01") == "01"

    assert resolve_specialty("内科") == "01"

    assert resolve_specialty("02") == "02"

    with pytest.raises(ValueError):
        resolve_specialty("宇宙外科")


def test_day_of_week_from_source() -> None:

    assert DayOfWeek.from_source("MON") == DayOfWeek.MON

    assert DayOfWeek.from_source("土") == DayOfWeek.SAT

    with pytest.raises(ValueError):
        DayOfWeek.from_source("XYZ")


def test_normalize_time_variants() -> None:

    assert normalize_time("9:00") == "09:00:00"

    assert normalize_time("09:00:00") == "09:00:00"

    with pytest.raises(ValueError):
        normalize_time("25:00")


def test_split_lunch_break_splits_when_within_range() -> None:

    result = split_lunch_break("09:00", "19:00", "12:00", "13:00")

    assert result == [("09:00:00", "12:00:00"), ("13:00:00", "19:00:00")]


def test_split_lunch_break_noop_when_none() -> None:

    assert split_lunch_break("09:00", "17:00", None, None) == [("09:00:00", "17:00:00")]


def test_split_lunch_break_noop_when_out_of_range() -> None:

    assert split_lunch_break("09:00", "12:00", "12:00", "13:00") == [("09:00:00", "12:00:00")]


def test_normalize_facilities_dedups_by_facility_id() -> None:

    facilities = normalize_facilities(FIXTURES / "facilities.csv")

    ids = [f.facility_id for f in facilities]

    assert len(ids) == 4

    assert len(set(ids)) == 4


def test_normalize_services_dedups_pairs() -> None:

    services = normalize_services(FIXTURES / "services.csv")

    assert len(services) == 5


def test_normalize_schedules_returns_split_rows_from_fixture() -> None:

    schedules = normalize_schedules(FIXTURES / "schedules.csv")

    assert len(schedules) == 13

    for s in schedules:
        assert s.opens < s.closes

        assert len(s.opens) == 8


def test_normalize_end_to_end() -> None:

    ds = normalize(
        FIXTURES / "facilities.csv",
        FIXTURES / "services.csv",
        FIXTURES / "schedules.csv",
    )

    assert isinstance(ds, NormalizedDataset)

    assert len(ds.facilities) == 4

    assert len(ds.services) == 5

    assert len(ds.schedules) == 13
