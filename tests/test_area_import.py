from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from shapely.geometry import Polygon, box
from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.building import Building
from app.models.egr_record import EgrRecord
from app.services import area_import_service
from app.services.address_matcher import normalize_city, normalize_housenumber, normalize_street
from app.services.cadastral_service import ensure_seed_data

BUILDING_INSIDE_ID = 990001
BUILDING_OUTSIDE_ID = 990002
POI_ID = 880001

RING_INSIDE = [
    [27.5600, 53.9040],
    [27.5610, 53.9040],
    [27.5610, 53.9046],
    [27.5600, 53.9046],
    [27.5600, 53.9040],
]
RING_OUTSIDE = [
    [27.5800, 53.9040],
    [27.5810, 53.9040],
    [27.5810, 53.9046],
    [27.5800, 53.9046],
    [27.5800, 53.9040],
]

SELECTION = {
    "type": "Polygon",
    "coordinates": [
        [
            [27.5595, 53.9035],
            [27.5615, 53.9035],
            [27.5615, 53.9050],
            [27.5595, 53.9050],
            [27.5595, 53.9035],
        ]
    ],
}


def _inside_building() -> dict:
    return {
        "osm_id": BUILDING_INSIDE_ID,
        "tags": {
            "building": "apartments",
            "addr:city": "Минск",
            "addr:street": "ул. Тестовая",
            "addr:housenumber": "99",
            "name": "Тестовый дом 99",
        },
        "ring": RING_INSIDE,
        "polygon": Polygon(RING_INSIDE),
    }


def _outside_building() -> dict:
    return {
        "osm_id": BUILDING_OUTSIDE_ID,
        "tags": {
            "building": "house",
            "addr:city": "Минск",
            "addr:street": "ул. Внешняя",
            "addr:housenumber": "1",
        },
        "ring": RING_OUTSIDE,
        "polygon": Polygon(RING_OUTSIDE),
    }


def _poi() -> dict:
    return {
        "osm_id": POI_ID,
        "lat": 53.9043,
        "lon": 27.5605,
        "tags": {"name": "Кафе Тест", "amenity": "cafe"},
    }


@pytest.fixture(autouse=True)
async def setup_database():
    settings.enable_live_osm_enrichment = False
    await init_db()
    async with AsyncSessionLocal() as session:
        await ensure_seed_data(session)


async def _seed_egr_records() -> None:
    n_city = normalize_city("Минск")
    n_street = normalize_street("ул. Тестовая")
    n_house = normalize_housenumber("99")
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(EgrRecord).where(EgrRecord.unp.in_(["190000001", "190000002"]))
            )
        ).scalars().all()
        if existing:
            return
        session.add_all(
            [
                EgrRecord(
                    unp="190000001",
                    short_name="ООО Действующий Тест",
                    full_name="Общество с ограниченной ответственностью Действующий Тест",
                    status_label="Действующий",
                    category="Офисы и ИТ",
                    city="Минск",
                    street="ул. Тестовая",
                    housenumber="99",
                    norm_city=n_city,
                    norm_street=n_street,
                    norm_house=n_house,
                ),
                EgrRecord(
                    unp="190000002",
                    short_name="ООО Исключённый Тест",
                    full_name="Общество с ограниченной ответственностью Исключённый Тест",
                    status_label="Исключен из ЕГР",
                    category="Торговля",
                    city="Минск",
                    street="ул. Тестовая",
                    housenumber="99",
                    norm_city=n_city,
                    norm_street=n_street,
                    norm_house=n_house,
                ),
            ]
        )
        await session.commit()


@pytest.mark.asyncio
async def test_area_import_filters_dedup_and_egr_status(monkeypatch):
    async def fake_fetch(min_lon, min_lat, max_lon, max_lat):
        # Дубликаты имитируют одно и то же здание/POI, попавшее в соседние тайлы.
        return {
            "buildings": [_inside_building(), _inside_building(), _outside_building()],
            "pois": [_poi(), _poi()],
        }

    monkeypatch.setattr(area_import_service, "fetch_osm_map_box", fake_fetch)
    await _seed_egr_records()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/areas/import-and-stats",
            json={"geometry": SELECTION},
        )
        assert resp.status_code == 200
        stats = resp.json()

    assert stats["area_km2"] > 0
    assert stats["buildings_total"] >= 1
    assert (
        stats["import_stats"]["buildings_added"] + stats["import_stats"]["buildings_updated"] >= 1
    )
    assert stats["organizations_osm"] >= 1
    assert stats["organizations_egr"] >= 2
    assert stats["egr_active"] >= 1
    assert stats["egr_excluded"] >= 1
    assert stats["import_stats"]["tiles_requested"] >= 1

    async with AsyncSessionLocal() as session:
        inside_count = (
            await session.execute(
                select(func.count(Building.id)).where(Building.osm_id == BUILDING_INSIDE_ID)
            )
        ).scalar_one()
        outside_count = (
            await session.execute(
                select(func.count(Building.id)).where(Building.osm_id == BUILDING_OUTSIDE_ID)
            )
        ).scalar_one()
    assert inside_count == 1
    assert outside_count == 0


@pytest.mark.asyncio
async def test_area_import_invalid_geometry_returns_422():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        point_resp = await client.post(
            "/api/v1/areas/import-and-stats",
            json={"geometry": {"type": "Point", "coordinates": [27.56, 53.9]}},
        )
        assert point_resp.status_code == 422

        self_intersecting = {
            "type": "Polygon",
            "coordinates": [
                [
                    [27.5590, 53.9040],
                    [27.5620, 53.9045],
                    [27.5620, 53.9040],
                    [27.5590, 53.9045],
                    [27.5590, 53.9040],
                ]
            ],
        }
        invalid_resp = await client.post(
            "/api/v1/areas/import-and-stats",
            json={"geometry": self_intersecting},
        )
        assert invalid_resp.status_code == 422


@pytest.mark.asyncio
async def test_area_journal_saves_and_returns_stats(monkeypatch):
    async def fake_fetch(min_lon, min_lat, max_lon, max_lat):
        return {"buildings": [_inside_building()], "pois": []}

    monkeypatch.setattr(area_import_service, "fetch_osm_map_box", fake_fetch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/areas/import-and-stats",
            json={"geometry": SELECTION},
        )
        assert resp.status_code == 200
        stats = resp.json()
        entry_id = stats["journal_id"]
        assert entry_id is not None
        assert stats["journal_created_at"]

        list_resp = await client.get("/api/v1/areas/journal")
        assert list_resp.status_code == 200
        entries = list_resp.json()
        assert any(entry["id"] == entry_id for entry in entries)
        listed = next(entry for entry in entries if entry["id"] == entry_id)
        assert listed["geometry"]["type"] in ("Polygon", "MultiPolygon")

        detail_resp = await client.get(f"/api/v1/areas/journal/{entry_id}")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["stats"]["buildings_total"] >= 1
        assert detail["stats"]["area_km2"] > 0

        delete_resp = await client.delete(f"/api/v1/areas/journal/{entry_id}")
        assert delete_resp.status_code == 204

        missing_resp = await client.get(f"/api/v1/areas/journal/{entry_id}")
        assert missing_resp.status_code == 404


@pytest.mark.asyncio
async def test_area_stats_use_local_db_without_osm(monkeypatch):
    async def empty_fetch(min_lon, min_lat, max_lon, max_lat):
        return None

    monkeypatch.setattr(area_import_service, "fetch_osm_map_box", empty_fetch)

    # Нижняя часть выделенной области включает уже сохранённое тестовое здание.
    local_selection = box(27.5595, 53.9035, 27.5615, 53.9050)
    calculation = {
        "type": "Polygon",
        "coordinates": [
            [
                [local_selection.bounds[0], local_selection.bounds[1]],
                [local_selection.bounds[2], local_selection.bounds[1]],
                [local_selection.bounds[2], local_selection.bounds[3]],
                [local_selection.bounds[0], local_selection.bounds[3]],
                [local_selection.bounds[0], local_selection.bounds[1]],
            ]
        ],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/areas/import-and-stats",
            json={"geometry": calculation},
        )
        assert resp.status_code == 200
        stats = resp.json()

    assert stats["data_scope"] == "partial"
    assert any("OpenStreetMap не вернул зданий" in warning for warning in stats["warnings"])
    assert stats["buildings_total"] >= 1
