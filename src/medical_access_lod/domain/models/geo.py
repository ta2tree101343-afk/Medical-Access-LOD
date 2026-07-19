from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GeoCoordinates(BaseModel):
    model_config = ConfigDict(frozen=True)

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
