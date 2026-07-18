from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from medical_access_lod.domain.values.day_of_week import DayOfWeek
from medical_access_lod.domain.values.facility_id import FacilityId
from medical_access_lod.domain.values.medical_specialty import SpecialtyCode


class Schedule(BaseModel):
    model_config = ConfigDict(frozen=True)

    facility_id: FacilityId

    specialty_code: SpecialtyCode

    day_of_week: DayOfWeek

    opens: str = Field(pattern=r"^\d{2}:\d{2}:\d{2}$")

    closes: str = Field(pattern=r"^\d{2}:\d{2}:\d{2}$")

    @model_validator(mode="after")
    def _opens_before_closes(self) -> Schedule:

        if self.opens >= self.closes:
            raise ValueError(f"opens must be < closes: {self.opens} >= {self.closes}")

        return self
