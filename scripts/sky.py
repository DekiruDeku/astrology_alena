#!/usr/bin/env python3
"""Собирает факты о небе на один день по эфемеридам NASA JPL Horizons.

Все позиции геоцентрические (видимые эклиптические координаты). Время в отчёте
московское (UTC+3). Скрипт ничего не толкует: только знаки, градусы, фазы
Луны, затмения, смены знаков, развороты планет и точные аспекты.

Отчёт делится на «сегодня» и «не для поста» (прошлое и будущее за пределами
дня), чтобы в пост попадало только то, что происходит сегодня.

Запуск:
    python scripts/sky.py                  # сегодня по Москве
    python scripts/sky.py --date 2026-11-23
Результат: out/sky.json (для card.py) и out/sky.txt (для автора поста).
Код выхода 2, если эфемериды получить не удалось.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"
USER_AGENT = "astro-channel-sky/1.1"
REQUEST_TIMEOUT_SECONDS = 40
REQUEST_ATTEMPTS = 3

MSK = timezone(timedelta(hours=3), "MSK")

# Окно почасовых данных вокруг дня: хватает, чтобы найти вход Луны в текущий
# знак (Луна стоит в знаке до ~2,5 суток) и ближайшие фазы.
HOURLY_BEFORE = timedelta(hours=60)
HOURLY_AFTER = timedelta(hours=72)
# Окно суточных данных для поиска разворотов планет.
STATION_WINDOW_DAYS = 15

PLANET_ASPECT_ORB = 3.0

EARTH_RADIUS_KM = 6378.14
MOON_RADIUS_KM = 1737.4
SUN_RADIUS_KM = 696000.0
SUPERMOON_DISTANCE_KM = 362000.0


@dataclass(frozen=True)
class Body:
    key: str
    horizons_id: str
    name: str


BODIES = (
    Body("sun", "10", "Солнце"),
    Body("moon", "301", "Луна"),
    Body("mercury", "199", "Меркурий"),
    Body("venus", "299", "Венера"),
    Body("mars", "499", "Марс"),
    Body("jupiter", "599", "Юпитер"),
    Body("saturn", "699", "Сатурн"),
    Body("uranus", "799", "Уран"),
    Body("neptune", "899", "Нептун"),
    Body("pluto", "999", "Плутон"),
)
PLANETS_THAT_STATION = tuple(
    body for body in BODIES if body.key not in ("sun", "moon")
)

SIGNS = (
    "Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
    "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы",
)
SIGNS_IN = (
    "в Овне", "в Тельце", "в Близнецах", "в Раке", "во Льве", "в Деве",
    "в Весах", "в Скорпионе", "в Стрельце", "в Козероге", "в Водолее",
    "в Рыбах",
)
SIGNS_INTO = (
    "в Овна", "в Тельца", "в Близнецов", "в Рака", "во Льва", "в Деву",
    "в Весы", "в Скорпиона", "в Стрельца", "в Козерога", "в Водолея",
    "в Рыбы",
)
# Вход Солнца в эти знаки: равноденствия и солнцестояния.
SEASON_POINTS = {
    0: "день весеннего равноденствия",
    3: "день летнего солнцестояния",
    6: "день осеннего равноденствия",
    9: "день зимнего солнцестояния",
}

ASPECTS = {
    0: "соединение",
    60: "секстиль",
    90: "квадрат",
    120: "трин",
    180: "оппозиция",
}
LUNATIONS = {
    0: "новолуние",
    90: "первая четверть",
    180: "полнолуние",
    270: "последняя четверть",
}

MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)
HORIZONS_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
RELATIVE_DAYS = {
    -2: "позавчера",
    -1: "вчера",
    0: "сегодня",
    1: "завтра",
    2: "послезавтра",
}


@dataclass(frozen=True)
class EphemerisRow:
    time: datetime
    longitude: float
    latitude: float
    diameter_arcsec: float | None


Series = list[tuple[datetime, float]]


class SkyDataError(RuntimeError):
    """Эфемериды недоступны или пришли в неожиданном виде."""


# --------------------------------------------------------------------------
# Загрузка данных
# --------------------------------------------------------------------------


def fetch_rows(
    body: Body, start_utc: datetime, stop_utc: datetime, step: str
) -> list[EphemerisRow]:
    """Долгота, широта и угловой диаметр тела с заданным шагом."""
    params = {
        "format": "json",
        "COMMAND": f"'{body.horizons_id}'",
        "OBJ_DATA": "'NO'",
        "MAKE_EPHEM": "'YES'",
        "EPHEM_TYPE": "'OBSERVER'",
        "CENTER": "'500@399'",
        "QUANTITIES": "'13,31'",
        "ANG_FORMAT": "'DEG'",
        "CSV_FORMAT": "'YES'",
        "START_TIME": f"'{start_utc:%Y-%m-%d %H:%M}'",
        "STOP_TIME": f"'{stop_utc:%Y-%m-%d %H:%M}'",
        "STEP_SIZE": f"'{step}'",
    }
    url = f"{HORIZONS_URL}?{urllib.parse.urlencode(params)}"
    payload = _get_json(url)
    rows = _parse_horizons_table(payload.get("result", ""))
    if len(rows) < 2:
        raise SkyDataError(f"Horizons не вернул данных для тела «{body.name}»")
    return rows


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(2 ** attempt)
    raise SkyDataError(f"Не удалось получить эфемериды из Horizons: {last_error}")


def _parse_horizons_table(text: str) -> list[EphemerisRow]:
    if "$$SOE" not in text or "$$EOE" not in text:
        raise SkyDataError("В ответе Horizons нет таблицы эфемерид")
    header_part, rest = text.split("$$SOE", 1)
    columns = _parse_header(header_part)
    try:
        lon_index = columns.index("ObsEcLon")
        lat_index = columns.index("ObsEcLat")
        diameter_index = columns.index("Ang-diam")
    except ValueError as error:
        raise SkyDataError(f"Неожиданные колонки Horizons: {columns}") from error

    rows = []
    for line in rest.split("$$EOE", 1)[0].strip().splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) <= max(lon_index, lat_index, diameter_index):
            continue
        rows.append(EphemerisRow(
            time=_parse_horizons_time(fields[0]),
            longitude=float(fields[lon_index]),
            latitude=float(fields[lat_index]),
            diameter_arcsec=_parse_optional_float(fields[diameter_index]),
        ))
    return rows


def _parse_header(header_part: str) -> list[str]:
    header_lines = [line for line in header_part.splitlines() if "Date__" in line]
    if not header_lines:
        raise SkyDataError("В ответе Horizons нет строки с названиями колонок")
    return [name.strip() for name in header_lines[-1].split(",")]


def _parse_optional_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _parse_horizons_time(value: str) -> datetime:
    # Формат Horizons: "2026-Nov-23 00:00"
    day_part, clock_part = value.split(" ")
    year, month_name, day = day_part.split("-")
    hour, minute = clock_part.split(":")[:2]
    return datetime(
        int(year), HORIZONS_MONTHS[month_name], int(day),
        int(hour), int(minute), tzinfo=timezone.utc,
    )


# --------------------------------------------------------------------------
# Геометрия
# --------------------------------------------------------------------------


def signed_diff(a: float, b: float) -> float:
    """Разница углов a - b в диапазоне [-180, 180)."""
    return (a - b + 180.0) % 360.0 - 180.0


def separation(a: float, b: float) -> float:
    return abs(signed_diff(a, b))


def unwrap(values: list[float]) -> list[float]:
    """Убирает скачки 360 -> 0, чтобы ряд был непрерывным."""
    unwrapped = [values[0]]
    for value in values[1:]:
        unwrapped.append(unwrapped[-1] + signed_diff(value, unwrapped[-1]))
    return unwrapped


def find_crossings(
    times: list[datetime], values: list[float], target: float, period: float
) -> list[datetime]:
    """Моменты, когда непрерывный ряд проходит target + k * period."""
    crossings = []
    for index in range(len(values) - 1):
        first, second = values[index], values[index + 1]
        level_first = math.floor((first - target) / period)
        level_second = math.floor((second - target) / period)
        if level_first == level_second or first == second:
            continue
        level = target + period * max(level_first, level_second)
        fraction = (level - first) / (second - first)
        start, stop = times[index], times[index + 1]
        crossings.append(start + (stop - start) * fraction)
    return crossings


def value_at(series: Series, moment: datetime) -> float:
    """Долгота в момент moment (линейная интерполяция по ряду)."""
    times = [point[0] for point in series]
    index = bisect.bisect_right(times, moment) - 1
    index = max(0, min(index, len(series) - 2))
    (t1, lon1), (t2, lon2) = series[index], series[index + 1]
    fraction = (moment - t1) / (t2 - t1)
    return (lon1 + signed_diff(lon2, lon1) * fraction) % 360.0


def linear_at(rows: list[EphemerisRow], moment: datetime, field: str) -> float:
    """Линейная интерполяция широты или диаметра (без перехода через 360)."""
    times = [row.time for row in rows]
    index = bisect.bisect_right(times, moment) - 1
    index = max(0, min(index, len(rows) - 2))
    first, second = rows[index], rows[index + 1]
    value_first, value_second = getattr(first, field), getattr(second, field)
    if value_first is None or value_second is None:
        raise SkyDataError(f"Нет значения {field} в данных Horizons")
    fraction = (moment - first.time) / (second.time - first.time)
    return value_first + (value_second - value_first) * fraction


def sign_index(longitude: float) -> int:
    return int(longitude // 30) % 12


def degree_in_sign(longitude: float) -> int:
    return int(longitude % 30)


# --------------------------------------------------------------------------
# Форматирование
# --------------------------------------------------------------------------


def format_moment(moment: datetime, day: date) -> str:
    local = moment.astimezone(MSK)
    shift = (local.date() - day).days
    calendar = f"{local.day} {MONTHS_GENITIVE[local.month - 1]}"
    relative = RELATIVE_DAYS.get(shift)
    prefix = f"{relative}, {calendar}" if relative else calendar
    return f"{prefix} в {local:%H:%M}"


def format_date(moment: datetime) -> str:
    local = moment.astimezone(MSK)
    return f"{local.day} {MONTHS_GENITIVE[local.month - 1]}"


def describe_position(longitude: float) -> str:
    return f"{SIGNS[sign_index(longitude)]} {degree_in_sign(longitude)}°"


def msk_date(moment: datetime) -> date:
    return moment.astimezone(MSK).date()


# --------------------------------------------------------------------------
# Расчёт фактов
# --------------------------------------------------------------------------


@dataclass
class SkyData:
    day: date
    day_start: datetime
    noon: datetime
    rows: dict[str, list[EphemerisRow]]
    hourly: dict[str, Series]
    daily: dict[str, Series]

    def is_today(self, moment: datetime) -> bool:
        return msk_date(moment) == self.day


def load_sky_data(day: date) -> SkyData:
    day_start = datetime(day.year, day.month, day.day, tzinfo=MSK).astimezone(
        timezone.utc
    )
    noon = day_start + timedelta(hours=12)
    rows = {
        body.key: fetch_rows(
            body, day_start - HOURLY_BEFORE, day_start + HOURLY_AFTER, "1h"
        )
        for body in BODIES
    }
    window = timedelta(days=STATION_WINDOW_DAYS)
    daily = {
        body.key: [
            (row.time, row.longitude)
            for row in fetch_rows(body, noon - window, noon + window, "1d")
        ]
        for body in PLANETS_THAT_STATION
    }
    hourly = {
        key: [(row.time, row.longitude) for row in body_rows]
        for key, body_rows in rows.items()
    }
    return SkyData(day, day_start, noon, rows, hourly, daily)


def collect_positions(sky: SkyData) -> list[dict]:
    positions = []
    for body in BODIES:
        series = sky.hourly[body.key]
        longitude = value_at(series, sky.noon)
        daily_motion = signed_diff(
            value_at(series, sky.noon + timedelta(hours=12)),
            value_at(series, sky.noon - timedelta(hours=12)),
        )
        positions.append({
            "body": body.key,
            "name": body.name,
            "longitude": round(longitude, 3),
            "sign": SIGNS[sign_index(longitude)],
            "degree": degree_in_sign(longitude),
            "retrograde": daily_motion < 0,
            "daily_motion": round(daily_motion, 3),
        })
    return positions


def collect_ingresses(sky: SkyData) -> list[dict]:
    """Смены знаков: для Луны во всём окне, для остальных ±2 дня."""
    ingresses = []
    for body in BODIES:
        series = sky.hourly[body.key]
        times = [point[0] for point in series]
        values = unwrap([point[1] for point in series])
        for moment in find_crossings(times, values, 0.0, 30.0):
            shift = abs((msk_date(moment) - sky.day).days)
            if body.key != "moon" and shift > 2:
                continue
            new_sign = sign_index(value_at(series, moment + timedelta(minutes=30)))
            ingresses.append({
                "body": body.key,
                "name": body.name,
                "time": moment.isoformat(),
                "today": sky.is_today(moment),
                "sign": SIGNS[new_sign],
                "sign_index": new_sign,
                "text": (
                    f"{body.name} переходит {SIGNS_INTO[new_sign]}: "
                    f"{format_moment(moment, sky.day)}"
                ),
            })
    return sorted(ingresses, key=lambda item: item["time"])


def describe_moon_today(sky: SkyData, ingresses: list[dict]) -> str:
    """Где Луна в течение суток по Москве."""
    moon_moves_today = [
        item for item in ingresses if item["body"] == "moon" and item["today"]
    ]
    start_sign = sign_index(
        value_at(sky.hourly["moon"], sky.day_start + timedelta(minutes=1))
    )
    if not moon_moves_today:
        return f"Луна весь день {SIGNS_IN[start_sign]}"
    parts = [f"Луна {SIGNS_IN[start_sign]}"]
    for move in moon_moves_today:
        local = datetime.fromisoformat(move["time"]).astimezone(MSK)
        parts.append(
            f"в {local:%H:%M} переходит {SIGNS_INTO[move['sign_index']]}"
        )
    return ", ".join(parts)


def collect_moon_phase(sky: SkyData) -> dict:
    moon = value_at(sky.hourly["moon"], sky.noon)
    sun = value_at(sky.hourly["sun"], sky.noon)
    elongation = (moon - sun) % 360.0
    illumination = (1 - math.cos(math.radians(elongation))) / 2
    return {
        "elongation": round(elongation, 2),
        "illumination_percent": round(illumination * 100),
        "waxing": elongation < 180.0,
        "name": _phase_name(elongation),
    }


def _phase_name(elongation: float) -> str:
    if elongation < 6 or elongation > 354:
        return "новолуние"
    if elongation < 84:
        return "растущий серп"
    if elongation < 96:
        return "первая четверть"
    if elongation < 174:
        return "растущая Луна"
    if elongation < 186:
        return "полнолуние"
    if elongation < 264:
        return "убывающая Луна"
    if elongation < 276:
        return "последняя четверть"
    return "убывающий серп"


def collect_lunations(sky: SkyData) -> list[dict]:
    """Новолуния, полнолуния и четверти, плюс затмения и суперлуния."""
    moon_series, sun_series = sky.hourly["moon"], sky.hourly["sun"]
    times = [point[0] for point in moon_series]
    elongations = unwrap([
        (moon_lon - sun_lon) % 360.0
        for (_, moon_lon), (_, sun_lon) in zip(moon_series, sun_series)
    ])
    lunations = []
    for angle, name in LUNATIONS.items():
        for moment in find_crossings(times, elongations, float(angle), 360.0):
            if moment < sky.day_start - timedelta(hours=24):
                continue
            moon_lon = value_at(moon_series, moment)
            geometry = _lunation_geometry(sky, moment)
            extras = []
            if angle in (0, 180):
                eclipse = _classify_eclipse(angle, geometry)
                if eclipse:
                    extras.append(eclipse)
            if angle == 180 and geometry["moon_distance_km"] < SUPERMOON_DISTANCE_KM:
                extras.append("суперлуние: Луна близко к Земле и выглядит крупнее")
            text = (
                f"{name.capitalize()}: {format_moment(moment, sky.day)}, "
                f"Луна {SIGNS_IN[sign_index(moon_lon)]} {degree_in_sign(moon_lon)}°"
            )
            if extras:
                details = "; ".join(extras)
                text += f". {details[0].upper()}{details[1:]}"
            lunations.append({
                "name": name,
                "time": moment.isoformat(),
                "today": sky.is_today(moment),
                "major": angle in (0, 180),
                "moon_position": describe_position(moon_lon),
                "extras": extras,
                "moon_distance_km": round(geometry["moon_distance_km"]),
                "moon_latitude": round(geometry["moon_latitude"], 3),
                "text": text,
            })
    return sorted(lunations, key=lambda item: item["time"])


def _lunation_geometry(sky: SkyData, moment: datetime) -> dict:
    moon_rows, sun_rows = sky.rows["moon"], sky.rows["sun"]
    moon_radius_deg = linear_at(moon_rows, moment, "diameter_arcsec") / 7200.0
    sun_radius_deg = linear_at(sun_rows, moment, "diameter_arcsec") / 7200.0
    moon_distance = MOON_RADIUS_KM / math.sin(math.radians(moon_radius_deg))
    sun_distance = SUN_RADIUS_KM / math.sin(math.radians(sun_radius_deg))
    return {
        "moon_latitude": linear_at(moon_rows, moment, "latitude"),
        "sun_latitude": linear_at(sun_rows, moment, "latitude"),
        "moon_radius": moon_radius_deg,
        "sun_radius": sun_radius_deg,
        "moon_parallax": math.degrees(math.asin(EARTH_RADIUS_KM / moon_distance)),
        "sun_parallax": math.degrees(math.asin(EARTH_RADIUS_KM / sun_distance)),
        "moon_distance_km": moon_distance,
    }


def _classify_eclipse(angle: int, geometry: dict) -> str | None:
    """Затмение по широте Луны в момент новолуния или полнолуния.

    Упрощённая геометрия (как у Меёса): тень Земли расширена на 2%,
    видимость на конкретной территории не учитывается.
    """
    # Наклон пути Луны к эклиптике ~5°, минимальное расстояние чуть меньше.
    moon_latitude = geometry["moon_latitude"] - (
        geometry["sun_latitude"] if angle == 0 else -geometry["sun_latitude"]
    )
    distance = abs(moon_latitude) * math.cos(math.radians(5.15))
    moon_r, sun_r = geometry["moon_radius"], geometry["sun_radius"]
    moon_p, sun_p = geometry["moon_parallax"], geometry["sun_parallax"]

    if angle == 0:
        if distance > moon_p - sun_p + moon_r + sun_r:
            return None
        if distance < moon_p - sun_p:
            kind = "полное" if moon_r > sun_r else "кольцеобразное"
            return f"{kind} солнечное затмение (видно не везде)"
        return "частное солнечное затмение (видно не везде)"

    umbra = 1.02 * (moon_p + sun_p - sun_r)
    penumbra = 1.02 * (moon_p + sun_p + sun_r)
    if distance < umbra - moon_r:
        return "полное лунное затмение, «кровавая Луна» (видно не везде)"
    if distance < umbra + moon_r:
        return "частное лунное затмение (видно не везде)"
    if distance < penumbra + moon_r:
        return "полутеневое лунное затмение, заметно слабо (видно не везде)"
    return None


def collect_stations(sky: SkyData) -> list[dict]:
    """Развороты планет (ретро/директ) в окне ±STATION_WINDOW_DAYS."""
    stations = []
    for body in PLANETS_THAT_STATION:
        series = sky.daily[body.key]
        motions = [
            (
                series[index][0] + (series[index + 1][0] - series[index][0]) / 2,
                signed_diff(series[index + 1][1], series[index][1]),
            )
            for index in range(len(series) - 1)
        ]
        for (t1, m1), (t2, m2) in zip(motions, motions[1:]):
            if (m1 < 0) == (m2 < 0):
                continue
            moment = t1 + (t2 - t1) * (m1 / (m1 - m2))
            longitude = value_at(series, moment)
            direct = m2 > 0
            days_ago = (sky.day - msk_date(moment)).days
            stations.append({
                "body": body.key,
                "name": body.name,
                "time": moment.isoformat(),
                "today": days_ago == 0,
                "days_ago": days_ago,
                "direction": "директный" if direct else "ретроградный",
                "position": describe_position(longitude),
                "text": (
                    f"{body.name} разворачивается и становится "
                    f"{'директным' if direct else 'ретроградным'}: "
                    f"около {format_date(moment)} ({_days_phrase(days_ago)}), "
                    f"{describe_position(longitude)}"
                ),
            })
    return sorted(stations, key=lambda item: item["time"])


def _days_phrase(days_ago: int) -> str:
    if days_ago == 0:
        return "сегодня"
    if days_ago > 0:
        return f"{days_ago} дн. назад"
    return f"через {-days_ago} дн."


def collect_planet_aspects(sky: SkyData) -> list[dict]:
    """Аспекты между планетами (без Луны) на полдень по Москве."""
    bodies = [body for body in BODIES if body.key != "moon"]
    aspects = []
    for first_index, first in enumerate(bodies):
        for second in bodies[first_index + 1:]:
            aspect = _planet_aspect(sky, first, second)
            if aspect:
                aspects.append(aspect)
    return sorted(aspects, key=lambda item: item["orb"])


def _planet_aspect(sky: SkyData, first: Body, second: Body) -> dict | None:
    first_series, second_series = sky.hourly[first.key], sky.hourly[second.key]
    lon_first = value_at(first_series, sky.noon)
    lon_second = value_at(second_series, sky.noon)
    angle = separation(lon_first, lon_second)
    for aspect_angle, aspect_name in ASPECTS.items():
        orb = abs(angle - aspect_angle)
        if orb > PLANET_ASPECT_ORB:
            continue
        later = separation(
            value_at(first_series, sky.noon + timedelta(hours=24)),
            value_at(second_series, sky.noon + timedelta(hours=24)),
        )
        applying = abs(later - aspect_angle) < orb
        exact = _exact_time(first_series, second_series, aspect_angle)
        exact_today = exact is not None and sky.is_today(exact)
        sign_gap = (sign_index(lon_first) - sign_index(lon_second)) % 12
        sign_aspect = aspect_angle // 30
        same_signs_pattern = sign_gap in (sign_aspect, (12 - sign_aspect) % 12)
        text = (
            f"{first.name} ({describe_position(lon_first)}) и "
            f"{second.name} ({describe_position(lon_second)}): {aspect_name}, "
            f"орбис {orb:.1f}°, {'сходящийся' if applying else 'расходящийся'}"
        )
        if exact_today:
            text += f", точный сегодня в {exact.astimezone(MSK):%H:%M}"
        if not same_signs_pattern:
            text += " (аспект через границу знаков)"
        return {
            "bodies": [first.key, second.key],
            "aspect": aspect_name,
            "orb": round(orb, 2),
            "applying": applying,
            "exact_today": exact_today,
            "text": text,
        }
    return None


def _exact_time(
    first_series: Series, second_series: Series, aspect_angle: int
) -> datetime | None:
    times = [point[0] for point in first_series]
    elongations = unwrap([
        (a - b) % 360.0
        for (_, a), (_, b) in zip(first_series, second_series)
    ])
    targets = {float(aspect_angle), float((360 - aspect_angle) % 360)}
    moments = [
        moment
        for target in targets
        for moment in find_crossings(times, elongations, target, 360.0)
    ]
    return min(moments, default=None)


def collect_moon_aspects(sky: SkyData) -> list[dict]:
    """Точные аспекты Луны к планетам в течение суток по Москве."""
    moon_series = sky.hourly["moon"]
    times = [point[0] for point in moon_series]
    events = []
    for body in BODIES:
        if body.key in ("moon", "sun"):
            continue  # фазы Луны считаются отдельно
        body_series = sky.hourly[body.key]
        elongations = unwrap([
            (moon_lon - body_lon) % 360.0
            for (_, moon_lon), (_, body_lon) in zip(moon_series, body_series)
        ])
        for aspect_angle, aspect_name in ASPECTS.items():
            targets = {float(aspect_angle), float((360 - aspect_angle) % 360)}
            for target in targets:
                for moment in find_crossings(times, elongations, target, 360.0):
                    if not sky.is_today(moment):
                        continue
                    body_lon = value_at(body_series, moment)
                    events.append({
                        "body": body.key,
                        "aspect": aspect_name,
                        "time": moment.isoformat(),
                        "text": (
                            f"{moment.astimezone(MSK):%H:%M} Луна — {body.name} "
                            f"({describe_position(body_lon)}): {aspect_name}"
                        ),
                    })
    return sorted(events, key=lambda item: item["time"])


def collect_special_today(
    lunations: list[dict], ingresses: list[dict], stations: list[dict]
) -> list[str]:
    """Особые события сегодняшнего дня, на которых строится пост."""
    special = []
    for item in lunations:
        if item["today"] and (item["major"] or item["extras"]):
            special.append(item["text"])
    for item in ingresses:
        if not item["today"] or item["body"] == "moon":
            continue
        if item["body"] == "sun" and item["sign_index"] in SEASON_POINTS:
            special.append(
                f"{SEASON_POINTS[item['sign_index']].capitalize()}: {item['text']}"
            )
        else:
            special.append(item["text"])
    for item in stations:
        if item["today"]:
            special.append(item["text"])
    return special


def build_report(day: date) -> dict:
    sky = load_sky_data(day)
    ingresses = collect_ingresses(sky)
    lunations = collect_lunations(sky)
    stations = collect_stations(sky)
    weekday = WEEKDAYS[day.weekday()]
    return {
        "date": day.isoformat(),
        "date_text": f"{day.day} {MONTHS_GENITIVE[day.month - 1]} {day.year}, {weekday}",
        "timezone": "Europe/Moscow (UTC+3)",
        "source": "NASA JPL Horizons, геоцентрические видимые эклиптические координаты",
        "special_today": collect_special_today(lunations, ingresses, stations),
        "moon_today": describe_moon_today(sky, ingresses),
        "positions": collect_positions(sky),
        "moon_phase": collect_moon_phase(sky),
        "lunations": lunations,
        "ingresses": ingresses,
        "stations": stations,
        "planet_aspects": collect_planet_aspects(sky),
        "moon_aspects": collect_moon_aspects(sky),
    }


def _bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] or ["- нет"]


def render_text(report: dict) -> str:
    phase = report["moon_phase"]
    past_stations = [
        item for item in report["stations"] if 0 < item["days_ago"] <= STATION_WINDOW_DAYS
    ]
    not_for_post = (
        [item["text"] for item in report["lunations"] if not item["today"]]
        + [item["text"] for item in report["ingresses"] if not item["today"]]
        + [item["text"] for item in report["stations"] if item["days_ago"] < 0]
    )
    lines = [
        f"НЕБО НА {report['date_text'].upper()}",
        f"Время московское. Источник: {report['source']}.",
        "",
        "ОСОБОЕ СЕГОДНЯ (если есть, пост строится вокруг этого)",
        *_bullets(report["special_today"]),
        "",
        "ЛУНА СЕГОДНЯ",
        f"- {report['moon_today']}",
        f"- Фаза на 12:00: {phase['name']}, освещена на {phase['illumination_percent']}%",
        "",
        "ТОЧНЫЕ АСПЕКТЫ ЛУНЫ СЕГОДНЯ",
        *_bullets([item["text"] for item in report["moon_aspects"]]),
        "",
        "ПОЛОЖЕНИЯ НА 12:00",
        *[
            f"- {item['name']}: {item['sign']} {item['degree']}°"
            f"{', ретроградный' if item['retrograde'] and item['body'] not in ('sun', 'moon') else ''}"
            for item in report["positions"]
        ],
        "",
        f"АСПЕКТЫ МЕЖДУ ПЛАНЕТАМИ СЕЙЧАС (орбис до {PLANET_ASPECT_ORB:.0f}°)",
        *_bullets([item["text"] for item in report["planet_aspects"]]),
        "",
        "НЕДАВНИЕ РАЗВОРОТЫ (можно упомянуть как текущее состояние, без дат)",
        *_bullets([item["text"] for item in past_stations]),
        "",
        "НЕ ДЛЯ ПОСТА: прошлые и будущие события, только для понимания контекста",
        *_bullets(not_for_post),
    ]
    return "\n".join(lines) + "\n"


def today_in_moscow() -> date:
    return datetime.now(MSK).date()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=today_in_moscow(),
        help="день в формате YYYY-MM-DD (по умолчанию сегодня по Москве)",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("out"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = build_report(args.date)
    except SkyDataError as error:
        print(f"ОШИБКА: {error}", file=sys.stderr)
        return 2
    text = render_text(report)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "sky.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.out_dir / "sky.txt").write_text(text, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
