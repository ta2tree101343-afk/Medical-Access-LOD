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

        raw = value.strip()
        key = raw.upper()

        if key in cls.__members__:
            return cls[key]

        # フル英名 (Monday / Tuesday / …) と一致する場合
        for member in cls:
            if member.value.upper() == key:
                return member

        # 日本語表記ゆれ (「月」「月曜」「月曜日」) の許容集合を明示。
        # `raw[:1] in ja` のような prefix 判定にすると「月末」→ MON、「日本」→ SUN
        # のような意図しない一致を許してしまうため、集合を明示列挙する。
        ja_forms = {
            "MON": ("月", "月曜", "月曜日"),
            "TUE": ("火", "火曜", "火曜日"),
            "WED": ("水", "水曜", "水曜日"),
            "THU": ("木", "木曜", "木曜日"),
            "FRI": ("金", "金曜", "金曜日"),
            "SAT": ("土", "土曜", "土曜日"),
            "SUN": ("日", "日曜", "日曜日"),
        }
        for member_key, forms in ja_forms.items():
            if raw in forms:
                return cls[member_key]

        raise ValueError(f"unknown day_of_week: {value!r}")
