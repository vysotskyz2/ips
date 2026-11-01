from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.building import Building


class CadastralParcel(Base):
    """Земельный участок / кадастровый квартал Республики Беларусь."""

    __tablename__ = "cadastral_parcels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cadastral_number: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    district: Mapped[str | None] = mapped_column(String(120), nullable=True)
    land_use: Mapped[str] = mapped_column(String(120), nullable=False, default="commercial")
    purpose: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ownership_type: Mapped[str | None] = mapped_column(
        String(120), nullable=True, default="Государственная собственность"
    )
    area_sqm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cadastral_value_byn: Mapped[float | None] = mapped_column(Float, nullable=True)

    center_lat: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    center_lon: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    min_lat: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    min_lon: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    max_lat: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    max_lon: Mapped[float] = mapped_column(Float, nullable=False, index=True)

    geometry_geojson: Mapped[str] = mapped_column(Text, nullable=False)

    buildings: Mapped[list[Building]] = relationship(
        "Building", back_populates="parcel", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_parcel_bbox", "min_lon", "max_lon", "min_lat", "max_lat"),
    )
