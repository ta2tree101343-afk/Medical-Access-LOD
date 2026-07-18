from __future__ import annotations

import re

_HHMM = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def normalize_time(value: str) -> str:
    """`H:MM` / `HH:MM` / `HH:MM:SS` を `HH:MM:SS` に統一する。"""

    m = _HHMM.match(value.strip())

    if not m:
        raise ValueError(f"invalid time: {value!r}")

    hour = int(m.group(1))

    minute = int(m.group(2))

    second = int(m.group(3) or 0)

    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"time out of range: {value!r}")

    return f"{hour:02d}:{minute:02d}:{second:02d}"


def split_lunch_break(
    opens: str, closes: str, lunch_start: str | None, lunch_end: str | None
) -> list[tuple[str, str]]:
    """昼休みで診療時間を分割する。

    lunch_start / lunch_end が None、または昼休みが opens..closes 範囲外の場合は分割しない。
    """

    opens_n = normalize_time(opens)

    closes_n = normalize_time(closes)

    if opens_n >= closes_n:
        raise ValueError(f"opens >= closes: {opens_n} >= {closes_n}")

    if lunch_start is None or lunch_end is None:
        return [(opens_n, closes_n)]

    ls = normalize_time(lunch_start)

    le = normalize_time(lunch_end)

    if ls >= le:
        raise ValueError(f"lunch_start >= lunch_end: {ls} >= {le}")

    if ls <= opens_n or le >= closes_n:
        return [(opens_n, closes_n)]

    return [(opens_n, ls), (le, closes_n)]
