from __future__ import annotations

import re
from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

_PATTERN = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


class FacilityId(str):
    """データ提供元が付与する施設ID。表示名は識別子に使わない。"""

    __slots__ = ()

    def __new__(cls, value: str) -> FacilityId:

        if not isinstance(value, str) or not _PATTERN.match(value):
            raise ValueError(f"invalid facility_id: {value!r}")

        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:

        return core_schema.no_info_after_validator_function(cls, handler(str))
