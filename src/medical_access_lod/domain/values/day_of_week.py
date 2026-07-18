from __future__ import annotations

from enum import StrEnum


class DayOfWeek(StrEnum):
    """曜日。schema.org DayOfWeek のローカル名と一致させる。"""

    MON = "Monday"

    TUE = "Tuesday"

    WED = "Wednesday"

    THU = "Thursday"

    FRI = "Friday"

    SAT = "Saturday"

    SUN = "Sunday"

    @classmethod
    def from_source(cls, value: str) -> DayOfWeek:

        key = value.strip().upper()

        if key in cls.__members__:
            return cls[key]

        ja = {
            "月": "MON",
            "火": "TUE",
            "水": "WED",
            "木": "THU",
            "金": "FRI",
            "土": "SAT",
            "日": "SUN",
        }

        if key in ja:
            return cls[ja[key]]

        raise ValueError(f"unknown day_of_week: {value!r}")
