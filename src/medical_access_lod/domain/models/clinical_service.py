from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from medical_access_lod.domain.values.facility_id import FacilityId
from medical_access_lod.domain.values.medical_specialty import SpecialtyCode


class ClinicalService(BaseModel):
    model_config = ConfigDict(frozen=True)

    facility_id: FacilityId

    specialty_code: SpecialtyCode
