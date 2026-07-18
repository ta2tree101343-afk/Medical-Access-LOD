from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from medical_access_lod.domain.models.clinical_service import ClinicalService
from medical_access_lod.domain.models.facility import Address, Facility, FacilityType
from medical_access_lod.domain.models.schedule import Schedule
from medical_access_lod.domain.values.day_of_week import DayOfWeek
from medical_access_lod.domain.values.facility_id import FacilityId
from medical_access_lod.domain.values.medical_specialty import SpecialtyCode, resolve_specialty
from medical_access_lod.domain.values.time_of_day import normalize_time, split_lunch_break


@dataclass(frozen=True)
class NormalizedDataset:
    facilities: list[Facility]

    services: list[ClinicalService]

    schedules: list[Schedule]


def _dedup_by_facility_id(rows: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    """施設IDによる重複除去。最初に現れた行を採用する。"""

    seen: dict[str, dict[str, object]] = {}

    for row in rows:
        fid = str(row["facility_id"])

        if fid not in seen:
            seen[fid] = row

    return list(seen.values())


def normalize_facilities(csv_path: Path) -> list[Facility]:

    df = pl.read_csv(csv_path)

    rows = _dedup_by_facility_id(df.to_dicts())

    return [
        Facility(
            facility_id=FacilityId(str(row["facility_id"])),
            name=str(row["name"]).strip(),
            facility_type=FacilityType(str(row["type"]).strip().lower()),
            address=Address(
                prefecture=str(row["prefecture"]).strip(),
                city=str(row["city"]).strip(),
                street_address=str(row["street_address"]).strip(),
            ),
        )
        for row in rows
    ]


def normalize_services(csv_path: Path) -> list[ClinicalService]:

    df = pl.read_csv(csv_path, schema_overrides={"specialty_code": pl.Utf8})

    seen: set[tuple[str, str]] = set()

    out: list[ClinicalService] = []

    for row in df.to_dicts():
        fid = str(row["facility_id"])

        code = resolve_specialty(str(row["specialty_code"]).strip())

        key = (fid, str(code))

        if key in seen:
            continue

        seen.add(key)

        out.append(ClinicalService(facility_id=FacilityId(fid), specialty_code=code))

    return out


def normalize_schedules(
    csv_path: Path, *, lunch_start: str | None = None, lunch_end: str | None = None
) -> list[Schedule]:
    """診療時間を正規化する。

    昼休み (lunch_start / lunch_end) が指定されている場合、
    opens..closes の範囲内であれば時間帯を分割する。
    """

    df = pl.read_csv(csv_path, schema_overrides={"specialty_code": pl.Utf8})

    out: list[Schedule] = []

    for row in df.to_dicts():
        fid = FacilityId(str(row["facility_id"]))

        code = resolve_specialty(str(row["specialty_code"]).strip())

        day = DayOfWeek.from_source(str(row["day_of_week"]))

        opens_raw = str(row["opens"]).strip()

        closes_raw = str(row["closes"]).strip()

        for opens, closes in split_lunch_break(opens_raw, closes_raw, lunch_start, lunch_end):
            out.append(
                Schedule(
                    facility_id=fid,
                    specialty_code=SpecialtyCode(str(code)),
                    day_of_week=day,
                    opens=normalize_time(opens),
                    closes=normalize_time(closes),
                )
            )

    return out


def normalize(
    facilities_csv: Path,
    services_csv: Path,
    schedules_csv: Path,
    *,
    lunch_start: str | None = None,
    lunch_end: str | None = None,
) -> NormalizedDataset:

    return NormalizedDataset(
        facilities=normalize_facilities(facilities_csv),
        services=normalize_services(services_csv),
        schedules=normalize_schedules(schedules_csv, lunch_start=lunch_start, lunch_end=lunch_end),
    )
