from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

from shapely.geometry import Point, shape
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import AsyncSessionLocal, init_db, sync_postgis_geometries
from app.models.building import Building
from app.models.organization import Organization
from app.models.parcel import CadastralParcel
from app.services.cadastral_service import compute_polygon_area_sqm, fetch_osm_map_box
from app.services.geo_locator import resolve_district


def _source(org: Organization) -> str:
    return str((org.metadata_json or {}).get("source") or "").lower()


def _is_osm_org(org: Organization) -> bool:
    return "openstreetmap" in _source(org)


def _is_egr_org(org: Organization) -> bool:
    return "егр" in _source(org)


def _floor_label(tags: dict[str, str]) -> str | None:
    level = tags.get("addr:floor") or tags.get("level")
    if level is None:
        return None
    value = str(level).strip()
    if value.lstrip("-").isdigit():
        return f"{value} этаж"
    return value if "этаж" in value.lower() else f"Этаж {value}"


def _recompute_area(building: Building) -> None:
    try:
        geometry = shape(json.loads(building.geometry_geojson))
    except Exception:
        return
    polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
    area = 0.0
    for polygon in polygons:
        if polygon.is_empty:
            continue
        coords = [[float(x), float(y)] for x, y in polygon.exterior.coords]
        area += compute_polygon_area_sqm(coords)
    if area > 0:
        building.area_sqm = area


def _apply_building_tags(building: Building, tags: dict[str, str]) -> None:
    levels = tags.get("building:levels")
    try:
        building.floors = max(1, int(float(levels))) if levels else 0
    except (TypeError, ValueError):
        building.floors = 0

    building.wall_material = tags.get("building:material") or None
    start_date = tags.get("start_date") or ""
    if start_date[:4].isdigit():
        building.year_built = int(start_date[:4])
    if not building.name:
        building.name = tags.get("name:ru") or tags.get("name")
    if not building.postal_code:
        building.postal_code = tags.get("addr:postcode")


def _clean_egr_org(org: Organization) -> None:
    org.floor = None
    org.opening_hours = None
    org.lat = None
    org.lon = None
    if org.subcategory == "egr_active":
        org.subcategory = None
    if org.description:
        cleaned = (
            org.description.replace("Действующий субъект ЕГР РБ • ", "")
            .replace("Действующий субъект ЕГР РБ", "")
            .strip(" •")
        )
        org.description = cleaned or None


def _clean_osm_org_defaults(org: Organization) -> None:
    org.floor = None
    org.opening_hours = None
    org.description = None


def _match_building(osm_box: dict, building: Building) -> dict | None:
    osm_buildings = osm_box.get("buildings", [])
    for item in osm_buildings:
        if building.osm_id and item.get("osm_id") == building.osm_id:
            return item
    center = Point(building.center_lon, building.center_lat)
    for item in osm_buildings:
        try:
            if item["polygon"].buffer(0.00004).contains(center):
                return item
        except Exception:
            continue
    return None


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    settings.enable_live_osm_enrichment = True
    await init_db()

    async with AsyncSessionLocal() as session:
        buildings = list((await session.execute(select(Building))).scalars().all())
        organizations = list((await session.execute(select(Organization))).scalars().all())
        orgs_by_building: dict[int, list[Organization]] = defaultdict(list)
        for org in organizations:
            orgs_by_building[org.building_id].append(org)

        updated = 0
        osm_refreshed = 0
        osm_unavailable = 0

        for building in buildings:
            building.purpose_label = None
            building.inventory_number = None
            building.parcel_id = None
            _recompute_area(building)
            building.address_district = resolve_district(
                building.center_lat, building.center_lon, building.address_city
            )

            for org in orgs_by_building.get(building.id, []):
                if _is_egr_org(org):
                    _clean_egr_org(org)

            pad_lon = max((building.max_lon - building.min_lon) * 1.2, 0.0022)
            pad_lat = max((building.max_lat - building.min_lat) * 1.2, 0.0016)
            osm_box = await fetch_osm_map_box(
                building.center_lon - pad_lon,
                building.center_lat - pad_lat,
                building.center_lon + pad_lon,
                building.center_lat + pad_lat,
            )

            if not osm_box:
                building.floors = 0
                building.wall_material = None
                for org in orgs_by_building.get(building.id, []):
                    if _is_osm_org(org):
                        _clean_osm_org_defaults(org)
                osm_unavailable += 1
                continue

            matched = _match_building(osm_box, building)
            if matched is not None:
                _apply_building_tags(building, matched.get("tags") or {})
            else:
                building.floors = 0
                building.wall_material = None

            pois_by_id = {poi.get("osm_id"): poi for poi in osm_box.get("pois", [])}
            for org in orgs_by_building.get(building.id, []):
                if not _is_osm_org(org):
                    continue
                tags: dict[str, str] | None = None
                if building.osm_id and org.osm_id == building.osm_id and matched is not None:
                    tags = matched.get("tags") or {}
                elif org.osm_id in pois_by_id:
                    tags = pois_by_id[org.osm_id].get("tags") or {}
                if tags is None:
                    _clean_osm_org_defaults(org)
                    continue
                org.floor = _floor_label(tags)
                org.opening_hours = tags.get("opening_hours") or None
                org.description = tags.get("description:ru") or tags.get("description") or None

            updated += 1
            osm_refreshed += 1
            await session.flush()

        await session.execute(delete(CadastralParcel))
        await session.commit()
        await sync_postgis_geometries(session)

        print(
            "Очистка выдуманных полей завершена:\n"
            f"  - Зданий обработано: {updated}\n"
            f"  - OSM-данные обновлены: {osm_refreshed}\n"
            f"  - OSM был недоступен: {osm_unavailable}\n"
            "  - Синтетические земельные участки удалены"
        )


if __name__ == "__main__":
    asyncio.run(main())
