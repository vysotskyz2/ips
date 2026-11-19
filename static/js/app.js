/* Клиентское приложение Кадастровой карты Республики Беларусь (MapLibre GL JS + MVT) */

const BELARUS_BOUNDS = [
    [22.8, 51.1], // Юго-Запад РБ
    [33.1, 56.3], // Северо-Восток РБ
];

const BUILDING_TYPE_LABELS = {
    retail: "Торгово-развлекательный комплекс",
    office: "Бизнес-центр / Офисное здание",
    commercial: "Многофункциональное деловое здание",
    public: "Административно-общественное здание",
    university: "Учреждение высшего образования",
    apartments: "Многоквартирный жилой дом",
    industrial: "Производственно-складской объект",
    hospital: "Учреждение здравоохранения",
};

let map = null;
let currentBuilding = null;
let selectedCategoryFilter = "ALL";
let is3DEnabled = true;
let isDistrictsVisible = true;
let hoverPopup = null;
let districtPopup = null;
let allCatalogBuildings = [];

let isAreaDrawing = false;
let areaDrawMode = null;
let areaRectStart = null;
let areaRectEnd = null;
let areaPolygonVertices = [];
let areaCursorPoint = null;
let areaPreviousPitch = 45;
let areaIgnoreClickUntil = 0;
let areaImportRequestId = 0;

document.addEventListener("DOMContentLoaded", async () => {
    initMap();
    initUIHandlers();
    await loadHeaderStats();
    await loadCatalogBuildings();
});

function initMap() {
    map = new maplibregl.Map({
        container: "map",
        style: "https://tiles.openfreemap.org/styles/liberty",
        center: [27.5615, 53.9045], // Минск по умолчанию (или вся РБ)
        zoom: 12.2,
        pitch: 45,
        bearing: -10,
        maxBounds: BELARUS_BOUNDS,
        attributionControl: true,
    });

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");

    hoverPopup = new maplibregl.Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 12,
    });

    districtPopup = new maplibregl.Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 10,
    });

    map.getCanvas().addEventListener("mouseleave", () => {
        if (districtPopup) districtPopup.remove();
    });

    map.on("mousemove", (e) => {
        const lat = e.lngLat.lat.toFixed(5);
        const lon = e.lngLat.lng.toFixed(5);
        document.getElementById("cursor-coords").textContent = `Ш: ${lat}°, Д: ${lon}° (WGS84)`;
    });

    map.on("zoom", () => {
        document.getElementById("map-zoom-indicator").textContent = `Масштаб: Z${map.getZoom().toFixed(1)}`;
    });

    map.on("load", async () => {
        await setupCadastralLayers();
    });
}

async function setupCadastralLayers() {
    const origin = window.location.origin;

    // 1. Векторный тайловый источник MVT (.pbf) из нашего FastAPI + PostGIS бэкенда
    map.addSource("cadastral-mvt", {
        type: "vector",
        tiles: [`${origin}/api/v1/tiles/cadastral/{z}/{x}/{y}.pbf`],
        minzoom: 5,
        maxzoom: 19,
    });

    // 2. GeoJSON-источники зданий и реальных границ районов РБ
    const [buildingsGeoJSON, districtsGeoJSON] = await Promise.all([
        fetch("/api/v1/geojson/buildings").then((r) => r.json()),
        fetch("/api/v1/geojson/districts").then((r) => r.json()),
    ]);

    map.addSource("belarus-districts-geojson", {
        type: "geojson",
        data: districtsGeoJSON,
    });

    map.addSource("cadastral-buildings-geojson", {
        type: "geojson",
        data: buildingsGeoJSON,
    });

    map.addSource("selected-building-source", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
    });

    // Города областного подчинения — fallback там, где нет внутренних районов
    map.addLayer({
        id: "belarus-cities-fill",
        type: "fill",
        source: "belarus-districts-geojson",
        filter: ["==", ["get", "kind"], "city_fallback"],
        paint: {
            "fill-color": "#64748b",
            "fill-opacity": 0.14,
            "fill-antialias": false,
        },
    });

    map.addLayer({
        id: "belarus-cities-outline",
        type: "line",
        source: "belarus-districts-geojson",
        filter: ["==", ["get", "kind"], "city_fallback"],
        paint: {
            "line-color": "#64748b",
            "line-width": 1.4,
            "line-dasharray": [4, 3],
            "line-opacity": 0.8,
            "line-join": "round",
            "line-cap": "round",
            "line-blur": 0,
        },
    });

    // Реальные границы районов областей (актуальные OSM admin_level=6)
    map.addLayer({
        id: "belarus-rayons-fill",
        type: "fill",
        source: "belarus-districts-geojson",
        filter: ["==", ["get", "kind"], "district"],
        paint: {
            "fill-color": ["coalesce", ["get", "fill_color"], "#60a5fa"],
            "fill-opacity": 0.18,
            "fill-antialias": false,
        },
    });

    map.addLayer({
        id: "belarus-rayons-outline",
        type: "line",
        source: "belarus-districts-geojson",
        filter: ["==", ["get", "kind"], "district"],
        paint: {
            "line-color": "#94a3b8",
            "line-width": ["interpolate", ["linear"], ["zoom"], 6, 1.2, 12, 2.2],
            "line-opacity": 0.9,
            "line-join": "round",
            "line-cap": "round",
            "line-blur": 0,
        },
    });

    // Реальные границы районов внутри городов (OSM admin_level=9)
    map.addLayer({
        id: "belarus-city-districts-fill",
        type: "fill",
        source: "belarus-districts-geojson",
        filter: ["==", ["get", "kind"], "city_district"],
        paint: {
            "fill-color": ["coalesce", ["get", "fill_color"], "#f59e0b"],
            "fill-opacity": 0.24,
            "fill-antialias": false,
        },
    });

    map.addLayer({
        id: "belarus-city-districts-outline",
        type: "line",
        source: "belarus-districts-geojson",
        filter: ["==", ["get", "kind"], "city_district"],
        paint: {
            "line-color": "#f59e0b",
            "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.8, 14, 3.0],
            "line-opacity": 0.95,
            "line-join": "round",
            "line-cap": "round",
            "line-blur": 0,
        },
    });

    // Плоский 2D-слой контуров зданий для точного определения клика по основанию здания (без искажений 3D-экструзии)
    map.addLayer({
        id: "cadastral-buildings-footprint",
        type: "fill",
        source: "cadastral-buildings-geojson",
        paint: {
            "fill-color": "#0ea5e9",
            "fill-opacity": 0.02,
        },
    });

    // Слой 3D/2D кадастровых зданий из нашей БД
    map.addLayer({
        id: "cadastral-buildings-3d",
        type: "fill-extrusion",
        source: "cadastral-buildings-geojson",
        paint: {
            "fill-extrusion-color": [
                "match",
                ["get", "building_type"],
                "retail", "#f59e0b",
                "office", "#0ea5e9",
                "commercial", "#06b6d4",
                "public", "#ec4899",
                "university", "#a855f7",
                "#3b82f6",
            ],
            "fill-extrusion-height": [
                "*",
                ["coalesce", ["get", "floors"], 3],
                4.2,
            ],
            "fill-extrusion-base": 0,
            "fill-extrusion-opacity": 0.88,
        },
    });

    // Контур выбранного пользователем здания
    map.addLayer({
        id: "selected-building-highlight",
        type: "line",
        source: "selected-building-source",
        paint: {
            "line-color": "#38bdf8",
            "line-width": 4,
        },
    });

    map.addLayer({
        id: "selected-building-extrusion",
        type: "fill-extrusion",
        source: "selected-building-source",
        paint: {
            "fill-extrusion-color": "#38bdf8",
            "fill-extrusion-height": 22,
            "fill-extrusion-opacity": 0.55,
        },
    });

    // Источник и слои для выделенной пользователем области (прямоугольник/полигон)
    map.addSource("area-selection-source", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
    });

    map.addLayer({
        id: "area-selection-fill",
        type: "fill",
        source: "area-selection-source",
        paint: {
            "fill-color": "#22d3ee",
            "fill-opacity": 0.16,
        },
    });

    map.addLayer({
        id: "area-selection-outline",
        type: "line",
        source: "area-selection-source",
        paint: {
            "line-color": "#22d3ee",
            "line-width": 2.5,
            "line-dasharray": [1.5, 1],
        },
    });

    registerAreaDrawingEvents();

    // Слой точечных маркеров с числом организаций внутри здания (из MVT векторного тайла)
    map.addLayer({
        id: "cadastral-markers-circle",
        type: "circle",
        source: "cadastral-mvt",
        "source-layer": "building_markers",
        maxzoom: 16.2,
        paint: {
            "circle-radius": [
                "interpolate", ["linear"], ["zoom"],
                6, 5,
                12, 9,
                15, 10,
                16, 7,
            ],
            "circle-color": "#0ea5e9",
            "circle-stroke-width": 1.5,
            "circle-stroke-color": "#ffffff",
        },
    });

    map.addLayer({
        id: "cadastral-markers-count",
        type: "symbol",
        source: "cadastral-mvt",
        "source-layer": "building_markers",
        maxzoom: 16.2,
        layout: {
            "text-field": ["to-string", ["get", "org_count"]],
            "text-size": 10,
            "text-font": ["Open Sans Bold", "Arial Unicode MS Bold"],
        },
        paint: {
            "text-color": "#ffffff",
        },
    });

    // Подписи районов — выше зданий и маркеров
    map.addLayer({
        id: "belarus-rayons-labels",
        type: "symbol",
        source: "belarus-districts-geojson",
        filter: ["==", ["get", "kind"], "district"],
        minzoom: 7.2,
        layout: {
            "text-field": ["coalesce", ["get", "name_ru"], ["get", "shapeName"]],
            "text-size": ["interpolate", ["linear"], ["zoom"], 7, 9, 10, 12],
            "text-font": ["Open Sans Bold", "Arial Unicode MS Bold"],
            "text-allow-overlap": false,
            "text-padding": 6,
        },
        paint: {
            "text-color": "#e2e8f0",
            "text-halo-color": "#0f172a",
            "text-halo-width": 1.5,
            "text-opacity": 0.92,
        },
    });

    map.addLayer({
        id: "belarus-city-districts-labels",
        type: "symbol",
        source: "belarus-districts-geojson",
        filter: ["==", ["get", "kind"], "city_district"],
        minzoom: 9.5,
        layout: {
            "text-field": ["get", "name_ru"],
            "text-size": ["interpolate", ["linear"], ["zoom"], 9.5, 10, 13, 13],
            "text-font": ["Open Sans Bold", "Arial Unicode MS Bold"],
            "text-allow-overlap": false,
            "text-padding": 5,
        },
        paint: {
            "text-color": "#fdba74",
            "text-halo-color": "#0f172a",
            "text-halo-width": 1.8,
            "text-opacity": 0.95,
        },
    });

    map.addLayer({
        id: "belarus-cities-labels",
        type: "symbol",
        source: "belarus-districts-geojson",
        filter: ["==", ["get", "kind"], "city_fallback"],
        minzoom: 9.5,
        layout: {
            "text-field": ["get", "name_ru"],
            "text-size": ["interpolate", ["linear"], ["zoom"], 9.5, 10, 13, 12],
            "text-font": ["Open Sans Regular", "Arial Unicode MS Regular"],
            "text-allow-overlap": false,
            "text-padding": 6,
        },
        paint: {
            "text-color": "#cbd5e1",
            "text-halo-color": "#0f172a",
            "text-halo-width": 1.4,
            "text-opacity": 0.9,
        },
    });

    // Подсказка при наведении на район / город
    const districtFillLayers = [
        "belarus-city-districts-fill",
        "belarus-rayons-fill",
        "belarus-cities-fill",
    ].filter((layerId) => map.getLayer(layerId));

    map.on("mousemove", (e) => {
        if (isAreaDrawing) {
            districtPopup.remove();
            hoverPopup.remove();
            return;
        }
        if (!districtPopup) return;
        const buildingHit = map.queryRenderedFeatures(e.point, {
            layers: ["cadastral-buildings-footprint", "cadastral-markers-circle"],
        });
        if (buildingHit.length > 0) {
            districtPopup.remove();
            return;
        }
        const districtHit = map.queryRenderedFeatures(e.point, {
            layers: districtFillLayers,
        });
        if (!districtHit.length) {
            districtPopup.remove();
            return;
        }
        const props = districtHit[0].properties || {};
        const title = props.name_ru || props.shapeName || "Район";
        let kindLabel = "Район области";
        if (props.kind === "city_district") {
            kindLabel = "Район города";
            if (props.city_ru) kindLabel += ` • ${props.city_ru}`;
        } else if (props.kind === "city" || props.kind === "city_fallback") {
            kindLabel = "Город областного подчинения";
        } else if (props.region_ru) {
            kindLabel += ` • ${props.region_ru}`;
        }
        districtPopup
            .setLngLat(e.lngLat)
            .setHTML(
                `<div>
                    <strong>${escapeHtml(title)}</strong><br/>
                    <span style="color:#cbd5e1; font-size:11px;">${escapeHtml(kindLabel)}</span>
                </div>`
            )
            .addTo(map);
    });

    // Наведение курсора на кадастровые здания
    const interactiveLayers = ["cadastral-buildings-footprint", "cadastral-markers-circle"];
    interactiveLayers.forEach((layerId) => {
        map.on("mouseenter", layerId, (e) => {
            if (isAreaDrawing) return;
            map.getCanvas().style.cursor = "pointer";
            if (e.features && e.features.length > 0) {
                const props = e.features[0].properties;
                const title = props.name || props.label || "Капитальное строение";
                const addr = props.full_address || [props.address_street, props.address_housenumber].filter(Boolean).join(", ");
                const orgCount = props.org_count ?? 0;
                hoverPopup
                    .setLngLat(e.lngLat)
                    .setHTML(
                        `<div>
                            <strong>${escapeHtml(title)}</strong><br/>
                            ${addr ? `<span style="color:#cbd5e1">${escapeHtml(addr)}</span><br/>` : ""}
                            <span style="color:#34d399">Организаций внутри: <b>${orgCount}</b> (нажмите для просмотра)</span>
                        </div>`
                    )
                    .addTo(map);
            }
        });

        map.on("mouseleave", layerId, () => {
            if (isAreaDrawing) return;
            map.getCanvas().style.cursor = "";
            hoverPopup.remove();
        });
    });

    // Универсальный клик по карте:
    // 1) Сначала проверяем точное попадание клика в 2D-основание зарегистрированного здания
    // 2) Только на обзорных масштабах (< 15) проверяем клик по точечному маркеру
    // 3) Если кликнули по ЛЮБОМУ другому зданию на карте Беларуси — инспектируем его точно по координатам клика!
    map.on("click", async (e) => {
        if (isAreaDrawing || Date.now() < areaIgnoreClickUntil) return;
        const clickLon = e.lngLat.lng;
        const clickLat = e.lngLat.lat;

        const hitFootprints = map.queryRenderedFeatures(e.point, {
            layers: ["cadastral-buildings-footprint"],
        });

        // Ищем здание, внутри полигона которого реально находится точка клика (clickLon, clickLat)
        const exactFootprint = hitFootprints.find(
            (f) => f.geometry && isPointInsideGeoJSON(clickLon, clickLat, f.geometry)
        ) || hitFootprints[0];

        if (exactFootprint && exactFootprint.properties && exactFootprint.properties.id) {
            await selectBuildingById(exactFootprint.properties.id, false);
            return;
        }

        if (map.getZoom() < 15.2) {
            const hitMarkers = map.queryRenderedFeatures(e.point, {
                layers: ["cadastral-markers-circle"],
            });
            if (hitMarkers.length > 0 && hitMarkers[0].properties.id) {
                await selectBuildingById(hitMarkers[0].properties.id, true);
                return;
            }
        }

        // Проверяем векторный слой зданий базовой карты (OpenFreeMap) СТРОГО по вхождению точки клика в полигон здания.
        // Клик по пустому месту, дороге, парку или воде игнорируется и не создает никаких объектов!
        const allRendered = map.queryRenderedFeatures(e.point);
        const osmBuildingFeature = allRendered.find((f) => {
            if (!f.geometry || (f.geometry.type !== "Polygon" && f.geometry.type !== "MultiPolygon")) {
                return false;
            }
            const srcLayer = (f.sourceLayer || "").toLowerCase();
            const lid = (f.layer?.id || "").toLowerCase();
            const isBuildingLayer =
                srcLayer.includes("building") ||
                lid.includes("building") ||
                (f.properties && f.properties.building);
            if (!isBuildingLayer) return false;
            return isPointInsideGeoJSON(clickLon, clickLat, f.geometry);
        });

        if (osmBuildingFeature) {
            const props = osmBuildingFeature.properties || {};
            await inspectBuildingAtCoords(clickLat, clickLon, props, osmBuildingFeature.geometry);
        }
    });
}

function isPointInRing(lon, lat, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const xi = ring[i][0], yi = ring[i][1];
        const xj = ring[j][0], yj = ring[j][1];
        const intersect =
            yi > lat !== yj > lat &&
            lon < ((xj - xi) * (lat - yi)) / (yj - yi + 1e-12) + xi;
        if (intersect) inside = !inside;
    }
    return inside;
}

function isPointInsideGeoJSON(lon, lat, geometry) {
    if (!geometry || !geometry.coordinates) return false;
    if (geometry.type === "Polygon") {
        return geometry.coordinates[0] && isPointInRing(lon, lat, geometry.coordinates[0]);
    }
    if (geometry.type === "MultiPolygon") {
        return geometry.coordinates.some((poly) => poly[0] && isPointInRing(lon, lat, poly[0]));
    }
    return false;
}

function setAreaSelectionGeometry(geometry) {
    if (!map || !map.getSource("area-selection-source")) return;
    map.getSource("area-selection-source").setData(
        geometry
            ? {
                  type: "FeatureCollection",
                  features: [{ type: "Feature", geometry, properties: {} }],
              }
            : { type: "FeatureCollection", features: [] }
    );
}

function rectangleAreaGeometry(start, end) {
    const [x1, y1] = start;
    const [x2, y2] = end;
    return {
        type: "Polygon",
        coordinates: [[[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]],
    };
}

function buildAreaPreviewGeometry() {
    if (areaDrawMode === "rectangle" && areaRectStart && areaRectEnd) {
        return rectangleAreaGeometry(areaRectStart, areaRectEnd);
    }
    if (areaDrawMode === "polygon") {
        const points = areaPolygonVertices.slice();
        if (areaCursorPoint) points.push(areaCursorPoint);
        if (points.length < 2) return null;
        if (points.length < 3) {
            return { type: "LineString", coordinates: points };
        }
        const ring = points.map((p) => [p[0], p[1]]);
        const first = ring[0];
        const last = ring[ring.length - 1];
        if (first[0] !== last[0] || first[1] !== last[1]) {
            ring.push([first[0], first[1]]);
        }
        return { type: "Polygon", coordinates: [ring] };
    }
    return null;
}

function updateAreaPreview() {
    setAreaSelectionGeometry(buildAreaPreviewGeometry());
}

function normalizedPolygonVertices() {
    const vertices = [];
    for (const point of areaPolygonVertices) {
        const prev = vertices[vertices.length - 1];
        if (!prev || Math.abs(prev[0] - point[0]) > 1e-10 || Math.abs(prev[1] - point[1]) > 1e-10) {
            vertices.push(point);
        }
    }
    if (vertices.length > 1) {
        const first = vertices[0];
        const last = vertices[vertices.length - 1];
        if (Math.abs(first[0] - last[0]) < 1e-10 && Math.abs(first[1] - last[1]) < 1e-10) {
            vertices.pop();
        }
    }
    return vertices;
}

function setAreaModeActive(mode) {
    document.querySelectorAll("[data-area-mode]").forEach((btn) => {
        btn.classList.toggle("active", Boolean(mode) && btn.dataset.areaMode === mode);
    });
}

function setAreaModeButtonsDisabled(disabled) {
    document.querySelectorAll("[data-area-mode]").forEach((btn) => {
        btn.disabled = disabled;
    });
}

function showAreaHint(text) {    const hint = document.getElementById("area-draw-hint");
    if (!hint) return;
    hint.textContent = text;
    hint.classList.remove("hidden");
}

function hideAreaHint() {
    const hint = document.getElementById("area-draw-hint");
    if (hint) hint.classList.add("hidden");
}

function restoreAreaMapInteractions() {
    if (!map) return;
    map.dragPan.enable();
    map.boxZoom.enable();
    map.doubleClickZoom.enable();
    map.getCanvas().style.cursor = "";
}

function startAreaDrawing(mode) {
    if (!map || isAreaDrawing) return;
    areaImportRequestId += 1;
    isAreaDrawing = true;
    areaDrawMode = mode;
    areaPreviousPitch = map.getPitch();
    areaRectStart = null;
    areaRectEnd = null;
    areaPolygonVertices = [];
    areaCursorPoint = null;
    areaIgnoreClickUntil = 0;

    map.dragPan.disable();
    map.boxZoom.disable();
    map.doubleClickZoom.disable();
    map.easeTo({ pitch: 0, duration: 300 });
    map.getCanvas().style.cursor = "crosshair";

    setAreaSelectionGeometry(null);
    document.getElementById("building-drawer").classList.add("hidden");
    document.getElementById("area-stats-panel").classList.add("hidden");
    setAreaModeActive(mode);
    showAreaHint(
        mode === "rectangle"
            ? "Протяните мышью прямоугольник на карте. Esc — отмена."
            : "Кликайте по вершинам полигона. Двойной клик или Enter — завершить, Esc — отмена."
    );
}

function cancelAreaDrawing() {
    areaImportRequestId += 1;
    const wasDrawing = isAreaDrawing;
    isAreaDrawing = false;
    areaDrawMode = null;
    areaRectStart = null;
    areaRectEnd = null;
    areaPolygonVertices = [];
    areaCursorPoint = null;
    restoreAreaMapInteractions();
    if (wasDrawing && map) map.easeTo({ pitch: areaPreviousPitch, duration: 300 });
    setAreaSelectionGeometry(null);
    hideAreaHint();
    setAreaModeActive(null);
}

function finishAreaDrawing(geometry) {
    if (!geometry) {
        cancelAreaDrawing();
        return;
    }
    isAreaDrawing = false;
    areaDrawMode = null;
    areaRectStart = null;
    areaRectEnd = null;
    areaPolygonVertices = [];
    areaCursorPoint = null;
    areaIgnoreClickUntil = Date.now() + 450;
    restoreAreaMapInteractions();
    if (map) map.easeTo({ pitch: areaPreviousPitch, duration: 300 });
    setAreaSelectionGeometry(geometry);
    hideAreaHint();
    setAreaModeActive(null);
    runAreaImport(geometry);
}

function finishPolygonDrawing() {
    const vertices = normalizedPolygonVertices();
    if (vertices.length < 3) {
        showAreaHint("Для полигона нужно минимум 3 вершины. Продолжайте кликать по карте.");
        return;
    }
    const ring = vertices.map((p) => [p[0], p[1]]);
    ring.push([vertices[0][0], vertices[0][1]]);
    finishAreaDrawing({ type: "Polygon", coordinates: [ring] });
}

function registerAreaDrawingEvents() {
    if (!map) return;

    map.on("mousedown", (e) => {
        if (!isAreaDrawing || areaDrawMode !== "rectangle") return;
        areaRectStart = [e.lngLat.lng, e.lngLat.lat];
        areaRectEnd = [e.lngLat.lng, e.lngLat.lat];
        updateAreaPreview();
    });

    map.on("mousemove", (e) => {
        if (!isAreaDrawing) return;
        if (areaDrawMode === "rectangle" && areaRectStart) {
            areaRectEnd = [e.lngLat.lng, e.lngLat.lat];
            updateAreaPreview();
        } else if (areaDrawMode === "polygon" && areaPolygonVertices.length) {
            areaCursorPoint = [e.lngLat.lng, e.lngLat.lat];
            updateAreaPreview();
        }
    });

    map.on("mouseup", (e) => {
        if (!isAreaDrawing || areaDrawMode !== "rectangle" || !areaRectStart) return;
        areaRectEnd = [e.lngLat.lng, e.lngLat.lat];
        const width = Math.abs(areaRectEnd[0] - areaRectStart[0]);
        const height = Math.abs(areaRectEnd[1] - areaRectStart[1]);
        if (width < 1e-5 || height < 1e-5) {
            showAreaHint("Слишком маленькая область. Протяните прямоугольник большего размера.");
            areaRectStart = null;
            areaRectEnd = null;
            return;
        }
        finishAreaDrawing(rectangleAreaGeometry(areaRectStart, areaRectEnd));
    });

    map.on("click", (e) => {
        if (!isAreaDrawing || areaDrawMode !== "polygon") return;
        areaPolygonVertices.push([e.lngLat.lng, e.lngLat.lat]);
        areaCursorPoint = null;
        updateAreaPreview();
    });

    map.on("dblclick", (e) => {
        if (!isAreaDrawing || areaDrawMode !== "polygon") return;
        if (e.preventDefault) e.preventDefault();
        finishPolygonDrawing();
    });

    document.addEventListener("keydown", (e) => {
        if (!isAreaDrawing) return;
        if (e.key === "Enter" && areaDrawMode === "polygon") {
            e.preventDefault();
            finishPolygonDrawing();
        } else if (e.key === "Escape") {
            e.preventDefault();
            cancelAreaDrawing();
        }
    });
}

async function runAreaImport(geometry) {
    const panel = document.getElementById("area-stats-panel");
    const sub = document.getElementById("area-stats-sub");
    const body = document.getElementById("area-stats-body");
    const requestId = ++areaImportRequestId;

    document.getElementById("building-drawer").classList.add("hidden");
    panel.classList.remove("hidden");
    sub.textContent = "Импорт объектов и расчёт статистики...";
    body.innerHTML = '<div class="area-stats-loading">Загрузка данных OpenStreetMap и ЕГР РБ...</div>';
    setAreaModeButtonsDisabled(true);

    try {
        const resp = await fetch("/api/v1/areas/import-and-stats", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ geometry }),
        });
        if (requestId !== areaImportRequestId) return;
        if (!resp.ok) {
            let message = "Не удалось выполнить импорт области";
            try {
                const errorPayload = await resp.json();
                if (errorPayload && errorPayload.detail) {
                    message =
                        typeof errorPayload.detail === "string"
                            ? errorPayload.detail
                            : JSON.stringify(errorPayload.detail);
                }
            } catch (err) {
                console.debug("Ответ с ошибкой не является JSON:", err);
            }
            if (requestId !== areaImportRequestId) return;
            renderAreaError(message);
            return;
        }
        const stats = await resp.json();
        if (requestId !== areaImportRequestId) return;
        renderAreaStats(stats);
        await refreshGeoJSONSources();
        await loadHeaderStats();
        await loadCatalogBuildings();
    } catch (err) {
        console.error("Ошибка импорта выделенной области:", err);
        if (requestId === areaImportRequestId) {
            renderAreaError(
                "Сетевая ошибка при импорте области. Проверьте соединение и повторите попытку."
            );
        }
    } finally {
        setAreaModeButtonsDisabled(false);
    }
}

function renderAreaError(message) {
    document.getElementById("area-stats-panel").classList.remove("hidden");
    document.getElementById("area-stats-sub").textContent = "Ошибка импорта";
    document.getElementById("area-stats-body").innerHTML =
        `<div class="area-warning">${escapeHtml(message)}</div>`;
}

function formatNumber(value) {
    return Number(value || 0).toLocaleString("ru-RU");
}

function formatAreaKm2(value) {
    return Number(value || 0).toLocaleString("ru-RU", { maximumFractionDigits: 3 });
}

function renderAreaDict(title, data) {
    const entries = Object.entries(data || {}).sort((a, b) => Number(b[1]) - Number(a[1]));
    const content = entries.length
        ? `<div class="area-list">${entries
              .map(
                  ([key, value]) =>
                      `<div class="area-list-row"><span>${escapeHtml(key)}</span><strong>${formatNumber(
                          value
                      )}</strong></div>`
              )
              .join("")}</div>`
        : '<div class="area-empty">Нет данных</div>';
    return `<section class="area-section"><h3>${escapeHtml(title)}</h3>${content}</section>`;
}

function renderAreaKv(rows) {
    return rows
        .map(
            ([label, value]) =>
                `<div class="area-kv"><span>${escapeHtml(label)}</span><strong>${escapeHtml(
                    String(value)
                )}</strong></div>`
        )
        .join("");
}

function renderAreaStats(stats) {
    const counters = stats.import_stats || {};
    const scopeLabel = stats.data_scope === "full" ? "полные данные" : "частичные данные";
    document.getElementById("area-stats-sub").textContent =
        `${formatAreaKm2(stats.area_km2)} км² • ${scopeLabel}`;

    const warnings = (stats.warnings || [])
        .map((warning) => `<div class="area-warning">${escapeHtml(warning)}</div>`)
        .join("");

    const body = document.getElementById("area-stats-body");
    body.innerHTML = `
        <div class="area-stat-grid">
            <div class="area-stat-card"><span>Площадь области</span><strong>${formatAreaKm2(
                stats.area_km2
            )} км²</strong></div>
            <div class="area-stat-card"><span>Зданий в БД</span><strong>${formatNumber(
                stats.buildings_total
            )}</strong></div>
            <div class="area-stat-card"><span>Организаций</span><strong>${formatNumber(
                stats.organizations_total
            )}</strong></div>
            <div class="area-stat-card"><span>ЕГР: действующие</span><strong>${formatNumber(
                stats.egr_active
            )}</strong></div>
        </div>
        ${warnings ? `<div class="area-warning-list">${warnings}</div>` : ""}
        <section class="area-section">
            <h3>Импорт из OpenStreetMap</h3>
            ${renderAreaKv([
                ["Добавлено зданий", formatNumber(counters.buildings_added)],
                ["Обновлено зданий", formatNumber(counters.buildings_updated)],
                ["Добавлено организаций", formatNumber(counters.organizations_added)],
                ["Запрошено тайлов OSM", formatNumber(counters.tiles_requested)],
                ["Успешно загружено тайлов", formatNumber(counters.tiles_ok)],
                ["Ошибок загрузки тайлов", formatNumber(counters.tiles_failed)],
            ])}
        </section>
        <section class="area-section">
            <h3>Здания</h3>
            ${renderAreaKv([
                ["Всего зданий", formatNumber(stats.buildings_total)],
                ["С организациями", formatNumber(stats.buildings_with_organizations)],
                ["Без организаций", formatNumber(stats.buildings_without_organizations)],
            ])}
            <div class="area-list">
                ${Object.entries(stats.buildings_by_type || {})
                    .sort((a, b) => Number(b[1]) - Number(a[1]))
                    .map(
                        ([key, value]) =>
                            `<div class="area-list-row"><span>${escapeHtml(
                                key
                            )}</span><strong>${formatNumber(value)}</strong></div>`
                    )
                    .join("")}
            </div>
        </section>
        <section class="area-section">
            <h3>Организации по источникам</h3>
            ${renderAreaKv([
                ["Всего организаций", formatNumber(stats.organizations_total)],
                ["OpenStreetMap", formatNumber(stats.organizations_osm)],
                ["ЕГР РБ", formatNumber(stats.organizations_egr)],
                ["Прочие источники", formatNumber(stats.organizations_other)],
            ])}
        </section>
        ${renderAreaDict("Организации по категориям", stats.organizations_by_category)}
        <section class="area-section">
            <h3>Статусы ЕГР РБ</h3>
            ${renderAreaKv([
                ["Действующие", formatNumber(stats.egr_active)],
                ["Исключённые", formatNumber(stats.egr_excluded)],
                ["Прочие статусы", formatNumber(stats.egr_other)],
            ])}
        </section>
        ${renderAreaDict("Здания по городам", stats.buildings_by_city)}
        ${renderAreaDict("Здания по районам", stats.buildings_by_district)}
    `;
}

async function selectBuildingById(buildingId, flyToBuilding = true) {
    try {
        const resp = await fetch(`/api/v1/buildings/${buildingId}`);
        if (!resp.ok) return;
        const building = await resp.json();
        renderBuildingDrawer(building, flyToBuilding);
    } catch (err) {
        console.error("Ошибка загрузки здания:", err);
    }
}

async function inspectBuildingAtCoords(lat, lon, props = {}, geometry = null) {
    try {
        const payload = {
            lat,
            lon,
            name: props.name || props["name:ru"] || null,
            street: props["addr:street"] || props.street || null,
            housenumber: props["addr:housenumber"] || props.housenumber || null,
            city: props["addr:city"] || props.city || null,
            building_type: props.building || props.class || null,
            osm_id: null,
            geometry: geometry,
        };

        const resp = await fetch("/api/v1/buildings/inspect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (!resp.ok) return;
        const building = await resp.json();
        renderBuildingDrawer(building, false);
        await refreshGeoJSONSources();
        await loadHeaderStats();
    } catch (err) {
        console.error("Ошибка инспекции здания на карте:", err);
    }
}

async function refreshGeoJSONSources() {
    if (!map) return;
    try {
        const buildingsGeoJSON = await fetch("/api/v1/geojson/buildings").then((r) => r.json());
        const bSrc = map.getSource("cadastral-buildings-geojson");
        if (bSrc) bSrc.setData(buildingsGeoJSON);
    } catch (e) {
        console.debug("Обновление слоев пропущено:", e);
    }
}

function renderBuildingDrawer(building, shouldFly = true) {
    currentBuilding = building;
    selectedCategoryFilter = "ALL";
    document.getElementById("inner-org-search").value = "";

    const areaPanel = document.getElementById("area-stats-panel");
    if (areaPanel) areaPanel.classList.add("hidden");

    const drawer = document.getElementById("building-drawer");
    drawer.classList.remove("hidden");

    // Подсветка выбранного полигона здания на карте
    if (map && map.getSource("selected-building-source") && building.geometry) {
        map.getSource("selected-building-source").setData({
            type: "FeatureCollection",
            features: [
                {
                    type: "Feature",
                    geometry: building.geometry,
                    properties: { id: building.id },
                },
            ],
        });
    }

    if (shouldFly && map) {
        map.flyTo({
            center: [building.center_lon, building.center_lat],
            zoom: Math.max(map.getZoom(), 16.2),
            pitch: is3DEnabled ? 50 : 0,
            duration: 1100,
        });
    }

    document.getElementById("b-type-badge").textContent =
        BUILDING_TYPE_LABELS[building.building_type] || "Капитальное строение";
    document.getElementById("b-city-badge").textContent = formatCityLabel(building.address_city);
    document.getElementById("b-title").textContent = building.name || building.full_address;
    document.getElementById("b-address").textContent = building.full_address;

    document.getElementById("b-metric-orgs").textContent = String(building.organizations_count);
    document.getElementById("b-metric-floors").textContent =
        building.floors > 0 ? `${building.floors} эт.` : "—";
    document.getElementById("b-metric-area").textContent = `${building.area_sqm.toLocaleString("ru-RU")} м²`;
    document.getElementById("tab-org-count").textContent = String(building.organizations_count);

    renderCategoryChips(building);
    renderOrganizationsList();
    renderObjectPassport(building);
}

function renderCategoryChips(building) {
    const container = document.getElementById("org-category-chips");
    container.innerHTML = "";

    const allChip = document.createElement("button");
    allChip.className = `cat-chip ${selectedCategoryFilter === "ALL" ? "active" : ""}`;
    allChip.textContent = `Все (${building.organizations_count})`;
    allChip.onclick = () => {
        selectedCategoryFilter = "ALL";
        renderCategoryChips(building);
        renderOrganizationsList();
    };
    container.appendChild(allChip);

    Object.entries(building.categories_summary || {}).forEach(([cat, count]) => {
        const btn = document.createElement("button");
        btn.className = `cat-chip ${selectedCategoryFilter === cat ? "active" : ""}`;
        btn.textContent = `${cat} (${count})`;
        btn.onclick = () => {
            selectedCategoryFilter = cat;
            renderCategoryChips(building);
            renderOrganizationsList();
        };
        container.appendChild(btn);
    });
}

function renderOrganizationsList() {
    const listEl = document.getElementById("organizations-list");
    listEl.innerHTML = "";

    if (!currentBuilding) return;

    const filterText = (document.getElementById("inner-org-search").value || "").trim().toLowerCase();
    const orgs = (currentBuilding.organizations || []).filter((org) => {
        if (selectedCategoryFilter !== "ALL" && org.category !== selectedCategoryFilter) {
            return false;
        }
        if (!filterText) return true;
        const hay = [
            org.name,
            org.category,
            org.subcategory,
            org.description,
            org.floor,
            org.room,
            org.unp,
            org.phone,
        ]
            .filter(Boolean)
            .join(" ")
            .toLowerCase();
        return hay.includes(filterText);
    });

    if (orgs.length === 0) {
        listEl.innerHTML = `
            <div style="text-align:center; padding: 28px 12px; color: var(--text-muted); font-size: 13px;">
                Организации по заданному фильтру не найдены.
            </div>
        `;
        return;
    }

    orgs.forEach((org) => {
        const card = document.createElement("div");
        card.className = "org-card";

        const floorRoom = [org.floor, org.room].filter(Boolean).join(", ") || "этаж не указан";
        const metaSource = (org.metadata_json && org.metadata_json.source) || "OpenStreetMap / ЕГР РБ";
        const isEgr = metaSource.toLowerCase().includes("егр");
        const egrStatus = (org.metadata_json && org.metadata_json.status) || "";
        const isExcluded = /исключ|прекращ|ликвид/i.test(egrStatus);
        const sourceTagHtml = isEgr
            ? `<span style="color:${isExcluded ? "#f97316" : "#34d399"}; font-size:10.5px; font-weight:600;">${isExcluded ? "✕" : "✓"} ЕГР РБ: ${escapeHtml(
                  egrStatus || "статус не указан"
              )}</span>`
            : `<span style="color:#94a3b8; font-size:10.5px;">OSM РБ</span>`;
        const websiteHtml = org.website
            ? `<a href="${escapeHtml(org.website)}" target="_blank" rel="noopener noreferrer">${escapeHtml(
                  org.website.replace(/^https?:\/\//, "")
              )}</a>`
            : "—";
        const egrLinkHtml =
            org.unp && /^\d+$/.test(String(org.unp))
                ? `<a class="org-egr-link" href="https://egr.gov.by/egrmobile/information?pan=${encodeURIComponent(
                      org.unp
                  )}" target="_blank" rel="noopener noreferrer">Доп. информация ↗</a>`
                : "";

        card.innerHTML = `
            <div class="org-card-top">
                <div class="org-name">${escapeHtml(org.name)}</div>
                <span class="org-floor-badge">${escapeHtml(floorRoom)}</span>
            </div>
            <div class="org-cat-line" style="display:flex; justify-content:space-between; align-items:center;">
                <span>${escapeHtml(org.category)}</span>
                ${sourceTagHtml}
            </div>
            ${org.description ? `<div class="org-desc">${escapeHtml(org.description)}</div>` : ""}
            <div class="org-details-grid">
                <div class="org-detail-item">Режим: <strong>${escapeHtml(org.opening_hours || "—")}</strong></div>
                <div class="org-detail-item">УНП: <strong>${escapeHtml(org.unp || "—")}</strong></div>
                <div class="org-detail-item">Тел.: <strong>${escapeHtml(org.phone || "—")}</strong></div>
                <div class="org-detail-item">Сайт: ${websiteHtml}</div>
                ${egrLinkHtml ? `<div class="org-detail-item org-egr-action">ЕГР: ${egrLinkHtml}</div>` : ""}
            </div>
        `;
        listEl.appendChild(card);
    });
}

function renderObjectPassport(b) {
    const bDl = document.getElementById("building-passport-dl");
    const rows = [
        ["Наименование", b.name || "—"],
        ["Тип строения (OSM)", b.building_type || "—"],
        ["OSM ID", b.osm_id || "—"],
        ["Область / Регион", b.address_region || "—"],
        ["Район", b.address_district || "—"],
        ["Адрес объекта", b.full_address],
        ["Почтовый индекс", b.postal_code || "—"],
        ["Количество этажей", b.floors > 0 ? `${b.floors}` : "—"],
        ["Год постройки", b.year_built ? `${b.year_built} г.` : "—"],
        ["Материал стен", b.wall_material || "—"],
        ["Площадь по контуру OSM", `${b.area_sqm.toLocaleString("ru-RU")} м²`],
        ["Координаты центра", `${b.center_lat.toFixed(5)}°, ${b.center_lon.toFixed(5)}°`],
        ["Источник данных", "OpenStreetMap (ODbL)"],
    ];

    bDl.innerHTML = rows
        .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`)
        .join("");
}

async function loadHeaderStats() {
    try {
        const resp = await fetch("/api/v1/stats");
        if (!resp.ok) return;
        const stats = await resp.json();
        document.getElementById("stat-buildings").textContent = stats.total_buildings;
        document.getElementById("stat-orgs").textContent = stats.total_organizations;
    } catch (e) {
        console.error("Не удалось загрузить статистику:", e);
    }
}

async function loadCatalogBuildings(city = "") {
    try {
        const url = city
            ? `/api/v1/geojson/buildings?city=${encodeURIComponent(city)}`
            : "/api/v1/geojson/buildings";
        const resp = await fetch(url);
        if (!resp.ok) return;
        const fc = await resp.json();
        allCatalogBuildings = fc.features || [];
        renderCatalogList();
    } catch (e) {
        console.error("Ошибка загрузки реестра:", e);
    }
}

function renderCatalogList() {
    const listEl = document.getElementById("catalog-list");
    listEl.innerHTML = "";

    allCatalogBuildings.forEach((feat) => {
        const p = feat.properties;
        const card = document.createElement("div");
        card.className = "catalog-card";
        card.innerHTML = `
            <div class="catalog-card-title">${escapeHtml(p.name)}</div>
            <div class="catalog-card-meta">
                <span>${escapeHtml(formatCityLabel(p.address_city))}</span>
                <span style="color:#34d399; font-weight:600;">Орг.: ${p.org_count}</span>
            </div>
        `;
        card.onclick = () => {
            selectBuildingById(p.id, true);
        };
        listEl.appendChild(card);
    });
}

function initUIHandlers() {
    // Быстрый перелет по областным центрам Беларуси
    document.querySelectorAll(".city-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".city-btn").forEach((b) => b.classList.remove("active"));
            btn.classList.add("active");

            const lat = parseFloat(btn.dataset.lat);
            const lon = parseFloat(btn.dataset.lon);
            const zoom = parseFloat(btn.dataset.zoom);
            const pitch = parseFloat(btn.dataset.pitch || "0");

            if (map) {
                map.flyTo({
                    center: [lon, lat],
                    zoom,
                    pitch: is3DEnabled ? pitch : 0,
                    duration: 1200,
                });
            }
        });
    });

    // Переключение 3D / 2D режима зданий
    const btn3d = document.getElementById("toggle-3d-btn");
    btn3d.addEventListener("click", () => {
        is3DEnabled = !is3DEnabled;
        btn3d.classList.toggle("active", is3DEnabled);
        if (map) {
            map.easeTo({ pitch: is3DEnabled ? 48 : 0, duration: 600 });
        }
    });

    // Переключение видимости реальных границ районов РБ
    const btnDistricts = document.getElementById("toggle-districts-btn");
    btnDistricts.addEventListener("click", () => {
        isDistrictsVisible = !isDistrictsVisible;
        btnDistricts.classList.toggle("active", isDistrictsVisible);
        const vis = isDistrictsVisible ? "visible" : "none";
        if (map) {
            [
                "belarus-cities-fill",
                "belarus-cities-outline",
                "belarus-rayons-fill",
                "belarus-rayons-outline",
                "belarus-city-districts-fill",
                "belarus-city-districts-outline",
                "belarus-rayons-labels",
                "belarus-city-districts-labels",
                "belarus-cities-labels",
            ].forEach(
                (layerId) => {
                    if (map.getLayer(layerId)) {
                        map.setLayoutProperty(layerId, "visibility", vis);
                    }
                }
            );
        }
    });

    // Панель реестра объектов
    const catalogPanel = document.getElementById("catalog-panel");
    const btnCatalog = document.getElementById("toggle-catalog-btn");
    btnCatalog.addEventListener("click", () => {
        const isHidden = catalogPanel.classList.toggle("hidden");
        btnCatalog.classList.toggle("active", !isHidden);
    });

    document.getElementById("close-catalog-btn").addEventListener("click", () => {
        catalogPanel.classList.add("hidden");
        btnCatalog.classList.remove("active");
    });

    document.getElementById("catalog-city-filter").addEventListener("change", (e) => {
        loadCatalogBuildings(e.target.value);
    });

    // Выделение области и аналитика
    document.querySelectorAll("[data-area-mode]").forEach((btn) => {
        btn.addEventListener("click", () => {
            startAreaDrawing(btn.dataset.areaMode);
        });
    });

    document.getElementById("cancel-area-btn").addEventListener("click", () => {
        document.getElementById("area-stats-panel").classList.add("hidden");
        cancelAreaDrawing();
    });

    document.getElementById("close-area-stats-btn").addEventListener("click", () => {
        document.getElementById("area-stats-panel").classList.add("hidden");
        cancelAreaDrawing();
    });

    // Закрытие карточки здания
    document.getElementById("close-drawer-btn").addEventListener("click", () => {
        document.getElementById("building-drawer").classList.add("hidden");
        if (map && map.getSource("selected-building-source")) {
            map.getSource("selected-building-source").setData({
                type: "FeatureCollection",
                features: [],
            });
        }
    });

    // Переключение вкладок внутри карточки здания
    document.querySelectorAll(".tab-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
            document.querySelectorAll(".tab-content").forEach((c) => {
                c.classList.add("hidden");
                c.classList.remove("active");
            });
            btn.classList.add("active");
            const target = document.getElementById(btn.dataset.tab);
            target.classList.remove("hidden");
            target.classList.add("active");
        });
    });

    // Фильтр организаций внутри открытого здания
    document.getElementById("inner-org-search").addEventListener("input", () => {
        renderOrganizationsList();
    });

    // Сворачивание легенды
    document.getElementById("legend-collapse-btn").addEventListener("click", () => {
        const body = document.getElementById("legend-body");
        const isHidden = body.classList.toggle("hidden");
        document.getElementById("legend-collapse-btn").textContent = isHidden ? "+" : "–";
    });

    // Глобальный поиск по кадастровому номеру, адресу и организациям
    const searchInput = document.getElementById("global-search-input");
    const clearBtn = document.getElementById("search-clear-btn");
    const dropdown = document.getElementById("search-results-dropdown");
    let debounceTimer = null;

    searchInput.addEventListener("input", () => {
        const q = searchInput.value.trim();
        clearBtn.classList.toggle("hidden", q.length === 0);
        clearTimeout(debounceTimer);
        if (q.length === 0) {
            dropdown.classList.add("hidden");
            return;
        }
        debounceTimer = setTimeout(() => executeGlobalSearch(q), 220);
    });

    searchInput.addEventListener("focus", () => {
        if (searchInput.value.trim().length > 0) {
            executeGlobalSearch(searchInput.value.trim());
        }
    });

    clearBtn.addEventListener("click", () => {
        searchInput.value = "";
        clearBtn.classList.add("hidden");
        dropdown.classList.add("hidden");
    });

    document.addEventListener("click", (e) => {
        if (!e.target.closest(".search-container")) {
            dropdown.classList.add("hidden");
        }
    });
}

async function executeGlobalSearch(query) {
    const dropdown = document.getElementById("search-results-dropdown");
    try {
        const resp = await fetch(`/api/v1/search?q=${encodeURIComponent(query)}&limit=15`);
        if (!resp.ok) return;
        const items = await resp.json();

        dropdown.innerHTML = "";
        dropdown.classList.remove("hidden");

        if (items.length === 0) {
            dropdown.innerHTML = `
                <div style="padding: 12px 14px; color: var(--text-muted); font-size: 13px;">
                    Ничего не найдено по запросу «${escapeHtml(query)}»
                </div>
            `;
            return;
        }

        items.forEach((item) => {
            const row = document.createElement("div");
            row.className = "search-result-item";
            const isOrg = item.result_type === "organization";
            row.innerHTML = `
                <div>
                    <div class="search-result-title">${escapeHtml(item.title)}</div>
                    <div class="search-result-sub">${escapeHtml(item.subtitle)}</div>
                </div>
                <span class="search-type-tag ${isOrg ? "tag-organization" : "tag-building"}">
                    ${isOrg ? "Организация" : "Здание"}
                </span>
            `;
            row.onclick = async () => {
                dropdown.classList.add("hidden");
                if (item.building_id) {
                    await selectBuildingById(item.building_id, true);
                }
            };
            dropdown.appendChild(row);
        });
    } catch (e) {
        console.error("Ошибка поиска:", e);
    }
}

function formatCityLabel(city) {
    const value = String(city || "").trim();
    if (!value || value === "—" || value === "-") return "—";
    const lower = value.toLowerCase();
    if (lower === "республика беларусь" || lower === "рб" || lower === "беларусь") return "—";
    if (/(область|район)$/i.test(value)) return "—";
    if (/^(г\.|город)\s/i.test(value)) {
        return value;
    }
    return `г. ${value}`;
}

function escapeHtml(str) {
    return String(str ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
