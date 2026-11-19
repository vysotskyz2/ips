from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.parcel import CadastralParcel


class Building(Base):
    """Капитальное строение (здание, сооружение) Республики Беларусь."""

    __tablename__ = "buildings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    osm_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    cadastral_number: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    inventory_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parcel_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cadastral_parcels.id", ondelete="SET NULL"), nullable=True, index=True
    )

    name: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    building_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="commercial", index=True
    )
    purpose_label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    address_region: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    address_district: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    address_city: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    address_street: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    address_housenumber: Mapped[str | None] = mapped_column(String(48), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(24), nullable=True)

    floors: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    year_built: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wall_material: Mapped[str | None] = mapped_column(String(100), nullable=True)
    area_sqm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    center_lat: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    center_lon: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    min_lat: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    min_lon: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    max_lat: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    max_lon: Mapped[float] = mapped_column(Float, nullable=False, index=True)

    geometry_geojson: Mapped[str] = mapped_column(Text, nullable=False)

    parcel: Mapped[CadastralParcel | None] = relationship(
        "CadastralParcel", back_populates="buildings"
    )
    organizations: Mapped[list[Organization]] = relationship(
        "Organization",
        back_populates="building",
        cascade="all, delete-orphan",
        order_by="Organization.floor, Organization.name",
    )

    __table_args__ = (
        Index("idx_building_bbox", "min_lon", "max_lon", "min_lat", "max_lat"),
        Index("idx_building_city_street", "address_city", "address_street"),
    )

    @property
    def full_address(self) -> str:
        parts: list[str] = []
        if self.address_city:
            city = self.address_city.strip()
            is_region_or_district = city.lower().endswith(("область", "район"))
            if city.startswith(("г.", "г ")) or is_region_or_district:
                parts.append(city)
            else:
                parts.append(f"г. {city}")
        if self.address_street:
            parts.append(self.address_street)
        if self.address_housenumber:
            parts.append(f"д. {self.address_housenumber}")
        return ", ".join(parts) if parts else "Адрес не указан"
