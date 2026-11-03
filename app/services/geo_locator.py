from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from shapely.geometry import Point, shape

from app.config import settings
from app.services.address_matcher import canonicalize_city_name, normalize_city

logger = logging.getLogger(__name__)

REGIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "belarus_regions.geojson"

NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

REGION_DEFS: dict[str, tuple[str, str, str]] = {
    "Brest": ("Брестская область", "Брест", "1400000000"),
    "Vitebsk": ("Витебская область", "Витебск", "2400000000"),
    "Gomel": ("Гомельская область", "Гомель", "3400000000"),
    "Grodno": ("Гродненская область", "Гродно", "4400000000"),
    "Minsk": ("Минская область", "Минская область", "6400000000"),
    "Mogilev": ("Могилёвская область", "Могилёв", "7400000000"),
    "Minsk City": ("г. Минск", "Минск", "5000000000"),
}

CITY_SOATO_PREFIXES: dict[str, str] = {
    "минск": "5000000000",
    "брест": "1401000000",
    "витебск": "2401000000",
    "гомель": "3401000000",
    "гродно": "4401000000",
    "могилев": "7401000000",
}

KNOWN_CITY_POINTS: dict[str, tuple[float, float]] = {
    "Минск": (53.9023, 27.5619),
    "Брест": (52.0975, 23.7341),
    "Гродно": (53.6667, 23.8333),
    "Гомель": (52.4345, 30.9754),
    "Витебск": (55.1904, 30.2049),
    "Могилёв": (53.9006, 30.3319),
    "Бобруйск": (53.1384, 29.2214),
    "Барановичи": (53.1327, 26.0139),
    "Борисов": (54.2279, 28.5057),
    "Пинск": (52.0975, 26.0933),
    "Орша": (54.5081, 30.4172),
    "Мозырь": (52.0495, 29.2456),
    "Солигорск": (52.7879, 27.5411),
    "Новополоцк": (55.5300, 28.6600),
    "Лида": (53.8833, 25.2997),
    "Молодечно": (54.3130, 26.8540),
    "Полоцк": (55.4857, 28.7856),
    "Жлобин": (52.8926, 30.0240),
    "Светлогорск": (52.6328, 29.7389),
    "Речица": (52.3617, 30.3917),
    "Жодино": (54.0985, 28.3331),
    "Слуцк": (53.0274, 27.5597),
    "Кобрин": (52.2138, 24.3564),
    "Волковыск": (53.1561, 24.4513),
    "Слоним": (53.0934, 25.3211),
    "Сморгонь": (54.4798, 26.3957),
    "Осиповичи": (53.3006, 28.6401),
    "Береза": (52.5336, 24.9780),
    "Дзержинск": (53.6833, 27.1333),
    "Заславль": (54.0064, 27.2672),
}

CITY_REGIONS: dict[str, str] = {
    "минск": "г. Минск",
    "брест": "Брестская область",
    "барановичи": "Брестская область",
    "пинск": "Брестская область",
    "кобрин": "Брестская область",
    "береза": "Брестская область",
    "гродно": "Гродненская область",
    "лида": "Гродненская область",
    "слоним": "Гродненская область",
    "волковыск": "Гродненская область",
    "сморгонь": "Гродненская область",
    "витебск": "Витебская область",
    "орша": "Витебская область",
    "новополоцк": "Витебская область",
    "полоцк": "Витебская область",
    "гомель": "Гомельская область",
    "мозырь": "Гомельская область",
    "жлобин": "Гомельская область",
    "светлогорск": "Гомельская область",
    "речица": "Гомельская область",
    "могилев": "Могилёвская область",
    "бобруйск": "Могилёвская область",
    "осиповичи": "Могилёвская область",
    "борисов": "Минская область",
    "солигорск": "Минская область",
    "молодечно": "Минская область",
    "жодино": "Минская область",
    "слуцк": "Минская область",
    "дзержинск": "Минская область",
    "заславль": "Минская область",
}

NON_CITY_VALUES = {
    "",
    "беларусь",
    "республика беларусь",
    "рб",
    "минская область",
    "брестская область",
    "витебская область",
    "гомельская область",
    "гродненская область",
    "могилевская область",
}

_REVERSE_CACHE: dict[tuple[float, float], tuple[float, tuple[str | None, str | None]]] = {}
_REVERSE_LOCK = asyncio.Lock()
_LAST_REVERSE_TS = 0.0


@lru_cache(maxsize=1)
def _load_region_features() -> list[tuple[Any, str, str, str]]:
    if not REGIONS_PATH.exists():
        logger.warning("Файл границ областей РБ не найден: %s", REGIONS_PATH)
        return []
    with REGIONS_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    features: list[tuple[Any, str, str, str]] = []
    for feat in data.get("features", []):
        props = feat.get("properties") or {}
        shape_name = props.get("shapeName") or ""
        region_def = REGION_DEFS.get(shape_name)
        if region_def is None:
            continue
        try:
            geom = shape(feat["geometry"])
        except Exception:
            continue
        features.append((geom, *region_def))
    return features


def detect_belarus_region_and_soato(lat: float, lon: float) -> tuple[str, str, str]:
    """Определяет область РБ и SOATO-префикс по реальным границам областей."""
    pt = Point(lon, lat)
    for geom, region_name, default_city, soato_prefix in _load_region_features():
        try:
            if geom.contains(pt):
                return region_name, default_city, soato_prefix
        except Exception:
            continue

    if 53.82 <= lat <= 53.98 and 27.40 <= lon <= 27.72:
        return "г. Минск", "Минск", "5000000000"
    if lat < 53.4 and lon < 26.8:
        return "Брестская область", "Брест", "1400000000"
    if lat >= 52.85 and lon < 26.3:
        return "Гродненская область", "Гродно", "4400000000"
    if lat < 53.35 and lon >= 28.5:
        return "Гомельская область", "Гомель", "3400000000"
    if lat >= 54.55:
        return "Витебская область", "Витебск", "2400000000"
    if lon >= 29.2 and 53.35 <= lat < 54.55:
        return "Могилёвская область", "Могилёв", "7400000000"
    return "Минская область", "Минская область", "6400000000"


def city_soato_prefix(city: str | None) -> str | None:
    norm = normalize_city(city)
    return CITY_SOATO_PREFIXES.get(norm)


def _is_valid_city(city: str | None) -> bool:
    norm = normalize_city(city)
    return bool(norm) and norm not in NON_CITY_VALUES


def nearest_known_city(lat: float, lon: float, max_distance_km: float = 35.0) -> str | None:
    """Ближайший крупный населённый пункт РБ для офлайн-фолбэка без Nominatim."""
    best_city: str | None = None
    best_distance = float("inf")
    lat_rad = math.radians(lat)
    for city, (city_lat, city_lon) in KNOWN_CITY_POINTS.items():
        dlat = math.radians(city_lat - lat)
        dlon = math.radians(city_lon - lon)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat_rad) * math.cos(math.radians(city_lat)) * math.sin(dlon / 2) ** 2
        )
        distance_km = 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))
        if distance_km < best_distance:
            best_distance = distance_km
            best_city = city
    if best_city is not None and best_distance <= max_distance_km:
        return best_city
    return None


def _is_non_city_name(city: str | None) -> bool:
    norm = normalize_city(city)
    if not norm:
        return True
    if norm in NON_CITY_VALUES:
        return True
    return norm.endswith((" область", " район"))


def _extract_city_from_address(address: dict[str, Any]) -> str | None:
    for key in ("city", "town", "village", "hamlet"):
        value = address.get(key)
        if value and not str(value).strip().lower().endswith("район"):
            return str(value).strip()
    municipality = address.get("municipality")
    if municipality and not str(municipality).strip().lower().endswith("район"):
        return str(municipality).strip()
    return None


async def reverse_geocode_city(lat: float, lon: float) -> str | None:
    """Возвращает название населённого пункта через Nominatim (с кэшем и лимитом 1 запрос/сек)."""
    if not settings.enable_live_reverse_geocoding:
        return None

    key = (round(lat, 3), round(lon, 3))
    now = time.monotonic()
    cached = _REVERSE_CACHE.get(key)
    if cached is not None:
        ts, value = cached
        ttl = 86400.0 if value[0] else 300.0
        if now - ts < ttl:
            return value[0]

    global _LAST_REVERSE_TS
    async with _REVERSE_LOCK:
        cached = _REVERSE_CACHE.get(key)
        if cached is not None:
            ts, value = cached
            ttl = 86400.0 if value[0] else 300.0
            if time.monotonic() - ts < ttl:
                return value[0]

        wait = 1.0 - (time.monotonic() - _LAST_REVERSE_TS)
        if wait > 0:
            await asyncio.sleep(wait)

        city_name: str | None = None
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(
                    NOMINATIM_REVERSE_URL,
                    params={
                        "format": "jsonv2",
                        "lat": f"{lat:.6f}",
                        "lon": f"{lon:.6f}",
                        "accept-language": "ru",
                        "addressdetails": 1,
                    },
                    headers={"User-Agent": "BelarusCadastralMap/1.0 (OpenGIS local app)"},
                )
            _LAST_REVERSE_TS = time.monotonic()
            if resp.status_code == 200:
                payload = resp.json()
                address = payload.get("address") or {}
                city_name = _extract_city_from_address(address)
        except Exception as exc:
            _LAST_REVERSE_TS = time.monotonic()
            logger.debug("Nominatim недоступен для %.6f,%.6f: %s", lat, lon, exc)

        _REVERSE_CACHE[key] = (time.monotonic(), (city_name, None))
        return city_name


async def resolve_city_and_region(
    lat: float,
    lon: float,
    raw_city: str | None = None,
) -> tuple[str, str, str]:
    """Возвращает (город, область, SOATO-префикс) для точки на карте РБ."""
    region_name, region_default_city, region_soato = detect_belarus_region_and_soato(lat, lon)

    city_source = raw_city
    if not _is_valid_city(city_source):
        city_source = await reverse_geocode_city(lat, lon)
    if not _is_valid_city(city_source):
        nearest_city = nearest_known_city(lat, lon)
        if nearest_city and CITY_REGIONS.get(normalize_city(nearest_city)) == region_name:
            city_source = nearest_city
        else:
            return region_name, region_name, region_soato

    city_value = canonicalize_city_name(city_source, default_city=region_default_city)
    if _is_non_city_name(city_value):
        city_value = region_name

    city_region = CITY_REGIONS.get(normalize_city(city_value))
    if city_region is not None and city_region != region_name:
        soato_prefix = region_soato
    else:
        soato_prefix = city_soato_prefix(city_value) or region_soato
    return city_value, region_name, soato_prefix
