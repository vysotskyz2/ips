from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.building import CadastralStats, SearchResultItem
from app.services.cadastral_service import get_stats, search_cadastral

router = APIRouter(tags=["Поиск и статистика"])


@router.get(
    "/search",
    response_model=list[SearchResultItem],
    summary="Поиск по кадастровому номеру, адресу, названию здания или организации внутри здания",
)
async def search_objects(
    q: str = Query(default="", description="Поисковый запрос (адрес, кадастровый №, название организации, УНП)"),
    city: str | None = Query(default=None, description="Фильтр по городу РБ"),
    category: str | None = Query(default=None, description="Фильтр по категории организации"),
    limit: int = Query(default=25, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
) -> list[SearchResultItem]:
    return await search_cadastral(
        session=session,
        query=q,
        city=city,
        category=category,
        limit=limit,
    )


@router.get(
    "/stats",
    response_model=CadastralStats,
    summary="Сводная статистика кадастровой карты РБ",
)
async def get_cadastral_statistics(
    session: AsyncSession = Depends(get_db),
) -> CadastralStats:
    return await get_stats(session)
