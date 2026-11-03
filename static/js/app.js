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
let isParcelsVisible = true;
let hoverPopup = null;
let allCatalogBuildings = [];

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

    // 2. GeoJSON-источник земельных участков и зданий для мгновенного отображения и подсветки
    const [parcelsGeoJSON, buildingsGeoJSON] = await Promise.all([
        fetch("/api/v1/geojson/parcels").then((r) => r.json()),
        fetch("/api/v1/geojson/buildings").then((r) => r.json()),
    ]);

    map.addSource("cadastral-parcels-geojson", {
        type: "geojson",
        data: parcelsGeoJSON,
    });

    map.addSource("cadastral-buildings-geojson", {
        type: "geojson",
        data: buildingsGeoJSON,
    });

    map.addSource("selected-building-source", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
    });

    // Слой заливки земельных участков (кадастровых кварталов)
    map.addLayer({
        id: "cadastral-parcels-fill",
        type: "fill",
        source: "cadastral-parcels-geojson",
        paint: {
            "fill-color": "#f59e0b",
            "fill-opacity": 0.14,
        },
    });

    // Слой границ кадастровых участков
    map.addLayer({
        id: "cadastral-parcels-outline",
        type: "line",
        source: "cadastral-parcels-geojson",
        paint: {
            "line-color": "#f59e0b",
            "line-width": 2,
            "line-dasharray": [3, 2],
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

    // Наведение курсора на кадастровые здания
    const interactiveLayers = ["cadastral-buildings-footprint", "cadastral-markers-circle"];
    interactiveLayers.forEach((layerId) => {
        map.on("mouseenter", layerId, (e) => {
            map.getCanvas().style.cursor = "pointer";
            if (e.features && e.features.length > 0) {
                const props = e.features[0].properties;
                const title = props.name || props.label || "Капитальное строение";
                const addr = props.full_address || [props.address_street, props.address_housenumber].filter(Boolean).join(", ");
                const orgCount = props.org_count ?? 0;
                const cadNum = props.cadastral_number || "";
                hoverPopup
                    .setLngLat(e.lngLat)
                    .setHTML(
                        `<div>
                            <strong>${escapeHtml(title)}</strong><br/>
                            ${addr ? `<span style="color:#cbd5e1">${escapeHtml(addr)}</span><br/>` : ""}
                            <span style="color:#94a3b8">Кадастровый №: ${escapeHtml(cadNum)}</span><br/>
                            <span style="color:#34d399">Организаций внутри: <b>${orgCount}</b> (нажмите для просмотра)</span>
                        </div>`
                    )
                    .addTo(map);
            }
        });

        map.on("mouseleave", layerId, () => {
            map.getCanvas().style.cursor = "";
            hoverPopup.remove();
        });
    });

    // Универсальный клик по карте:
    // 1) Сначала проверяем точное попадание клика в 2D-основание зарегистрированного здания
    // 2) Только на обзорных масштабах (< 15) проверяем клик по точечному маркеру
    // 3) Если кликнули по ЛЮБОМУ другому зданию на карте Беларуси — инспектируем его точно по координатам клика!
    map.on("click", async (e) => {
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
            floors: props.render_height ? Math.max(1, Math.round(Number(props.render_height) / 3.5)) : null,
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
        const [parcelsGeoJSON, buildingsGeoJSON] = await Promise.all([
            fetch("/api/v1/geojson/parcels").then((r) => r.json()),
            fetch("/api/v1/geojson/buildings").then((r) => r.json()),
        ]);
        const pSrc = map.getSource("cadastral-parcels-geojson");
        const bSrc = map.getSource("cadastral-buildings-geojson");
        if (pSrc) pSrc.setData(parcelsGeoJSON);
        if (bSrc) bSrc.setData(buildingsGeoJSON);
    } catch (e) {
        console.debug("Обновление слоев пропущено:", e);
    }
}

function renderBuildingDrawer(building, shouldFly = true) {
    currentBuilding = building;
    selectedCategoryFilter = "ALL";
    document.getElementById("inner-org-search").value = "";

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
        BUILDING_TYPE_LABELS[building.building_type] || building.purpose_label || "Капитальное строение";
    document.getElementById("b-city-badge").textContent = formatCityLabel(building.address_city);
    document.getElementById("b-title").textContent = building.name || building.full_address;
    document.getElementById("b-address").textContent = building.full_address;
    document.getElementById("b-cadastral-num").textContent = building.cadastral_number;

    document.getElementById("b-metric-orgs").textContent = String(building.organizations_count);
    document.getElementById("b-metric-floors").textContent = `${building.floors} эт.`;
    document.getElementById("b-metric-area").textContent = `${building.area_sqm.toLocaleString("ru-RU")} м²`;
    document.getElementById("tab-org-count").textContent = String(building.organizations_count);

    renderCategoryChips(building);
    renderOrganizationsList();
    renderCadastralPassport(building);
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

        const floorRoom = [org.floor, org.room].filter(Boolean).join(", ") || "1 этаж";
        const metaSource = (org.metadata_json && org.metadata_json.source) || "OpenStreetMap / ЕГР РБ";
        const isEgr = metaSource.toLowerCase().includes("егр");
        const sourceTagHtml = isEgr
            ? `<span style="color:#34d399; font-size:10.5px; font-weight:600;">✓ ЕГР РБ (Действующий)</span>`
            : `<span style="color:#94a3b8; font-size:10.5px;">OSM РБ</span>`;
        const websiteHtml = org.website
            ? `<a href="${escapeHtml(org.website)}" target="_blank" rel="noopener noreferrer">${escapeHtml(
                  org.website.replace(/^https?:\/\//, "")
              )}</a>`
            : "—";

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
                <div class="org-detail-item">Режим: <strong>${escapeHtml(org.opening_hours || "09:00-18:00")}</strong></div>
                <div class="org-detail-item">УНП: <strong>${escapeHtml(org.unp || "—")}</strong></div>
                <div class="org-detail-item">Тел.: <strong>${escapeHtml(org.phone || "—")}</strong></div>
                <div class="org-detail-item">Сайт: ${websiteHtml}</div>
            </div>
        `;
        listEl.appendChild(card);
    });
}

function renderCadastralPassport(b) {
    const bDl = document.getElementById("building-passport-dl");
    const rows = [
        ["Кадастровый номер", b.cadastral_number],
        ["Инвентарный № ЕГРНИ", b.inventory_number || "—"],
        ["Наименование", b.name || "Капитальное строение"],
        ["Назначение по реестру", b.purpose_label || BUILDING_TYPE_LABELS[b.building_type] || b.building_type],
        ["Область / Регион", b.address_region || "Республика Беларусь"],
        ["Адрес объекта", b.full_address],
        ["Почтовый индекс", b.postal_code || "—"],
        ["Количество этажей", `${b.floors}`],
        ["Год постройки", b.year_built ? `${b.year_built} г.` : "—"],
        ["Материал стен", b.wall_material || "Железобетон / Кирпич"],
        ["Площадь застройки", `${b.area_sqm.toLocaleString("ru-RU")} м²`],
        ["Координаты центра", `${b.center_lat.toFixed(5)}°, ${b.center_lon.toFixed(5)}°`],
    ];

    bDl.innerHTML = rows
        .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`)
        .join("");

    const parcelSection = document.getElementById("parcel-passport-section");
    const pDl = document.getElementById("parcel-passport-dl");

    if (b.parcel) {
        parcelSection.classList.remove("hidden");
        const p = b.parcel;
        const pRows = [
            ["Кадастровый № участка", p.cadastral_number],
            ["Наименование квартала", p.name || "—"],
            ["Район / Регион", [p.region, p.district].filter(Boolean).join(", ") || "—"],
            ["Целевое назначение", p.purpose || p.land_use],
            ["Вид права", p.ownership_type || "Государственная собственность"],
            ["Площадь участка", `${p.area_sqm.toLocaleString("ru-RU")} м²`],
            [
                "Кадастровая стоимость",
                p.cadastral_value_byn
                    ? `${p.cadastral_value_byn.toLocaleString("ru-RU")} BYN`
                    : "По запросу в НКА",
            ],
        ];
        pDl.innerHTML = pRows
            .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`)
            .join("");
    } else {
        parcelSection.classList.add("hidden");
    }
}

async function loadHeaderStats() {
    try {
        const resp = await fetch("/api/v1/stats");
        if (!resp.ok) return;
        const stats = await resp.json();
        document.getElementById("stat-parcels").textContent = stats.total_parcels;
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

    // Переключение видимости земельных участков
    const btnParcels = document.getElementById("toggle-parcels-btn");
    btnParcels.addEventListener("click", () => {
        isParcelsVisible = !isParcelsVisible;
        btnParcels.classList.toggle("active", isParcelsVisible);
        const vis = isParcelsVisible ? "visible" : "none";
        if (map) {
            if (map.getLayer("cadastral-parcels-fill")) {
                map.setLayoutProperty("cadastral-parcels-fill", "visibility", vis);
            }
            if (map.getLayer("cadastral-parcels-outline")) {
                map.setLayoutProperty("cadastral-parcels-outline", "visibility", vis);
            }
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

    // Копирование кадастрового номера
    document.getElementById("copy-cad-btn").addEventListener("click", async () => {
        const num = document.getElementById("b-cadastral-num").textContent;
        try {
            await navigator.clipboard.writeText(num);
            const btn = document.getElementById("copy-cad-btn");
            const prev = btn.textContent;
            btn.textContent = "Скопировано!";
            setTimeout(() => (btn.textContent = prev), 1400);
        } catch (_) {}
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
    if (!value) return "Республика Беларусь";
    if (/^(г\.|город)\s/i.test(value) || /(область|район)$/i.test(value)) {
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
