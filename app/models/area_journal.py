from __future__ import annotations

from typing import Any

from sqlalchemy import Float, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AreaJournalEntry(Base):
    """Сохранённое выделение области и статистика на момент импорта."""

    __tablename__ = "area_journal_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    area_sqm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    area_km2: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    data_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="full")
    buildings_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    organizations_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    geometry_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
