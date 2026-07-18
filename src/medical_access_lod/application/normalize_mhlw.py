from __future__ import annotations

from pathlib import Path

import polars as pl

from medical_access_lod.application.normalize_data import NormalizedDataset
from medical_access_lod.domain.models.clinical_service import ClinicalService
from medical_access_lod.domain.models.facility import Address, Facility, FacilityType
from medical_access_lod.domain.models.schedule import Schedule
from medical_access_lod.domain.values.day_of_week import DayOfWeek
from medical_access_lod.domain.values.facility_id import FacilityId
from medical_access_lod.domain.values.medical_specialty import SpecialtyCode
from medical_access_lod.domain.values.time_of_day import normalize_time

CHIBA_PREF_CODE = 12

CHIBA_CITY_WARDS: dict[int, str] = {
    101: "千葉市中央区",
    102: "千葉市花見川区",
    103: "千葉市稲毛区",
    104: "千葉市若葉区",
    105: "千葉市緑区",
    106: "千葉市美浜区",
}

_DAY_COLS: list[tuple[str, str, DayOfWeek]] = [
    ("月_診療開始時間", "月_診療終了時間", DayOfWeek.MON),
    ("火_診療開始時間", "火_診療終了時間", DayOfWeek.TUE),
    ("水_診療開始時間", "水_診療終了時間", DayOfWeek.WED),
    ("木_診療開始時間", "木_診療終了時間", DayOfWeek.THU),
    ("金_診療開始時間", "金_診療終了時間", DayOfWeek.FRI),
    ("土_診療開始時間", "土_診療終了時間", DayOfWeek.SAT),
    ("日_診療開始時間", "日_診療終了時間", DayOfWeek.SUN),
]

_SPECIALTY_LABELS_SCRATCH: dict[str, str] = {}


def _load_facility_csv(path: Path, facility_type: FacilityType) -> pl.DataFrame:
    df = pl.read_csv(path, ignore_errors=True, infer_schema_length=1000)
    chiba = df.filter(
        (pl.col("都道府県コード") == CHIBA_PREF_CODE)
        & (pl.col("市区町村コード").is_in(list(CHIBA_CITY_WARDS.keys())))
    )
    return chiba.with_columns(pl.lit(facility_type.value).alias("_facility_type"))


def _load_schedule_csv(path: Path, allowed_ids: set[int]) -> pl.DataFrame:
    df = pl.read_csv(path, ignore_errors=True, infer_schema_length=1000)
    return df.filter(pl.col("ID").is_in(list(allowed_ids)))


def _strip_prefix(street: str, prefecture: str, city: str) -> str:
    remainder = street
    if remainder.startswith(prefecture):
        remainder = remainder[len(prefecture):]
    if remainder.startswith(city):
        remainder = remainder[len(city):]
    return remainder.strip() or street


def _build_facilities(facility_df: pl.DataFrame) -> list[Facility]:
    out: list[Facility] = []
    seen: set[str] = set()
    for row in facility_df.iter_rows(named=True):
        fid = str(row["ID"])
        if fid in seen:
            continue
        seen.add(fid)
        ward_code = int(row["市区町村コード"])
        city = CHIBA_CITY_WARDS.get(ward_code)
        if city is None:
            continue
        prefecture = "千葉県"
        street_raw = str(row.get("所在地") or "").strip()
        street = _strip_prefix(street_raw, prefecture, city) or street_raw or city
        ftype = FacilityType(str(row["_facility_type"]))
        out.append(
            Facility(
                facility_id=FacilityId(fid),
                name=str(row["正式名称"]).strip(),
                facility_type=ftype,
                address=Address(
                    prefecture=prefecture,
                    city=city,
                    street_address=street,
                ),
            )
        )
    return out


def _build_services_and_schedules(
    schedule_df: pl.DataFrame,
) -> tuple[list[ClinicalService], list[Schedule]]:
    service_seen: set[tuple[str, str]] = set()
    services: list[ClinicalService] = []
    schedules: list[Schedule] = []

    for row in schedule_df.iter_rows(named=True):
        fid_int = row.get("ID")
        code_int = row.get("診療科目コード")
        if fid_int is None or code_int is None:
            continue
        fid = FacilityId(str(int(fid_int)))
        code_str = f"{int(code_int):04d}"
        code = SpecialtyCode(code_str)
        label = row.get("診療科目名")
        if label:
            _SPECIALTY_LABELS_SCRATCH.setdefault(code_str, str(label).strip())

        key = (str(fid), str(code))
        if key not in service_seen:
            service_seen.add(key)
            services.append(ClinicalService(facility_id=fid, specialty_code=code))

        for opens_col, closes_col, day in _DAY_COLS:
            opens_raw = row.get(opens_col)
            closes_raw = row.get(closes_col)
            if not opens_raw or not closes_raw:
                continue
            opens_str = str(opens_raw).strip()
            closes_str = str(closes_raw).strip()
            if not opens_str or not closes_str:
                continue
            try:
                opens = normalize_time(opens_str)
                closes = normalize_time(closes_str)
            except ValueError:
                continue
            if opens >= closes:
                continue
            schedules.append(
                Schedule(
                    facility_id=fid,
                    specialty_code=code,
                    day_of_week=day,
                    opens=opens,
                    closes=closes,
                )
            )
    return services, schedules


def normalize_mhlw(raw_dir: Path) -> NormalizedDataset:
    """厚労省 医療情報ネットのオープンデータ (raw ZIP 展開後) から
    千葉市 (中央/花見川/稲毛/若葉/緑/美浜 の 6 区) を抽出して正規化する。"""
    hospital_facility_csv = next(raw_dir.glob("*hospital_facility_info*.csv"))
    clinic_facility_csv = next(raw_dir.glob("*clinic_facility_info*.csv"))
    hospital_hours_csv = next(raw_dir.glob("*hospital_speciality_hours*.csv"))
    clinic_hours_csv = next(raw_dir.glob("*clinic_speciality_hours*.csv"))

    hospital_df = _load_facility_csv(hospital_facility_csv, FacilityType.HOSPITAL)
    clinic_df = _load_facility_csv(clinic_facility_csv, FacilityType.CLINIC)
    facility_df = pl.concat([hospital_df, clinic_df], how="diagonal_relaxed")

    allowed_ids: set[int] = {int(v) for v in facility_df["ID"].to_list()}
    hospital_hours_df = _load_schedule_csv(hospital_hours_csv, allowed_ids)
    clinic_hours_df = _load_schedule_csv(clinic_hours_csv, allowed_ids)
    schedule_df = pl.concat([hospital_hours_df, clinic_hours_df], how="diagonal_relaxed")

    _SPECIALTY_LABELS_SCRATCH.clear()
    facilities = _build_facilities(facility_df)
    services, schedules = _build_services_and_schedules(schedule_df)

    keyed = {(str(s.facility_id), str(s.specialty_code)) for s in schedules}
    services = [s for s in services if (str(s.facility_id), str(s.specialty_code)) in keyed]

    covered_ids = {str(s.facility_id) for s in services}
    facilities = [f for f in facilities if str(f.facility_id) in covered_ids]

    used_codes = {str(s.specialty_code) for s in services}
    labels = {c: label for c, label in _SPECIALTY_LABELS_SCRATCH.items() if c in used_codes}

    return NormalizedDataset(
        facilities=facilities,
        services=services,
        schedules=schedules,
        specialty_labels=labels,
    )
