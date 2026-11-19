from __future__ import annotations

import json
import re
from typing import Any

from shapely.geometry import Point, Polygon, shape

STREET_PREFIX_RE = re.compile(
    r"\b("
    r"улица|ул\.?|вуліца|вул\.?|проспект|пр-т\.?|пр\.?|праспект|"
    r"переулок|пер\.?|завулак|зав\.?|бульвар|б-р\.?|"
    r"площадь|пл\.?|плошча|проезд|пр-д\.?|праезд|тракт|шоссе|шаша|"
    r"набережная|наб\.?|набярэжная|тупик|туп\.?|тупік|"
    r"микрорайон|мкр\.?|квартал|кв-л\.?"
    r")\b",
    flags=re.IGNORECASE,
)

CITY_PREFIX_RE = re.compile(
    r"^(\s*(г\.п\.|г\.|гор\.|город|горад|пгт\.?|пос\.|д\.|дер\.|в\.|вёска|аг\.|агрогородок)\s*)+",
    flags=re.IGNORECASE,
)

KORPUS_RE = re.compile(
    r"\s*(?:корп(?:ус)?\.?|к\.?|стр\.?)\s*([0-9а-яa-z]+)",
    flags=re.IGNORECASE,
)

BELARUSIAN_CITY_MAP: dict[str, str] = {
    "мінск": "минск",
    "брэст": "брест",
    "берасце": "брест",
    "гродна": "гродно",
    "гародня": "гродно",
    "віцебск": "витебск",
    "магілеў": "могилев",
    "магілёў": "могилев",
    "бабруйск": "бобруйск",
    "баранавічы": "барановичи",
    "барысаў": "борисов",
    "пінск": "пинск",
    "мазыр": "мозырь",
    "салігорск": "солигорск",
    "наваполацк": "новополоцк",
    "ліда": "лида",
    "маладзечна": "молодечно",
    "полацк": "полоцк",
    "жлобін": "жлобин",
    "светлагорск": "светлогорск",
    "рэчыца": "речица",
    "жодзіна": "жодино",
    "кобрын": "кобрин",
    "ваўкавыск": "волковыск",
    "слонім": "слоним",
    "смаргонь": "сморгонь",
    "рагачоў": "рогачев",
    "асіповічы": "осиповичи",
    "бяроза": "береза",
    "дзяржынск": "дзержинск",
    "заслаўе": "заславль",
    "фаніпаль": "фаниполь",
    "лагойск": "логойск",
    "мачулішчы": "мачулищи",
}

BELARUSIAN_STREET_MAP: dict[str, str] = {
    "незалежнасці": "независимости",
    "пераможцаў": "победителей",
    "пятра мсціслаўца": "петра мстиславца",
    "мсціслаўца": "петра мстиславца",
    "гарадскі вал": "городской вал",
    "валадарскага": "володарского",
    "карла маркса": "карла маркса",
    "к маркса": "карла маркса",
    "няміга": "немига",
    "леніна": "ленина",
    "савецкая": "советская",
    "першамайская": "первомайская",
    "машэрава": "машерова",
    "п машэрава": "машерова",
    "петра машерова": "машерова",
    "янкі купалы": "янки купалы",
    "я купалы": "янки купалы",
    "якуба коласа": "якуба коласа",
    "я коласа": "якуба коласа",
    "гагарына": "гагарина",
    "фрунзэ": "фрунзе",
    "кірава": "кирова",
    "свярдлова": "свердлова",
    "камсамольская": "комсомольская",
    "інтэрнацыянальная": "интернациональная",
    "рэвалюцыйная": "революционная",
    "кастрычніцкая": "октябрьская",
    "прытыцкага": "притыцкого",
    "кальварыйская": "кальварийская",
    "сурганава": "сурганова",
    "багдановіча": "максима богдановича",
    "максіма багдановіча": "максима богдановича",
    "м богдановича": "максима богдановича",
    "богдановича": "максима богдановича",
    "веры харужай": "веры хоружей",
    "в хоружей": "веры хоружей",
    "харужай": "веры хоружей",
    "хоружей": "веры хоружей",
    "куйбышава": "куйбышева",
    "чырвоная": "красная",
    "камуністычная": "коммунистическая",
    "дзяржынскага": "дзержинского",
    "партызанскі": "партизанский",
    "маскоўская": "московская",
    "чыгуначная": "железнодорожная",
    "будаўнікоў": "строителей",
    "маладзежная": "молодежная",
    "маладзёжная": "молодежная",
    "цэнтральная": "центральная",
    "пушкіна": "пушкина",
    "міру": "мира",
    "перамогі": "победы",
    "свабоды": "свободы",
    "ажэшкі": "ожешко",
    "элізы ажэшкі": "ожешко",
    "э ожешко": "ожешко",
    "акадэміка купрэвіча": "академика купревича",
    "купрэвіча": "академика купревича",
    "купревича": "академика купревича",
    "ак купревича": "академика купревича",
    "чырвоназорны": "краснозвездный",
    "красназорны": "краснозвездный",
    "кіжаватава": "кижеватова",
    "дзімітрава": "димитрова",
    "замкавая": "замковая",
    "раманаўская слабада": "романовская слобода",
    "мельнікайтэ": "мельникайте",
    "ціміразева": "тимирязева",
    "сухая": "сухая",
    "калектарная": "коллекторная",
    "яснінская": "яснинская",
    "бегамльская": "бегомльская",
    "арлоўская": "орловская",
    "ружы люксембург": "розы люксембург",
    "максіма горкага": "максима горького",
    "м горкага": "максима горького",
    "горкага": "максима горького",
    "ракаўская": "раковская",
    "вызвалення": "освобождения",
    "інтэрнацыянальная": "интернациональная",
    "камсамольская": "комсомольская",
    "рэвалюцыйная": "революционная",
}

CANONICAL_CITY_DISPLAY: dict[str, str] = {
    "минск": "Минск",
    "брест": "Брест",
    "гродно": "Гродно",
    "гомель": "Гомель",
    "витебск": "Витебск",
    "могилев": "Могилёв",
    "бобруйск": "Бобруйск",
    "барановичи": "Барановичи",
    "борисов": "Борисов",
    "пинск": "Пинск",
    "орша": "Орша",
    "мозырь": "Мозырь",
    "солигорск": "Солигорск",
    "новополоцк": "Новополоцк",
    "лида": "Лида",
    "молодечно": "Молодечно",
    "полоцк": "Полоцк",
    "жлобин": "Жлобин",
    "светлогорск": "Светлогорск",
    "речица": "Речица",
    "жодино": "Жодино",
    "слуцк": "Слуцк",
    "кобрин": "Кобрин",
}


def canonicalize_city_name(city: str | None, default_city: str = "") -> str:
    """Возвращает эталонное русское название города РБ (например, 'Гродна' -> 'Гродно', 'Магілёў' -> 'Могилёв')."""
    norm = normalize_city(city)
    if not norm:
        return default_city
    return CANONICAL_CITY_DISPLAY.get(norm, (city or default_city).strip())

# Динамический кэш переводов улиц из OSM (name:be / name -> name:ru)
DYNAMIC_STREET_ALIASES: dict[str, str] = {}


def register_street_alias(be_or_local_name: str | None, ru_name: str | None) -> None:
    """Регистрирует соответствие белорусского и русского названия улицы из OSM highway/building."""
    if not be_or_local_name or not ru_name:
        return
    k = _clean_street_tokens(be_or_local_name)
    v = _clean_street_tokens(ru_name)
    if k and v and k != v:
        DYNAMIC_STREET_ALIASES[k] = v


def translate_belarusian_street_to_ru(street: str | None) -> str | None:
    """Переводит белорусское название улицы из OSM в стандартное русское название."""
    if not street:
        return street
    s = street.strip()
    s = re.sub(r"\bвуліца\b", "ул.", s, flags=re.IGNORECASE)
    s = re.sub(r"\bпраспект\b", "пр-т", s, flags=re.IGNORECASE)
    s = re.sub(r"\bзавулак\b", "пер.", s, flags=re.IGNORECASE)
    s = re.sub(r"\bплошча\b", "пл.", s, flags=re.IGNORECASE)
    s = re.sub(r"\bшаша\b", "шоссе", s, flags=re.IGNORECASE)
    s = re.sub(r"\bнабярэжная\b", "наб.", s, flags=re.IGNORECASE)
    s = re.sub(r"\bпраезд\b", "пр-д", s, flags=re.IGNORECASE)

    norm_key = _clean_street_tokens(street)
    ru_core = DYNAMIC_STREET_ALIASES.get(norm_key) or BELARUSIAN_STREET_MAP.get(norm_key)
    if ru_core:
        prefix = "ул."
        low = street.lower()
        if "праспект" in low or "проспект" in low or "пр-т" in low:
            prefix = "пр-т"
        elif "завулак" in low or "переулок" in low or "пер." in low:
            prefix = "пер."
        elif "плошча" in low or "площадь" in low or "пл." in low:
            prefix = "пл."
        elif "бульвар" in low or "б-р" in low:
            prefix = "б-р"
        elif "тракт" in low:
            return f"{ru_core.title()} тракт"
        return f"{prefix} {ru_core.title()}"
    return s


def normalize_city(city: str | None) -> str:
    if not city:
        return ""
    s = city.strip().lower().replace("ё", "е")
    s = CITY_PREFIX_RE.sub("", s)
    s = re.sub(r"\s+район.*$", "", s)
    s = re.sub(r"[^0-9a-zа-яіїў\s-]", "", s)
    s = " ".join(s.split())
    return BELARUSIAN_CITY_MAP.get(s, s)


def _clean_street_tokens(street: str) -> str:
    s = street.strip().lower().replace("ё", "е")
    if s in ("адрес не указан", "б/н", "-", "ул. центральная"):
        return ""
    s = STREET_PREFIX_RE.sub(" ", s)
    s = re.sub(r"[.,\"'«»()]", " ", s)
    return " ".join(s.split())


def normalize_street(street: str | None) -> str:
    if not street:
        return ""
    cleaned = _clean_street_tokens(street)
    if not cleaned:
        return ""
    if cleaned in DYNAMIC_STREET_ALIASES:
        cleaned = DYNAMIC_STREET_ALIASES[cleaned]
    if cleaned in BELARUSIAN_STREET_MAP:
        cleaned = BELARUSIAN_STREET_MAP[cleaned]
    return cleaned


def normalize_housenumber(housenumber: str | None, korpus: str | None = None) -> str:
    if not housenumber:
        return ""
    s = str(housenumber).strip().lower().replace("ё", "е")
    # Если передана строка вида "11, ТЦ Бюргер" — берем только номер дома до запятой
    if "," in s:
        s = s.split(",", 1)[0].strip()
    s = re.sub(r"^(д\.|дом)\s*", "", s)

    def _replace_korp(match: re.Match[str]) -> str:
        val = match.group(1)
        if val.isdigit():
            return f"/{val}"
        return val

    s = KORPUS_RE.sub(_replace_korp, s)
    s = re.sub(r"\s+", "", s)
    # Латинские буквы корпуса А, В, С, Е, К, М переводим в кириллицу для единообразия с ЕГР РБ
    lat_to_cyr = str.maketrans("abcekmhoptxy", "авсекмнортху")
    s = s.translate(lat_to_cyr)

    if korpus:
        k = str(korpus).strip().lower().replace("ё", "е").translate(lat_to_cyr)
        k = re.sub(r"^(корп(?:ус)?\.?|к\.?)\s*", "", k).strip()
        if k and k not in ("б/н", "-"):
            if k.isdigit() and not s.endswith(f"/{k}"):
                s = f"{s}/{k}"
            elif not k.isdigit() and not s.endswith(k):
                s = f"{s}{k}"

    return s


def addresses_strictly_match(
    city1: str | None,
    street1: str | None,
    house1: str | None,
    city2: str | None,
    street2: str | None,
    house2: str | None,
    korp2: str | None = None,
) -> bool:
    """
    Строгое сопоставление адресов:
    учитывает город, нормализованное название улицы (с поддержкой бел/рус) и точный номер дома с корпусом.
    Исключает путаницу между домами с одинаковым номером (например, д. 9) на разных улицах
    или между д. 9, д. 9А и д. 9/1.
    """
    c1, c2 = normalize_city(city1), normalize_city(city2)
    if c1 and c2 and c1 != c2:
        return False

    st1, st2 = normalize_street(street1), normalize_street(street2)
    if not st1 or not st2 or st1 != st2:
        return False

    h1 = normalize_housenumber(house1)
    h2 = normalize_housenumber(house2, korp2)
    if not h1 or not h2 or h1 != h2:
        return False

    return True


def has_address_conflict(
    building_city: str | None,
    building_street: str | None,
    building_house: str | None,
    poi_city: str | None,
    poi_street: str | None,
    poi_house: str | None,
    poi_korp: str | None = None,
) -> bool:
    """
    Проверяет, не принадлежит ли организация (POI) ДРУГОМУ зданию по адресным тегам.
    - Здание: пр-т Победителей, 9; POI: ул. Немига, 9 -> КОНФЛИКТ (True)
    - Здание: пр-т Победителей, 9; POI: пр-т Победителей, 9А -> КОНФЛИКТ (True)
    - Здание: пр-т Победителей, 9; POI: пр-т Победителей, 7 -> КОНФЛИКТ (True)
    """
    b_city = normalize_city(building_city)
    p_city = normalize_city(poi_city)
    if b_city and p_city and b_city != p_city:
        return True

    b_street = normalize_street(building_street)
    p_street = normalize_street(poi_street)
    if b_street and p_street and b_street != p_street:
        return True

    b_house = normalize_housenumber(building_house)
    p_house = normalize_housenumber(poi_house, poi_korp)
    if b_house and p_house and b_house != p_house:
        return True

    return False


def extract_single_building_polygon(
    geometry: dict[str, Any] | None, click_lon: float, click_lat: float
) -> dict[str, Any] | None:
    """
    Извлекает ТОЛЬКО тот единственный полигон (Polygon) из MultiPolygon векторного тайла,
    внутри которого находится точка клика (click_lon, click_lat).
    """
    if not geometry or not isinstance(geometry, dict):
        return None

    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None

    pt = Point(click_lon, click_lat)

    if gtype == "Polygon":
        try:
            poly = Polygon(coords[0])
            if poly.buffer(0.00012).contains(pt):
                return {"type": "MultiPolygon", "coordinates": [coords]}
        except Exception:
            pass
        return None

    if gtype == "MultiPolygon":
        best_poly_coords: list[list[list[float]]] | None = None
        best_dist = float("inf")

        for poly_coords in coords:
            if not poly_coords or len(poly_coords[0]) < 3:
                continue
            try:
                poly = Polygon(poly_coords[0])
                if poly.contains(pt):
                    return {"type": "MultiPolygon", "coordinates": [poly_coords]}
                d = poly.distance(pt)
                if d < best_dist:
                    best_dist = d
                    best_poly_coords = poly_coords
            except Exception:
                continue

        if best_poly_coords is not None and best_dist <= 0.00010:
            return {"type": "MultiPolygon", "coordinates": [best_poly_coords]}

    return None


def is_point_inside_building_polygon(
    building_geojson_str: str,
    lon: float,
    lat: float,
    tolerance_deg: float = 0.000035,
) -> bool:
    """
    Проверяет, входит ли точка (lon, lat) непосредственно в контур конкретного здания
    (с минимальным допуском ~3.5 метра на погрешность стен в OSM).
    """
    try:
        geom = shape(json.loads(building_geojson_str))
        pt = Point(lon, lat)
        if geom.contains(pt):
            return True
        return geom.distance(pt) <= tolerance_deg
    except Exception:
        return False
