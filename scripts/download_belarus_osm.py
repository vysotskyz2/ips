from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings


async def download_belarus_pbf(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    url = settings.geofabrik_belarus_pbf_url
    print(f"Скачивание геоданных Республики Беларусь с Geofabrik:\n  URL: {url}\n  Файл: {output_path}")

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            with output_path.open("wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded * 100.0 / total
                        mb_done = downloaded / (1024 * 1024)
                        mb_total = total / (1024 * 1024)
                        print(
                            f"\rЗагружено: {mb_done:.1f} / {mb_total:.1f} МБ ({pct:.1f}%)",
                            end="",
                            flush=True,
                        )
    print(f"\nГотово! Дамп РБ сохранен в {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Скачивание бесплатного дампа OpenStreetMap Республики Беларусь (belarus-latest.osm.pbf)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/belarus-latest.osm.pbf"),
        help="Путь для сохранения файла belarus-latest.osm.pbf",
    )
    args = parser.parse_args()
    asyncio.run(download_belarus_pbf(args.output))


if __name__ == "__main__":
    main()
