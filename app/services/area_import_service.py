from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import Counter
from typing import Any

from shapely import transform as shapely_transform
from shapely.geometry import Point, Polygon, box, shape
from shapely.strtree import STRtree
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import sync_postgis_geometries
from app.models.building import Building
from app.models.organization import Organization
from app.schemas.analytics import AreaImportCounters, AreaImportResponse
from app.services.address_matcher import (
    canonicalize_city_name,
    has_address_conflict,
    translate_belarusian_street_to_ru,
)
from app.services.cadastral_service import (
    _classify_osm_poi,
    _extract_poi_name,
    _is_poi_element,
    compute_polygon_area_sqm,
    fetch_osm_map_box,
    generate_cadastral_numbers,
)
from app.services.egr_service import attach_egr_records_to_building
from app.services.geo_locator import (
    detect_belarus_region_and_soato,
    is_valid_city_name,
    resolve_district,
)

logger = logging.getLogger(__name__)

BELARUS_BBOX = (23.1, 51.2, 32.85, 56.25)
ENTRANCE_TOLERANCE_DEG = 0.00018  # ~18 метров у входа в здание
STRTREE_BUFFER_DEG = 0.00022
DB_QUERY_PADDING_DEG = 1e-7


class InvalidAreaGeometry(ValueError):
    """Выделенная область имеет некорректную или неподдерживаемую геометрию."""


def parse_area_geometry(geometry: dict[str, Any] | None) -> Any:
    """Проверяет и возвращает валидную геометрию Polygon/MultiPolygon из GeoJSON."""
    if not isinstance(geometry, dict):
        raise InvalidAreaGeometry("Геометрия области должна быть GeoJSON-объектом")
    try:
        geom = shape(geometry)
    except Exception as exc:
        raise InvalidAreaGeometry(f"Не удалось разобрать геометрию области: {exc}") from exc

    if geom.is_empty:
        raise InvalidAreaGeometry("Геометрия области пуста")
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise InvalidAreaGeometry("Поддерживаются только Polygon или MultiPolygon")
    if not geom.is_valid:
        raise InvalidAreaGeometry("Геометрия области самопересекается или некорректна")
    if geom.area <= 0:
        raise InvalidAreaGeometry("Площадь выделенной области должна быть больше нуля")
    return geom


def geometry_area_sqm(geom: Any) -> float:
    """Площадь произвольной геометрии WGS84 в квадратных метрах (локальная проекция)."""
    centroid = geom.centroid
    lat0, lon0 = centroid.y, centroid.x
    m_per_deg_lat = 111_132.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))

    def _to_meters(coords):
        result = coords.copy()
        result[:, 0] = (coords[:, 0] - lon0) * m_per_deg_lon
        result[:, 1] = (coords[:, 1] - lat0) * m_per_deg_lat
        return result

    try:
        projected = shapely_transform(geom, _to_meters)
        return round(abs(projected.area), 1)
    except Exception:
        return 0.0


def _tile_boxes(selection: Any) -> tuple[list[Any], bool]:
    """Нарезает bbox выделенной области на тайлы OSM API v0.6 с ограничением количества."""
    minx, miny, maxx, maxy = selection.bounds
    bx1, by1, bx2, by2 = BELARUS_BBOX
    minx, miny = max(minx, bx1), max(miny, by1)
    maxx, maxy = min(maxx, bx2), min(maxy, by2)
    if minx >= maxx or miny >= maxy:
        return [], False

    tile_size = max(settings.area_import_tile_size_deg, 1e-4)
    max_tiles = max(settings.area_import_max_tiles, 1)

    nx = max(1, math.ceil((maxx - minx) / tile_size))
    ny = max(1, math.ceil((maxy - miny) / tile_size))
    dx = (maxx - minx) / nx
    dy = (maxy - miny) / ny

    cells: list[Any] = []
    truncated = False
    for ix in range(nx):
        for iy in range(ny):
            cell = box(
                minx + ix * dx,
                miny + iy * dy,
                minx + (ix + 1) * dx,
                miny + (iy + 1) * dy,
            )
            try:
                intersects = selection.intersects(cell)
            except Exception:
                intersects = False
            if not intersects:
                continue
            if len(cells) >= max_tiles:
                truncated = True
                break
            cells.append(cell)
        if truncated:
            break
    return cells, truncated


async def _fetch_tiles(cells: list[Any]) -> list[dict[str, Any] | None]:
    """Последовательно/параллельно загружает тайлы через OpenStreetMap API v0.6."""
    if not cells:
        return []
    semaphore = asyncio.Semaphore(max(settings.area_import_max_fetch_concurrency, 1))

    async def _fetch_one(cell: Any) -> dict[str, Any] | None:
        min_lon, min_lat, max_lon, max_lat = cell.bounds
        async with semaphore:
            try:
                return await fetch_osm_map_box(min_lon, min_lat, max_lon, max_lat)
            except Exception as exc:
                logger.debug(
                    "Ошибка загрузки тайла OSM %.6f,%.6f,%.6f,%.6f: %s",
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    exc,
                )
                return None

    return list(await asyncio.gather(*(_fetch_one(cell) for cell in cells)))


def _merge_osm_results(
    results: list[dict[str, Any] | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Объединяет ответы тайлов и убирает дубликаты по osm_id."""
    buildings_by_id: dict[int, dict[str, Any]] = {}
    pois_by_id: dict[int, dict[str, Any]] = {}

    for data in results:
        if not data:
            continue
        for building in data.get("buildings") or []:
            osm_id = building.get("osm_id")
            if osm_id is None:
                continue
            existing = buildings_by_id.get(osm_id)
            if existing is None:
                buildings_by_id[osm_id] = building
            else:
                existing_tags = existing.setdefault("tags", {})
                for key, value in (building.get("tags") or {}).items():
                    existing_tags.setdefault(key, value)

        for poi in data.get("pois") or []:
            osm_id = poi.get("osm_id")
            if osm_id is None:
                continue
            pois_by_id[osm_id] = poi

    return list(buildings_by_id.values()), list(pois_by_id.values())


def _bulk_city_region(
    tags: dict[str, str], lat: float, lon: float
) -> tuple[str, str, str, str | None]:
    """
    Определяет (город, область, SOATO, город для проверки конфликтов) без обращений к Nominatim.
    Город берётся ТОЛЬКО из OSM-тегов. Если addr:city отсутствует — город пустой,
    без подстановки ближайшего города или названия области.
    """
    region_name, _, soato_prefix = detect_belarus_region_and_soato(lat, lon)
    raw_city = (
        tags.get("addr:city:ru")
        or tags.get("addr:city")
        or tags.get("addr:town")
        or tags.get("addr:village")
        or tags.get("addr:municipality")
    )

    if raw_city and is_valid_city_name(raw_city):
        city = canonicalize_city_name(raw_city)
        return city, region_name, soato_prefix, city
    return "", region_name, soato_prefix, None


def _building_type_from_tags(tags: dict[str, str]) -> str:
    raw = tags.get("building") or "building"
    if raw in ("yes", "true"):
        return "building"
    return raw


def _floors_from_tags(tags: dict[str, str]) -> int:
    raw = tags.get("building:levels")
    if not raw:
        return 0
    try:
        return max(1, int(float(str(raw).replace(",", "."))))
    except (TypeError, ValueError):
        return 0


def _year_from_tags(tags: dict[str, str]) -> int | None:
    start_date = str(tags.get("start_date") or "")
    if len(start_date) >= 4 and start_date[:4].isdigit():
        return int(start_date[:4])
    return None


def _floor_label(tags: dict[str, str]) -> str | None:
    level = tags.get("addr:floor") or tags.get("level")
    if level is None:
        return None
    value = str(level).strip()
    if value.lstrip("-").isdigit():
        return f"{value} этаж"
    return value if "этаж" in value.lower() else f"Этаж {value}"


def _organization_from_tags(
    building_id: int, osm_id: int | None, tags: dict[str, str], lat: float, lon: float
) -> Organization | None:
    name = _extract_poi_name(tags)
    if not name:
        return None
    category, subcategory, _ = _classify_osm_poi(tags)
    room_value = tags.get("addr:unit") or tags.get("addr:door") or tags.get("room")
    if room_value and not str(room_value).lower().startswith(("пом", "оф", "каб", "пав")):
        room_value = f"пом. {room_value}"
    return Organization(
        osm_id=osm_id,
        building_id=building_id,
        name=name,
        category=category,
        subcategory=subcategory,
        description=tags.get("description:ru") or tags.get("description"),
        unp=tags.get("ref:vatin") or tags.get("ref:unp"),
        floor=_floor_label(tags),
        room=room_value,
        opening_hours=tags.get("opening_hours"),
        phone=tags.get("contact:phone") or tags.get("phone"),
        website=tags.get("contact:website") or tags.get("website"),
        email=tags.get("contact:email") or tags.get("email"),
        lat=lat,
        lon=lon,
        metadata_json={"source": "OpenStreetMap РБ"},
    )


def _prepare_buildings(
    buildings: list[dict[str, Any]], selection: Any
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for building in buildings:
        polygon: Polygon = building.get("polygon")
        if polygon is None:
            continue
        try:
            if not selection.intersects(polygon):
                continue
        except Exception:
            continue

        tags = building.get("tags") or {}
        centroid = polygon.centroid
        lat, lon = centroid.y, centroid.x
        city, region, soato, conflict_city = _bulk_city_region(tags, lat, lon)
        street = tags.get("addr:street:ru") or translate_belarusian_street_to_ru(
            tags.get("addr:street")
        )
        prepared.append(
            {
                "building": building,
                "city": city,
                "region": region,
                "soato": soato,
                "conflict_city": conflict_city,
                "street": street,
                "house": tags.get("addr:housenumber"),
                "district": resolve_district(lat, lon, city),
            }
        )
    return prepared


def _poi_conflicts_with_building(item: dict[str, Any], poi: dict[str, Any]) -> bool:
    tags = poi.get("tags") or {}
    return has_address_conflict(
        item.get("conflict_city"),
        item.get("street"),
        item.get("house"),
        tags.get("addr:city"),
        tags.get("addr:street:ru") or tags.get("addr:street"),
        tags.get("addr:housenumber"),
    )


def _assign_pois_to_buildings(
    prepared: list[dict[str, Any]], pois: list[dict[str, Any]], selection: Any
) -> dict[int, list[dict[str, Any]]]:
    """Привязывает POI к ближайшему зданию через пространственный индекс STRtree."""
    assigned: dict[int, list[dict[str, Any]]] = {
        item["building"]["osm_id"]: [] for item in prepared
    }
    if not prepared or not pois:
        return assigned

    polygons = [item["building"]["polygon"] for item in prepared]
    tree = STRtree(polygons)

    for poi in pois:
        lat, lon = poi.get("lat"), poi.get("lon")
        if lat is None or lon is None:
            continue
        point = Point(float(lon), float(lat))
        try:
            if not selection.contains(point):
                continue
        except Exception:
            continue

        try:
            candidate_indexes = tree.query(point.buffer(STRTREE_BUFFER_DEG))
        except Exception:
            candidate_indexes = []

        containing: list[tuple[float, dict[str, Any]]] = []
        nearby: list[tuple[float, dict[str, Any]]] = []
        for raw_index in candidate_indexes:
            try:
                item = prepared[int(raw_index)]
                poly: Polygon = item["building"]["polygon"]
            except (IndexError, TypeError, ValueError):
                continue
            if poly.contains(point):
                containing.append((poly.area, item))
            else:
                distance = poly.distance(point)
                if distance <= ENTRANCE_TOLERANCE_DEG:
                    nearby.append((distance, item))

        chosen: dict[str, Any] | None = None
        for _, item in sorted(containing, key=lambda pair: pair[0]):
            if not _poi_conflicts_with_building(item, poi):
                chosen = item
                break
        if chosen is None and not containing:
            for _, item in sorted(nearby, key=lambda pair: pair[0]):
                if not _poi_conflicts_with_building(item, poi):
                    chosen = item
                    break

        if chosen is not None:
            assigned[chosen["building"]["osm_id"]].append(poi)

    return assigned


def _osm_body_geojson(ring: list[list[float]]) -> str:
    closed = [list(point) for point in ring]
    if closed and closed[0] != closed[-1]:
        closed.append(list(closed[0]))
    return json.dumps({"type": "MultiPolygon", "coordinates": [[closed]]}, ensure_ascii=False)


async def _upsert_area_objects(
    session: AsyncSession,
    prepared: list[dict[str, Any]],
    assigned_pois: dict[int, list[dict[str, Any]]],
) -> AreaImportCounters:
    counters = AreaImportCounters()

    osm_ids = [item["building"]["osm_id"] for item in prepared]
    existing_by_osm: dict[int, Building] = {}
    for start in range(0, len(osm_ids), 500):
        chunk = osm_ids[start : start + 500]
        if not chunk:
            continue
        result = await session.execute(select(Building).where(Building.osm_id.in_(chunk)))
        for building in result.scalars().all():
            if building.osm_id is not None and building.osm_id not in existing_by_osm:
                existing_by_osm[building.osm_id] = building

    existing_orgs: dict[int, list[Organization]] = {}
    existing_building_ids = [building.id for building in existing_by_osm.values()]
    for start in range(0, len(existing_building_ids), 500):
        chunk = existing_building_ids[start : start + 500]
        if not chunk:
            continue
        result = await session.execute(
            select(Organization).where(Organization.building_id.in_(chunk))
        )
        for org in result.scalars().all():
            existing_orgs.setdefault(org.building_id, []).append(org)

    for item in prepared:
        bdata = item["building"]
        osm_id = bdata["osm_id"]
        tags: dict[str, str] = bdata.get("tags") or {}
        ring: list[list[float]] = bdata.get("ring") or []
        if not ring:
            continue
        polygon: Polygon = bdata["polygon"]
        lat, lon = polygon.centroid.y, polygon.centroid.x
        area_sqm = compute_polygon_area_sqm(ring)
        new_name = tags.get("name:ru") or tags.get("name")
        new_type = _building_type_from_tags(tags)
        new_floors = _floors_from_tags(tags)
        new_year = _year_from_tags(tags)
        new_wall = tags.get("building:material")
        new_postcode = tags.get("addr:postcode")

        building = existing_by_osm.get(osm_id)
        if building is None:
            _, building_cad, _ = generate_cadastral_numbers(
                lat, lon, osm_id, soato_prefix=item["soato"]
            )
            building = Building(
                osm_id=osm_id,
                cadastral_number=building_cad,
                inventory_number=None,
                parcel_id=None,
                name=new_name,
                building_type=new_type,
                purpose_label=None,
                address_region=item["region"],
                address_district=item["district"],
                address_city=item["city"],
                address_street=item["street"],
                address_housenumber=item["house"],
                postal_code=new_postcode,
                floors=new_floors,
                year_built=new_year,
                wall_material=new_wall,
                area_sqm=area_sqm,
                center_lat=round(lat, 6),
                center_lon=round(lon, 6),
                min_lon=min(point[0] for point in ring),
                min_lat=min(point[1] for point in ring),
                max_lon=max(point[0] for point in ring),
                max_lat=max(point[1] for point in ring),
                geometry_geojson=_osm_body_geojson(ring),
            )
            session.add(building)
            await session.flush()
            existing_by_osm[osm_id] = building
            counters.buildings_added += 1
        else:
            if new_name:
                building.name = new_name
            building.building_type = new_type
            building.address_region = item["region"]
            building.address_district = item["district"]
            building.address_city = item["city"]
            if item["street"]:
                building.address_street = item["street"]
            if item["house"]:
                building.address_housenumber = item["house"]
            if new_postcode:
                building.postal_code = new_postcode
            if new_floors > 0:
                building.floors = new_floors
            if new_year is not None:
                building.year_built = new_year
            if new_wall:
                building.wall_material = new_wall
            building.area_sqm = area_sqm
            building.center_lat = round(lat, 6)
            building.center_lon = round(lon, 6)
            building.min_lon = min(point[0] for point in ring)
            building.min_lat = min(point[1] for point in ring)
            building.max_lon = max(point[0] for point in ring)
            building.max_lat = max(point[1] for point in ring)
            building.geometry_geojson = _osm_body_geojson(ring)
            counters.buildings_updated += 1

        current_orgs = existing_orgs.get(building.id, [])
        existing_osm_ids = {org.osm_id for org in current_orgs if org.osm_id}
        existing_names = {
            (org.name.strip().lower(), (org.floor or "").strip().lower())
            for org in current_orgs
            if org.name
        }

        if _is_poi_element(tags):
            name = _extract_poi_name(tags)
            dedup_key = ((name or "").lower(), "")
            if name and (name.lower(), "") not in existing_names:
                category, subcategory, _ = _classify_osm_poi(tags)
                session.add(
                    Organization(
                        osm_id=osm_id,
                        building_id=building.id,
                        name=name,
                        category=category,
                        subcategory=subcategory,
                        description=tags.get("description:ru") or tags.get("description"),
                        unp=tags.get("ref:vatin") or tags.get("ref:unp"),
                        floor=None,
                        opening_hours=tags.get("opening_hours"),
                        phone=tags.get("contact:phone") or tags.get("phone"),
                        website=tags.get("contact:website") or tags.get("website"),
                        email=tags.get("contact:email") or tags.get("email"),
                        lat=building.center_lat,
                        lon=building.center_lon,
                        metadata_json={"source": "OpenStreetMap РБ"},
                    )
                )
                existing_osm_ids.add(osm_id)
                existing_names.add(dedup_key)
                counters.organizations_added += 1

        for poi in assigned_pois.get(osm_id, []):
            poi_osm_id = poi.get("osm_id")
            if poi_osm_id in existing_osm_ids:
                continue
            org = _organization_from_tags(
                building.id,
                poi_osm_id,
                poi.get("tags") or {},
                float(poi["lat"]),
                float(poi["lon"]),
            )
            if org is None:
                continue
            dedup_key = (org.name.lower(), (org.floor or "").strip().lower())
            if dedup_key in existing_names:
                continue
            session.add(org)
            existing_osm_ids.add(poi_osm_id)
            existing_names.add(dedup_key)
            counters.organizations_added += 1

        if (
            settings.enable_egr_enrichment
            and building.address_street
            and building.address_housenumber
        ):
            egr_added = await attach_egr_records_to_building(
                session, building, auto_commit=False
            )
            counters.organizations_added += egr_added

    await session.commit()
    await sync_postgis_geometries(session)
    return counters


def _building_intersects_selection(building: Building, selection: Any) -> bool:
    try:
        geom = shape(json.loads(building.geometry_geojson))
        return geom.intersects(selection)
    except Exception:
        return False


async def _compute_area_stats(
    session: AsyncSession,
    selection: Any,
    area_sqm: float,
    counters: AreaImportCounters,
    warnings: list[str],
    data_scope: str,
) -> AreaImportResponse:
    minx, miny, maxx, maxy = selection.bounds
    stmt = (
        select(Building)
        .options(selectinload(Building.organizations))
        .where(
            Building.max_lon >= minx - DB_QUERY_PADDING_DEG,
            Building.min_lon <= maxx + DB_QUERY_PADDING_DEG,
            Building.max_lat >= miny - DB_QUERY_PADDING_DEG,
            Building.min_lat <= maxy + DB_QUERY_PADDING_DEG,
        )
    )
    result = await session.execute(stmt)
    buildings = [
        building
        for building in result.scalars().all()
        if _building_intersects_selection(building, selection)
    ]

    building_types: Counter[str] = Counter()
    cities: Counter[str] = Counter()
    districts: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    buildings_with_orgs = 0
    organizations_osm = 0
    organizations_egr = 0
    organizations_other = 0
    egr_active = 0
    egr_excluded = 0
    egr_other = 0
    organizations_total = 0

    for building in buildings:
        building_types[building.building_type or "—"] += 1
        cities[building.address_city or "—"] += 1
        districts[building.address_district or "—"] += 1
        if building.organizations:
            buildings_with_orgs += 1

        for org in building.organizations:
            organizations_total += 1
            categories[org.category or "—"] += 1
            metadata = org.metadata_json or {}
            source = str(metadata.get("source") or "").lower()
            status = str(metadata.get("status") or "").lower()

            if "егр" in source:
                organizations_egr += 1
                if "исключ" in status or "ликвид" in status or "прекращ" in status:
                    egr_excluded += 1
                elif "действ" in status:
                    egr_active += 1
                else:
                    egr_other += 1
            elif "openstreetmap" in source or source.startswith("osm"):
                organizations_osm += 1
            else:
                organizations_other += 1

    return AreaImportResponse(
        area_sqm=area_sqm,
        area_km2=round(area_sqm / 1_000_000.0, 4),
        data_scope=data_scope,
        warnings=warnings,
        buildings_total=len(buildings),
        buildings_with_organizations=buildings_with_orgs,
        buildings_without_organizations=len(buildings) - buildings_with_orgs,
        organizations_total=organizations_total,
        organizations_osm=organizations_osm,
        organizations_egr=organizations_egr,
        organizations_other=organizations_other,
        egr_active=egr_active,
        egr_excluded=egr_excluded,
        egr_other=egr_other,
        buildings_by_type=dict(building_types),
        organizations_by_category=dict(categories),
        buildings_by_city=dict(cities),
        buildings_by_district=dict(districts),
        import_stats=counters,
    )


async def import_area_and_compute_stats(
    session: AsyncSession, geometry: dict[str, Any] | None
) -> AreaImportResponse:
    """
    Импортирует все реальные объекты OpenStreetMap внутри выделенной области в локальную БД
    и строит статистику по локальной БД в пределах этой же фигуры.
    """
    selection = parse_area_geometry(geometry)
    area_sqm = geometry_area_sqm(selection)
    warnings: list[str] = []

    if not settings.enable_live_osm_enrichment:
        warnings.append(
            "Загрузка из OpenStreetMap отключена (ENABLE_LIVE_OSM_ENRICHMENT=false); "
            "статистика построена только по уже сохранённой локальной БД."
        )

    cells, truncated = _tile_boxes(selection)
    if truncated:
        warnings.append(
            f"Область слишком большая: обработаны только первые {len(cells)} тайлов OSM. "
            "Данные могут быть неполными — уменьшите выделенную область."
        )
    if not cells:
        warnings.append(
            "Выделенная область не пересекается с территорией Республики Беларусь."
        )

    tile_results = await _fetch_tiles(cells)
    tiles_ok = sum(1 for data in tile_results if data)
    tiles_failed = len(cells) - tiles_ok
    if tiles_failed > 0:
        warnings.append(
            f"Не удалось загрузить {tiles_failed} из {len(cells)} тайлов OpenStreetMap. "
            "Данные могут быть неполными."
        )

    buildings, pois = _merge_osm_results(tile_results)
    if cells and not buildings:
        warnings.append("OpenStreetMap не вернул зданий для выделенной области.")

    prepared = _prepare_buildings(buildings, selection)
    assigned_pois = _assign_pois_to_buildings(prepared, pois, selection)
    counters = await _upsert_area_objects(session, prepared, assigned_pois)
    counters.tiles_requested = len(cells)
    counters.tiles_ok = tiles_ok
    counters.tiles_failed = tiles_failed

    data_scope = "full" if (cells and not truncated and tiles_failed == 0) else "partial"
    return await _compute_area_stats(
        session,
        selection,
        area_sqm,
        counters,
        warnings,
        data_scope,
    )
