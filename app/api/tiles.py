from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.parcel import CadastralParcel
from app.services.cadastral_service import get_cadastral_geojson
from app.services.mvt_encoder import generate_cadastral_mvt_tile

router = APIRouter(tags=["Векторные тайлы и гео-слои"])


@router.get(
    "/tiles/cadastral/{z}/{x}/{y}.pbf",
    response_class=Response,
    summary="Векторный тайл MVT (.pbf) кадастровых участков и зданий РБ",
)
async def get_cadastral_vector_tile(
    z: int,
    x: int,
    y: int,
    session: AsyncSession = Depends(get_db),
) -> Response:
    if z < 0 or z > 22:
        raise HTTPException(status_code=400, detail="Некорректный уровень масштабирования z")
    max_xy = (1 << z) - 1
    if x < 0 or x > max_xy or y < 0 or y > max_xy:
        raise HTTPException(status_code=400, detail="Координаты тайла x/y выходят за пределы сетки")

    tile_bytes = await generate_cadastral_mvt_tile(session, z, x, y)
    return Response(
        content=tile_bytes,
        media_type="application/x-protobuf",
        headers={
            "Cache-Control": "public, max-age=60",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get(
    "/geojson/buildings",
    summary="GeoJSON-слой капитальных строений (зданий) РБ",
)
async def get_buildings_geojson_endpoint(
    city: str | None = Query(default=None, description="Фильтр по городу РБ"),
    building_type: str | None = Query(default=None, description="Фильтр по назначению здания"),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await get_cadastral_geojson(session, city=city, building_type=building_type)


@router.get(
    "/geojson/parcels",
    summary="GeoJSON-слой земельных участков (кадастровых кварталов) РБ",
)
async def get_parcels_geojson_endpoint(
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    res = await session.execute(select(CadastralParcel))
    parcels = res.scalars().all()
    features: list[dict[str, Any]] = []
    for p in parcels:
        try:
            geom = json.loads(p.geometry_geojson)
        except Exception:
            continue
        features.append(
            {
                "type": "Feature",
                "id": p.id,
                "geometry": geom,
                "properties": {
                    "id": p.id,
                    "cadastral_number": p.cadastral_number,
                    "name": p.name or "",
                    "region": p.region or "",
                    "district": p.district or "",
                    "land_use": p.land_use,
                    "purpose": p.purpose or "",
                    "ownership_type": p.ownership_type or "",
                    "area_sqm": round(p.area_sqm, 1),
                    "cadastral_value_byn": p.cadastral_value_byn,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
