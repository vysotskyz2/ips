from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AreaImportRequest(BaseModel):
    geometry: dict[str, Any] = Field(
        ...,
        description="Геометрия выделенной области GeoJSON Polygon или MultiPolygon (WGS84)",
    )


class AreaImportCounters(BaseModel):
    buildings_added: int = 0
    buildings_updated: int = 0
    organizations_added: int = 0
    tiles_requested: int = 0
    tiles_ok: int = 0
    tiles_failed: int = 0


class AreaImportResponse(BaseModel):
    area_sqm: float
    area_km2: float
    data_scope: str
    warnings: list[str] = Field(default_factory=list)

    buildings_total: int = 0
    buildings_with_organizations: int = 0
    buildings_without_organizations: int = 0

    organizations_total: int = 0
    organizations_osm: int = 0
    organizations_egr: int = 0
    organizations_other: int = 0

    egr_active: int = 0
    egr_excluded: int = 0
    egr_other: int = 0

    buildings_by_type: dict[str, int] = Field(default_factory=dict)
    organizations_by_category: dict[str, int] = Field(default_factory=dict)
    buildings_by_city: dict[str, int] = Field(default_factory=dict)
    buildings_by_district: dict[str, int] = Field(default_factory=dict)

    import_stats: AreaImportCounters = Field(default_factory=AreaImportCounters)
