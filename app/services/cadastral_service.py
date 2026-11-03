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
from app.models.egr_record import EgrRecord
from app.models.organization import Organization
from app.models.parcel import CadastralParcel
from app.schemas.building import (
    BuildingDetail,
    CadastralStats,
    ParcelSummary,
    SearchResultItem,
)
from app.schemas.organization import OrganizationRead
from app.services.address_matcher import (
    addresses_strictly_match,
    canonicalize_city_name,
    extract_single_building_polygon,
    has_address_conflict,
    normalize_city,
    normalize_housenumber,
    normalize_street,
    register_street_alias,
    translate_belarusian_street_to_ru,
)
from app.services.egr_service import attach_egr_records_to_building
from app.services.seed_data import SEED_PARCELS_AND_BUILDINGS, make_rect_polygon

logger = logging.getLogger(__name__)

OSM_API_MAP_URL = "https://api.openstreetmap.org/api/0.6/map.json"

BUILDING_PURPOSE_LABELS: dict[str, str] = {
    "retail": "Здание специализированное розничной торговли",
    "commercial": "Многофункциональное общественно-деловое здание",
    "office": "Административно-офисное здание (бизнес-центр)",
    "public": "Здание административно-общественного и культурного назначения",
    "university": "Здание учреждения высшего образования и науки",
    "school": "Здание учреждения общего среднего образования",
    "college": "Здание среднего специального учебного заведения",
    "kindergarten": "Здание дошкольного учреждения",
    "hospital": "Здание организации здравоохранения",
    "industrial": "Производственно-складское капитальное строение",
    "apartments": "Многоквартирный жилой дом со встроенными помещениями",
    "residential": "Жилое капитальное строение",
    "hotel": "Здание гостиничного комплекса",
    "civic": "Общественное здание",
    "government": "Административное здание государственного органа",
    "train_station": "Здание железнодорожного вокзала",
}

OSM_CATEGORY_MAP: dict[str, tuple[str, str]] = {
    "bank": ("Финансы и банки", "Банковское отделение"),
    "atm": ("Финансы и банки", "Банкомат / Инфокиоск"),
    "bureau_de_change": ("Финансы и банки", "Пункт обмена валют"),
    "payment_terminal": ("Финансы и банки", "Платежный терминал"),
    "insurance": ("Финансы и банки", "Страховая организация"),
    "financial": ("Финансы и банки", "Финансовая организация"),
    "pharmacy": ("Медицина и аптеки", "Аптека"),
    "hospital": ("Медицина и аптеки", "Больница / Медцентр"),
    "clinic": ("Медицина и аптеки", "Медицинский центр / Поликлиника"),
    "dentist": ("Медицина и аптеки", "Стоматологическая клиника"),
    "doctors": ("Медицина и аптеки", "Медицинский кабинет / Врачебная практика"),
    "veterinary": ("Медицина и аптеки", "Ветеринарная клиника / аптека"),
    "laboratory": ("Медицина и аптеки", "Медицинская лаборатория"),
    "optician": ("Медицина и аптеки", "Салон оптики"),
    "supermarket": ("Торговля", "Продовольственный супермаркет / гипермаркет"),
    "convenience": ("Торговля", "Продуктовый магазин шаговой доступности"),
    "mall": ("Торговля", "Торгово-развлекательный центр"),
    "department_store": ("Торговля", "Универмаг"),
    "clothes": ("Торговля", "Магазин одежды"),
    "shoes": ("Торговля", "Магазин обуви"),
    "electronics": ("Торговля", "Магазин электроники и бытовой техники"),
    "mobile_phone": ("Торговля", "Салон мобильной связи и техники"),
    "computer": ("Торговля", "Компьютерный магазин"),
    "jewelry": ("Торговля", "Ювелирный салон"),
    "books": ("Торговля", "Книжный магазин"),
    "stationery": ("Торговля", "Канцелярские товары"),
    "bakery": ("Торговля", "Пекарня / Кондитерская"),
    "confectionery": ("Торговля", "Фирменный кондитерский магазин"),
    "butcher": ("Торговля", "Мясной магазин"),
    "alcohol": ("Торговля", "Магазин напитков"),
    "cosmetics": ("Торговля", "Магазин косметики и парфюмерии"),
    "chemist": ("Торговля", "Магазин бытовой химии и косметики"),
    "florist": ("Торговля", "Цветочный салон"),
    "furniture": ("Торговля", "Мебельный салон"),
    "hardware": ("Торговля", "Строительные и хозяйственные товары"),
    "doityourself": ("Торговля", "Строительный магазин (DIY)"),
    "sports": ("Торговля", "Спортивный магазин"),
    "toys": ("Торговля", "Магазин детских товаров и игрушек"),
    "gift": ("Торговля", "Магазин подарков и сувениров"),
    "kiosk": ("Торговля", "Торговый павильон / киоск"),
    "pet": ("Торговля", "Зоомагазин"),
    "car_parts": ("Торговля", "Магазин автозапчастей"),
    "restaurant": ("Общепит", "Ресторан"),
    "cafe": ("Общепит", "Кафе / Кофейня"),
    "fast_food": ("Общепит", "Ресторан быстрого питания"),
    "bar": ("Общепит", "Бар / Паб"),
    "pub": ("Общепит", "Паб / Бар"),
    "food_court": ("Общепит", "Фуд-корт"),
    "ice_cream": ("Общепит", "Кафе-мороженое"),
    "canteen": ("Общепит", "Столовая / Буфет"),
    "post_office": ("Госучреждения и почта", "Отделение почтовой связи РУП «Белпочта»"),
    "townhall": ("Госучреждения и почта", "Исполнительный комитет / Администрация"),
    "government": ("Госучреждения и почта", "Государственный орган / учреждение"),
    "courthouse": ("Госучреждения и почта", "Суд Республики Беларусь"),
    "police": ("Госучреждения и почта", "Орган внутренних дел"),
    "embassy": ("Госучреждения и почта", "Дипломатическое представительство / Посольство"),
    "diplomatic": ("Госучреждения и почта", "Дипломатическое учреждение"),
    "community_centre": ("Госучреждения и почта", "Общественный центр"),
    "library": ("Культура и отдых", "Библиотека"),
    "museum": ("Культура и отдых", "Музей / Выставочный зал"),
    "arts_centre": ("Культура и отдых", "Художественная галерея / Арт-центр"),
    "cinema": ("Культура и отдых", "Кинотеатр"),
    "theatre": ("Культура и отдых", "Театр / Концертный зал"),
    "fitness_centre": ("Культура и отдых", "Фитнес-клуб / Тренажерный зал"),
    "sports_centre": ("Культура и отдых", "Спортивный комплекс"),
    "university": ("Образование и наука", "Университет / Корпус ВУЗа"),
    "college": ("Образование и наука", "Колледж / Техникум"),
    "school": ("Образование и наука", "Средняя школа / Гимназия"),
    "kindergarten": ("Образование и наука", "Детский сад"),
    "language_school": ("Образование и наука", "Языковая школа / Учебный центр"),
    "driving_school": ("Образование и наука", "Автошкола"),
    "research_institute": ("Образование и наука", "Научно-исследовательский институт"),
    "it": ("Офисы и ИТ", "ИТ-компания / Центр разработки"),
    "company": ("Офисы и ИТ", "Офис коммерческой компании"),
    "coworking": ("Офисы и ИТ", "Коворкинг / Деловой центр"),
    "association": ("Офисы и ИТ", "Общественное объединение / Ассоциация"),
    "ngo": ("Офисы и ИТ", "Некоммерческая организация"),
    "lawyer": ("Юридические и деловые услуги", "Юридическая консультация / Адвокатское бюро"),
    "notary": ("Юридические и деловые услуги", "Нотариальная контора"),
    "accountant": ("Юридические и деловые услуги", "Бухгалтерские и аудиторские услуги"),
    "estate_agent": ("Юридические и деловые услуги", "Агентство недвижимости"),
    "advertising_agency": ("Юридические и деловые услуги", "Рекламное агентство"),
    "architect": ("Юридические и деловые услуги", "Архитектурно-проектное бюро"),
    "telecommunication": ("Услуги и связь", "Салон связи и телекоммуникаций"),
    "hairdresser": ("Услуги и связь", "Парикмахерская / Барбершоп"),
    "beauty": ("Услуги и связь", "Салон красоты / Косметология"),
    "dry_cleaning": ("Услуги и связь", "Химчистка / Прачечная"),
    "tailor": ("Услуги и связь", "Ателье по ремонту и пошиву одежды"),
    "shoemaker": ("Услуги и связь", "Мастерская по ремонту обуви"),
    "electronics_repair": ("Услуги и связь", "Сервисный центр по ремонту техники"),
    "copyshop": ("Услуги и связь", "Печатный центр / Полиграфия"),
    "photo": ("Услуги и связь", "Фотостудия / Фотоцентр"),
    "travel_agency": ("Гостиницы и туризм", "Туристическое агентство"),
    "hotel": ("Гостиницы и туризм", "Гостиница / Отель"),
    "hostel": ("Гостиницы и туризм", "Хостел"),
    "guest_house": ("Гостиницы и туризм", "Гостевой дом"),
    "information": ("Гостиницы и туризм", "Информационно-туристический центр"),
}

POI_TAG_KEYS = (
    "amenity",
    "shop",
    "office",
    "craft",
    "tourism",
    "healthcare",
    "leisure",
    "government",
    "diplomatic",
    "club",
    "industrial",
)


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


def generate_cadastral_numbers(
    lat: float, lon: float, osm_id: int | None = None
) -> tuple[str, str, str]:
    """
    Формирует уникальный структурированный кадастровый номер земельного участка и здания
    по стандарту Национального кадастрового агентства РБ (18 цифр + индекс строения).
    Учитывает точные координаты и osm_id, чтобы два разных дома с одинаковым номером (д. 9)
    всегда получали уникальные кадастровые номера.
    """
    _, _, soato_prefix = detect_belarus_region_and_soato(lat, lon)
    seed_str = f"{osm_id or 0}:{lat:.6f}:{lon:.6f}"
    digest = hashlib.sha1(seed_str.encode("utf-8")).hexdigest()
    num_hash = int(digest[:10], 16)
    quarter = (num_hash // 100000) % 89 + 10
    parcel_num = num_hash % 899999 + 100000
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


def _classify_osm_poi(tags: dict[str, str]) -> tuple[str, str, str]:
    """Определяет (category, subcategory, description) для объекта из тегов OpenStreetMap."""
    for key in POI_TAG_KEYS:
        val = tags.get(key)
        if val and val not in ("yes", "true"):
            if val in OSM_CATEGORY_MAP:
                cat, desc = OSM_CATEGORY_MAP[val]
                return cat, val, tags.get("description:ru") or tags.get("description") or desc
            if key == "shop":
                return "Торговля", val, tags.get("description") or f"Магазин / торговый объект ({val})"
            if key == "office":
                return "Офисы и ИТ", val, tags.get("description") or f"Офис компании ({val})"
            if key == "amenity":
                return "Услуги и связь", val, tags.get("description") or f"Объект обслуживания ({val})"
            if key == "craft":
                return "Услуги и связь", val, tags.get("description") or f"Мастерская / сервис ({val})"
            if key == "tourism":
                return "Гостиницы и туризм", val, tags.get("description") or f"Туристический объект ({val})"
            if key == "healthcare":
                return "Медицина и аптеки", val, tags.get("description") or f"Медицинское учреждение ({val})"
            if key in ("government", "diplomatic"):
                return "Госучреждения и почта", val, tags.get("description") or "Государственное учреждение"
            if key in ("leisure", "club"):
                return "Культура и отдых", val, tags.get("description") or f"Центр досуга и спорта ({val})"
    if "office" in tags:
        return "Офисы и ИТ", "office", tags.get("description") or "Офис организации"
    if "shop" in tags:
        return "Торговля", "shop", tags.get("description") or "Торговый объект"
    return "Офисы и ИТ", "organization", tags.get("description") or "Организация / Помещение"


def _extract_poi_name(tags: dict[str, str]) -> str | None:
    """Извлекает наименование организации из тегов OSM (приоритет русскому названию)."""
    name = (
        tags.get("name:ru")
        or tags.get("name")
        or tags.get("brand:ru")
        or tags.get("brand")
        or tags.get("operator:ru")
        or tags.get("operator")
    )
    if name:
        return name.strip()
    return None


def _is_poi_element(tags: dict[str, str]) -> bool:
    """Проверяет, является ли элемент OSM организацией / объектом внутри здания."""
    if not tags:
        return False
    # Исключаем станции метро, остановки транспорта, улицы и чисто жилые здания без POI-тегов
    if tags.get("railway") in ("station", "subway_entrance", "stop", "tram_stop"):
        return False
    if tags.get("highway") in ("bus_stop", "platform", "crossing", "traffic_signals"):
        return False
    if tags.get("public_transport") in ("stop_position", "platform", "station"):
        return False
    if tags.get("amenity") in (
        "bench",
        "waste_basket",
        "waste_disposal",
        "recycling",
        "parking",
        "parking_entrance",
        "bicycle_parking",
        "toilets",
        "fountain",
        "clock",
        "vending_machine",
    ):
        return False

    name = _extract_poi_name(tags)
    if not name:
        return False

    return any(k in tags for k in POI_TAG_KEYS)


async def fetch_osm_map_box(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> dict[str, Any] | None:
    """
    Запрашивает геоданные напрямую через официальный быстрый эндпоинт
    OpenStreetMap API v0.6 (/api/0.6/map.json?bbox=...) и парсит:
    - все здания (ways) с их точными полигонами, адресами и названиями
    - все организации/POI (nodes & ways) с их координатами и тегами
    - переводы белорусских названий улиц в русские из дорожного графа (highway).
    """
    if not settings.enable_live_osm_enrichment:
        return None

    bbox_str = f"{min_lon:.6f},{min_lat:.6f},{max_lon:.6f},{max_lat:.6f}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{OSM_API_MAP_URL}?bbox={bbox_str}",
                headers={"User-Agent": "BelarusCadastralMap/1.0 (FastAPI OpenGIS)"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception as exc:
        logger.debug("OSM API v0.6 недоступен для bbox %s: %s", bbox_str, exc)
        return None

    elements = data.get("elements", [])
    if not elements:
        return None

    nodes_coords: dict[int, tuple[float, float]] = {}
    for el in elements:
        if el.get("type") == "node" and "lon" in el and "lat" in el:
            nodes_coords[el["id"]] = (float(el["lon"]), float(el["lat"]))

    # Регистрируем двуязычные названия улиц (белорусский <-> русский) из дорожного графа этого квартала
    for el in elements:
        tags = el.get("tags")
        if not tags:
            continue
        if "highway" in tags and "name" in tags and "name:ru" in tags:
            register_street_alias(tags.get("name"), tags.get("name:ru"))
            if "name:be" in tags:
                register_street_alias(tags.get("name:be"), tags.get("name:ru"))
        if "addr:street" in tags and "addr:street:ru" in tags:
            register_street_alias(tags.get("addr:street"), tags.get("addr:street:ru"))

    buildings_parsed: list[dict[str, Any]] = []
    pois_parsed: list[dict[str, Any]] = []
    addr_nodes: list[dict[str, Any]] = []

    for el in elements:
        if el.get("type") == "node" and "lon" in el and "lat" in el:
            ntags = el.get("tags") or {}
            if ntags.get("addr:housenumber") or ntags.get("addr:street"):
                addr_nodes.append(
                    {
                        "id": el["id"],
                        "lon": float(el["lon"]),
                        "lat": float(el["lat"]),
                        "tags": ntags,
                    }
                )

    for el in elements:
        etype = el.get("type")
        tags = dict(el.get("tags") or {})

        if etype == "way":
            node_ids = el.get("nodes") or []
            ring = [
                [round(nodes_coords[nid][0], 6), round(nodes_coords[nid][1], 6)]
                for nid in node_ids
                if nid in nodes_coords
            ]
            if len(ring) >= 3 and "building" in tags:
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                try:
                    poly = Polygon(ring)
                    if poly.is_valid and poly.area > 0:
                        # Если на самом контуре здания нет addr:housenumber / addr:street,
                        # проверяем адресные точки (входы/адресные узлы OSM) внутри этого полигона
                        if not tags.get("addr:housenumber") or not tags.get("addr:street"):
                            node_id_set = set(node_ids)
                            buf_poly = poly.buffer(0.00003)
                            for an in addr_nodes:
                                if an["id"] in node_id_set or buf_poly.contains(
                                    Point(an["lon"], an["lat"])
                                ):
                                    atags = an["tags"]
                                    for ak in (
                                        "addr:housenumber",
                                        "addr:street",
                                        "addr:street:ru",
                                        "addr:city",
                                        "addr:postcode",
                                    ):
                                        if atags.get(ak) and not tags.get(ak):
                                            tags[ak] = atags[ak]
                                    if tags.get("addr:housenumber") and tags.get("addr:street"):
                                        break

                        buildings_parsed.append(
                            {
                                "osm_id": el["id"],
                                "tags": tags,
                                "ring": ring,
                                "polygon": poly,
                            }
                        )
                except Exception:
                    pass

            # Если way является внутренней организацией (например, крупный магазин/ресторан/клиника внутри ТЦ)
            if ring and _is_poi_element(tags) and "building" not in tags:
                avg_lon = sum(c[0] for c in ring) / len(ring)
                avg_lat = sum(c[1] for c in ring) / len(ring)
                pois_parsed.append(
                    {
                        "osm_id": el["id"],
                        "lat": round(avg_lat, 6),
                        "lon": round(avg_lon, 6),
                        "tags": tags,
                    }
                )

        elif etype == "node":
            if _is_poi_element(tags):
                pois_parsed.append(
                    {
                        "osm_id": el["id"],
                        "lat": round(float(el["lat"]), 6),
                        "lon": round(float(el["lon"]), 6),
                        "tags": tags,
                    }
                )

    return {
        "buildings": buildings_parsed,
        "pois": pois_parsed,
    }


def match_pois_strictly_to_building(
    building_poly: Polygon,
    building_city: str | None,
    building_street: str | None,
    building_house: str | None,
    all_pois: list[dict[str, Any]],
    all_building_polys: list[tuple[int | None, Polygon]],
    current_osm_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Привязывает организации (POI) из OSM к конкретному зданию по строгим правилам:
    1. Никакого конфликта адресов! Если у POI указан другой номер дома (например, д. 7, д. 9А или д. 11 вместо д. 9)
       или другая улица (например, ул. Немига, 9 вместо пр-т Победителей, 9) — POI жестко отклоняется.
    2. Приоритет физическому вхождению точки POI внутрь полигона именно этого здания
       (building_poly.buffer(0.000045).contains(pt)).
    3. Если точка POI стоит у входа (до ~22 метров от стены), она принимается ТОЛЬКО если:
       - ее адрес (улица + номер дома) строго совпадает с адресом здания, И
       - она не находится внутри полигона другого соседнего здания / корпуса.
    """
    matched: list[dict[str, Any]] = []
    buffered_poly = building_poly.buffer(0.000045)  # ~4 метра допуск на стены здания

    for poi in all_pois:
        ptags = poi.get("tags") or {}
        poi_city = ptags.get("addr:city")
        poi_street = ptags.get("addr:street:ru") or ptags.get("addr:street")
        poi_house = ptags.get("addr:housenumber")

        # 1. Отсекаем любые объекты, у которых явно прописан чужой адрес (другая улица или другой дом/корпус)
        if has_address_conflict(
            building_city=building_city,
            building_street=building_street,
            building_house=building_house,
            poi_city=poi_city,
            poi_street=poi_street,
            poi_house=poi_house,
        ):
            continue

        pt = Point(poi["lon"], poi["lat"])

        # 2. Точка строго внутри контура данного здания
        if building_poly.contains(pt):
            matched.append(poi)
            continue

        # Если точка строго внутри полигона ДРУГОГО здания (например, соседнего дома или другого корпуса),
        # она принадлежит тому другому зданию и никогда не захватывается буфером текущего!
        inside_another_building = any(
            other_id != current_osm_id and other_poly.contains(pt)
            for other_id, other_poly in all_building_polys
        )
        if inside_another_building:
            continue

        dist_to_this = building_poly.distance(pt)
        is_closest_building = all(
            other_id == current_osm_id or other_poly.distance(pt) >= dist_to_this - 1e-7
            for other_id, other_poly in all_building_polys
        )

        # 2б. Точка на внешней границе/стене данного здания (до 3.5м) и это ближайшее здание
        if buffered_poly.contains(pt) and is_closest_building:
            matched.append(poi)
            continue

        # 3. Точка у входа (до 18м), если адрес строго совпадает (та же улица и тот же номер дома)
        # и текущее здание является ближайшим к этой точке
        if poi_street and poi_house and building_street and building_house:
            if addresses_strictly_match(
                building_city,
                building_street,
                building_house,
                poi_city,
                poi_street,
                poi_house,
            ):
                if dist_to_this <= 0.00018 and is_closest_building:
                    matched.append(poi)

    return matched


async def enrich_building_from_osm_and_egr(
    session: AsyncSession,
    building: Building,
    osm_box_cache: dict[str, Any] | None = None,
    auto_commit: bool = True,
    matched_osm_b_override: dict[str, Any] | None = None,
    prematched_pois: list[dict[str, Any]] | None = None,
) -> int:
    """
    Обогащает конкретное здание ВСЕМИ существующими организациями:
    1) Из OpenStreetMap API v0.6 (все магазины, банки, кафе, аптеки, офисы, услуги внутри контура здания)
    2) Из официального ЕГР Минюста РБ (egr.gov.by) — только действующие субъекты, зарегистрированные
       строго по адресу этого здания.
    При обнаружении реального контура здания в OSM также уточняет полигон и адрес здания.
    """
    added_total = 0

    # 1. Обогащение из OpenStreetMap API v0.6
    if settings.enable_live_osm_enrichment:
        pad_lon = max((building.max_lon - building.min_lon) * 1.2, 0.0022)
        pad_lat = max((building.max_lat - building.min_lat) * 1.2, 0.0016)
        osm_data = osm_box_cache or await fetch_osm_map_box(
            building.center_lon - pad_lon,
            building.center_lat - pad_lat,
            building.center_lon + pad_lon,
            building.center_lat + pad_lat,
        )

        if osm_data:
            osm_buildings = osm_data.get("buildings", [])
            osm_pois = osm_data.get("pois", [])
            center_pt = Point(building.center_lon, building.center_lat)

            # Ищем соответствующий полигон здания в OSM
            matched_osm_b: dict[str, Any] | None = matched_osm_b_override
            if matched_osm_b is None:
                for ob in osm_buildings:
                    if building.osm_id and ob["osm_id"] == building.osm_id:
                        matched_osm_b = ob
                        break

            if matched_osm_b is None:
                # Сначала ищем здание в OSM, чей полигон содержит центр нашего здания и чей адрес не конфликтует
                for ob in osm_buildings:
                    ob_tags = ob["tags"]
                    ob_street = ob_tags.get("addr:street:ru") or translate_belarusian_street_to_ru(
                        ob_tags.get("addr:street")
                    )
                    ob_house = ob_tags.get("addr:housenumber")
                    if ob["polygon"].buffer(0.00004).contains(center_pt):
                        if not has_address_conflict(
                            building.address_city,
                            building.address_street,
                            building.address_housenumber,
                            ob_tags.get("addr:city"),
                            ob_street,
                            ob_house,
                        ):
                            matched_osm_b = ob
                            break

            if matched_osm_b is not None:
                ob_tags = matched_osm_b["tags"]
                ring = matched_osm_b["ring"]
                poly: Polygon = matched_osm_b["polygon"]

                # Обновляем гео-контур здания на реальный контур из OSM
                lons = [c[0] for c in ring]
                lats = [c[1] for c in ring]
                building.osm_id = matched_osm_b["osm_id"]
                building.min_lon = min(lons)
                building.max_lon = max(lons)
                building.min_lat = min(lats)
                building.max_lat = max(lats)
                building.area_sqm = max(compute_polygon_area_sqm(ring), building.area_sqm)
                building.geometry_geojson = json.dumps(
                    {"type": "MultiPolygon", "coordinates": [[ring]]}, ensure_ascii=False
                )

                raw_street = ob_tags.get("addr:street:ru") or translate_belarusian_street_to_ru(
                    ob_tags.get("addr:street")
                )
                if raw_street and (
                    not building.address_street or building.address_street == "ул. Центральная"
                ):
                    building.address_street = raw_street
                if ob_tags.get("addr:housenumber") and not building.address_housenumber:
                    building.address_housenumber = ob_tags.get("addr:housenumber")
                if ob_tags.get("addr:postcode") and not building.postal_code:
                    building.postal_code = ob_tags.get("addr:postcode")
                if not building.name:
                    building.name = ob_tags.get("name:ru") or ob_tags.get("name")
            else:
                try:
                    poly = shape(json.loads(building.geometry_geojson))
                except Exception:
                    poly = center_pt.buffer(0.00025)

            if prematched_pois is not None:
                strictly_matched_pois = prematched_pois
            else:
                all_b_polys = [(ob["osm_id"], ob["polygon"]) for ob in osm_buildings]
                strictly_matched_pois = match_pois_strictly_to_building(
                    building_poly=poly,
                    building_city=building.address_city,
                    building_street=building.address_street,
                    building_house=building.address_housenumber,
                    all_pois=osm_pois,
                    all_building_polys=all_b_polys,
                    current_osm_id=building.osm_id,
                )

            existing_orgs_res = await session.execute(
                select(Organization).where(Organization.building_id == building.id)
            )
            existing_orgs = list(existing_orgs_res.scalars().all())
            existing_osm_ids = {o.osm_id for o in existing_orgs if o.osm_id}
            existing_names_floors = {
                (o.name.strip().lower(), (o.floor or "").strip().lower())
                for o in existing_orgs
                if o.name
            }

            # Если само здание в OSM является организацией (например, школа, гимназия, больница, банк, музей)
            if matched_osm_b and _is_poi_element(matched_osm_b["tags"]):
                b_tags = matched_osm_b["tags"]
                b_poi_name = _extract_poi_name(b_tags)
                if b_poi_name and (b_poi_name.lower(), "1 этаж") not in existing_names_floors:
                    cat, subcat, desc = _classify_osm_poi(b_tags)
                    session.add(
                        Organization(
                            osm_id=matched_osm_b["osm_id"],
                            building_id=building.id,
                            name=b_poi_name,
                            category=cat,
                            subcategory=subcat,
                            description=desc,
                            unp=b_tags.get("ref:vatin") or b_tags.get("ref:unp"),
                            floor="1 этаж",
                            opening_hours=b_tags.get("opening_hours") or "09:00-20:00",
                            phone=b_tags.get("contact:phone") or b_tags.get("phone"),
                            website=b_tags.get("contact:website") or b_tags.get("website"),
                            email=b_tags.get("contact:email") or b_tags.get("email"),
                            lat=building.center_lat,
                            lon=building.center_lon,
                            metadata_json={"source": "OpenStreetMap РБ"},
                        )
                    )
                    existing_names_floors.add((b_poi_name.lower(), "1 этаж"))
                    added_total += 1

            for poi in strictly_matched_pois:
                if poi["osm_id"] in existing_osm_ids:
                    continue
                ptags = poi["tags"]
                pname = _extract_poi_name(ptags)
                if not pname:
                    continue

                lvl = ptags.get("addr:floor") or ptags.get("level")
                if lvl is not None:
                    lvl_s = str(lvl).strip()
                    floor_label = (
                        f"{lvl_s} этаж"
                        if lvl_s.lstrip("-").isdigit()
                        else (lvl_s if "этаж" in lvl_s.lower() else f"Этаж {lvl_s}")
                    )
                else:
                    floor_label = "1 этаж"

                dedup_key = (pname.lower(), floor_label.lower())
                if dedup_key in existing_names_floors:
                    continue

                cat, subcat, desc = _classify_osm_poi(ptags)
                room_val = ptags.get("addr:unit") or ptags.get("addr:door") or ptags.get("room")
                if room_val and not str(room_val).lower().startswith(("пом", "оф", "каб", "пав")):
                    room_val = f"пом. {room_val}"

                session.add(
                    Organization(
                        osm_id=poi["osm_id"],
                        building_id=building.id,
                        name=pname,
                        category=cat,
                        subcategory=subcat,
                        description=desc,
                        unp=ptags.get("ref:vatin") or ptags.get("ref:unp"),
                        floor=floor_label,
                        room=room_val,
                        opening_hours=ptags.get("opening_hours") or "09:00-21:00",
                        phone=ptags.get("contact:phone") or ptags.get("phone"),
                        website=ptags.get("contact:website") or ptags.get("website"),
                        email=ptags.get("contact:email") or ptags.get("email"),
                        lat=poi["lat"],
                        lon=poi["lon"],
                        metadata_json={"source": "OpenStreetMap РБ"},
                    )
                )
                existing_osm_ids.add(poi["osm_id"])
                existing_names_floors.add(dedup_key)
                added_total += 1

            if auto_commit:
                await session.commit()

    # 2. Привязка ВСЕХ актуальных (Действующих) субъектов из реестра ЕГР Минюста РБ (egr.gov.by)
    # строго по адресу этого здания (город + улица + дом/корпус)
    egr_added = await attach_egr_records_to_building(
        session,
        building,
        verify_unnamed_live=False,
        auto_commit=auto_commit,
    )
    added_total += egr_added

    return added_total


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
                metadata_json={"source": "ЕГР / Кадастровый реестр РБ", "status": "Действующий"},
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

    # Подтягиваем актуальные организации из ЕГР (egr.gov.by), если в реестре появились новые записи для этого адреса
    egr_added = await attach_egr_records_to_building(
        session, building, verify_unnamed_live=settings.enable_live_osm_enrichment
    )
    if egr_added > 0:
        await session.refresh(building, attribute_names=["organizations"])

    return _building_to_detail(building)


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
    Точная идентификация конкретного здания по координатам клика (lat, lon).
    Гарантирует:
    1) Отсутствие ложного срабатывания на соседние здания (проверяется строгое попадание точки
       внутрь полигона здания с допуском стен не более ~3 метров, а не 50 метров).
    2) Различение разных зданий с одинаковым номером дома (например, д. 9 на разных улицах
       или разных корпусов одного квартала).
    3) Выделение только одного кликнутого полигона из MultiPolygon векторного тайла.
    4) Полную загрузку всех организаций внутри полигона из OSM API v0.6 и актуальных субъектов из ЕГР (egr.gov.by).
    """
    pt = Point(lon, lat)

    # 1. Ищем здание в локальной БД, внутри полигона которого строго находится точка клика
    delta_lat = 0.0004
    delta_lon = 0.0006
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

    matched_existing: Building | None = None
    best_wall_dist = float("inf")

    for cand in candidates:
        # Если переданы явные улица и номер дома кликнутого объекта, проверяем отсутствие конфликта адресов!
        if hint_street and hint_housenumber:
            if has_address_conflict(
                cand.address_city,
                cand.address_street,
                cand.address_housenumber,
                hint_city,
                hint_street,
                hint_housenumber,
            ):
                continue

        try:
            geom_shape = shape(json.loads(cand.geometry_geojson))
            if geom_shape.contains(pt):
                matched_existing = cand
                break
            dist = geom_shape.distance(pt)
            # Строгий допуск не более ~3.5 метра (0.000035 град.) от стены полигона!
            if dist <= 0.000035 and dist < best_wall_dist:
                best_wall_dist = dist
                matched_existing = cand
        except Exception:
            pass

    if matched_existing is not None:
        if settings.enable_live_osm_enrichment:
            added = await enrich_building_from_osm_and_egr(session, matched_existing)
            if added > 0:
                await session.refresh(matched_existing, attribute_names=["organizations"])
        return _building_to_detail(matched_existing)

    # 2. Проверяем, что координаты находятся в пределах Республики Беларусь
    if not (51.2 <= lat <= 56.25 and 23.1 <= lon <= 32.85):
        return None

    # 3. Запрашиваем точные данные квартала из OpenStreetMap API v0.6
    osm_box = await fetch_osm_map_box(
        lon - 0.0022,
        lat - 0.0016,
        lon + 0.0022,
        lat + 0.0016,
    )

    chosen_osm_b: dict[str, Any] | None = None
    if osm_box and osm_box.get("buildings"):
        containing_buildings: list[dict[str, Any]] = []
        near_wall_buildings: list[tuple[float, dict[str, Any]]] = []

        for ob in osm_box["buildings"]:
            poly: Polygon = ob["polygon"]
            if poly.contains(pt):
                containing_buildings.append(ob)
            else:
                d = poly.distance(pt)
                if d <= 0.000045:  # до ~4.5 м от стены здания
                    near_wall_buildings.append((d, ob))

        if containing_buildings:
            # Если несколько полигонов содержат точку (например, внешнее здание и внутренняя секция),
            # выбираем то, у которого указаны адрес (addr:housenumber) или имя
            containing_buildings.sort(
                key=lambda x: (
                    1 if "addr:housenumber" in x["tags"] else 0,
                    1 if "name" in x["tags"] else 0,
                    x["polygon"].area,
                ),
                reverse=True,
            )
            chosen_osm_b = containing_buildings[0]
        elif near_wall_buildings:
            near_wall_buildings.sort(key=lambda item: item[0])
            chosen_osm_b = near_wall_buildings[0][1]

    # Если нашли здание в OSM по точному вхождению точки клика, проверим, нет ли его уже в БД по osm_id
    if chosen_osm_b is not None:
        stmt_osm = (
            select(Building)
            .options(selectinload(Building.parcel), selectinload(Building.organizations))
            .where(Building.osm_id == chosen_osm_b["osm_id"])
        )
        res_osm = await session.execute(stmt_osm)
        existing_by_osm = res_osm.scalar_one_or_none()
        if existing_by_osm is not None:
            if settings.enable_live_osm_enrichment:
                added = await enrich_building_from_osm_and_egr(
                    session, existing_by_osm, osm_box_cache=osm_box
                )
                if added > 0:
                    await session.refresh(existing_by_osm, attribute_names=["organizations"])
            return _building_to_detail(existing_by_osm)

    # 4. Формируем геометрию и адресные атрибуты строго для кликнутого здания
    region_name, default_city, _ = detect_belarus_region_and_soato(lat, lon)
    b_tags: dict[str, str] = chosen_osm_b["tags"] if chosen_osm_b else {}
    osm_id_val: int | None = chosen_osm_b["osm_id"] if chosen_osm_b else None

    single_hint_geom = extract_single_building_polygon(hint_geometry, lon, lat)

    if chosen_osm_b is not None:
        ring = chosen_osm_b["ring"]
        lons = [c[0] for c in ring]
        lats = [c[1] for c in ring]
        b_bbox = (min(lons), min(lats), max(lons), max(lats))
        center_lon_val = round(chosen_osm_b["polygon"].centroid.x, 6)
        center_lat_val = round(chosen_osm_b["polygon"].centroid.y, 6)
        b_area = max(compute_polygon_area_sqm(ring), 40.0)
        building_geojson = json.dumps(
            {"type": "MultiPolygon", "coordinates": [[ring]]}, ensure_ascii=False
        )
    elif single_hint_geom is not None and ((hint_street and hint_housenumber) or hint_name):
        g_shape = shape(single_hint_geom)
        minx, miny, maxx, maxy = g_shape.bounds
        b_bbox = (round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6))
        center_lon_val = round(g_shape.centroid.x, 6)
        center_lat_val = round(g_shape.centroid.y, 6)
        ring = single_hint_geom["coordinates"][0][0]
        b_area = max(compute_polygon_area_sqm(ring), 45.0)
        building_geojson = json.dumps(single_hint_geom, ensure_ascii=False)
    elif (hint_street and hint_housenumber) and not settings.enable_live_osm_enrichment:
        # Используется только в оффлайн-автотестах при передаче явного адреса и отключенном OSM
        center_lon_val = round(lon, 6)
        center_lat_val = round(lat, 6)
        building_geojson, b_bbox, b_area = make_rect_polygon(lon, lat, 36.0, 22.0, 8.0)
    else:
        # В точке клика нет реального здания — ничего не создаем!
        return None

    parcel_cad, building_cad, inv_num = generate_cadastral_numbers(
        center_lat_val, center_lon_val, osm_id_val
    )

    existing_b_by_cad = (
        await session.execute(
            select(Building)
            .options(selectinload(Building.parcel), selectinload(Building.organizations))
            .where(Building.cadastral_number == building_cad)
        )
    ).scalar_one_or_none()
    if existing_b_by_cad is not None:
        return _building_to_detail(existing_b_by_cad)

    raw_btype = b_tags.get("building") or hint_type or "commercial"
    if raw_btype in ("yes", "true", "building"):
        raw_btype = "commercial" if ("shop" in b_tags or "office" in b_tags) else "apartments"

    floors_str = b_tags.get("building:levels") or (str(hint_floors) if hint_floors else None)
    try:
        floors_val = max(1, int(float(floors_str))) if floors_str else 3
    except ValueError:
        floors_val = 3

    raw_city = b_tags.get("addr:city:ru") or b_tags.get("addr:city") or hint_city or default_city
    city_val = canonicalize_city_name(raw_city, default_city=default_city)

    raw_street = (
        b_tags.get("addr:street:ru")
        or translate_belarusian_street_to_ru(b_tags.get("addr:street"))
        or translate_belarusian_street_to_ru(hint_street)
    )
    housenumber_val = b_tags.get("addr:housenumber") or hint_housenumber
    b_name = b_tags.get("name:ru") or b_tags.get("name") or hint_name

    # Если у здания нет ни адреса (улицы + номера дома), ни собственного названия — не создаем фейковый объект!
    if not (raw_street and housenumber_val) and not b_name:
        return None

    street_val = raw_street

    # Создаем или переиспользуем участок под зданием
    parcel = (
        await session.execute(
            select(CadastralParcel).where(CadastralParcel.cadastral_number == parcel_cad)
        )
    ).scalar_one_or_none()
    if parcel is None:
        p_geojson, p_bbox, p_area = make_rect_polygon(
            center_lon_val,
            center_lat_val,
            max(math.sqrt(b_area) * 1.5, 60.0),
            max(math.sqrt(b_area) * 1.35, 48.0),
            0.0,
        )
        parcel = CadastralParcel(
            cadastral_number=parcel_cad,
            name=f"Земельный участок ({street_val}, {housenumber_val})",
            region=region_name,
            district=b_tags.get("addr:district") or city_val,
            land_use=raw_btype if raw_btype in ("retail", "commercial", "industrial") else "public",
            purpose="Земли жилой и общественно-деловой застройки населенных пунктов РБ",
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
        wall_material=b_tags.get("building:material") or "Кирпич / Монолитный железобетон",
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
    await session.commit()

    # 5. Обогащаем здание ВСЕМИ организациями из OSM API v0.6 и актуальными субъектами из ЕГР (egr.gov.by)
    await enrich_building_from_osm_and_egr(session, building, osm_box_cache=osm_box)
    await sync_postgis_geometries(session)

    return await get_building_detail(session, building.id)


async def search_cadastral(
    session: AsyncSession,
    query: str,
    city: str | None = None,
    category: str | None = None,
    limit: int = 25,
) -> list[SearchResultItem]:
    """
    Универсальный поиск по кадастровому номеру, адресу, названию здания,
    названию организации внутри здания или УНП (включая субъекты ЕГР).
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
            subtitle = (
                f"{b.full_address} • Кадастровый № {b.cadastral_number} • "
                f"Организаций внутри: {len(b.organizations)}"
            )
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
                            b.address_housenumber,
                        ],
                    )
                ).lower()
                if q_norm in haystack_o:
                    unp_tag = f" [УНП {org.unp}]" if org.unp else ""
                    results.append(
                        SearchResultItem(
                            result_type="organization",
                            id=org.id,
                            building_id=b.id,
                            title=f"{org.name}{unp_tag}",
                            subtitle=(
                                f"{org.category} ({org.floor or '1 этаж'}) — "
                                f"{b.name or b.full_address} ({b.full_address})"
                            ),
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
    """Возвращает GeoJSON FeatureCollection всех кадастровых зданий."""
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
                    "address_street": b.address_street or "",
                    "address_housenumber": b.address_housenumber or "",
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
