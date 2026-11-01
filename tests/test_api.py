from __future__ import annotations

import math

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.services.cadastral_service import ensure_seed_data


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2.0**z
    xtile = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    ytile = int((1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * n)
    return xtile, ytile


@pytest.fixture(autouse=True)
async def setup_database():
    settings.enable_live_osm_enrichment = False
    await init_db()
    async with AsyncSessionLocal() as session:
        await ensure_seed_data(session)


@pytest.mark.asyncio
async def test_index_html_served():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Кадастровая карта Республики Беларусь" in resp.text


@pytest.mark.asyncio
async def test_cadastral_stats():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_parcels"] >= 10
        assert data["total_buildings"] >= 10
        assert data["total_organizations"] >= 35
        assert data["cities_covered"] >= 6


@pytest.mark.asyncio
async def test_building_detail_with_organizations():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/buildings/1")
        assert resp.status_code == 200
        b = resp.json()
        assert b["id"] == 1
        assert "cadastral_number" in b
        assert b["organizations_count"] > 0
        assert len(b["organizations"]) == b["organizations_count"]
        first_org = b["organizations"][0]
        assert "name" in first_org
        assert "category" in first_org
        assert "floor" in first_org
        assert b["parcel"] is not None


@pytest.mark.asyncio
async def test_mvt_vector_tile_generation():
    # Проверим генерацию бинарного векторного тайла MVT (.pbf) для центра Минска на зуме Z=13
    z = 13
    x, y = lonlat_to_tile(27.5590, 53.9006, z)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/tiles/cadastral/{z}/{x}/{y}.pbf")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-protobuf"
        assert len(resp.content) > 50
        # Проверяем, что внутри бинарного Protobuf присутствуют имена слоев MVT
        assert b"buildings" in resp.content
        assert b"building_markers" in resp.content


@pytest.mark.asyncio
async def test_search_by_organization_and_cadastral_number():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Поиск по названию организации внутри здания
        resp_org = await client.get("/api/v1/search", params={"q": "Беларусбанк"})
        assert resp_org.status_code == 200
        items_org = resp_org.json()
        assert len(items_org) >= 1
        assert any(i["result_type"] == "organization" for i in items_org)

        # Поиск по кадастровому номеру
        resp_cad = await client.get("/api/v1/search", params={"q": "500000000001004512"})
        assert resp_cad.status_code == 200
        items_cad = resp_cad.json()
        assert len(items_cad) >= 1
        assert items_cad[0]["building_id"] == 1


@pytest.mark.asyncio
async def test_inspect_any_building_on_map():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Клик по существующему зданию (Главный корпус БГУ)
        resp_existing = await client.get(
            "/api/v1/buildings/by-coords",
            params={"lat": 53.8939, "lon": 27.5471},
        )
        assert resp_existing.status_code == 200
        data_ex = resp_existing.json()
        assert "Белорусский государственный университет" in data_ex["name"]
        assert data_ex["organizations_count"] >= 4

        # 2. Клик по новому зданию на карте РБ (динамическая регистрация паспорта и организаций)
        resp_new = await client.post(
            "/api/v1/buildings/inspect",
            json={
                "lat": 53.9052,
                "lon": 27.5612,
                "name": "Деловой центр «Октябрьская площадь»",
                "street": "пр-т Независимости",
                "housenumber": "23",
                "city": "Минск",
                "floors": 6,
                "building_type": "office",
            },
        )
        assert resp_new.status_code == 200
        data_new = resp_new.json()
        assert data_new["address_city"] == "Минск"
        assert data_new["organizations_count"] >= 1
        assert data_new["cadastral_number"].startswith("5000000000")

        # 3. Координаты вне территории Республики Беларусь -> 404
        resp_outside = await client.get(
            "/api/v1/buildings/by-coords",
            params={"lat": 48.8566, "lon": 2.3522},
        )
        assert resp_outside.status_code == 404
