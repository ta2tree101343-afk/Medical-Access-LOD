from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from medical_access_lod.domain.models.geo import GeoCoordinates
from medical_access_lod.domain.values.facility_id import FacilityId


class FacilityType(StrEnum):
    HOSPITAL = "hospital"

    CLINIC = "clinic"


class Address(BaseModel):
    model_config = ConfigDict(frozen=True)

    prefecture: str = Field(min_length=1)

    city: str = Field(min_length=1)

    street_address: str = Field(min_length=1)


class Facility(BaseModel):
    model_config = ConfigDict(frozen=True)

    facility_id: FacilityId

    name: str = Field(min_length=1)

    facility_type: FacilityType

    address: Address

    geo: GeoCoordinates | None = None
