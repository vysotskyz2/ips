from collections.abc import AsyncGenerator
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Инициализирует схему БД и расширения PostGIS (при работе с PostgreSQL)."""
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        if settings.is_postgres:
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))
            except Exception as exc:
                logger.warning("Не удалось активировать расширения PostGIS: %s", exc)

        await conn.run_sync(Base.metadata.create_all)
        await _ensure_runtime_columns(conn)
        await _sanitize_placeholder_cities(conn)
        await _sanitize_inconsistent_city_values(conn)

        if settings.is_postgres:
            await _setup_postgis_columns_and_indexes(conn)


async def _ensure_runtime_columns(conn) -> None:
    """Добавляет новые колонки в существующую БД без потери данных."""
    if settings.is_postgres:
        try:
            await conn.execute(
                text("ALTER TABLE buildings ADD COLUMN IF NOT EXISTS address_district VARCHAR(120);")
            )
            await conn.execute(
                text("CREATE INDEX IF NOT EXISTS idx_buildings_district ON buildings (address_district);")
            )
        except Exception as exc:
            logger.debug("Не удалось добавить address_district: %s", exc)
        return

    try:
        columns = await conn.execute(text("PRAGMA table_info(buildings)"))
        existing = {row[1] for row in columns.fetchall()}
        if "address_district" not in existing:
            await conn.execute(text("ALTER TABLE buildings ADD COLUMN address_district VARCHAR(120);"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_buildings_district ON buildings (address_district);")
        )
    except Exception as exc:
        logger.debug("Не удалось добавить address_district в SQLite: %s", exc)


async def _sanitize_placeholder_cities(conn) -> None:
    """Удаляет выдуманные значения в address_city (область/район вместо города)."""
    try:
        result = await conn.execute(
            text(
                """
                UPDATE buildings
                SET address_city = ''
                WHERE address_city IS NOT NULL
                  AND (
                    LOWER(address_city) LIKE '%область'
                    OR LOWER(address_city) LIKE '%район'
                    OR LOWER(address_city) IN ('республика беларусь', 'рб', 'беларусь', '—', '-')
                  );
                """
            )
        )
        rowcount = getattr(result, "rowcount", 0) or 0
        if isinstance(rowcount, int) and rowcount > 0:
            logger.info("Очищены выдуманные значения address_city у %s зданий", rowcount)
    except Exception as exc:
        logger.debug("Не удалось очистить выдуманные значения address_city: %s", exc)


async def _sanitize_inconsistent_city_values(conn) -> None:
    """Очищает address_city, если сохранённый город не относится к реальной области здания."""
    from app.services.geo_locator import (
        city_matches_region,
        detect_belarus_region_and_soato,
    )

    try:
        rows = await conn.execute(
            text(
                "SELECT id, address_city, center_lat, center_lon FROM buildings "
                "WHERE address_city IS NOT NULL AND address_city <> ''"
            )
        )
        updates: list[dict[str, int]] = []
        for building_id, city, lat, lon in rows:
            if lat is None or lon is None:
                continue
            region_name, _, _ = detect_belarus_region_and_soato(float(lat), float(lon))
            if not city_matches_region(city, region_name):
                updates.append({"building_id": building_id})

        if updates:
            await conn.execute(
                text("UPDATE buildings SET address_city = '' WHERE id = :building_id"),
                updates,
            )
            logger.info(
                "Очищены несоответствующие области значения address_city у %s зданий",
                len(updates),
            )
    except Exception as exc:
        logger.debug("Не удалось проверить соответствие address_city области: %s", exc)


async def _setup_postgis_columns_and_indexes(conn) -> None:
    """Создает нативные геометрические колонки PostGIS (4326 и 3857) и GIST-индексы."""
    statements = [
        """
        ALTER TABLE cadastral_parcels
        ADD COLUMN IF NOT EXISTS geom geometry(MultiPolygon, 4326),
        ADD COLUMN IF NOT EXISTS geom_3857 geometry(MultiPolygon, 3857);
        """,
        """
        ALTER TABLE buildings
        ADD COLUMN IF NOT EXISTS geom geometry(MultiPolygon, 4326),
        ADD COLUMN IF NOT EXISTS geom_3857 geometry(MultiPolygon, 3857);
        """,
        """
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326);
        """,
        "CREATE INDEX IF NOT EXISTS idx_parcels_geom_gist ON cadastral_parcels USING GIST (geom);",
        "CREATE INDEX IF NOT EXISTS idx_parcels_geom_3857_gist ON cadastral_parcels USING GIST (geom_3857);",
        "CREATE INDEX IF NOT EXISTS idx_buildings_geom_gist ON buildings USING GIST (geom);",
        "CREATE INDEX IF NOT EXISTS idx_buildings_geom_3857_gist ON buildings USING GIST (geom_3857);",
        "CREATE INDEX IF NOT EXISTS idx_organizations_geom_gist ON organizations USING GIST (geom);",
    ]
    for stmt in statements:
        try:
            await conn.execute(text(stmt))
        except Exception as exc:
            logger.debug("Пропуск создания PostGIS-индекса: %s", exc)


async def sync_postgis_geometries(session: AsyncSession) -> None:
    """Синхронизирует GeoJSON-геометрию в нативные колонки PostGIS geom и geom_3857."""
    if not settings.is_postgres:
        return
    try:
        await session.execute(
            text(
                """
                UPDATE cadastral_parcels
                SET geom = ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(geometry_geojson), 4326)),
                    geom_3857 = ST_Transform(ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(geometry_geojson), 4326)), 3857)
                WHERE geom IS NULL AND geometry_geojson IS NOT NULL;
                """
            )
        )
        await session.execute(
            text(
                """
                UPDATE buildings
                SET geom = ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(geometry_geojson), 4326)),
                    geom_3857 = ST_Transform(ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(geometry_geojson), 4326)), 3857)
                WHERE geom IS NULL AND geometry_geojson IS NOT NULL;
                """
            )
        )
        await session.execute(
            text(
                """
                UPDATE organizations
                SET geom = ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                WHERE geom IS NULL AND lon IS NOT NULL AND lat IS NOT NULL;
                """
            )
        )
        await session.commit()
    except Exception as exc:
        logger.warning("Не удалось синхронизировать PostGIS геометрии: %s", exc)
