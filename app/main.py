from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.buildings import router as buildings_router
from app.api.search import router as search_router
from app.api.tiles import router as tiles_router
from app.config import settings
from app.database import init_db

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Публичная интерактивная кадастровая карта Республики Беларусь на базе FastAPI, "
        "PostGIS (MVT Vector Tiles) и MapLibre GL JS с просмотром паспортов зданий и организаций внутри них."
    ),
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tiles_router, prefix="/api/v1")
app.include_router(buildings_router, prefix="/api/v1")
app.include_router(search_router, prefix="/api/v1")

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))
