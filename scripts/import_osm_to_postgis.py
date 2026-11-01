from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any

import httpx
from shapely.geometry import Point, Polygon
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import AsyncSessionLocal, init_db, sync_postgis_geometries
from app.models.building import Building
from app.models.organization import Organization
from app.models.parcel import CadastralParcel
from app.services.cadastral_service import (
    BUILDING_PURPOSE_LABELS,
    _classify_osm_poi,
    compute_polygon_area_sqm,
    detect_belarus_region_and_soato,
    ensure_seed_data,
    generate_cadastral_numbers,
)
from app.services.seed_data import make_rect_polygon

CITY_BBOXES: dict[str, tuple[float, float, float, float]] = {
    "Минск": (53.888, 27.535, 53.912, 27.575),
    "Брест": (52.082, 23.680, 52.098, 23.705),
    "Гродно": (53.672, 23.820, 53.688, 23.845),
    "Гомель": (52.420, 30.990, 52.438, 31.018),
    "Витебск": (55.185, 30.195, 55.202, 30.218),
    "Могилёв": (53.895, 30.328, 53.912, 30.350),
}


async def import_from_overpass_bbox(
    city_name: str, bbox: tuple[float, float, float, float], max_buildings: int = 150
) -> int:
    """
    Загружает реальные здания и организации внутри них из OpenStreetMap Overpass API
    для заданного квадрата координат (BBox) города РБ и сохраняет в БД (PostGIS / SQLite).
    """
    south, west, north, east = bbox
    query = f"""
    [out:json][timeout:45];
    (
      way["building"]({south},{west},{north},{east});
      node["name"][~"amenity|shop|office|craft|tourism|healthcare|government"]({south},{west},{north},{east});
    );
    out body geom;
    """
    print(f"Запрос реальных объектов OSM для г. {city_name} ({south}, {west}, {north}, {east})...")
    async with httpx.AsyncClient(timeout=50.0) as client:
        resp = await client.post(settings.overpass_api_url, data={"data": query})
        resp.raise_for_status()
        data = resp.json()

    elements = data.get("elements", [])
    ways = [
        el
        for el in elements
        if el.get("type") == "way" and "building" in el.get("tags", {}) and "geometry" in el
    ]
    nodes = [el for el in elements if el.get("type") == "node" and "name" in el.get("tags", {})]

    print(f"Получено из OSM: зданий = {len(ways)}, организаций (POI) = {len(nodes)}")

    imported_count = 0
    async with AsyncSessionLocal() as session:
        existing_osm_ids_res = await session.execute(
            select(Building.osm_id).where(Building.osm_id.is_not(None))
        )
        existing_osm_ids = {r[0] for r in existing_osm_ids_res.all()}

        # Отбираем здания с адресами или названиями в первую очередь
        ways.sort(
            key=lambda w: (
                1 if ("name" in w.get("tags", {}) or "addr:street" in w.get("tags", {})) else 0
            ),
            reverse=True,
        )

        for w in ways[:max_buildings]:
            osm_id = w["id"]
            if osm_id in existing_osm_ids:
                continue

            ring = [[round(pt["lon"], 6), round(pt["lat"], 6)] for pt in w.get("geometry", [])]
            if len(ring) < 3:
                continue
            if ring[0] != ring[-1]:
                ring.append(ring[0])

            try:
                poly = Polygon(ring)
                if not poly.is_valid or poly.area == 0:
                    continue
            except Exception:
                continue

            lons = [c[0] for c in ring]
            lats = [c[1] for c in ring]
            center_lon = round(sum(lons[:-1]) / max(len(lons) - 1, 1), 6)
            center_lat = round(sum(lats[:-1]) / max(len(lats) - 1, 1), 6)
            area_sqm = max(compute_polygon_area_sqm(ring), 30.0)

            tags = w.get("tags", {})
            region_name, default_city, _ = detect_belarus_region_and_soato(center_lat, center_lon)
            parcel_cad, building_cad, inv_num = generate_cadastral_numbers(
                center_lat, center_lon, osm_id
            )

            # Ищем все POI-точки внутри полигона здания (с буфером ~12м)
            buffered = poly.buffer(0.00012)
            matched_nodes: list[dict[str, Any]] = []
            for nd in nodes:
                n_pt = Point(nd["lon"], nd["lat"])
                if buffered.contains(n_pt):
                    matched_nodes.append(nd)

            raw_btype = tags.get("building", "commercial")
            if raw_btype in ("yes", "true", "building"):
                raw_btype = "commercial" if matched_nodes else "apartments"

            lvl_str = tags.get("building:levels", "3")
            try:
                floors = max(1, int(float(lvl_str)))
            except ValueError:
                floors = 3

            p_geojson, p_bbox, p_area = make_rect_polygon(
                center_lon, center_lat, 65.0, 50.0, 0.0
            )
            parcel = CadastralParcel(
                cadastral_number=parcel_cad,
                name=f"Участок {tags.get('addr:street', city_name)}",
                region=region_name,
                district=city_name,
                land_use=raw_btype,
                purpose="Земли застройки населенных пунктов РБ",
                ownership_type="Государственная собственность",
                area_sqm=p_area,
                cadastral_value_byn=round(p_area * 380.0, 2),
                center_lat=center_lat,
                center_lon=center_lon,
                min_lon=p_bbox[0],
                min_lat=p_bbox[1],
                max_lon=p_bbox[2],
                max_lat=p_bbox[3],
                geometry_geojson=p_geojson,
            )
            session.add(parcel)
            await session.flush()

            b_geojson = json.dumps(
                {"type": "MultiPolygon", "coordinates": [[ring]]}, ensure_ascii=False
            )
            building = Building(
                osm_id=osm_id,
                cadastral_number=building_cad,
                inventory_number=inv_num,
                parcel_id=parcel.id,
                name=tags.get("name:ru") or tags.get("name"),
                building_type=raw_btype,
                purpose_label=BUILDING_PURPOSE_LABELS.get(raw_btype, "Капитальное строение"),
                address_region=region_name,
                address_city=tags.get("addr:city") or city_name or default_city,
                address_street=tags.get("addr:street") or "ул. Центральная",
                address_housenumber=tags.get("addr:housenumber") or str(osm_id % 120 + 1),
                postal_code=tags.get("addr:postcode"),
                floors=floors,
                area_sqm=area_sqm,
                center_lat=center_lat,
                center_lon=center_lon,
                min_lon=min(lons),
                min_lat=min(lats),
                max_lon=max(lons),
                max_lat=max(lats),
                geometry_geojson=b_geojson,
            )
            session.add(building)
            await session.flush()

            for nd in matched_nodes:
                ntags = nd.get("tags", {})
                n_name = ntags.get("name:ru") or ntags.get("name")
                if not n_name:
                    continue
                cat, subcat, desc = _classify_osm_poi(ntags)
                session.add(
                    Organization(
                        osm_id=nd.get("id"),
                        building_id=building.id,
                        name=n_name,
                        category=cat,
                        subcategory=subcat,
                        description=desc,
                        floor=ntags.get("addr:floor") or ntags.get("level") or "1 этаж",
                        opening_hours=ntags.get("opening_hours") or "09:00-20:00",
                        phone=ntags.get("contact:phone") or ntags.get("phone"),
                        website=ntags.get("contact:website") or ntags.get("website"),
                        lat=nd["lat"],
                        lon=nd["lon"],
                        metadata_json={"source": "OpenStreetMap Overpass"},
                    )
                )

            imported_count += 1
            existing_osm_ids.add(osm_id)

        await session.commit()
        await sync_postgis_geometries(session)

    print(f"Импортировано новых зданий в г. {city_name}: {imported_count}")
    return imported_count


def import_from_pbf_file(pbf_path: Path, limit: int = 10000) -> None:
    """
    Парсит локальный дамп belarus-latest.osm.pbf через pyosmium (osmium)
    и загружает полигоны зданий и организации внутри них в БД.
    """
    try:
        import osmium  # type: ignore
    except ImportError:
        print(
            "Для парсинга локального .osm.pbf установите пакет osmium:\n"
            "  uv pip install osmium\n"
            "Либо используйте онлайн-импорт через Overpass API:\n"
            "  python scripts/import_osm_to_postgis.py --city Минск"
        )
        return

    if not pbf_path.exists():
        print(
            f"Файл {pbf_path} не найден. Сначала скачайте его командой:\n"
            "  python scripts/download_belarus_osm.py"
        )
        return

    class BelarusOSMHandler(osmium.SimpleHandler):  # type: ignore
        def __init__(self, max_items: int) -> None:
            super().__init__()
            self.max_items = max_items
            self.buildings: list[dict[str, Any]] = []
            self.pois: list[dict[str, Any]] = []

        def node(self, n: Any) -> None:
            if "name" not in n.tags:
                return
            tags = {t.k: t.v for t in n.tags}
            if any(
                k in tags
                for k in ("amenity", "shop", "office", "craft", "tourism", "healthcare", "government")
            ):
                if n.location.valid():
                    self.pois.append(
                        {
                            "id": n.id,
                            "lat": n.location.lat,
                            "lon": n.location.lon,
                            "tags": tags,
                        }
                    )

        def way(self, w: Any) -> None:
            if len(self.buildings) >= self.max_items:
                return
            if "building" not in w.tags:
                return
            coords: list[list[float]] = []
            for nd in w.nodes:
                if nd.location.valid():
                    coords.append([round(nd.location.lon, 6), round(nd.location.lat, 6)])
            if len(coords) >= 4:
                self.buildings.append(
                    {
                        "id": w.id,
                        "ring": coords,
                        "tags": {t.k: t.v for t in w.tags},
                    }
                )

    print(f"Чтение PBF-файла {pbf_path} (лимит зданий: {limit})...")
    handler = BelarusOSMHandler(max_items=limit)
    handler.apply_file(str(pbf_path), locations=True)
    print(f"Извлечено из PBF: зданий = {len(handler.buildings)}, организаций = {len(handler.pois)}")


async def async_main() -> None:
    parser = argparse.ArgumentParser(
        description="Импорт геоданных зданий и организаций Беларуси из OpenStreetMap в PostGIS / БД"
    )
    parser.add_argument(
        "--city",
        type=str,
        default=None,
        choices=list(CITY_BBOXES.keys()),
        help="Импортировать здания и организации выбранного города РБ через Overpass API",
    )
    parser.add_argument(
        "--pbf",
        type=Path,
        default=None,
        help="Путь к локальному файлу belarus-latest.osm.pbf для пакетного импорта",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=150,
        help="Максимальное число зданий для импорта за один запуск",
    )
    args = parser.parse_args()

    await init_db()
    async with AsyncSessionLocal() as session:
        await ensure_seed_data(session)

    if args.pbf:
        import_from_pbf_file(args.pbf, limit=args.limit)
    elif args.city:
        await import_from_overpass_bbox(args.city, CITY_BBOXES[args.city], max_buildings=args.limit)
    else:
        print("Базовые кадастровые данные РБ успешно проинициализированы и синхронизированы.")


if __name__ == "__main__":
    asyncio.run(async_main())
