from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import delete, func, select, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models.building import Building
from app.models.egr_record import EgrRecord
from app.models.organization import Organization
from app.models.parcel import CadastralParcel


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    await init_db()
    async with AsyncSessionLocal() as session:
        before_buildings = (await session.execute(select(func.count(Building.id)))).scalar_one()
        before_parcels = (
            await session.execute(select(func.count(CadastralParcel.id)))
        ).scalar_one()
        before_orgs = (
            await session.execute(select(func.count(Organization.id)))
        ).scalar_one()
        egr_count = (await session.execute(select(func.count(EgrRecord.id)))).scalar_one()

        await session.execute(delete(Organization))
        await session.execute(delete(Building))
        await session.execute(delete(CadastralParcel))

        if not settings.is_postgres:
            try:
                await session.execute(
                    text(
                        "DELETE FROM sqlite_sequence "
                        "WHERE name IN ('organizations', 'buildings', 'cadastral_parcels')"
                    )
                )
            except Exception:
                pass
        await session.commit()

        print(
            "Очистка кадастровых слоёв завершена:\n"
            f"  - Удалено зданий: {before_buildings}\n"
            f"  - Удалено участков: {before_parcels}\n"
            f"  - Удалено организаций: {before_orgs}\n"
            f"  - Записей ЕГР сохранено для обогащения: {egr_count}"
        )


if __name__ == "__main__":
    asyncio.run(main())
