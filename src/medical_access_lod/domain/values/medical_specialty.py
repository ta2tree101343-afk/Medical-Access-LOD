from __future__ import annotations

import re
from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

_CODE_PATTERN = re.compile(r"^[0-9]{2}$")


DISPLAY_TO_CODE: dict[str, str] = {
    "内科": "01",
    "小児科": "02",
    "皮膚科": "03",
}

CODE_TO_DISPLAY: dict[str, str] = {v: k for k, v in DISPLAY_TO_CODE.items()}


class SpecialtyCode(str):
    """診療科の暫定コード (2桁ゼロ埋め文字列)。"""

    __slots__ = ()

    def __new__(cls, value: str) -> SpecialtyCode:

        if not isinstance(value, str) or not _CODE_PATTERN.match(value):
            raise ValueError(f"invalid specialty_code: {value!r}")

        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:

        return core_schema.no_info_after_validator_function(cls, handler(str))


def resolve_specialty(value: str) -> SpecialtyCode:
    """表示名またはコードから SpecialtyCode へ解決する。"""

    if _CODE_PATTERN.match(value):
        return SpecialtyCode(value)

    if value in DISPLAY_TO_CODE:
        return SpecialtyCode(DISPLAY_TO_CODE[value])

    raise ValueError(f"unknown specialty: {value!r}")
