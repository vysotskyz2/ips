# План реализации веб-приложения «Кадастровая карта Республики Беларусь»

Проект строится полностью на **бесплатных и открытых (Open-Source) технологиях** без платных API-ключей и лимитов, с поддержкой плавного рендеринга всех зданий и объектов на территории РБ.

---

## 1. Технологический стек (100% Free & Open-Source)

* **Бэкенд:** Python 3.12+, **FastAPI**, Uvicorn.
* **База данных и ГИС-ядро:** **PostgreSQL 16 + PostGIS 3.4** (хранение геометрий, пространственные индексы GIST, генерация векторных тайлов `ST_AsMVT` на лету).
* **Доступ к БД:** `SQLAlchemy 2.0 (async)` + `AsyncPG` + `GeoAlchemy2`.
* **Источник геоданных РБ:**
  * Дамп всей Беларуси с **Geofabrik (`belarus-latest.osm.pbf`)** и/или подгрузка реальных данных OSM через **Overpass API** — 100% бесплатно.
  * Утилита/скрипт на базе **`pyosmium` / `Shapely`** для парсинга зданий (`building=*`), земельных участков (`landuse=*`, `boundary=*`) и организаций/POI (`amenity`, `office`, `shop`, `craft`, `name`, `opening_hours` и т.д.).
* **Картографический фронтенд:**
  * **MapLibre GL JS** (бесплатный WebGL-движок для векторных карт, открытый форк Mapbox GL).
  * **Подложка карты (Basemap):** Бесплатные тайлы OpenFreeMap (`https://tiles.openfreemap.org`) и OpenStreetMap — не требуют ключей и регистрации.
  * **UI:** Чистый HTML5 + CSS3 + Vanilla JS (отдается напрямую через FastAPI `StaticFiles`).
* **Инфраструктура:** **Docker & Docker Compose** (для быстрого развертывания PostGIS и приложения).

---

## 2. Архитектура и принцип работы

1. **Отрисовка всех объектов РБ без зависаний браузера:**
   * Передача миллионов полигонов зданий через обычный GeoJSON повесит браузер. Поэтому используется стандарт **MVT (Mapbox Vector Tiles)**.
   * MapLibre запрашивает у FastAPI тайлы по координатам сетки: `GET /api/v1/tiles/cadastral/{z}/{x}/{y}.pbf`.
   * PostGIS за миллисекунды вырезает нужный квадрат по GIST-индексу через `ST_AsMVT` и отдает бинарный буфер (`.pbf`), а видеокарта клиента (WebGL) отрисовывает контуры и 2.5D-экструзию зданий.
2. **Взаимодействие (Клик по зданию -> Содержимое):**
   * При клике на полигон здания на карте фронтенд считывает его `id` из векторного тайла (или делает пространственный запрос по координатам клика) и вызывает `GET /api/v1/buildings/{building_id}`.
   * Бэкенд возвращает полную карточку здания (кадастровый номер, адрес, этажность, площадь, назначение) и список всех организаций, находящихся внутри этого здания.
   * Сбоку открывается информационная панель (Drawer) с характеристиками здания и списком организаций внутри.

---

## 3. Схема базы данных (PostgreSQL + PostGIS)

### Таблица `cadastral_parcels` (Земельные участки / Кадастровые кварталы)
* `id` (BigInt, PK)
* `cadastral_number` (VARCHAR, UNIQUE, индекс) — например, `500000000001001234`.
* `name` (VARCHAR, nullable) — наименование участка / квартала.
* `land_use` (VARCHAR) — назначение земли (жилая, общественно-деловая, коммерческая, промышленная застройка).
* `area_sqm` (Numeric) — площадь участка в м² (`ST_Area(geom::geography)`).
* `geom` (Geometry(MultiPolygon, 4326), GIST-индекс).
* `geom_3857` (Geometry(MultiPolygon, 3857), GIST-индекс).

### Таблица `buildings` (Здания и сооружения)
* `id` (BigInt, PK)
* `osm_id` (BigInt, индекс)
* `cadastral_number` (VARCHAR, UNIQUE, индекс) — кадастровый/инвентарный номер капитального строения.
* `parcel_id` (BigInt, FK -> `cadastral_parcels.id`, nullable)
* `name` (VARCHAR, nullable) — собственное название здания (например, «ТЦ Dana Mall», «Главпочтамт»).
* `building_type` (VARCHAR) — тип (`apartments`, `commercial`, `industrial`, `retail`, `public`, `office`, `university` и т.д.).
* `address_region` (VARCHAR, nullable) — область / район.
* `address_city` (VARCHAR, nullable) — населенный пункт (`addr:city`).
* `address_street` (VARCHAR, nullable) — улица (`addr:street`).
* `address_housenumber` (VARCHAR, nullable) — номер дома (`addr:housenumber`).
* `floors` (Integer, nullable) — этажность (`building:levels`).
* `year_built` (Integer, nullable) — год постройки (`start_date`).
* `area_sqm` (Numeric) — площадь застройки по контуру (`ST_Area(geom::geography)`).
* `center_lat` (Float), `center_lon` (Float) — координаты центроида для быстрого перелета камеры.
* `geom` (Geometry(MultiPolygon, 4326), **GIST-индекс**) — геометрия для поиска и кликов.
* `geom_3857` (Geometry(MultiPolygon, 3857), **GIST-индекс**) — проекция Web Mercator для сверхбыстрой генерации MVT-тайлов.

### Таблица `organizations` (Организации и объекты внутри зданий)
* `id` (BigInt, PK)
* `osm_id` (BigInt, nullable)
* `building_id` (BigInt, FK -> `buildings.id`, B-Tree индекс) — привязка к зданию.
* `name` (VARCHAR, индекс для поиска) — название организации.
* `category` (VARCHAR) — категория деятельности (Торговля, Финансы и банки, Медицина и аптеки, Общепит, Офисы и ИТ, Госучреждения, Образование, Услуги и др.).
* `subcategory` (VARCHAR) — точный тег OSM (`pharmacy`, `bank`, `supermarket`, `it`, `cafe`, `government`...).
* `unp` (VARCHAR, nullable) — УНП организации (при наличии).
* `floor` (VARCHAR, nullable) — этаж/помещение (`level`, `addr:floor`, `addr:unit`).
* `opening_hours` (VARCHAR, nullable) — время работы.
* `phone` (VARCHAR, nullable) — телефон (`contact:phone`, `phone`).
* `website` (VARCHAR, nullable) — сайт (`contact:website`, `website`).
* `metadata_json` (JSONB) — дополнительные теги из OSM (оператор, бренд, email, доступная среда).
* `geom` (Geometry(Point, 4326), GIST-индекс).

---

## 4. Структура проекта

```text
D:\IPS\
├── PLAN.md                     # План реализации проекта
├── README.md                   # Инструкция по запуску и импорту данных РБ
├── docker-compose.yml          # PostgreSQL + PostGIS и сервис FastAPI
├── Dockerfile                  # Сборка контейнера приложения
├── pyproject.toml              # Конфигурация проекта и зависимостей
├── requirements.txt            # Список пакетов Python
├── .env.example                # Шаблон переменных окружения
├── scripts/
│   ├── download_belarus_osm.py # Скачивание belarus-latest.osm.pbf с Geofabrik
│   ├── import_osm_to_postgis.py# Парсинг PBF / Overpass и загрузка в PostGIS
│   └── seed_belarus_data.py    # Начальное наполнение реальными объектами всех областных центров РБ
├── app/
│   ├── __init__.py
│   ├── main.py                 # Точка входа FastAPI, роутеры и статика
│   ├── config.py               # Настройки через pydantic-settings
│   ├── database.py             # AsyncEngine и сессии SQLAlchemy (PostGIS)
│   ├── models/                 # ORM-модели (SQLAlchemy + GeoAlchemy2)
│   │   ├── __init__.py
│   │   ├── parcel.py
│   │   ├── building.py
│   │   └── organization.py
│   ├── schemas/                # Pydantic-схемы ответов API
│   │   ├── __init__.py
│   │   ├── building.py
│   │   └── organization.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── tiles.py            # Генерация векторных тайлов MVT (.pbf) и GeoJSON-слоев
│   │   ├── buildings.py        # Получение паспорта здания, списка организаций, гео-клик
│   │   └── search.py           # Поиск по адресу, кадастровому номеру или названию организации
│   └── services/
│       ├── __init__.py
│       ├── mvt_encoder.py      # Генерация MVT-тайлов (ST_AsMVT в PostGIS + чистый Python MVT-энкодер)
│       ├── cadastral_service.py# Бизнес-логика выборки карточек, гео-поиска и синхронизации OSM
│       └── seed_data.py        # Реестр ключевых зданий, участков и организаций городов РБ
├── static/
│   ├── index.html              # Главная страница с картой и боковой панелью
│   ├── css/
│   │   └── styles.css          # Стили современной ГИС-системы
│   └── js/
│       └── app.js              # MapLibre GL, MVT-слои, 2D/3D-режимы, поиск и карточка здания
└── tests/
    └── test_api.py             # Автотесты API, MVT-тайлов, поиска и карточек зданий
```

---

## 5. Пошаговый план разработки

1. **Инфраструктура и База данных:** `docker-compose.yml` с PostGIS, настройки FastAPI и SQLAlchemy/GeoAlchemy2.
2. **ETL-пайплайн Беларуси:** Скрипт скачивания дампа `belarus-latest.osm.pbf`, парсер зданий/участков/организаций, пространственная привязка организаций к зданиям (`ST_Contains`), плюс автоматическая предзагрузка опорных кадастровых данных по всем регионам РБ (Минск, Брест, Гродно, Гомель, Витебск, Могилёв) и динамическая подгрузка из OSM Overpass API для любого уголка Беларуси.
3. **Бэкенд (FastAPI):** Эндпоинты MVT-тайлов (`/api/v1/tiles/cadastral/{z}/{x}/{y}.pbf`), карточки зданий (`/api/v1/buildings/{id}`), гео-координатного выбора (`/api/v1/buildings/by-coords`), поиска (`/api/v1/search`) и статистики (`/api/v1/stats`).
4. **Фронтенд (MapLibre GL JS):** Векторная карта РБ, переключение 2D/3D зданий, земельных участков, фильтрация по типам объектов, клик по любому зданию на карте с отображением кадастрового паспорта и всех организаций внутри здания.
5. **Тестирование:** Полное покрытие `pytest` всех маршрутов API и генерации бинарных тайлов `.pbf`.
