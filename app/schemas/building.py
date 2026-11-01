from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.organization import OrganizationRead


class ParcelSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cadastral_number: str
    name: str | None = None
    region: str | None = None
    district: str | None = None
    land_use: str
    purpose: str | None = None
    ownership_type: str | None = None
    area_sqm: float
    cadastral_value_byn: float | None = None


class BuildingDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    osm_id: int | None = None
    cadastral_number: str
    inventory_number: str | None = None
    parcel_id: int | None = None
    name: str | None = None
    building_type: str
    purpose_label: str | None = None
    address_region: str | None = None
    address_city: str
    address_street: str | None = None
    address_housenumber: str | None = None
    full_address: str
    postal_code: str | None = None
    floors: int
    year_built: int | None = None
    wall_material: str | None = None
    area_sqm: float
    center_lat: float
    center_lon: float
    geometry: dict[str, Any]
    parcel: ParcelSummary | None = None
    organizations_count: int = 0
    categories_summary: dict[str, int] = {}
    organizations: list[OrganizationRead] = []


class SearchResultItem(BaseModel):
    result_type: str  # "building" | "organization" | "parcel"
    id: int
    building_id: int | None = None
    title: str
    subtitle: str
    cadastral_number: str | None = None
    category: str | None = None
    city: str | None = None
    lat: float
    lon: float


class CadastralStats(BaseModel):
    total_parcels: int
    total_buildings: int
    total_organizations: int
    cities_covered: int
    buildings_by_type: dict[str, int]
    organizations_by_category: dict[str, int]
