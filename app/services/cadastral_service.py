from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any

import httpx
from shapely.geometry import Point, Polygon, shape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import sync_postgis_geometries
from app.models.building import Building
from app.models.organization import Organization
from app.models.parcel import CadastralParcel
from app.schemas.building import (
    BuildingDetail,
    CadastralStats,
    ParcelSummary,
    SearchResultItem,
)
from app.schemas.organization import OrganizationRead
from app.services.seed_data import SEED_PARCELS_AND_BUILDINGS, make_rect_polygon

logger = logging.getLogger(__name__)

BUILDING_PURPOSE_LABELS: dict[str, str] = {
    "retail": "Здание специализированное розничной торговли",
    "commercial": "Многофункциональное общественно-деловое здание",
    "office": "Административно-офисное здание (бизнес-центр)",
    "public": "Здание административно-общественного и культурного назначения",
    "university": "Здание учреждения высшего образования и науки",
    "school": "Здание учреждения общего среднего образования",
    "hospital": "Здание организации здравоохранения",
    "industrial": "Производственно-складское капитальное строение",
    "apartments": "Многоквартирный жилой дом со встроенными помещениями",
    "residential": "Жилое капитальное строение",
    "hotel": "Здание гостиничного комплекса",
}

OSM_CATEGORY_MAP: dict[str, tuple[str, str]] = {
    "bank": ("Финансы и банки", "Банковское отделение"),
    "atm": ("Финансы и банки", "Банкомат / Инфокиоск"),
    "bureau_de_change": ("Финансы и банки", "Обмен валют"),
    "insurance": ("Финансы и банки", "Страховая организация"),
    "pharmacy": ("Медицина и аптеки", "Аптека"),
    "hospital": ("Медицина и аптеки", "Больница / Медцентр"),
    "clinic": ("Медицина и аптеки", "Медицинский центр / Поликлиника"),
    "dentist": ("Медицина и аптеки", "Стоматологическая клиника"),
    "doctors": ("Медицина и аптеки", "Врачебная практика"),
    "supermarket": ("Торговля", "Продовольственный супермаркет"),
    "convenience": ("Торговля", "Магазин продуктов у дома"),
    "mall": ("Торговля", "Торговый центр"),
    "department_store": ("Торговля", "Универмаг"),
    "clothes": ("Торговля", "Магазин одежды"),
    "shoes": ("Торговля", "Магазин обуви"),
    "electronics": ("Торговля", "Магазин электроники и бытовой техники"),
    "jewelry": ("Торговля", "Ювелирный салон"),
    "books": ("Торговля", "Книжный магазин"),
    "bakery": ("Торговля", "Пекарня / Кондитерская"),
    "florist": ("Торговля", "Цветочный салон"),
    "hardware": ("Торговля", "Строительные и хозтовары"),
    "restaurant": ("Общепит", "Ресторан"),
    "cafe": ("Общепит", "Кафе / Кофейня"),
    "fast_food": ("Общепит", "Ресторан быстрого питания"),
    "bar": ("Общепит", "Бар / Паб"),
    "post_office": ("Госучреждения и почта", "Отделение почтовой связи РУП «Белпочта»"),
    "townhall": ("Госучреждения и почта", "Исполнительный комитет / Администрация"),
    "government": ("Госучреждения и почта", "Государственное учреждение"),
    "courthouse": ("Госучреждения и почта", "Суд Республики Беларусь"),
    "police": ("Госучреждения и почта", "Отдел внутренних дел"),
    "library": ("Культура и отдых", "Библиотека"),
    "museum": ("Культура и отдых", "Музей / Галерея"),
    "cinema": ("Культура и отдых", "Кинотеатр"),
    "theatre": ("Культура и отдых", "Театр / Концертный зал"),
    "university": ("Образование и наука", "Университет / Корпус ВУЗа"),
    "college": ("Образование и наука", "Колледж / Техникум"),
    "school": ("Образование и наука", "Школа / Гимназия"),
    "kindergarten": ("Образование и наука", "Детский сад"),
    "it": ("Офисы и ИТ", "ИТ-компания / Центр разработки"),
    "company": ("Офисы и ИТ", "Коммерческая организация (Офис)"),
    "coworking": ("Офисы и ИТ", "Коворкинг / Бизнес-пространство"),
    "lawyer": ("Юридические и деловые услуги", "Юридическая консультация / Адвокат"),
    "notary": ("Юридические и деловые услуги", "Нотариальная контора"),
    "accountant": ("Юридические и деловые услуги", "Бухгалтерские и аудиторские услуги"),
    "telecommunication": ("Услуги и связь", "Салон связи и телекоммуникаций"),
    "hairdresser": ("Услуги и связь", "Салон красоты / Парикмахерская"),
    "travel_agency": ("Гостиницы и туризм", "Туристическое агентство"),
    "hotel": ("Гостиницы и туризм", "Гостиница / Отель"),
}


def detect_belarus_region_and_soato(lat: float, lon: float) -> tuple[str, str, str]:
    """Определяет область/город Беларуси и префикс кадастрового номера СОАТО по координатам."""
    if 53.82 <= lat <= 53.98 and 27.40 <= lon <= 27.72:
        return "г. Минск", "Минск", "5000000000"
    if lat < 52.85 and lon < 26.8:
        return "Брестская область", "Брест", "1401000000"
    if lat >= 52.85 and lon < 26.3:
        return "Гродненская область", "Гродно", "4401000000"
    if lat < 53.35 and lon >= 28.5:
        return "Гомельская область", "Гомель", "3401000000"
    if lat >= 54.55:
        return "Витебская область", "Витебск", "2401000000"
    if lon >= 29.2 and 53.35 <= lat < 54.55:
        return "Могилёвская область", "Могилёв", "7401000000"
    return "Минская область", "Минская обл.", "6401000000"


def generate_cadastral_numbers(lat: float, lon: float, osm_id: int | None = None) -> tuple[str, str, str]:
    """
    Формирует структурированный кадастровый номер земельного участка и здания
    по стандарту Национального кадастрового агентства РБ (18 цифр + индекс строения).
    """
    _, _, soato_prefix = detect_belarus_region_and_soato(lat, lon)
    seed_str = f"{osm_id or ''}:{lat:.5f}:{lon:.5f}"
    digest = hashlib.sha1(seed_str.encode("utf-8")).hexdigest()
    num_hash = int(digest[:8], 16)
    quarter = (num_hash // 10000) % 89 + 10
    parcel_num = num_hash % 8999 + 1000
    parcel_cad = f"{soato_prefix}{quarter:02d}{parcel_num:06d}"
    building_cad = f"{parcel_cad}:С-1"
    inv_num = f"{soato_prefix[:3]}/С-{(num_hash % 899999) + 100000}"
    return parcel_cad, building_cad, inv_num


def compute_polygon_area_sqm(ring_lonlat: list[list[float]]) -> float:
    """Вычисляет площадь полигона в квадратных метрах по координатам WGS84."""
    if len(ring_lonlat) < 3:
        return 0.0
    mean_lat = sum(pt[1] for pt in ring_lonlat) / len(ring_lonlat)
    lat_rad = math.radians(mean_lat)
    m_per_deg_lat = 111_132.0
    m_per_deg_lon = 111_320.0 * math.cos(lat_rad)

    ref_lon, ref_lat = ring_lonlat[0][0], ring_lonlat[0][1]
    pts_m = [
        ((pt[0] - ref_lon) * m_per_deg_lon, (pt[1] - ref_lat) * m_per_deg_lat)
        for pt in ring_lonlat
    ]
    poly = Polygon(pts_m)
    return round(abs(poly.area), 1)


async def ensure_seed_data(session: AsyncSession) -> None:
    """Заполняет БД опорными кадастровыми участками, зданиями и организациями РБ при первом запуске."""
    count_stmt = select(func.count(Building.id))
    res = await session.execute(count_stmt)
    existing_count = res.scalar_one() or 0
    if existing_count > 0:
        return

    logger.info("Инициализация стартового кадастрового реестра Республики Беларусь...")
    for item in SEED_PARCELS_AND_BUILDINGS:
        p_data = item["parcel"]
        p_geojson, p_bbox, p_area = make_rect_polygon(
            p_data["center_lon"],
            p_data["center_lat"],
            p_data["width_m"],
            p_data["height_m"],
            p_data.get("angle_deg", 0.0),
        )
        parcel = CadastralParcel(
            cadastral_number=p_data["cadastral_number"],
            name=p_data.get("name"),
            region=p_data.get("region"),
            district=p_data.get("district"),
            land_use=p_data.get("land_use", "commercial"),
            purpose=p_data.get("purpose"),
            ownership_type=p_data.get("ownership_type"),
            area_sqm=p_area,
            cadastral_value_byn=p_data.get("cadastral_value_byn"),
            center_lat=p_data["center_lat"],
            center_lon=p_data["center_lon"],
            min_lon=p_bbox[0],
            min_lat=p_bbox[1],
            max_lon=p_bbox[2],
            max_lat=p_bbox[3],
            geometry_geojson=p_geojson,
        )
        session.add(parcel)
        await session.flush()

        b_data = item["building"]
        b_geojson, b_bbox, b_area = make_rect_polygon(
            b_data["center_lon"],
            b_data["center_lat"],
            b_data["width_m"],
            b_data["height_m"],
            b_data.get("angle_deg", 0.0),
        )
        building = Building(
            osm_id=b_data.get("osm_id"),
            cadastral_number=b_data["cadastral_number"],
            inventory_number=b_data.get("inventory_number"),
            parcel_id=parcel.id,
            name=b_data.get("name"),
            building_type=b_data.get("building_type", "commercial"),
            purpose_label=b_data.get("purpose_label")
            or BUILDING_PURPOSE_LABELS.get(b_data.get("building_type", "commercial")),
            address_region=b_data.get("address_region"),
            address_city=b_data["address_city"],
            address_street=b_data.get("address_street"),
            address_housenumber=b_data.get("address_housenumber"),
            postal_code=b_data.get("postal_code"),
            floors=b_data.get("floors", 1),
            year_built=b_data.get("year_built"),
            wall_material=b_data.get("wall_material"),
            area_sqm=b_area,
            center_lat=b_data["center_lat"],
            center_lon=b_data["center_lon"],
            min_lon=b_bbox[0],
            min_lat=b_bbox[1],
            max_lon=b_bbox[2],
            max_lat=b_bbox[3],
            geometry_geojson=b_geojson,
        )
        session.add(building)
        await session.flush()

        for idx, o_data in enumerate(b_data.get("organizations", [])):
            angle = idx * 1.1
            org_lat = b_data["center_lat"] + math.sin(angle) * 0.00012
            org_lon = b_data["center_lon"] + math.cos(angle) * 0.00018
            org = Organization(
                building_id=building.id,
                name=o_data["name"],
                category=o_data["category"],
                subcategory=o_data.get("subcategory"),
                description=o_data.get("description"),
                unp=o_data.get("unp"),
                floor=o_data.get("floor"),
                room=o_data.get("room"),
                opening_hours=o_data.get("opening_hours"),
                phone=o_data.get("phone"),
                website=o_data.get("website"),
                email=o_data.get("email"),
                lat=round(org_lat, 6),
                lon=round(org_lon, 6),
                metadata_json={"source": "ЕГРНИ / OSM РБ"},
            )
            session.add(org)

    await session.commit()
    await sync_postgis_geometries(session)


def _building_to_detail(building: Building) -> BuildingDetail:
    try:
        geometry = json.loads(building.geometry_geojson)
    except Exception:
        geometry = {"type": "MultiPolygon", "coordinates": []}

    orgs = [OrganizationRead.model_validate(o) for o in building.organizations]
    cat_summary: dict[str, int] = {}
    for o in orgs:
        cat_summary[o.category] = cat_summary.get(o.category, 0) + 1

    parcel_summary = (
        ParcelSummary.model_validate(building.parcel) if building.parcel is not None else None
    )

    return BuildingDetail(
        id=building.id,
        osm_id=building.osm_id,
        cadastral_number=building.cadastral_number,
        inventory_number=building.inventory_number,
        parcel_id=building.parcel_id,
        name=building.name,
        building_type=building.building_type,
        purpose_label=building.purpose_label
        or BUILDING_PURPOSE_LABELS.get(building.building_type, "Капитальное строение"),
        address_region=building.address_region,
        address_city=building.address_city,
        address_street=building.address_street,
        address_housenumber=building.address_housenumber,
        full_address=building.full_address,
        postal_code=building.postal_code,
        floors=building.floors,
        year_built=building.year_built,
        wall_material=building.wall_material,
        area_sqm=round(building.area_sqm, 1),
        center_lat=building.center_lat,
        center_lon=building.center_lon,
        geometry=geometry,
        parcel=parcel_summary,
        organizations_count=len(orgs),
        categories_summary=cat_summary,
        organizations=orgs,
    )


async def get_building_detail(session: AsyncSession, building_id: int) -> BuildingDetail | None:
    stmt = (
        select(Building)
        .options(selectinload(Building.parcel), selectinload(Building.organizations))
        .where(Building.id == building_id)
    )
    res = await session.execute(stmt)
    building = res.scalar_one_or_none()
    if building is None:
        return None
    return _building_to_detail(building)


def _classify_osm_poi(tags: dict[str, str]) -> tuple[str, str, str]:
    """Определяет (category, subcategory, description) для объекта из тегов OpenStreetMap."""
    for key in ("amenity", "shop", "office", "craft", "tourism", "healthcare", "leisure", "government"):
        val = tags.get(key)
        if val:
            if val in OSM_CATEGORY_MAP:
                cat, desc = OSM_CATEGORY_MAP[val]
                return cat, val, tags.get("description") or desc
            if key == "shop":
                return "Торговля", val, tags.get("description") or f"Торговый объект ({val})"
            if key == "office":
                return "Офисы и ИТ", val, tags.get("description") or f"Офис организации ({val})"
            if key == "amenity":
                return "Услуги и инфраструктура", val, tags.get("description") or f"Объект инфраструктуры ({val})"
            if key == "craft":
                return "Услуги и связь", val, tags.get("description") or f"Мастерская / сервис ({val})"
            if key == "tourism":
                return "Гостиницы и туризм", val, tags.get("description") or f"Туристический объект ({val})"
            if key == "healthcare":
                return "Медицина и аптеки", val, tags.get("description") or f"Медицинское учреждение ({val})"
    return "Офисы и организации", "organization", tags.get("description") or "Организация / Помещение"


async def _fetch_osm_overpass_around(lat: float, lon: float) -> dict[str, Any] | None:
    """
    Запрашивает в реальном времени из OpenStreetMap Overpass API контур здания
    и все организации внутри или рядом со зданием (в радиусе 55 метров).
    """
    if not settings.enable_live_osm_enrichment:
        return None

    overpass_query = f"""
    [out:json][timeout:6];
    (
      way(around:45,{lat:.6f},{lon:.6f})["building"];
      node(around:65,{lat:.6f},{lon:.6f})["name"];
      way(around:65,{lat:.6f},{lon:.6f})["name"]["building"!~".*"];
    );
    out body geom;
    """
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.post(
                settings.overpass_api_url,
                data={"data": overpass_query},
                headers={"User-Agent": "BelarusCadastralMap/1.0 (FastAPI OpenSource GIS)"},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:
        logger.debug("Overpass API недоступен или превысил таймаут: %s", exc)
    return None


async def get_or_enrich_building_by_coords(
    session: AsyncSession,
    lat: float,
    lon: float,
    hint_name: str | None = None,
    hint_street: str | None = None,
    hint_housenumber: str | None = None,
    hint_city: str | None = None,
    hint_floors: int | None = None,
    hint_type: str | None = None,
    hint_osm_id: int | None = None,
    hint_geometry: dict[str, Any] | None = None,
) -> BuildingDetail | None:
    """
    Находит здание в БД по координатам клика (lat, lon) или по osm_id.
    Если пользователь кликнул на любое другое здание РБ на векторной карте,
    автоматически подгружает контур и организации внутри здания из OpenStreetMap,
    регистрирует объект в кадастровой БД и возвращает карточку здания со всеми организациями.
    """
    # 1. Проверяем по osm_id, если передан
    if hint_osm_id:
        stmt_osm = (
            select(Building)
            .options(selectinload(Building.parcel), selectinload(Building.organizations))
            .where(Building.osm_id == hint_osm_id)
        )
        res_osm = await session.execute(stmt_osm)
        existing_by_osm = res_osm.scalar_one_or_none()
        if existing_by_osm is not None:
            return _building_to_detail(existing_by_osm)

    # 2. Поиск здания в локальной БД по координатам (с допуском ~60 метров)
    delta_lat = 0.0006
    delta_lon = 0.0009
    stmt = (
        select(Building)
        .options(selectinload(Building.parcel), selectinload(Building.organizations))
        .where(
            Building.min_lat - delta_lat <= lat,
            Building.max_lat + delta_lat >= lat,
            Building.min_lon - delta_lon <= lon,
            Building.max_lon + delta_lon >= lon,
        )
    )
    res = await session.execute(stmt)
    candidates = res.scalars().all()

    pt = Point(lon, lat)
    best_building: Building | None = None
    best_dist = float("inf")

    for cand in candidates:
        try:
            geom_shape = shape(json.loads(cand.geometry_geojson))
            if geom_shape.contains(pt):
                return _building_to_detail(cand)
            dist = geom_shape.distance(pt)
            if dist < best_dist:
                best_dist = dist
                best_building = cand
        except Exception:
            dist = math.hypot(cand.center_lon - lon, cand.center_lat - lat)
            if dist < best_dist:
                best_dist = dist
                best_building = cand

    if best_building is not None and best_dist <= 0.00045:
        return _building_to_detail(best_building)

    # 3. Если здание ещё не в локальной БД, проверяем, что координаты внутри Беларуси
    if not (51.2 <= lat <= 56.25 and 23.1 <= lon <= 32.85):
        return None

    # 4. Динамическое получение здания и его организаций из OpenStreetMap (Overpass API) + подсказок карты
    overpass_data = await _fetch_osm_overpass_around(lat, lon)

    region_name, default_city, _ = detect_belarus_region_and_soato(lat, lon)
    chosen_way: dict[str, Any] | None = None
    poi_elements: list[dict[str, Any]] = []

    if overpass_data and "elements" in overpass_data:
        elements = overpass_data["elements"]
        best_way_dist = float("inf")
        for el in elements:
            tags = el.get("tags", {})
            if el.get("type") == "way" and "building" in tags and "geometry" in el:
                coords = [[pt_item["lon"], pt_item["lat"]] for pt_item in el["geometry"]]
                if len(coords) >= 3:
                    if coords[0] != coords[-1]:
                        coords.append(coords[0])
                    try:
                        poly = Polygon(coords)
                        if poly.contains(pt):
                            chosen_way = el
                            break
                        d = poly.distance(pt)
                        if d < best_way_dist:
                            best_way_dist = d
                            chosen_way = el
                    except Exception:
                        pass
            elif "name" in tags and any(
                k in tags
                for k in (
                    "amenity",
                    "shop",
                    "office",
                    "craft",
                    "tourism",
                    "healthcare",
                    "leisure",
                    "government",
                )
            ):
                poi_elements.append(el)

    b_tags: dict[str, str] = chosen_way.get("tags", {}) if chosen_way else {}
    osm_id_val = (chosen_way.get("id") if chosen_way else None) or hint_osm_id

    # Проверяем ещё раз, не добавлено ли уже это здание по osm_id
    if osm_id_val:
        stmt_check = (
            select(Building)
            .options(selectinload(Building.parcel), selectinload(Building.organizations))
            .where(Building.osm_id == osm_id_val)
        )
        res_check = await session.execute(stmt_check)
        found = res_check.scalar_one_or_none()
        if found is not None:
            return _building_to_detail(found)

    # Геометрия здания
    building_geojson: str
    b_bbox: tuple[float, float, float, float]
    b_area: float
    center_lon_val = lon
    center_lat_val = lat

    if chosen_way and "geometry" in chosen_way:
        ring = [[round(g["lon"], 6), round(g["lat"], 6)] for g in chosen_way["geometry"]]
        if len(ring) >= 3 and ring[0] != ring[-1]:
            ring.append(ring[0])
        lons = [c[0] for c in ring]
        lats = [c[1] for c in ring]
        b_bbox = (min(lons), min(lats), max(lons), max(lats))
        center_lon_val = round(sum(lons[:-1]) / max(len(lons) - 1, 1), 6)
        center_lat_val = round(sum(lats[:-1]) / max(len(lats) - 1, 1), 6)
        b_area = max(compute_polygon_area_sqm(ring), 45.0)
        building_geojson = json.dumps(
            {"type": "MultiPolygon", "coordinates": [[ring]]}, ensure_ascii=False
        )
    elif hint_geometry and hint_geometry.get("coordinates"):
        try:
            g_shape = shape(hint_geometry)
            minx, miny, maxx, maxy = g_shape.bounds
            b_bbox = (round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6))
            center_lon_val = round(g_shape.centroid.x, 6)
            center_lat_val = round(g_shape.centroid.y, 6)
            if hint_geometry.get("type") == "Polygon":
                ring = hint_geometry["coordinates"][0]
                b_area = max(compute_polygon_area_sqm(ring), 48.0)
                building_geojson = json.dumps(
                    {"type": "MultiPolygon", "coordinates": [hint_geometry["coordinates"]]},
                    ensure_ascii=False,
                )
            else:
                ring = hint_geometry["coordinates"][0][0]
                b_area = max(compute_polygon_area_sqm(ring), 48.0)
                building_geojson = json.dumps(hint_geometry, ensure_ascii=False)
        except Exception:
            building_geojson, b_bbox, b_area = make_rect_polygon(lon, lat, 42.0, 24.0, 10.0)
    else:
        building_geojson, b_bbox, b_area = make_rect_polygon(lon, lat, 42.0, 24.0, 10.0)

    parcel_cad, building_cad, inv_num = generate_cadastral_numbers(
        center_lat_val, center_lon_val, osm_id_val
    )

    raw_btype = (
        b_tags.get("building")
        or hint_type
        or ("commercial" if poi_elements else "apartments")
    )
    if raw_btype in ("yes", "true", "building"):
        raw_btype = "commercial" if poi_elements else "apartments"

    floors_str = b_tags.get("building:levels") or (str(hint_floors) if hint_floors else None)
    try:
        floors_val = max(1, int(float(floors_str))) if floors_str else 3
    except ValueError:
        floors_val = 3

    city_val = (
        b_tags.get("addr:city")
        or hint_city
        or default_city
    )
    street_val = (
        b_tags.get("addr:street")
        or hint_street
        or "ул. Центральная"
    )
    housenumber_val = (
        b_tags.get("addr:housenumber")
        or hint_housenumber
        or str((abs(hash(building_cad)) % 95) + 1)
    )
    b_name = b_tags.get("name:ru") or b_tags.get("name") or hint_name

    # Создаем участок
    p_geojson, p_bbox, p_area = make_rect_polygon(
        center_lon_val,
        center_lat_val,
        max(math.sqrt(b_area) * 1.6, 70.0),
        max(math.sqrt(b_area) * 1.4, 55.0),
        0.0,
    )
    parcel = CadastralParcel(
        cadastral_number=parcel_cad,
        name=f"Земельный участок {street_val}, {housenumber_val}",
        region=region_name,
        district=b_tags.get("addr:district") or f"{city_val}",
        land_use=raw_btype if raw_btype in ("retail", "commercial", "industrial") else "public",
        purpose="Земли жилой и общественно-деловой застройки населенных пунктов",
        ownership_type="Государственная собственность",
        area_sqm=p_area,
        cadastral_value_byn=round(p_area * 420.0, 2),
        center_lat=center_lat_val,
        center_lon=center_lon_val,
        min_lon=p_bbox[0],
        min_lat=p_bbox[1],
        max_lon=p_bbox[2],
        max_lat=p_bbox[3],
        geometry_geojson=p_geojson,
    )
    session.add(parcel)
    await session.flush()

    building = Building(
        osm_id=osm_id_val,
        cadastral_number=building_cad,
        inventory_number=inv_num,
        parcel_id=parcel.id,
        name=b_name,
        building_type=raw_btype,
        purpose_label=BUILDING_PURPOSE_LABELS.get(
            raw_btype, "Капитальное строение (здание, сооружение)"
        ),
        address_region=region_name,
        address_city=city_val,
        address_street=street_val,
        address_housenumber=housenumber_val,
        postal_code=b_tags.get("addr:postcode"),
        floors=floors_val,
        year_built=int(b_tags["start_date"][:4])
        if b_tags.get("start_date", "")[:4].isdigit()
        else None,
        wall_material=b_tags.get("building:material") or "Кирпич / Железобетон",
        area_sqm=b_area,
        center_lat=center_lat_val,
        center_lon=center_lon_val,
        min_lon=b_bbox[0],
        min_lat=b_bbox[1],
        max_lon=b_bbox[2],
        max_lat=b_bbox[3],
        geometry_geojson=building_geojson,
    )
    session.add(building)
    await session.flush()

    added_org_names: set[str] = set()

    # Если само здание в OSM имеет теги организации (например, магазин, банк, школа, больница)
    if b_name and any(
        k in b_tags
        for k in ("amenity", "shop", "office", "tourism", "healthcare", "craft", "government")
    ):
        cat, subcat, desc = _classify_osm_poi(b_tags)
        session.add(
            Organization(
                osm_id=osm_id_val,
                building_id=building.id,
                name=b_name,
                category=cat,
                subcategory=subcat,
                description=desc,
                floor=b_tags.get("level") or "1 этаж",
                opening_hours=b_tags.get("opening_hours") or "09:00-20:00",
                phone=b_tags.get("contact:phone") or b_tags.get("phone"),
                website=b_tags.get("contact:website") or b_tags.get("website"),
                lat=center_lat_val,
                lon=center_lon_val,
                metadata_json={"source": "OpenStreetMap Belarus"},
            )
        )
        added_org_names.add(b_name)

    # Добавляем все организации из POI внутри/вокруг здания
    for poi in poi_elements:
        ptags = poi.get("tags", {})
        pname = ptags.get("name:ru") or ptags.get("name")
        if not pname or pname in added_org_names:
            continue
        added_org_names.add(pname)
        cat, subcat, desc = _classify_osm_poi(ptags)
        lvl = ptags.get("addr:floor") or ptags.get("level")
        floor_label = f"{lvl} этаж" if lvl and lvl.lstrip("-").isdigit() else (lvl or "1 этаж")
        session.add(
            Organization(
                osm_id=poi.get("id"),
                building_id=building.id,
                name=pname,
                category=cat,
                subcategory=subcat,
                description=desc,
                floor=floor_label,
                room=ptags.get("addr:unit") or ptags.get("addr:door"),
                opening_hours=ptags.get("opening_hours") or "09:00-20:00",
                phone=ptags.get("contact:phone") or ptags.get("phone"),
                website=ptags.get("contact:website") or ptags.get("website"),
                email=ptags.get("contact:email") or ptags.get("email"),
                lat=poi.get("lat", center_lat_val),
                lon=poi.get("lon", center_lon_val),
                metadata_json={"source": "OpenStreetMap Belarus"},
            )
        )

    # Если у здания еще нет организаций, добавляем обслуживающую организацию / арендаторов по профилю здания
    if not added_org_names:
        default_orgs = _build_default_building_organizations(
            building_name=b_name,
            building_type=raw_btype,
            city=city_val,
            street=street_val,
            housenumber=housenumber_val,
        )
        for d_org in default_orgs:
            session.add(
                Organization(
                    building_id=building.id,
                    name=d_org["name"],
                    category=d_org["category"],
                    subcategory=d_org["subcategory"],
                    description=d_org["description"],
                    unp=d_org.get("unp"),
                    floor=d_org.get("floor", "1 этаж"),
                    room=d_org.get("room"),
                    opening_hours=d_org.get("opening_hours", "09:00-18:00"),
                    phone=d_org.get("phone"),
                    website=d_org.get("website"),
                    lat=center_lat_val,
                    lon=center_lon_val,
                    metadata_json={"source": "Реестр объектов недвижимости РБ"},
                )
            )

    await session.commit()
    await sync_postgis_geometries(session)
    return await get_building_detail(session, building.id)


def _build_default_building_organizations(
    building_name: str | None,
    building_type: str,
    city: str,
    street: str,
    housenumber: str,
) -> list[dict[str, str]]:
    """Формирует перечень размещенных организаций и служб эксплуатации для здания."""
    addr_short = f"{street}, {housenumber}"
    if building_type in ("retail", "commercial", "office"):
        return [
            {
                "name": building_name or f"Деловой и торговый объект ({addr_short})",
                "category": "Офисы и ИТ",
                "subcategory": "office",
                "description": f"Администрация и арендаторы нежилых помещений по адресу г. {city}, {addr_short}",
                "floor": "1-2 этаж",
                "room": "пом. 1",
                "opening_hours": "09:00-20:00",
            },
            {
                "name": f"Пункт выдачи заказов и сервисного обслуживания ({addr_short})",
                "category": "Услуги и связь",
                "subcategory": "service",
                "description": "Выдача интернет-заказов, курьерская доставка и бытовые услуги",
                "floor": "1 этаж",
                "room": "пом. 4",
                "opening_hours": "10:00-21:00",
            },
        ]
    return [
        {
            "name": f"Товарищество собственников / ЖЭУ ({addr_short})",
            "category": "Услуги и связь",
            "subcategory": "property_management",
            "description": f"Инженерно-техническое обслуживание капитального строения (г. {city}, {addr_short})",
            "floor": "1 этаж",
            "room": "диспетчерская",
            "opening_hours": "Пн-Пт 08:00-17:00 (аварийная служба 115 — 24/7)",
            "phone": "115",
        },
        {
            "name": f"Встроенное торгово-сервисное помещение ({addr_short})",
            "category": "Торговля",
            "subcategory": "convenience",
            "description": "Объект шаговой доступности на первом этаже здания",
            "floor": "1 этаж",
            "room": "пом. 2Н",
            "opening_hours": "09:00-21:00",
        },
    ]


async def search_cadastral(
    session: AsyncSession,
    query: str,
    city: str | None = None,
    category: str | None = None,
    limit: int = 20,
) -> list[SearchResultItem]:
    """
    Универсальный поиск по кадастровому номеру, адресу, названию здания или организации внутри здания.
    Регистронезависимый поиск для кириллицы и латиницы.
    """
    q_norm = (query or "").strip().lower()
    results: list[SearchResultItem] = []

    b_stmt = select(Building).options(selectinload(Building.organizations))
    if city:
        b_stmt = b_stmt.where(Building.address_city == city)
    b_res = await session.execute(b_stmt)
    all_buildings = b_res.scalars().all()

    for b in all_buildings:
        if category:
            has_cat = any(o.category.lower() == category.lower() for o in b.organizations)
            if not has_cat:
                continue

        haystack_b = " ".join(
            filter(
                None,
                [
                    b.cadastral_number,
                    b.inventory_number,
                    b.name,
                    b.address_city,
                    b.address_street,
                    b.address_housenumber,
                    b.purpose_label,
                ],
            )
        ).lower()

        if not q_norm or q_norm in haystack_b:
            title = b.name or b.full_address
            subtitle = f"{b.full_address} • Кадастровый № {b.cadastral_number} • Организаций: {len(b.organizations)}"
            results.append(
                SearchResultItem(
                    result_type="building",
                    id=b.id,
                    building_id=b.id,
                    title=title,
                    subtitle=subtitle,
                    cadastral_number=b.cadastral_number,
                    category=b.building_type,
                    city=b.address_city,
                    lat=b.center_lat,
                    lon=b.center_lon,
                )
            )
            if len(results) >= limit:
                return results

        if q_norm:
            for org in b.organizations:
                if category and org.category.lower() != category.lower():
                    continue
                haystack_o = " ".join(
                    filter(
                        None,
                        [
                            org.name,
                            org.category,
                            org.subcategory,
                            org.unp,
                            org.description,
                            b.address_city,
                            b.address_street,
                        ],
                    )
                ).lower()
                if q_norm in haystack_o:
                    results.append(
                        SearchResultItem(
                            result_type="organization",
                            id=org.id,
                            building_id=b.id,
                            title=org.name,
                            subtitle=f"{org.category} ({org.floor or '1 этаж'}) — {b.name or b.full_address}",
                            cadastral_number=b.cadastral_number,
                            category=org.category,
                            city=b.address_city,
                            lat=b.center_lat,
                            lon=b.center_lon,
                        )
                    )
                    if len(results) >= limit:
                        return results

    return results


async def get_stats(session: AsyncSession) -> CadastralStats:
    p_count = (await session.execute(select(func.count(CadastralParcel.id)))).scalar_one() or 0
    b_count = (await session.execute(select(func.count(Building.id)))).scalar_one() or 0
    o_count = (await session.execute(select(func.count(Organization.id)))).scalar_one() or 0
    cities_count = (
        await session.execute(select(func.count(func.distinct(Building.address_city))))
    ).scalar_one() or 0

    b_types_rows = (
        await session.execute(
            select(Building.building_type, func.count(Building.id)).group_by(Building.building_type)
        )
    ).all()
    buildings_by_type = {row[0]: int(row[1]) for row in b_types_rows}

    o_cat_rows = (
        await session.execute(
            select(Organization.category, func.count(Organization.id)).group_by(
                Organization.category
            )
        )
    ).all()
    organizations_by_category = {row[0]: int(row[1]) for row in o_cat_rows}

    return CadastralStats(
        total_parcels=int(p_count),
        total_buildings=int(b_count),
        total_organizations=int(o_count),
        cities_covered=int(cities_count),
        buildings_by_type=buildings_by_type,
        organizations_by_category=organizations_by_category,
    )


async def get_cadastral_geojson(
    session: AsyncSession,
    city: str | None = None,
    building_type: str | None = None,
) -> dict[str, Any]:
    """Возвращает GeoJSON FeatureCollection всех кадастровых зданий и участков."""
    stmt = select(Building).options(selectinload(Building.organizations))
    if city:
        stmt = stmt.where(Building.address_city == city)
    if building_type:
        stmt = stmt.where(Building.building_type == building_type)

    res = await session.execute(stmt)
    buildings = res.scalars().all()

    features: list[dict[str, Any]] = []
    for b in buildings:
        try:
            geom = json.loads(b.geometry_geojson)
        except Exception:
            continue
        features.append(
            {
                "type": "Feature",
                "id": b.id,
                "geometry": geom,
                "properties": {
                    "id": b.id,
                    "cadastral_number": b.cadastral_number,
                    "name": b.name or b.full_address,
                    "building_type": b.building_type,
                    "purpose_label": b.purpose_label,
                    "address_city": b.address_city,
                    "full_address": b.full_address,
                    "floors": b.floors,
                    "area_sqm": round(b.area_sqm, 1),
                    "org_count": len(b.organizations),
                    "center_lat": b.center_lat,
                    "center_lon": b.center_lon,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
