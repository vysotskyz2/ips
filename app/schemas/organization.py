from typing import Any

from pydantic import BaseModel, ConfigDict


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    osm_id: int | None = None
    building_id: int
    name: str
    category: str
    subcategory: str | None = None
    description: str | None = None
    unp: str | None = None
    floor: str | None = None
    room: str | None = None
    opening_hours: str | None = None
    phone: str | None = None
    website: str | None = None
    email: str | None = None
    lat: float | None = None
    lon: float | None = None
    metadata_json: dict[str, Any] | None = None
