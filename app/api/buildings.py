from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.building import BuildingDetail
from app.services.cadastral_service import (
    get_building_detail,
    get_or_enrich_building_by_coords,
)

router = APIRouter(prefix="/buildings", tags=["Здания и организации внутри"])


class BuildingInspectRequest(BaseModel):
    lat: float = Field(..., description="Широта точки клика (WGS84)")
    lon: float = Field(..., description="Долгота точки клика (WGS84)")
    name: str | None = None
    street: str | None = None
    housenumber: str | None = None
    city: str | None = None
    floors: int | None = None
    building_type: str | None = None
    osm_id: int | None = None
    geometry: dict[str, Any] | None = None


@router.get(
    "/by-coords",
    response_model=BuildingDetail,
    summary="Получить паспорт здания и список организаций по координатам клика на карте РБ",
)
async def get_building_by_coordinates(
    lat: float = Query(..., description="Широта (WGS84)"),
    lon: float = Query(..., description="Долгота (WGS84)"),
    name: str | None = Query(default=None),
    street: str | None = Query(default=None),
    housenumber: str | None = Query(default=None),
    city: str | None = Query(default=None),
    floors: int | None = Query(default=None),
    building_type: str | None = Query(default=None),
    osm_id: int | None = Query(default=None),
    session: AsyncSession = Depends(get_db),
) -> BuildingDetail:
    detail = await get_or_enrich_building_by_coords(
        session=session,
        lat=lat,
        lon=lon,
        hint_name=name,
        hint_street=street,
        hint_housenumber=housenumber,
        hint_city=city,
        hint_floors=floors,
        hint_type=building_type,
        hint_osm_id=osm_id,
    )
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail="Объект в указанных координатах не найден или находится за пределами Республики Беларусь",
        )
    return detail


@router.post(
    "/inspect",
    response_model=BuildingDetail,
    summary="Инспекция выбранного здания на карте РБ (с передачей геометрии полигона и подгрузкой организаций)",
)
async def inspect_building_on_map(
    payload: BuildingInspectRequest,
    session: AsyncSession = Depends(get_db),
) -> BuildingDetail:
    detail = await get_or_enrich_building_by_coords(
        session=session,
        lat=payload.lat,
        lon=payload.lon,
        hint_name=payload.name,
        hint_street=payload.street,
        hint_housenumber=payload.housenumber,
        hint_city=payload.city,
        hint_floors=payload.floors,
        hint_type=payload.building_type,
        hint_osm_id=payload.osm_id,
        hint_geometry=payload.geometry,
    )
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail="Здание в указанных координатах не найдено или находится вне территории РБ",
        )
    return detail


@router.get(
    "/{building_id}",
    response_model=BuildingDetail,
    summary="Получить кадастровый паспорт здания и все организации внутри него по ID",
)
async def get_building_by_id(
    building_id: int,
    session: AsyncSession = Depends(get_db),
) -> BuildingDetail:
    detail = await get_building_detail(session, building_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Здание с ID {building_id} не найдено")
    return detail
