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
                "lat": 53.9215,
                "lon": 27.6125,
                "name": "Деловой центр «Академический»",
                "street": "пр-т Независимости",
                "housenumber": "99",
                "city": "Минск",
                "floors": 6,
                "building_type": "office",
            },
        )
        assert resp_new.status_code == 200
        data_new = resp_new.json()
        assert data_new["address_city"] == "Минск"
        assert data_new["address_housenumber"] == "99"
        assert data_new["cadastral_number"].startswith("5000000000")

        # Удаляем тестовое здание после проверки
        from sqlalchemy import delete
        from app.models.building import Building
        from app.models.parcel import CadastralParcel

        async with AsyncSessionLocal() as session:
            await session.execute(delete(Building).where(Building.id == data_new["id"]))
            if data_new.get("parcel_id"):
                await session.execute(
                    delete(CadastralParcel).where(CadastralParcel.id == data_new["parcel_id"])
                )
            await session.commit()

        # 3. Клик по пустому месту (парк/дорога/река без здания) -> 404, ничего фейкового не создается!
        resp_empty_spot = await client.post(
            "/api/v1/buildings/inspect",
            json={
                "lat": 53.9030,
                "lon": 27.5540,
            },
        )
        assert resp_empty_spot.status_code == 404

        # 4. Координаты вне территории Республики Беларусь -> 404
        resp_outside = await client.get(
            "/api/v1/buildings/by-coords",
            params={"lat": 48.8566, "lon": 2.3522},
        )
        assert resp_outside.status_code == 404


def test_multiple_house_9_no_property_mixing():
    from shapely.geometry import Polygon

    from app.services.address_matcher import (
        addresses_strictly_match,
        extract_single_building_polygon,
        has_address_conflict,
    )
    from app.services.cadastral_service import match_pois_strictly_to_building

    # 1. Дома с одинаковым номером "9" на разных улицах НЕ должны совпадать и должны определяться как конфликт
    assert not addresses_strictly_match(
        "Минск", "пр-т Победителей", "9", "Минск", "ул. Немига", "9"
    )
    assert has_address_conflict(
        "Минск", "пр-т Победителей", "9", "Минск", "ул. Володарского", "9"
    )

    # 2. Дом "9", дом "9А" и дом "9 к. 1" на одной улице — это РАЗНЫЕ здания
    assert not addresses_strictly_match(
        "Минск", "пр-т Победителей", "9", "Минск", "пр-т Победителей", "9А"
    )
    assert not addresses_strictly_match(
        "Минск", "пр-т Победителей", "9", "Минск", "пр-т Победителей", "9", "1"
    )
    assert has_address_conflict(
        "Минск", "пр-т Победителей", "9", "Минск", "пр-т Победителей", "9А"
    )

    # 3. Белорусское и русское название одного и того же адреса (праспект Пераможцаў, 9 == пр-т Победителей, 9)
    assert addresses_strictly_match(
        "Мінск", "праспект Пераможцаў", "9", "Минск", "проспект Победителей", "9"
    )

    # 4. Два соседних корпуса впритык друг к другу (общая стена x=27.5510):
    # организация внутри корпуса B у самой стены НЕ должна попадать в корпус A
    poly_a = Polygon(
        [(27.5500, 53.8970), (27.5510, 53.8970), (27.5510, 53.8975), (27.5500, 53.8975)]
    )
    poly_b = Polygon(
        [(27.5510, 53.8970), (27.5520, 53.8970), (27.5520, 53.8975), (27.5510, 53.8975)]
    )
    pois = [
        {
            "osm_id": 1001,
            "lon": 27.5505,
            "lat": 53.8972,
            "tags": {"name": "Кофейня Корпус А", "amenity": "cafe", "addr:street": "ул. Карла Маркса", "addr:housenumber": "9"},
        },
        {
            "osm_id": 1002,
            "lon": 27.55101,  # Внутри корпуса B, в 1 метре от стены корпуса A!
            "lat": 53.8972,
            "tags": {"name": "Салон Корпус Б", "shop": "beauty", "addr:street": "ул. Карла Маркса", "addr:housenumber": "9"},
        },
        {
            "osm_id": 1003,
            "lon": 27.5505,
            "lat": 53.8972,
            "tags": {"name": "Чужой дом №9 с соседней улицы", "shop": "books", "addr:street": "ул. Володарского", "addr:housenumber": "9"},
        },
    ]
    all_polys = [(1, poly_a), (2, poly_b)]

    matched_a = match_pois_strictly_to_building(
        building_poly=poly_a,
        building_city="Минск",
        building_street="ул. Карла Маркса",
        building_house="9",
        all_pois=pois,
        all_building_polys=all_polys,
        current_osm_id=1,
    )
    assert [p["osm_id"] for p in matched_a] == [1001]

    matched_b = match_pois_strictly_to_building(
        building_poly=poly_b,
        building_city="Минск",
        building_street="ул. Карла Маркса",
        building_house="9",
        all_pois=pois,
        all_building_polys=all_polys,
        current_osm_id=2,
    )
    assert [p["osm_id"] for p in matched_b] == [1002]

    # 5. Выделение одного здания из объединенного MultiPolygon векторного тайла
    multi_geom = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[27.5500, 53.8970], [27.5508, 53.8970], [27.5508, 53.8975], [27.5500, 53.8975], [27.5500, 53.8970]]],
            [[[27.5520, 53.8980], [27.5528, 53.8980], [27.5528, 53.8985], [27.5520, 53.8985], [27.5520, 53.8980]]],
        ],
    }
    single = extract_single_building_polygon(multi_geom, 27.5524, 53.8982)
    assert single is not None
    assert len(single["coordinates"]) == 1
    assert single["coordinates"][0][0][0] == [27.5520, 53.8980]


def test_egr_only_active_entities_accepted():
    from app.services.egr_service import (
        is_egr_address_active,
        is_egr_entity_active,
        is_egr_go_active,
    )

    # Действующее ЮЛ
    assert is_egr_entity_active(
        {"ngrn": 100325912, "dto": None, "nsi00219": {"nksost": 1, "vnsostk": "Действующий"}}
    )
    # Ликвидированное / исключенное ЮЛ
    assert not is_egr_entity_active(
        {"ngrn": 190990610, "dto": "2019-02-20", "nsi00219": {"nksost": 2, "vnsostk": "Исключен из ЕГР"}}
    )
    assert not is_egr_entity_active(
        {"ngrn": 590830543, "dto": None, "nsi00219": {"nksost": 5, "vnsostk": "Прекращение деятельности"}}
    )

    # Актуальный адрес vs архивный адрес ЕГР
    assert is_egr_address_active(
        {"cact": "+", "dto": None, "vnp": "Минск", "vulitsa": "Независимости", "vdom": "10"}
    )
    assert not is_egr_address_active(
        {"cact": "-", "dto": "2025-01-01", "vnp": "Минск", "vulitsa": "Независимости", "vdom": "10"}
    )

    # Действующий госорган vs упраздненный/прекращенный
    assert is_egr_go_active({"cact": "+", "cstate": None, "dto": None})
    assert not is_egr_go_active({"cact": "+", "cstate": "П", "dto": None})
    assert not is_egr_go_active({"cact": "+", "cstate": "И", "dto": None})
