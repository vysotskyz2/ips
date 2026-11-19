from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.analytics import AreaImportRequest, AreaImportResponse
from app.services.area_import_service import (
    InvalidAreaGeometry,
    import_area_and_compute_stats,
)

router = APIRouter(prefix="/areas", tags=["Аналитика выделенной области"])


@router.post(
    "/import-and-stats",
    response_model=AreaImportResponse,
    summary="Импортировать объекты OSM в выделенной области и вернуть статистику",
)
async def import_area_and_stats(
    payload: AreaImportRequest,
    session: AsyncSession = Depends(get_db),
) -> AreaImportResponse:
    try:
        return await import_area_and_compute_stats(session, payload.geometry)
    except InvalidAreaGeometry as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
