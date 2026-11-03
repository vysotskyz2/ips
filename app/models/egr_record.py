from __future__ import annotations

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EgrRecord(Base):
    """
    Актуальная запись о действующем юридическом лице / государственном органе
    из Единого государственного регистра (egr.gov.by).
    Хранятся ТОЛЬКО действующие субъекты (nksost=1 'Действующий', cact='+', dto=None).
    """

    __tablename__ = "egr_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    unp: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(350), nullable=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(600), nullable=True)
    status_label: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Действующий"
    )
    category: Mapped[str] = mapped_column(
        String(100), nullable=False, default="Офисы и организации"
    )
    oked_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    oked_name: Mapped[str | None] = mapped_column(String(400), nullable=True)

    postal_code: Mapped[str | None] = mapped_column(String(24), nullable=True)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    district: Mapped[str | None] = mapped_column(String(120), nullable=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    street_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    street: Mapped[str] = mapped_column(String(180), nullable=False)
    housenumber: Mapped[str] = mapped_column(String(48), nullable=False)
    korpus: Mapped[str | None] = mapped_column(String(32), nullable=True)
    room_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    room: Mapped[str | None] = mapped_column(String(96), nullable=True)

    phone: Mapped[str | None] = mapped_column(String(180), nullable=True)
    email: Mapped[str | None] = mapped_column(String(180), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)

    norm_city: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    norm_street: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    norm_house: Mapped[str] = mapped_column(String(48), nullable=False, index=True)

    __table_args__ = (
        Index("idx_egr_norm_addr", "norm_city", "norm_street", "norm_house"),
    )
