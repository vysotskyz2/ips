from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import AsyncSessionLocal, init_db, sync_postgis_geometries
from app.models.building import Building
from app.models.egr_record import EgrRecord
from app.models.organization import Organization
from app.services.address_matcher import (
    canonicalize_city_name,
    normalize_city,
    normalize_housenumber,
    normalize_street,
    translate_belarusian_street_to_ru,
)
from app.services.cadastral_service import (
    _classify_osm_poi,
    _extract_poi_name,
    _is_poi_element,
    compute_polygon_area_sqm,
    detect_belarus_region_and_soato,
    enrich_building_from_osm_and_egr,
    fetch_osm_map_box,
    generate_cadastral_numbers,
    get_stats,
    match_pois_strictly_to_building,
)
from app.services.egr_service import (
    attach_egr_records_to_building,
    sync_egr_commercial_period,
    sync_egr_state_organizations,
)

# Ключевые деловые, торговые и административные зоны городов РБ для пакетной загрузки из OSM API v0.6
BELARUS_HOTSPOT_BBOXES: list[tuple[str, tuple[float, float, float, float]]] = [
    ("Минск (Немига — Победителей — Замковая)", (27.5455, 53.9055, 27.5545, 53.9105)),
    ("Минск (Независимости — ГУМ — Володарского — К.Маркса)", (27.5500, 53.8955, 27.5610, 53.9020)),
    ("Минск (Пл. Независимости — БГУ — Главпочтамт)", (27.5440, 53.8925, 27.5520, 53.8975)),
    ("Минск (Восток — Dana Mall — Нацбиблиотека)", (27.6440, 53.9300, 27.6550, 53.9355)),
    ("Минск (Пл. Якуба Коласа — ЦУМ — Гикало)", (27.5790, 53.9140, 27.5890, 53.9195)),
    ("Брест (Советская — Машерова — ЦУМ)", (23.6870, 52.0840, 23.6970, 52.0920)),
    ("Гродно (ТРК Triniti — Янки Купалы)", (23.8500, 53.6480, 23.8595, 53.6530)),
    ("Гродно (Советская — Ожешко — Центр)", (23.8270, 53.6760, 23.8440, 53.6860)),
    ("Гомель (Гагарина — Ленина — Советская)", (30.9940, 52.4235, 31.0140, 52.4320)),
    ("Витебск (Ленина — Пл. Свободы — Марко-Сити)", (30.2010, 55.1880, 30.2095, 55.1950)),
    ("Могилёв (Первомайская — Атриум — Дом Советов)", (30.3320, 53.8990, 30.3420, 53.9090)),
]


async def import_osm_and_egr_for_bbox(
    session,
    label: str,
    bbox: tuple[float, float, float, float],
    city_hint: str | None = None,
) -> tuple[int, int]:
    """
    Импортирует реальные здания и ВСЕ организации внутри них из OpenStreetMap API v0.6
    и привязывает действующие организации из ЕГР (egr.gov.by) по точному адресу.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    hotspot_city = city_hint or label.split("(")[0].strip()
    print(f"Загрузка квартала OSM: {label}...")
    osm_data = await fetch_osm_map_box(min_lon, min_lat, max_lon, max_lat)
    if not osm_data:
        return 0, 0

    osm_buildings: list[dict[str, Any]] = osm_data.get("buildings", [])
    osm_pois: list[dict[str, Any]] = osm_data.get("pois", [])
    all_b_polys = [(b["osm_id"], b["polygon"]) for b in osm_buildings]

    # Загружаем индекс существующих зданий и адресов ЕГР
    existing_b_res = await session.execute(
        select(Building).options(selectinload(Building.organizations))
    )
    existing_buildings = list(existing_b_res.scalars().all())
    by_osm_id = {b.osm_id: b for b in existing_buildings if b.osm_id}

    # Множество нормализованных адресов, которые есть в ЕГР со статусом "Действующий"
    egr_addr_res = await session.execute(
        select(EgrRecord.norm_city, EgrRecord.norm_street, EgrRecord.norm_house)
        .where(EgrRecord.short_name.is_not(None))
        .distinct()
    )
    egr_known_addresses = {(r[0], r[1], r[2]) for r in egr_addr_res.all()}

    new_buildings_count = 0
    new_orgs_count = 0

    for ob in osm_buildings:
        osm_id = ob["osm_id"]
        if osm_id in by_osm_id:
            continue
        tags = ob["tags"]
        poly = ob["polygon"]
        ring = ob["ring"]

        center_lon = round(poly.centroid.x, 6)
        center_lat = round(poly.centroid.y, 6)
        region_name, default_city, _ = detect_belarus_region_and_soato(center_lat, center_lon)

        raw_city = (
            tags.get("addr:city:ru")
            or tags.get("addr:city")
            or tags.get("addr:town")
            or tags.get("addr:village")
            or hotspot_city
        )
        city_val = canonicalize_city_name(raw_city, default_city=default_city)
        street_val = (
            tags.get("addr:street:ru")
            or translate_belarusian_street_to_ru(tags.get("addr:street"))
        )
        house_val = tags.get("addr:housenumber")
        b_name = tags.get("name:ru") or tags.get("name")

        # Находим все организации из OSM строго для этого полигона и адреса
        matched_pois = match_pois_strictly_to_building(
            building_poly=poly,
            building_city=city_val,
            building_street=street_val,
            building_house=house_val,
            all_pois=osm_pois,
            all_building_polys=all_b_polys,
            current_osm_id=osm_id,
        )

        # Если на самом контуре здания не был указан адрес, пробуем взять его из POI внутри полигона здания
        if not street_val or not house_val:
            for mp in matched_pois:
                mptags = mp.get("tags") or {}
                mp_st = mptags.get("addr:street:ru") or translate_belarusian_street_to_ru(
                    mptags.get("addr:street")
                )
                mp_hn = mptags.get("addr:housenumber")
                if mp_st and mp_hn and poly.contains(poly.centroid.__class__(mp["lon"], mp["lat"])):
                    street_val = street_val or mp_st
                    house_val = house_val or mp_hn
                    break

        # Не импортируем безымянные постройки без реального адреса (чтобы не было 'ул. Центральная, стр. ...')
        if not (street_val and house_val) and not b_name:
            continue

        has_self_poi = _is_poi_element(tags)
        norm_addr_tuple = (
            normalize_city(city_val),
            normalize_street(street_val),
            normalize_housenumber(house_val),
        )
        has_egr_orgs = all(norm_addr_tuple) and (norm_addr_tuple in egr_known_addresses)

        # Импортируем здание, если внутри него есть организации (в OSM или в ЕГР) либо оно уже есть в БД
        if not matched_pois and not has_self_poi and not has_egr_orgs and osm_id not in by_osm_id:
            continue

        building = by_osm_id.get(osm_id)
        if building is None:
            # Проверим, нет ли уже здания в БД, чей центр попадает внутрь этого полигона OSM и не конфликтует по адресу
            for eb in existing_buildings:
                if poly.buffer(0.00004).contains(eb_pt := poly.centroid.__class__(eb.center_lon, eb.center_lat)):
                    if not (
                        street_val
                        and house_val
                        and eb.address_street
                        and eb.address_housenumber
                        and (
                            normalize_street(street_val) != normalize_street(eb.address_street)
                            or normalize_housenumber(house_val)
                            != normalize_housenumber(eb.address_housenumber)
                        )
                    ):
                        building = eb
                        building.osm_id = osm_id
                        by_osm_id[osm_id] = building
                        break

        if building is None:
            lons = [c[0] for c in ring]
            lats = [c[1] for c in ring]
            b_area = compute_polygon_area_sqm(ring)
            _, building_cad, _ = generate_cadastral_numbers(center_lat, center_lon, osm_id)

            raw_btype = tags.get("building", "commercial")
            if raw_btype in ("yes", "true", "building"):
                raw_btype = "commercial" if (matched_pois or has_egr_orgs) else "apartments"

            lvl_str = tags.get("building:levels")
            try:
                floors = max(1, int(float(lvl_str))) if lvl_str else 0
            except ValueError:
                floors = 0

            b_geojson = json.dumps(
                {"type": "MultiPolygon", "coordinates": [[ring]]}, ensure_ascii=False
            )
            building = Building(
                osm_id=osm_id,
                cadastral_number=building_cad,
                inventory_number=None,
                parcel_id=None,
                name=b_name,
                building_type=raw_btype,
                purpose_label=None,
                address_region=region_name,
                address_city=city_val,
                address_street=street_val,
                address_housenumber=house_val,
                postal_code=tags.get("addr:postcode"),
                floors=floors,
                year_built=int(tags["start_date"][:4])
                if tags.get("start_date", "")[:4].isdigit()
                else None,
                wall_material=tags.get("building:material"),
                area_sqm=b_area,
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
            existing_buildings.append(building)
            by_osm_id[osm_id] = building
            new_buildings_count += 1

        # Обогащаем здание организациями из этого же кэша OSM и из ЕГР (с пакетным коммитом в конце квартала)
        added = await enrich_building_from_osm_and_egr(
            session,
            building,
            osm_box_cache=osm_data,
            auto_commit=False,
            matched_osm_b_override=ob,
            prematched_pois=matched_pois,
        )
        new_orgs_count += added

    await session.commit()
    return new_buildings_count, new_orgs_count


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    await init_db()
    async with AsyncSessionLocal() as session:
        from sqlalchemy import func

        existing_egr_cnt = (
            await session.execute(select(func.count(EgrRecord.id)))
        ).scalar_one() or 0

        if existing_egr_cnt < 5000:
            print("1. Синхронизация действующих государственных органов и организаций из ЕГР (egr.gov.by)...", flush=True)
            go_count = await sync_egr_state_organizations(session)
            print(f"   Загружено актуальных госорганизаций из ЕГР: {go_count}", flush=True)

            print("2. Синхронизация действующих коммерческих организаций из ЕГР (egr.gov.by)...", flush=True)
            periods = [
                ("01.01.2026", "01.04.2026"),
                ("02.04.2026", "01.07.2026"),
            ]
            comm_total = 0
            for start_d, end_d in periods:
                c = await sync_egr_commercial_period(session, start_d, end_d)
                comm_total += c
                print(f"   Период {start_d} - {end_d}: +{c} действующих субъектов", flush=True)
        else:
            print(f"1-2. В локальном реестре ЕГР уже загружено {existing_egr_cnt} действующих субъектов.", flush=True)

        b_cnt = (await session.execute(select(func.count(Building.id)))).scalar_one() or 0
        if b_cnt <= 20:
            print("3. Обогащение опорных зданий из OpenStreetMap API v0.6 и ЕГР РБ...", flush=True)
            b_res = await session.execute(select(Building).limit(20))
            for b in b_res.scalars().all():
                await enrich_building_from_osm_and_egr(session, b)

        print("4. Пакетная загрузка зданий и всех организаций по ключевым кварталам городов РБ...", flush=True)
        total_new_b = 0
        total_new_o = 0
        for label, bbox in BELARUS_HOTSPOT_BBOXES:
            nb, no = await import_osm_and_egr_for_bbox(session, label, bbox)
            total_new_b += nb
            total_new_o += no
            print(f"   -> {label}: +{nb} зданий, +{no} организаций", flush=True)

        await sync_postgis_geometries(session)
        stats = await get_stats(session)
        egr_total = (await session.execute(select(EgrRecord.id))).all()
        print(
            f"\nИТОГИ СИНХРОНИЗАЦИИ КАДАСТРОВОЙ КАРТЫ РБ:\n"
            f"  - Актуальных записей в локальном реестре ЕГР (egr.gov.by): {len(egr_total)}\n"
            f"  - Земельных участков на карте: {stats.total_parcels}\n"
            f"  - Капитальных строений (зданий) на карте: {stats.total_buildings}\n"
            f"  - Организаций внутри зданий: {stats.total_organizations}\n"
            f"  - Городов РБ: {stats.cities_covered}"
        )


if __name__ == "__main__":
    asyncio.run(main())
