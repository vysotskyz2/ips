from __future__ import annotations

import json
import math
import struct
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.building import Building
from app.models.parcel import CadastralParcel

MVT_EXTENT = 4096


def tile_to_bbox_wgs84(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Преобразует координаты тайла XYZ (z, x, y) в WGS84 BBox (min_lon, min_lat, max_lon, max_lat)."""
    n = 2.0**z
    min_lon = x / n * 360.0 - 180.0
    max_lon = (x + 1) / n * 360.0 - 180.0

    lat_rad_north = math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n)))
    lat_rad_south = math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n)))

    max_lat = math.degrees(lat_rad_north)
    min_lat = math.degrees(lat_rad_south)
    return min_lon, min_lat, max_lon, max_lat


def lonlat_to_tile_xy(
    lon: float, lat: float, z: int, tile_x: int, tile_y: int, extent: int = MVT_EXTENT
) -> tuple[int, int]:
    """Переводит (lon, lat) в локальные пиксельные координаты внутри тайла [0..extent]."""
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2.0**z
    world_x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    world_y = (1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * n

    px = int(round((world_x - tile_x) * extent))
    py = int(round((world_y - tile_y) * extent))
    return px, py


def _encode_varint(value: int) -> bytes:
    """Кодирует беззнаковое целое число в Protobuf Varint."""
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _zigzag(value: int) -> int:
    """ZigZag-кодирование знакового целого числа для MVT команд геометрии."""
    return (value << 1) ^ (value >> 63)


def _encode_tag(field_number: int, wire_type: int) -> bytes:
    return _encode_varint((field_number << 3) | wire_type)


def _encode_length_delimited(field_number: int, payload: bytes) -> bytes:
    return _encode_tag(field_number, 2) + _encode_varint(len(payload)) + payload


def _encode_mvt_value(val: Any) -> bytes:
    """Кодирует значение атрибута в сообщение MVT Value (поле 4 в Layer)."""
    if isinstance(val, bool):
        return _encode_tag(7, 0) + _encode_varint(1 if val else 0)
    if isinstance(val, int):
        if val >= 0:
            return _encode_tag(5, 0) + _encode_varint(val)
        return _encode_tag(4, 0) + _encode_varint(val & ((1 << 64) - 1))
    if isinstance(val, float):
        return _encode_tag(3, 1) + struct.pack("<d", val)
    str_bytes = str(val).encode("utf-8")
    return _encode_length_delimited(1, str_bytes)


def _ring_signed_area(ring: list[tuple[int, int]]) -> int:
    """Вычисляет удвоенную знаковую площадь кольца в экранных координатах (ось Y вниз)."""
    area = 0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        area += x1 * y2 - x2 * y1
    return area


def _encode_polygon_geometry(
    rings_lonlat: list[list[list[float]]], z: int, x: int, y: int, extent: int = MVT_EXTENT
) -> list[int]:
    """Кодирует полигон в поток команд MVT (MoveTo, LineTo, ClosePath)."""
    commands: list[int] = []
    cx, cy = 0, 0

    for ring_idx, raw_ring in enumerate(rings_lonlat):
        if len(raw_ring) < 3:
            continue

        pts: list[tuple[int, int]] = []
        for coord in raw_ring:
            px, py = lonlat_to_tile_xy(coord[0], coord[1], z, x, y, extent)
            if not pts or pts[-1] != (px, py):
                pts.append((px, py))

        if len(pts) > 1 and pts[0] == pts[-1]:
            pts.pop()

        if len(pts) < 3:
            continue

        area = _ring_signed_area(pts)
        if area == 0:
            continue

        # Внешнее кольцо должно иметь положительную площадь (по часовой стрелке при Y вниз),
        # внутренние кольца (дырки) — отрицательную.
        if ring_idx == 0 and area < 0:
            pts.reverse()
        elif ring_idx > 0 and area > 0:
            pts.reverse()

        # MoveTo(1)
        commands.append((1 & 0x7) | (1 << 3))
        dx = pts[0][0] - cx
        dy = pts[0][1] - cy
        commands.append(_zigzag(dx))
        commands.append(_zigzag(dy))
        cx, cy = pts[0][0], pts[0][1]

        # LineTo(N - 1)
        line_count = len(pts) - 1
        commands.append((2 & 0x7) | (line_count << 3))
        for px, py in pts[1:]:
            dx = px - cx
            dy = py - cy
            commands.append(_zigzag(dx))
            commands.append(_zigzag(dy))
            cx, cy = px, py

        # ClosePath(1)
        commands.append((7 & 0x7) | (1 << 3))

    return commands


def _encode_point_geometry(
    lon: float, lat: float, z: int, x: int, y: int, extent: int = MVT_EXTENT
) -> list[int]:
    """Кодирует точку (Point) в команды MVT."""
    px, py = lonlat_to_tile_xy(lon, lat, z, x, y, extent)
    return [(1 & 0x7) | (1 << 3), _zigzag(px), _zigzag(py)]


def encode_mvt_layer(
    layer_name: str, features: list[dict[str, Any]], extent: int = MVT_EXTENT
) -> bytes:
    """Формирует бинарный Protobuf-слой MVT (Mapbox Vector Tile v2.1)."""
    if not features:
        return b""

    keys_index: dict[str, int] = {}
    keys_list: list[str] = []
    values_index: dict[tuple[type, Any], int] = {}
    values_list: list[Any] = []
    encoded_features: list[bytes] = []

    for feat in features:
        geom_cmds: list[int] = feat.get("geometry_cmds", [])
        if not geom_cmds:
            continue

        geom_type: int = feat.get("geom_type", 3)  # 1 = POINT, 3 = POLYGON
        feat_id: int = int(feat.get("id", 0))
        props: dict[str, Any] = feat.get("properties", {})

        tags: list[int] = []
        for k, v in props.items():
            if v is None:
                continue
            if k not in keys_index:
                keys_index[k] = len(keys_list)
                keys_list.append(k)
            k_idx = keys_index[k]

            val_key = (type(v), v)
            if val_key not in values_index:
                values_index[val_key] = len(values_list)
                values_list.append(v)
            v_idx = values_index[val_key]

            tags.append(k_idx)
            tags.append(v_idx)

        feat_buf = bytearray()
        if feat_id > 0:
            feat_buf.extend(_encode_tag(1, 0) + _encode_varint(feat_id))
        if tags:
            packed_tags = b"".join(_encode_varint(t) for t in tags)
            feat_buf.extend(_encode_length_delimited(2, packed_tags))
        feat_buf.extend(_encode_tag(3, 0) + _encode_varint(geom_type))
        packed_geom = b"".join(_encode_varint(c) for c in geom_cmds)
        feat_buf.extend(_encode_length_delimited(4, packed_geom))

        encoded_features.append( _encode_length_delimited(2, bytes(feat_buf)) )

    if not encoded_features:
        return b""

    layer_buf = bytearray()
    # version = 2 (field 15, varint)
    layer_buf.extend(_encode_tag(15, 0) + _encode_varint(2))
    # name (field 1, string)
    layer_buf.extend(_encode_length_delimited(1, layer_name.encode("utf-8")))
    # features (field 2, repeated Feature)
    for ef in encoded_features:
        layer_buf.extend(ef)
    # keys (field 3, repeated string)
    for k in keys_list:
        layer_buf.extend(_encode_length_delimited(3, k.encode("utf-8")))
    # values (field 4, repeated Value)
    for v in values_list:
        layer_buf.extend(_encode_length_delimited(4, _encode_mvt_value(v)))
    # extent (field 5, uint32)
    layer_buf.extend(_encode_tag(5, 0) + _encode_varint(extent))

    # Внешнее сообщение Tile: repeated Layer layers = 3
    return _encode_length_delimited(3, bytes(layer_buf))


def _extract_polygons_from_geojson(geojson_str: str) -> list[list[list[list[float]]]]:
    """Возвращает список полигонов (каждый полигон — список колец [[lon, lat], ...])."""
    try:
        geom = json.loads(geojson_str)
    except Exception:
        return []

    gtype = geom.get("type")
    coords = geom.get("coordinates", [])
    if gtype == "Polygon":
        return [coords]
    if gtype == "MultiPolygon":
        return coords
    return []


async def generate_postgis_mvt_tile(
    session: AsyncSession, z: int, x: int, y: int
) -> bytes | None:
    """Генерирует бинарный MVT-тайл напрямую средствами PostgreSQL + PostGIS (ST_AsMVT)."""
    sql = text(
        """
        WITH
        env AS (
            SELECT ST_TileEnvelope(:z, :x, :y) AS bounds_3857
        ),
        parcels_mvt AS (
            SELECT ST_AsMVT(q, 'parcels', 4096, 'mvt_geom') AS tile
            FROM (
                SELECT
                    p.id,
                    p.cadastral_number,
                    COALESCE(p.name, '') AS name,
                    p.land_use,
                    ROUND(p.area_sqm::numeric, 1)::float8 AS area_sqm,
                    ST_AsMVTGeom(p.geom_3857, env.bounds_3857, 4096, 256, true) AS mvt_geom
                FROM cadastral_parcels p
                CROSS JOIN env
                WHERE p.geom_3857 && env.bounds_3857
                  AND :z >= 10
            ) q
            WHERE q.mvt_geom IS NOT NULL
        ),
        buildings_mvt AS (
            SELECT ST_AsMVT(q, 'buildings', 4096, 'mvt_geom') AS tile
            FROM (
                SELECT
                    b.id,
                    COALESCE(b.name, '') AS name,
                    b.building_type,
                    b.address_city,
                    COALESCE(b.address_street, '') AS address_street,
                    COALESCE(b.address_housenumber, '') AS address_housenumber,
                    b.floors,
                    ROUND(b.area_sqm::numeric, 1)::float8 AS area_sqm,
                    (SELECT COUNT(*)::int FROM organizations o WHERE o.building_id = b.id) AS org_count,
                    ST_AsMVTGeom(b.geom_3857, env.bounds_3857, 4096, 256, true) AS mvt_geom
                FROM buildings b
                CROSS JOIN env
                WHERE b.geom_3857 && env.bounds_3857
                  AND :z >= 11
            ) q
            WHERE q.mvt_geom IS NOT NULL
        ),
        building_points_mvt AS (
            SELECT ST_AsMVT(q, 'building_markers', 4096, 'mvt_geom') AS tile
            FROM (
                SELECT
                    b.id,
                    COALESCE(b.name, b.address_street || ' ' || COALESCE(b.address_housenumber, '')) AS label,
                    b.building_type,
                    b.address_city,
                    (SELECT COUNT(*)::int FROM organizations o WHERE o.building_id = b.id) AS org_count,
                    ST_AsMVTGeom(
                        ST_Transform(ST_SetSRID(ST_MakePoint(b.center_lon, b.center_lat), 4326), 3857),
                        env.bounds_3857,
                        4096,
                        256,
                        true
                    ) AS mvt_geom
                FROM buildings b
                CROSS JOIN env
                WHERE b.geom_3857 && env.bounds_3857
            ) q
            WHERE q.mvt_geom IS NOT NULL
        )
        SELECT
            COALESCE((SELECT tile FROM parcels_mvt), ''::bytea) ||
            COALESCE((SELECT tile FROM buildings_mvt), ''::bytea) ||
            COALESCE((SELECT tile FROM building_points_mvt), ''::bytea) AS full_tile;
        """
    )
    try:
        result = await session.execute(sql, {"z": z, "x": x, "y": y})
        row = result.scalar_one_or_none()
        if row is not None:
            return bytes(row)
    except Exception:
        return None
    return None


async def generate_cadastral_mvt_tile(
    session: AsyncSession, z: int, x: int, y: int
) -> bytes:
    """
    Возвращает бинарный векторный тайл MVT (.pbf) со слоями:
    - parcels (земельные участки)
    - buildings (полигоны зданий)
    - building_markers (центроиды зданий с количеством организаций для любых масштабов)
    Использует PostGIS ST_AsMVT при наличии PostgreSQL, либо встроенный MVT 2.1 энкодер.
    """
    if settings.is_postgres:
        pg_tile = await generate_postgis_mvt_tile(session, z, x, y)
        if pg_tile is not None and len(pg_tile) > 0:
            return pg_tile

    min_lon, min_lat, max_lon, max_lat = tile_to_bbox_wgs84(z, x, y)
    # Небольшой буфер вокруг тайла (5%) для гладкой склейки полигонов на границах тайлов
    lon_pad = (max_lon - min_lon) * 0.05
    lat_pad = (max_lat - min_lat) * 0.05

    q_min_lon = min_lon - lon_pad
    q_max_lon = max_lon + lon_pad
    q_min_lat = min_lat - lat_pad
    q_max_lat = max_lat + lat_pad

    tile_chunks: list[bytes] = []

    # 1. Слой земельных участков (parcels)
    if z >= 9:
        parcels_stmt = select(CadastralParcel).where(
            CadastralParcel.max_lon >= q_min_lon,
            CadastralParcel.min_lon <= q_max_lon,
            CadastralParcel.max_lat >= q_min_lat,
            CadastralParcel.min_lat <= q_max_lat,
        )
        parcels_res = await session.execute(parcels_stmt)
        parcels = parcels_res.scalars().all()

        parcel_features: list[dict[str, Any]] = []
        for p in parcels:
            polygons = _extract_polygons_from_geojson(p.geometry_geojson)
            for poly_rings in polygons:
                cmds = _encode_polygon_geometry(poly_rings, z, x, y, MVT_EXTENT)
                if cmds:
                    parcel_features.append(
                        {
                            "id": p.id,
                            "geom_type": 3,
                            "geometry_cmds": cmds,
                            "properties": {
                                "id": p.id,
                                "cadastral_number": p.cadastral_number,
                                "name": p.name or "",
                                "land_use": p.land_use,
                                "area_sqm": round(p.area_sqm, 1),
                            },
                        }
                    )
        if parcel_features:
            tile_chunks.append(encode_mvt_layer("parcels", parcel_features, MVT_EXTENT))

    # 2. Выборка зданий в области тайла
    buildings_stmt = select(Building).where(
        Building.max_lon >= q_min_lon,
        Building.min_lon <= q_max_lon,
        Building.max_lat >= q_min_lat,
        Building.min_lat <= q_max_lat,
    )
    buildings_res = await session.execute(buildings_stmt)
    buildings = buildings_res.scalars().all()

    if buildings:
        building_ids = [b.id for b in buildings]
        from sqlalchemy import func
        from app.models.organization import Organization

        counts_stmt = (
            select(Organization.building_id, func.count(Organization.id))
            .where(Organization.building_id.in_(building_ids))
            .group_by(Organization.building_id)
        )
        counts_res = await session.execute(counts_stmt)
        org_counts: dict[int, int] = {row[0]: int(row[1]) for row in counts_res.all()}

        building_poly_features: list[dict[str, Any]] = []
        marker_point_features: list[dict[str, Any]] = []

        for b in buildings:
            org_cnt = org_counts.get(b.id, 0)
            label = b.name or f"{b.address_street or ''} {b.address_housenumber or ''}".strip()

            # Точечный маркер здания (для отображения на обзорных и средних масштабах РБ)
            pt_cmds = _encode_point_geometry(b.center_lon, b.center_lat, z, x, y, MVT_EXTENT)
            marker_point_features.append(
                {
                    "id": b.id,
                    "geom_type": 1,
                    "geometry_cmds": pt_cmds,
                    "properties": {
                        "id": b.id,
                        "label": label,
                        "building_type": b.building_type,
                        "address_city": b.address_city,
                        "floors": b.floors,
                        "org_count": org_cnt,
                        "data_source": "OpenStreetMap",
                    },
                }
            )

            # Полигон здания
            if z >= 10:
                polygons = _extract_polygons_from_geojson(b.geometry_geojson)
                for poly_rings in polygons:
                    cmds = _encode_polygon_geometry(poly_rings, z, x, y, MVT_EXTENT)
                    if cmds:
                        building_poly_features.append(
                            {
                                "id": b.id,
                                "geom_type": 3,
                                "geometry_cmds": cmds,
                                "properties": {
                                    "id": b.id,
                                    "name": b.name or "",
                                    "building_type": b.building_type,
                                    "address_city": b.address_city,
                                    "address_street": b.address_street or "",
                                    "address_housenumber": b.address_housenumber or "",
                                    "floors": b.floors,
                                    "area_sqm": round(b.area_sqm, 1),
                                    "org_count": org_cnt,
                                    "data_source": "OpenStreetMap",
                                },
                            }
                        )

        if building_poly_features:
            tile_chunks.append(encode_mvt_layer("buildings", building_poly_features, MVT_EXTENT))
        if marker_point_features:
            tile_chunks.append(
                encode_mvt_layer("building_markers", marker_point_features, MVT_EXTENT)
            )

    return b"".join(tile_chunks)
