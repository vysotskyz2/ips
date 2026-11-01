from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import AsyncSessionLocal, init_db
from app.services.cadastral_service import ensure_seed_data, get_stats


async def main() -> None:
    await init_db()
    async with AsyncSessionLocal() as session:
        await ensure_seed_data(session)
        stats = await get_stats(session)
        print(
            f"Кадастровая база РБ готова:\n"
            f"  - Земельных участков: {stats.total_parcels}\n"
            f"  - Капитальных строений (зданий): {stats.total_buildings}\n"
            f"  - Организаций внутри зданий: {stats.total_organizations}\n"
            f"  - Охвачено городов РБ: {stats.cities_covered}"
        )


if __name__ == "__main__":
    asyncio.run(main())
