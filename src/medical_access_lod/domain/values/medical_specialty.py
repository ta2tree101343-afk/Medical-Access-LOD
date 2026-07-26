from __future__ import annotations

import re
from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

# 厚生労働省 医療情報ネットの標榜診療科コードは 4 桁 (例: 1001=内科, 3001=小児科)。
# 2 桁のプレースホルダは 2026-07 に廃止済 (fixture も 4 桁に統一)。
_CODE_PATTERN = re.compile(r"^[0-9]{4}$")


DISPLAY_TO_CODE: dict[str, str] = {
    "内科": "1001",
    "小児科": "3001",
    "皮膚科": "6001",
}

CODE_TO_DISPLAY: dict[str, str] = {v: k for k, v in DISPLAY_TO_CODE.items()}


class SpecialtyCode(str):
    """診療科コード (厚生労働省 医療情報ネットの 4 桁コード)。"""

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
