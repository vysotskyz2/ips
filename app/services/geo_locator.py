from __future__ import annotations

import asyncio
import json
import logging
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
DISTRICTS_PATH = Path(__file__).resolve().parent.parent / "data" / "belarus_districts.geojson"

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

CITY_REGION_HINTS: dict[str, str] = {
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


DISTRICT_KIND_PRIORITY = {"city_district": 3, "city": 2, "district": 1}


@lru_cache(maxsize=1)
def _load_district_features() -> list[tuple[Any, str, str]]:
    if not DISTRICTS_PATH.exists():
        logger.warning("Файл границ районов РБ не найден: %s", DISTRICTS_PATH)
        return []
    with DISTRICTS_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    features: list[tuple[Any, str, str]] = []
    for feat in data.get("features", []):
        props = feat.get("properties") or {}
        name_ru = props.get("name_ru") or props.get("shapeName")
        if not name_ru:
            continue
        kind = str(props.get("kind") or "district")
        try:
            geom = shape(feat["geometry"])
        except Exception:
            continue
        features.append((geom, str(name_ru), kind))
    features.sort(key=lambda item: DISTRICT_KIND_PRIORITY.get(item[2], 0), reverse=True)
    return features


def detect_belarus_district(lat: float, lon: float) -> str | None:
    """Возвращает реальный район/городской район по актуальным границам OSM."""
    pt = Point(lon, lat)
    for geom, name_ru, _kind in _load_district_features():
        try:
            if geom.contains(pt):
                return name_ru
        except Exception:
            continue
    return None


def resolve_district(lat: float, lon: float, city: str | None = None) -> str | None:
    """
    Определяет район для объекта по реальным границам OSM:
    приоритет — район внутри города, затем город областного подчинения,
    затем район области. Если ничего не найдено — None (без выдуманных границ).
    """
    return detect_belarus_district(lat, lon)


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


def city_matches_region(city: str | None, region_name: str) -> bool:
    """Проверяет, что сохранённый город относится к реально определённой области."""
    expected = CITY_REGION_HINTS.get(normalize_city(city))
    return expected is None or expected == region_name


def is_valid_city_name(city: str | None) -> bool:
    """Проверяет, что значение является реальным названием населённого пункта, а не областью/районом."""
    if not city:
        return False
    raw = str(city).strip().lower().replace("ё", "е")
    if not raw or "область" in raw or "район" in raw:
        return False
    norm = normalize_city(city)
    return bool(norm) and norm not in NON_CITY_VALUES


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
) -> tuple[str | None, str, str]:
    """
    Возвращает (город, область, SOATO-префикс) для точки на карте РБ.
    Город берётся ТОЛЬКО из реальных данных: тегов OSM или Nominatim.
    Если город достоверно не определён — возвращается None, без подстановки
    ближайшего города или названия области вместо города.
    """
    region_name, _region_default_city, region_soato = detect_belarus_region_and_soato(lat, lon)

    city_source = raw_city
    if not is_valid_city_name(city_source):
        city_source = await reverse_geocode_city(lat, lon)
    if not is_valid_city_name(city_source):
        return None, region_name, region_soato

    city_value = canonicalize_city_name(city_source)
    if not is_valid_city_name(city_value):
        return None, region_name, region_soato

    soato_prefix = city_soato_prefix(city_value) or region_soato
    return city_value, region_name, soato_prefix
