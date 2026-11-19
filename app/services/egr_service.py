from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.building import Building
from app.models.egr_record import EgrRecord
from app.models.organization import Organization
from app.services.address_matcher import (
    normalize_city,
    normalize_housenumber,
    normalize_street,
)

logger = logging.getLogger(__name__)

EGR_BASE_URL = "https://egr.gov.by/api/v2/egr"


def is_egr_entity_active(short_info: dict[str, Any]) -> bool:
    """
    Проверяет, что субъект хозяйствования в ЕГР является АКТУАЛЬНЫМ (Действующим):
    - запись не закрыта датой окончания (dto is None)
    - код состояния nsi00219.nksost == 1 ("Действующий").
    Ликвидированные, находящиеся в процессе ликвидации или реорганизованные субъекты отсеиваются.
    """
    if not short_info or short_info.get("dto") is not None:
        return False
    state_obj = short_info.get("nsi00219") or {}
    nksost = state_obj.get("nksost")
    vnsostk = str(state_obj.get("vnsostk") or "").strip().lower()
    return nksost == 1 or vnsostk == "действующий"


def is_egr_address_active(addr_item: dict[str, Any]) -> bool:
    """
    Проверяет, что адрес местонахождения субъекта в ЕГР является АКТУАЛЬНЫМ:
    - признак актуальности cact == '+'
    - отсутствует дата окончания действия адреса (dto is None)
    - указаны населенный пункт, улица и номер дома.
    """
    if not addr_item:
        return False
    if addr_item.get("cact") != "+" or addr_item.get("dto") is not None:
        return False
    city = addr_item.get("vnp") or addr_item.get("vplace")
    street = addr_item.get("vulitsa")
    dom = addr_item.get("vdom")
    return bool(city and street and dom)


def is_egr_go_active(go_info: dict[str, Any]) -> bool:
    """
    Проверяет, что государственный орган / госорганизация в ЕГР является ДЕЙСТВУЮЩЕЙ:
    - cact == '+'
    - cstate is None (не 'П' — прекращение и не 'И' — исключение/упразднение)
    - dto is None.
    """
    if not go_info:
        return False
    if go_info.get("cact") != "+" or go_info.get("dto") is not None:
        return False
    if go_info.get("cstate") is not None:
        return False
    return True


def classify_category_by_oked_or_name(
    name: str, oked_code: str | None = None, oked_name: str | None = None, is_state_org: bool = False
) -> str:
    """Определяет категорию организации по коду ОКЭД РБ и наименованию."""
    if is_state_org:
        return "Госучреждения и почта"

    low_name = (name or "").lower()
    low_oked = (oked_name or "").lower()
    code = (oked_code or "").strip()

    if code.startswith(("64", "65", "66")) or any(
        w in low_name or w in low_oked for w in ("банк", "страх", "финанс", "лизинг", "ломбард")
    ):
        return "Финансы и банки"
    if code.startswith(("62", "63", "58")) or any(
        w in low_name or w in low_oked
        for w in ("программ", "информац", "айти", "софт", "диджитал", "разработк", " компьютер")
    ):
        return "Офисы и ИТ"
    if code.startswith(("45", "46", "47")) or any(
        w in low_name or w in low_oked
        for w in ("торг", "магазин", "ритейл", "маркет", "аптек", "опт")
    ):
        if "аптек" in low_name or "фармац" in low_name or "фарм" in low_oked:
            return "Медицина и аптеки"
        return "Торговля"
    if code.startswith("86") or any(
        w in low_name or w in low_oked
        for w in ("медиц", "клиник", "стоматолог", "больниц", "поликлиник", "здрав")
    ):
        return "Медицина и аптеки"
    if code.startswith("56") or any(
        w in low_name or w in low_oked
        for w in ("ресторан", "кафе", "кофейн", "общепит", "бистро", "бар ", "пицц")
    ):
        return "Общепит"
    if code.startswith("85") or any(
        w in low_name or w in low_oked
        for w in ("образован", "университет", "колледж", "школ", "гимназ", "академ", "институт")
    ):
        return "Образование и наука"
    if code.startswith(("69", "70", "71", "73", "74")) or any(
        w in low_name or w in low_oked
        for w in ("юрид", "адвокат", "нотари", "консалт", "аудит", "бухгалтер", "проект", "инжиниринг")
    ):
        return "Юридические и деловые услуги"
    if code.startswith("84") or any(
        w in low_name
        for w in ("министерств", "комитет", "исполком", "управлен", "инспекц", "суд ", "прокуратур")
    ):
        return "Госучреждения и почта"
    if code.startswith(("90", "91", "93")) or any(
        w in low_name or w in low_oked
        for w in ("музей", "театр", "библиотек", "кино", "культур", "спорт", "фитнес")
    ):
        return "Культура и отдых"
    if code.startswith("55") or any(
        w in low_name or w in low_oked for w in ("гостиниц", "отель", "туризм", "туристич", "санатор")
    ):
        return "Гостиницы и туризм"
    return "Офисы и ИТ"


async def fetch_and_verify_egr_by_unp(
    unp: str | int, client: httpx.AsyncClient | None = None
) -> dict[str, Any] | None:
    """
    Запрашивает актуальные данные о субъекте из egr.gov.by по УНП.
    Возвращает данные ТОЛЬКО если субъект имеет статус 'Действующий' (nksost=1) и актуальный адрес (cact='+').
    """
    unp_str = str(unp).strip()
    if not unp_str.isdigit():
        return None

    own_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=8.0)
        own_client = True

    try:
        short_resp = await client.get(f"{EGR_BASE_URL}/getShortInfoByRegNum/{unp_str}")
        if short_resp.status_code != 200:
            return None
        short_list = short_resp.json()
        if not short_list or not isinstance(short_list, list):
            return None

        # Ищем актуальную запись состояния субъекта
        active_info = next((item for item in short_list if is_egr_entity_active(item)), None)
        if active_info is None:
            return None

        addr_resp = await client.get(f"{EGR_BASE_URL}/getAddressByRegNum/{unp_str}")
        active_addr = None
        if addr_resp.status_code == 200:
            addr_list = addr_resp.json()
            if isinstance(addr_list, list):
                active_addr = next((a for a in addr_list if is_egr_address_active(a)), None)

        oked_code = None
        oked_name = None
        ved_resp = await client.get(f"{EGR_BASE_URL}/getVEDByRegNum/{unp_str}")
        if ved_resp.status_code == 200:
            ved_list = ved_resp.json()
            if isinstance(ved_list, list):
                active_ved = next(
                    (v for v in ved_list if v.get("cact") == "+" and v.get("dto") is None),
                    None,
                )
                if active_ved and active_ved.get("nsi00114"):
                    oked_code = active_ved["nsi00114"].get("vkvdn")
                    oked_name = active_ved["nsi00114"].get("vnvdnp")

        short_name = (
            active_info.get("vn")
            or active_info.get("vfn")
            or active_info.get("vnaim")
            or (f"ИП {active_info['vfio']}" if active_info.get("vfio") else None)
        )
        full_name = active_info.get("vnaim") or short_name
        if not short_name:
            return None

        return {
            "unp": unp_str,
            "short_name": short_name,
            "full_name": full_name,
            "status_label": "Действующий (ЕГР РБ)",
            "oked_code": oked_code,
            "oked_name": oked_name,
            "address": active_addr,
        }
    except Exception as exc:
        logger.debug("Ошибка запроса к egr.gov.by для УНП %s: %s", unp_str, exc)
        return None
    finally:
        if own_client:
            await client.aclose()


async def sync_egr_state_organizations(session: AsyncSession) -> int:
    """
    Загружает из официального API egr.gov.by ВСЕ действующие государственные органы
    и государственные организации Республики Беларусь (getGOInfoAll, getGONameAll, getGOAddressAll, getGOTelAll).
    Отбирает строго актуальные (cact='+', cstate=None, dto=None).
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        info_r, name_r, addr_r, tel_r = (
            await client.get(f"{EGR_BASE_URL}/getGOInfoAll"),
            await client.get(f"{EGR_BASE_URL}/getGONameAll"),
            await client.get(f"{EGR_BASE_URL}/getGOAddressAll"),
            await client.get(f"{EGR_BASE_URL}/getGOTelAll"),
        )
        if info_r.status_code != 200 or name_r.status_code != 200 or addr_r.status_code != 200:
            return 0

        active_unps = {
            str(x["nunp"])
            for x in info_r.json()
            if x.get("nunp") and is_egr_go_active(x)
        }
        names_map = {
            str(x["nunp"]): x
            for x in name_r.json()
            if x.get("nunp")
            and str(x["nunp"]) in active_unps
            and x.get("cact") == "+"
            and x.get("dto") is None
        }
        tels_map = {}
        if tel_r.status_code == 200:
            tels_map = {
                str(x["nunp"]): x
                for x in tel_r.json()
                if x.get("nunp") and x.get("cact") == "+" and x.get("dto") is None
            }

        existing_unps_res = await session.execute(select(EgrRecord.unp))
        existing_unps = {r[0] for r in existing_unps_res.all()}

        added = 0
        for addr in addr_r.json():
            unp_val = str(addr.get("nunp") or "")
            if not unp_val or unp_val not in names_map or unp_val in existing_unps:
                continue
            if not is_egr_address_active(addr):
                continue

            nm = names_map[unp_val]
            tl = tels_map.get(unp_val, {})
            short_name = nm.get("vsname") or nm.get("vfname")
            full_name = nm.get("vfname") or short_name
            if not short_name:
                continue

            city = str(addr.get("vplace") or addr.get("vnp") or "").strip()
            street_type = (addr.get("nsi00226") or {}).get("vntulk") or "ул."
            street = str(addr.get("vulitsa") or "").strip()
            dom = str(addr.get("vdom") or "").strip()
            korp = str(addr.get("vkorp") or "").strip() or None
            pom = str(addr.get("vpom") or "").strip() or None

            n_city = normalize_city(city)
            n_street = normalize_street(street)
            n_house = normalize_housenumber(dom, korp)
            if not n_city or not n_street or not n_house:
                continue

            vvid = (nm.get("nsi00263") or {}).get("vvid") or "Государственный орган / учреждение"
            rec = EgrRecord(
                unp=unp_val,
                short_name=short_name,
                full_name=full_name,
                status_label="Действующий",
                category="Госучреждения и почта",
                oked_code="84110",
                oked_name=vvid,
                postal_code=str(addr.get("vindex") or "") or None,
                region=addr.get("vregion"),
                district=addr.get("vdistrict"),
                city=city,
                street_type=street_type,
                street=street,
                housenumber=dom,
                korpus=korp,
                room_type="пом." if pom else None,
                room=pom,
                phone=tl.get("vtel"),
                email=tl.get("vemail"),
                website=tl.get("vsite"),
                norm_city=n_city,
                norm_street=n_street,
                norm_house=n_house,
            )
            session.add(rec)
            existing_unps.add(unp_val)
            added += 1

        await session.commit()
        return added


async def sync_egr_commercial_period(
    session: AsyncSession, start_date: str, end_date: str
) -> int:
    """
    Загружает из официального API egr.gov.by актуальные коммерческие организации
    за указанный период (DD.MM.YYYY - DD.MM.YYYY).
    Сохраняет ТОЛЬКО действующие организации (cact='+', dto=None, nksost=1 'Действующий').
    """
    async with httpx.AsyncClient(timeout=35.0) as client:
        addr_r = await client.get(f"{EGR_BASE_URL}/getAddressByPeriod/{start_date}/{end_date}")
        if addr_r.status_code != 200:
            return 0
        addr_list = addr_r.json()

        names_r = await client.get(f"{EGR_BASE_URL}/getJurNamesByPeriod/{start_date}/{end_date}")
        names_map: dict[str, dict[str, Any]] = {}
        if names_r.status_code == 200:
            for x in names_r.json():
                if x.get("ngrn") and x.get("cact") == "+" and x.get("dto") is None:
                    names_map[str(x["ngrn"])] = x

        short_r = await client.get(f"{EGR_BASE_URL}/getShortInfoByPeriod/{start_date}/{end_date}")
        inactive_unps: set[str] = set()
        short_active_map: dict[str, dict[str, Any]] = {}
        if short_r.status_code == 200:
            for x in short_r.json():
                u = str(x.get("ngrn") or "")
                if not u:
                    continue
                if is_egr_entity_active(x):
                    short_active_map[u] = x
                else:
                    inactive_unps.add(u)

        ved_r = await client.get(f"{EGR_BASE_URL}/getVEDByPeriod/{start_date}/{end_date}")
        ved_map: dict[str, tuple[str | None, str | None]] = {}
        if ved_r.status_code == 200:
            for x in ved_r.json():
                if x.get("ngrn") and x.get("cact") == "+" and x.get("dto") is None:
                    nsi = x.get("nsi00114") or {}
                    ved_map[str(x["ngrn"])] = (nsi.get("vkvdn"), nsi.get("vnvdnp"))

        existing_unps_res = await session.execute(select(EgrRecord.unp))
        existing_unps = {r[0] for r in existing_unps_res.all()}

        added = 0
        for addr in addr_list:
            if not is_egr_address_active(addr):
                continue
            unp_val = str(addr.get("ngrn") or "")
            if not unp_val or unp_val in existing_unps or unp_val in inactive_unps:
                continue

            # Сохраняем только те записи за период, у которых известно актуальное наименование
            # и подтвержден статус 'Действующий'
            nm_obj = names_map.get(unp_val) or short_active_map.get(unp_val)
            if not nm_obj:
                continue
            short_name = nm_obj.get("vn") or nm_obj.get("vfn") or nm_obj.get("vnaim")
            full_name = nm_obj.get("vnaim") or short_name
            if not short_name:
                continue

            city = str(addr.get("vnp") or "").strip()
            street_type = (addr.get("nsi00226") or {}).get("vntulk") or "ул."
            street = str(addr.get("vulitsa") or "").strip()
            dom = str(addr.get("vdom") or "").strip()
            korp = str(addr.get("vkorp") or "").strip() or None
            room_type = (addr.get("nsi00227") or {}).get("vntpomk") or "пом."
            pom = str(addr.get("vpom") or "").strip() or None

            n_city = normalize_city(city)
            n_street = normalize_street(street)
            n_house = normalize_housenumber(dom, korp)
            if not n_city or not n_street or not n_house:
                continue

            oked_code, oked_name = ved_map.get(unp_val, (None, None))
            cat = classify_category_by_oked_or_name(
                short_name or "", oked_code, oked_name, is_state_org=False
            )

            rec = EgrRecord(
                unp=unp_val,
                short_name=short_name,  # Если None — дозапросится из getShortInfoByRegNum при клике на дом
                full_name=full_name,
                status_label="Действующий",
                category=cat,
                oked_code=oked_code,
                oked_name=oked_name,
                postal_code=str(addr.get("nindex") or "") or None,
                region=addr.get("vregion"),
                district=addr.get("vdistrict"),
                city=city,
                street_type=street_type,
                street=street,
                housenumber=dom,
                korpus=korp,
                room_type=room_type if pom else None,
                room=pom,
                phone=addr.get("vtels"),
                email=addr.get("vemail"),
                website=addr.get("vsite"),
                norm_city=n_city,
                norm_street=n_street,
                norm_house=n_house,
            )
            session.add(rec)
            existing_unps.add(unp_val)
            added += 1

        await session.commit()
        return added


def _excluded_address_score(item: dict[str, Any]) -> tuple[str, str, int]:
    """Приоритет адреса для исключённого субъекта: более поздняя запись важнее."""
    return (
        str(item.get("dfrom") or ""),
        str(item.get("dto") or ""),
        1 if item.get("cact") == "+" else 0,
    )


async def _fetch_excluded_names(client: httpx.AsyncClient) -> dict[str, str]:
    """Возвращает UNP -> наименование для всех исключённых ЮЛ и ИП."""
    names: dict[str, str] = {}
    for endpoint in ("getAllLikvidUL", "getAllLikvidIP"):
        try:
            resp = await client.get(f"{EGR_BASE_URL}/{endpoint}")
        except Exception as exc:
            logger.warning("Не удалось получить %s из ЕГР: %s", endpoint, exc)
            continue
        if resp.status_code != 200:
            continue
        payload = resp.json()
        if not isinstance(payload, list):
            continue
        for item in payload:
            unp = str(item.get("ngrn") or "").strip()
            name = (
                item.get("vnaim")
                or item.get("vn")
                or item.get("vfn")
                or item.get("vfio")
            )
            if unp and name:
                names[unp] = str(name).strip()
    return names


async def sync_egr_excluded_addresses(
    session: AsyncSession,
    from_year: int = 1990,
    to_year: int | None = None,
    auto_commit: bool = True,
) -> tuple[int, int]:
    """
    Синхронизирует исключённые из ЕГР субъекты (ЮЛ и ИП) вместе с их последним
    известным адресом. Адреса берутся из архива getAddressByPeriod по годам.
    Возвращает (добавлено, обновлено).
    """
    if to_year is None:
        to_year = 2026

    added = 0
    updated = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        excluded_names = await _fetch_excluded_names(client)
        if not excluded_names:
            return 0, 0

        existing_res = await session.execute(select(EgrRecord))
        existing_by_unp: dict[str, EgrRecord] = {
            str(record.unp): record for record in existing_res.scalars().all()
        }

        for year in range(from_year, to_year + 1):
            resp = await client.get(
                f"{EGR_BASE_URL}/getAddressByPeriod/01.01.{year}/31.12.{year}"
            )
            if resp.status_code != 200:
                continue
            addresses = resp.json()
            if not isinstance(addresses, list):
                continue

            best_by_unp: dict[str, dict[str, Any]] = {}
            for item in addresses:
                unp = str(item.get("ngrn") or "").strip()
                if unp not in excluded_names:
                    continue
                city = str(item.get("vnp") or item.get("vplace") or "").strip()
                street = str(item.get("vulitsa") or "").strip()
                dom = str(item.get("vdom") or "").strip()
                if not (city and street and dom):
                    continue
                current = best_by_unp.get(unp)
                if current is None or _excluded_address_score(item) > _excluded_address_score(current):
                    best_by_unp[unp] = item

            for unp, addr in best_by_unp.items():
                name = excluded_names.get(unp)
                if not name:
                    continue

                city = str(addr.get("vnp") or addr.get("vplace") or "").strip()
                street_type = (addr.get("nsi00226") or {}).get("vntulk") or "ул."
                street = str(addr.get("vulitsa") or "").strip()
                dom = str(addr.get("vdom") or "").strip()
                korp = str(addr.get("vkorp") or "").strip() or None
                pom = str(addr.get("vpom") or "").strip() or None

                n_city = normalize_city(city)
                n_street = normalize_street(street)
                n_house = normalize_housenumber(dom, korp)
                if not (n_city and n_street and n_house):
                    continue

                room_type = (addr.get("nsi00227") or {}).get("vntpomk") or "пом."
                category = classify_category_by_oked_or_name(name)
                short_name = name[:350]
                full_name = name[:600]

                record = existing_by_unp.get(unp)
                if record is None:
                    record = EgrRecord(
                        unp=unp,
                        short_name=short_name,
                        full_name=full_name,
                        status_label="Исключен из ЕГР",
                        category=category,
                        oked_code=None,
                        oked_name=None,
                        postal_code=str(addr.get("nindex") or addr.get("vindex") or "") or None,
                        region=addr.get("vregion"),
                        district=addr.get("vdistrict"),
                        city=city,
                        street_type=street_type,
                        street=street,
                        housenumber=dom,
                        korpus=korp,
                        room_type=room_type if pom else None,
                        room=pom,
                        phone=addr.get("vtels"),
                        email=addr.get("vemail"),
                        website=addr.get("vsite"),
                        norm_city=n_city,
                        norm_street=n_street,
                        norm_house=n_house,
                    )
                    session.add(record)
                    existing_by_unp[unp] = record
                    added += 1
                else:
                    record.short_name = short_name
                    record.full_name = full_name
                    record.status_label = "Исключен из ЕГР"
                    record.category = category
                    record.postal_code = (
                        str(addr.get("nindex") or addr.get("vindex") or "") or None
                    )
                    record.region = addr.get("vregion")
                    record.district = addr.get("vdistrict")
                    record.city = city
                    record.street_type = street_type
                    record.street = street
                    record.housenumber = dom
                    record.korpus = korp
                    record.room_type = room_type if pom else None
                    record.room = pom
                    record.phone = addr.get("vtels")
                    record.email = addr.get("vemail")
                    record.website = addr.get("vsite")
                    record.norm_city = n_city
                    record.norm_street = n_street
                    record.norm_house = n_house
                    updated += 1

            if auto_commit:
                await session.commit()
            logger.info(
                "ЕГР: обработан %s год, исключённых субъектов в выборке: %s",
                year,
                len(best_by_unp),
            )

    return added, updated


async def attach_egr_records_to_building(
    session: AsyncSession,
    building: Building,
    verify_unnamed_live: bool = False,
    auto_commit: bool = True,
) -> int:
    """
    Находит все актуальные организации из реестра ЕГР (egr.gov.by), которые зарегистрированы
    СТРОГО по адресу данного здания (город + улица + точный номер дома с корпусом),
    проверяет их статус 'Действующий' и привязывает к building.id.
    Исключает путаницу между домами с одинаковым номером на разных улицах или корпусах.
    """
    n_city = normalize_city(building.address_city)
    n_street = normalize_street(building.address_street)
    n_house = normalize_housenumber(building.address_housenumber)

    if not n_city or not n_street or not n_house:
        return 0

    stmt = select(EgrRecord).where(
        EgrRecord.norm_city == n_city,
        EgrRecord.norm_street == n_street,
        EgrRecord.norm_house == n_house,
    )
    res = await session.execute(stmt)
    egr_records = list(res.scalars().all())
    if not egr_records:
        return 0

    existing_orgs_res = await session.execute(
        select(Organization).where(Organization.building_id == building.id)
    )
    existing_orgs = list(existing_orgs_res.scalars().all())
    existing_unps = {o.unp for o in existing_orgs if o.unp}
    existing_names = {o.name.strip().lower() for o in existing_orgs if o.name}

    added_count = 0

    async with httpx.AsyncClient(timeout=6.0) as client:
        for rec in egr_records:
            if rec.unp in existing_unps:
                continue

            # Если у адресной записи ЕГР еще не было подтянуто наименование ЮЛ,
            # проверяем ее актуальность и имя напрямую через egr.gov.by/api/v2/egr/getShortInfoByRegNum/{unp}.
            # При временной недоступности/204 запись НЕ удаляем: синхронизация уже отфильтровала неактивные.
            if not rec.short_name and verify_unnamed_live:
                verified = await fetch_and_verify_egr_by_unp(rec.unp, client=client)
                if verified is not None:
                    rec.short_name = verified["short_name"]
                    rec.full_name = verified["full_name"]
                    rec.oked_code = verified["oked_code"] or rec.oked_code
                    rec.oked_name = verified["oked_name"] or rec.oked_name
                    rec.category = classify_category_by_oked_or_name(
                        rec.short_name, rec.oked_code, rec.oked_name
                    )

            if not rec.short_name:
                continue

            clean_name = rec.short_name.strip()
            if len(clean_name) < 2 or clean_name.lower() in (
                "-",
                "--",
                "—",
                "б/н",
                "нет данных",
                "не указано",
            ):
                continue

            if clean_name.lower() in existing_names:
                continue

            room_display = None
            if rec.room and rec.room not in ("б/н", "-"):
                room_display = f"{rec.room_type or 'пом.'} {rec.room}".strip()

            desc_parts = []
            if rec.oked_name:
                desc_parts.append(f"ОКЭД: {rec.oked_name}")
            if rec.full_name and rec.full_name != rec.short_name:
                desc_parts.append(rec.full_name)

            website = rec.website
            if website and not website.startswith(("http://", "https://")):
                website = f"https://{website}"

            org = Organization(
                building_id=building.id,
                name=clean_name,
                category=rec.category,
                subcategory=rec.oked_code,
                description=" • ".join(desc_parts) or None,
                unp=rec.unp,
                floor=None,
                room=room_display,
                opening_hours=None,
                phone=rec.phone,
                website=website,
                email=rec.email,
                lat=None,
                lon=None,
                metadata_json={
                    "source": "ЕГР Минюста РБ (egr.gov.by)",
                    "status": rec.status_label or "Действующий",
                    "full_name": rec.full_name,
                    "oked_code": rec.oked_code,
                    "oked_name": rec.oked_name,
                },
            )
            session.add(org)
            existing_unps.add(rec.unp)
            existing_names.add(clean_name.lower())
            added_count += 1

    if auto_commit and added_count > 0:
        await session.commit()

    return added_count
