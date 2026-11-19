from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.analytics import (
    AreaImportRequest,
    AreaImportResponse,
    AreaJournalEntryDetail,
    AreaJournalEntrySummary,
)
from app.services.area_import_service import (
    InvalidAreaGeometry,
    delete_area_journal_entry,
    get_area_journal_entry,
    import_area_and_compute_stats,
    list_area_journal_entries,
    save_area_journal_entry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/areas", tags=["Аналитика выделенной области"])


def _journal_summary(entry) -> AreaJournalEntrySummary:
    return AreaJournalEntrySummary(
        id=entry.id,
        created_at=entry.created_at,
        area_sqm=entry.area_sqm,
        area_km2=entry.area_km2,
        data_scope=entry.data_scope,
        buildings_total=entry.buildings_total,
        organizations_total=entry.organizations_total,
        geometry=entry.geometry_json,
    )


@router.post(
    "/import-and-stats",
    response_model=AreaImportResponse,
    summary="Импортировать объекты OSM в выделенной области, вернуть статистику и сохранить её в журнал",
)
async def import_area_and_stats(
    payload: AreaImportRequest,
    session: AsyncSession = Depends(get_db),
) -> AreaImportResponse:
    try:
        stats = await import_area_and_compute_stats(session, payload.geometry)
    except InvalidAreaGeometry as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        entry = await save_area_journal_entry(session, payload.geometry, stats)
        stats.journal_id = entry.id
        stats.journal_created_at = entry.created_at
    except Exception as exc:
        logger.warning("Не удалось сохранить выделенную область в журнал: %s", exc)
        stats.warnings.append("Не удалось сохранить выделенную область в журнал.")
    return stats


@router.get(
    "/journal",
    response_model=list[AreaJournalEntrySummary],
    summary="Список сохранённых областей и их статистики",
)
async def list_journal_entries(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
) -> list[AreaJournalEntrySummary]:
    entries = await list_area_journal_entries(session, limit)
    return [_journal_summary(entry) for entry in entries]


@router.get(
    "/journal/{entry_id}",
    response_model=AreaJournalEntryDetail,
    summary="Получить сохранённую область и её статистику",
)
async def get_journal_entry(
    entry_id: int,
    session: AsyncSession = Depends(get_db),
) -> AreaJournalEntryDetail:
    entry = await get_area_journal_entry(session, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Запись журнала не найдена")
    summary = _journal_summary(entry)
    return AreaJournalEntryDetail(
        **summary.model_dump(),
        stats=AreaImportResponse.model_validate(entry.stats_json),
    )


@router.delete(
    "/journal/{entry_id}",
    status_code=204,
    summary="Удалить запись журнала областей",
)
async def delete_journal_entry(
    entry_id: int,
    session: AsyncSession = Depends(get_db),
) -> Response:
    deleted = await delete_area_journal_entry(session, entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Запись журнала не найдена")
    return Response(status_code=204)
