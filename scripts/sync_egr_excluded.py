from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import AsyncSessionLocal, init_db
from app.services.egr_service import sync_egr_excluded_addresses


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Синхронизация исключённых из ЕГР субъектов и их последних адресов"
    )
    parser.add_argument("--from-year", type=int, default=1990)
    parser.add_argument("--to-year", type=int, default=2026)
    args = parser.parse_args()

    await init_db()
    async with AsyncSessionLocal() as session:
        added, updated = await sync_egr_excluded_addresses(
            session,
            from_year=args.from_year,
            to_year=args.to_year,
        )
    print(
        f"Синхронизация исключённых из ЕГР завершена:\n"
        f"  - Добавлено: {added}\n"
        f"  - Обновлено: {updated}"
    )


if __name__ == "__main__":
    asyncio.run(main())
