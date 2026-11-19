from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from shapely.geometry import mapping, shape
from shapely.validation import make_valid
from shapely.ops import unary_union

BASE_DIR = Path(__file__).resolve().parent.parent
DISTRICTS_PATH = BASE_DIR / "app" / "data" / "belarus_districts.geojson"
CACHE_DIR = Path(tempfile.gettempdir()) / "belarus_admin_boundaries_cache"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "BelarusCadastralMap/1.0 (admin boundaries builder)"

RAYON_PALETTE = [
    "#60a5fa",
    "#f59e0b",
    "#34d399",
    "#f472b6",
    "#a78bfa",
    "#22d3ee",
    "#fbbf24",
    "#4ade80",
    "#fb7185",
    "#818cf8",
    "#2dd4bf",
    "#f97316",
]
CITY_DISTRICT_PALETTE = [
    "#f59e0b",
    "#fb7185",
    "#f472b6",
    "#f97316",
    "#eab308",
    "#ef4444",
    "#ec4899",
    "#f43f5e",
    "#facc15",
    "#fdba74",
]

CITY_DISTRICTS: dict[str, list[str]] = {
    "Минск": [
        "Заводской",
        "Ленинский",
        "Московский",
        "Октябрьский",
        "Партизанский",
        "Первомайский",
        "Советский",
        "Фрунзенский",
        "Центральный",
    ],
    "Брест": ["Ленинский", "Московский"],
    "Витебск": ["Железнодорожный", "Октябрьский", "Первомайский"],
    "Гомель": ["Железнодорожный", "Новобелицкий", "Советский", "Центральный"],
    "Гродно": ["Ленинский", "Октябрьский"],
    "Могилёв": ["Ленинский", "Октябрьский"],
    "Бобруйск": ["Ленинский", "Первомайский"],
}

EXTRA_RAYONS = ["Дрибинский район"]

OBLAST_SUBORDINATION_CITIES = [
    "Минск",
    "Брест",
    "Витебск",
    "Гомель",
    "Гродно",
    "Могилёв",
    "Барановичи",
    "Бобруйск",
    "Пинск",
    "Новополоцк",
]

_last_request_ts = 0.0


def _cache_path(query: str, polygon_threshold: float) -> Path:
    key = f"{query}|{polygon_threshold}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _nominatim(client: httpx.Client, query: str, polygon_threshold: float) -> list[dict[str, Any]]:
    global _last_request_ts

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_path(query, polygon_threshold)
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    wait = 1.05 - (time.monotonic() - _last_request_ts)
    if wait > 0:
        time.sleep(wait)

    response = client.get(
        NOMINATIM_URL,
        params={
            "q": query,
            "format": "jsonv2",
            "polygon_geojson": 1,
            "polygon_threshold": polygon_threshold,
            "limit": 5,
            "accept-language": "ru",
            "addressdetails": 1,
            "extratags": 1,
        },
        headers={"User-Agent": USER_AGENT},
    )
    _last_request_ts = time.monotonic()
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    return []


def _pick_admin(
    results: list[dict[str, Any]],
    allowed_levels: set[str],
    require_rayon_word: bool | None = None,
) -> dict[str, Any] | None:
    for item in results:
        if item.get("osm_type") != "relation":
            continue
        if item.get("type") != "administrative":
            continue
        level = str((item.get("extratags") or {}).get("admin_level") or "")
        if level not in allowed_levels:
            continue
        geojson = item.get("geojson") or {}
        if geojson.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        display = str(item.get("display_name") or "").lower()
        if require_rayon_word is True and "район" not in display:
            continue
        if require_rayon_word is False and "район" in display:
            continue
        return item
    return None


def _feature_from_item(item: dict[str, Any], kind: str, city: str | None = None) -> dict[str, Any]:
    geojson = item["geojson"]
    geometry = make_valid(shape(geojson))
    simplified = geometry

    first_name = str(item.get("display_name") or "").split(",", 1)[0].strip()
    if kind == "city":
        name_ru = f"г. {first_name}"
    else:
        name_ru = first_name

    address = item.get("address") or {}
    display_parts = [part.strip() for part in str(item.get("display_name") or "").split(",")]
    region_ru = address.get("state") or (display_parts[2] if len(display_parts) > 2 else None)
    city_ru = city
    if kind == "district":
        city_ru = None
    elif kind == "city_district":
        city_ru = city or address.get("city") or address.get("town")

    return {
        "type": "Feature",
        "properties": {
            "name_ru": name_ru,
            "kind": kind,
            "admin_level": str((item.get("extratags") or {}).get("admin_level") or ""),
            "osm_id": int(item.get("osm_id") or 0),
            "region_ru": region_ru,
            "city_ru": city_ru,
        },
        "geometry": mapping(simplified),
    }


def _deduplicate_overlaps(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Убирает пересечения между полигонами, оставляя каждый участок ровно в одном районе."""
    ordered = sorted(features, key=lambda item: str((item.get("properties") or {}).get("name_ru") or ""))
    cleaned: list[dict[str, Any]] = []
    processed = None
    for feature in ordered:
        geometry = make_valid(shape(feature["geometry"]))
        if processed is not None:
            geometry = make_valid(geometry.difference(processed))
        if geometry.is_empty:
            continue
        feature["geometry"] = mapping(geometry)
        cleaned.append(feature)
        processed = geometry if processed is None else unary_union([processed, geometry])
    return cleaned


def _assign_colors(features: list[dict[str, Any]], palette: list[str]) -> None:
    if not features:
        return
    geometries = [shape(feature["geometry"]) for feature in features]
    adjacency: list[set[int]] = [set() for _ in features]
    for i in range(len(features)):
        for j in range(i + 1, len(features)):
            try:
                if geometries[i].buffer(1e-7).intersects(geometries[j].buffer(1e-7)):
                    adjacency[i].add(j)
                    adjacency[j].add(i)
            except Exception:
                continue

    order = sorted(range(len(features)), key=lambda idx: len(adjacency[idx]), reverse=True)
    colors: dict[int, str] = {}
    for idx in order:
        used = {colors[neighbor] for neighbor in adjacency[idx] if neighbor in colors}
        color = next((candidate for candidate in palette if candidate not in used), palette[0])
        colors[idx] = color
        features[idx]["properties"]["fill_color"] = color


def _load_rayon_names(seed_path: Path) -> list[str]:
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    names: list[str] = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        kind = props.get("kind")
        if kind and kind != "district":
            continue
        name = props.get("name_ru") or props.get("shapeName")
        if not name:
            continue
        name = str(name)
        if "район" not in name.lower():
            continue
        if name not in names:
            names.append(name)
    return names


def _find_rayon(client: httpx.Client, name: str) -> dict[str, Any] | None:
    queries = [
        f"{name}, Беларусь",
        f"{name}, район, Беларусь" if "район" not in name.lower() else f"{name}, Беларусь",
    ]
    for query in queries:
        try:
            results = _nominatim(client, query, 0.00001)
        except Exception as exc:
            print(f"  ! Ошибка запроса «{query}»: {exc}")
            continue
        picked = _pick_admin(results, {"6"}, require_rayon_word=True)
        if picked:
            return picked
    return None


def _find_city_district(
    client: httpx.Client, district: str, city: str
) -> dict[str, Any] | None:
    queries = [
        f"{district} район, {city}, Беларусь",
        f"{district}, {city}, Беларусь",
    ]
    for query in queries:
        try:
            results = _nominatim(client, query, 0.0)
        except Exception as exc:
            print(f"  ! Ошибка запроса «{query}»: {exc}")
            continue
        picked = _pick_admin(results, {"9"}, require_rayon_word=None)
        if picked:
            return picked
    return None


def _find_city(client: httpx.Client, city: str) -> dict[str, Any] | None:
    queries = [f"{city}, Беларусь", f"г. {city}, Беларусь"]
    for query in queries:
        try:
            results = _nominatim(client, query, 0.0)
        except Exception as exc:
            print(f"  ! Ошибка запроса «{query}»: {exc}")
            continue
        picked = _pick_admin(results, {"4", "6"}, require_rayon_word=False)
        if picked:
            return picked
    return None


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Сборка актуальных границ районов РБ из OSM/Nominatim")
    parser.add_argument("--seed-file", type=Path, default=DISTRICTS_PATH)
    parser.add_argument("--output", type=Path, default=DISTRICTS_PATH)
    args = parser.parse_args()

    rayon_names = _load_rayon_names(args.seed_file)
    for extra_name in EXTRA_RAYONS:
        if extra_name not in rayon_names:
            rayon_names.append(extra_name)
    if len(rayon_names) < 100:
        print(f"Не удалось прочитать список районов из {args.seed_file}: {len(rayon_names)}")
        raise SystemExit(1)

    district_features: list[dict[str, Any]] = []
    city_features: list[dict[str, Any]] = []
    city_district_features: list[dict[str, Any]] = []
    missing: list[str] = []

    with httpx.Client(timeout=40.0, follow_redirects=True) as client:
        print(f"Загрузка {len(rayon_names)} районов областей...")
        for index, name in enumerate(rayon_names, 1):
            item = _find_rayon(client, name)
            if not item:
                missing.append(f"район: {name}")
                print(f"  [{index}/{len(rayon_names)}] НЕ НАЙДЕН: {name}")
                continue
            district_features.append(_feature_from_item(item, "district"))
            print(f"  [{index}/{len(rayon_names)}] {name}: osm {item.get('osm_id')}")

        print(f"Загрузка {len(OBLAST_SUBORDINATION_CITIES)} городов областного подчинения...")
        for index, city in enumerate(OBLAST_SUBORDINATION_CITIES, 1):
            item = _find_city(client, city)
            if not item:
                missing.append(f"город: {city}")
                print(f"  [{index}/{len(OBLAST_SUBORDINATION_CITIES)}] НЕ НАЙДЕН: {city}")
                continue
            city_features.append(_feature_from_item(item, "city", city=city))
            print(f"  [{index}/{len(OBLAST_SUBORDINATION_CITIES)}] {city}: osm {item.get('osm_id')}")

        total_city_districts = sum(len(items) for items in CITY_DISTRICTS.values())
        print(f"Загрузка {total_city_districts} районов внутри городов...")
        counter = 0
        for city, districts in CITY_DISTRICTS.items():
            for district in districts:
                counter += 1
                item = _find_city_district(client, district, city)
                if not item:
                    missing.append(f"городской район: {district}, {city}")
                    print(f"  [{counter}/{total_city_districts}] НЕ НАЙДЕН: {district}, {city}")
                    continue
                city_district_features.append(
                    _feature_from_item(item, "city_district", city=city)
                )
                print(
                    f"  [{counter}/{total_city_districts}] {district}, {city}: "
                    f"osm {item.get('osm_id')}"
                )

    # Топологическая очистка: убираем пересечения городов с районами и обрезаем
    # городские районы строго по границе города. Это не выдумывает границы,
    # а устраняет наложения из-за независимых полигонов OSM.
    city_geoms: dict[str, Any] = {}
    for feature in city_features:
        props = feature["properties"]
        city_name = props.get("city_ru")
        if city_name:
            city_geoms[city_name] = make_valid(shape(feature["geometry"]))

    if city_geoms:
        city_union = unary_union(list(city_geoms.values()))

        cleaned_rayons: list[dict[str, Any]] = []
        for feature in district_features:
            geometry = make_valid(shape(feature["geometry"]).difference(city_union))
            if geometry.is_empty:
                continue
            feature["geometry"] = mapping(geometry)
            cleaned_rayons.append(feature)
        district_features = cleaned_rayons

        for feature in city_district_features:
            city_name = feature["properties"].get("city_ru")
            city_geom = city_geoms.get(city_name) if city_name else None
            if city_geom is None:
                continue
            geometry = make_valid(shape(feature["geometry"]).intersection(city_geom))
            if geometry.is_empty:
                continue
            feature["geometry"] = mapping(geometry)

    district_features = _deduplicate_overlaps(district_features)
    city_district_features = _deduplicate_overlaps(city_district_features)

    # Fallback-области городов: только те части города, которые не покрыты
    # внутренними районами. Нужны, чтобы не рисовать серый слой под районами.
    city_district_geoms: dict[str, list[Any]] = {}
    for feature in city_district_features:
        city_name = feature["properties"].get("city_ru")
        if city_name:
            city_district_geoms.setdefault(city_name, []).append(shape(feature["geometry"]))

    fallback_features: list[dict[str, Any]] = []
    for city_feature in city_features:
        props = dict(city_feature["properties"])
        city_name = props.get("city_ru")
        city_geom = make_valid(shape(city_feature["geometry"]))
        district_geoms = city_district_geoms.get(city_name) if city_name else None
        fallback_geom = city_geom
        if district_geoms:
            fallback_geom = make_valid(city_geom.difference(unary_union(district_geoms)))
        if fallback_geom.is_empty:
            continue
        fallback_props = dict(props)
        fallback_props["kind"] = "city_fallback"
        fallback_features.append(
            {
                "type": "Feature",
                "properties": fallback_props,
                "geometry": mapping(fallback_geom),
            }
        )

    _assign_colors(district_features, RAYON_PALETTE)
    _assign_colors(city_district_features, CITY_DISTRICT_PALETTE)

    features = district_features + city_district_features + city_features + fallback_features
    output = {
        "type": "FeatureCollection",
        "source": "OpenStreetMap via Nominatim",
        "license": "ODbL 1.0",
        "features": features,
    }
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print(
        "\nГотово:\n"
        f"  - районов областей: {len(district_features)} / {len(rayon_names)}\n"
        f"  - районов внутри городов: {len(city_district_features)} / {total_city_districts}\n"
        f"  - городов областного подчинения: {len(city_features)} / {len(OBLAST_SUBORDINATION_CITIES)}"
    )
    if missing:
        print("Не найдены (не добавлялись выдуманные границы):")
        for item in missing:
            print(f"  - {item}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
