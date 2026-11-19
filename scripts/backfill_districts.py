from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import AsyncSessionLocal, init_db
from app.models.building import Building
from app.services.geo_locator import resolve_district


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    await init_db()
    async with AsyncSessionLocal() as session:
        buildings = list((await session.execute(select(Building))).scalars().all())
        changed = 0
        for building in buildings:
            district = resolve_district(
                building.center_lat, building.center_lon, building.address_city
            )
            if district != building.address_district:
                building.address_district = district
                changed += 1
        await session.commit()

    print(
        f"Районы обновлены:\n"
        f"  - Зданий обработано: {len(buildings)}\n"
        f"  - Изменено: {changed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
