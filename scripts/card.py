#!/usr/bin/env python3
"""Рисует карточку к посту: дата, фаза Луны, заголовок и подзаголовок дня.

Стиль взят из приложения: тёмный графит, фиолетовое свечение, золото,
шрифты Cormorant Garamond (заголовки) и Manrope (текст).

Запуск:
    python scripts/card.py --sky out/sky.json \
        --headline "Луна в Тельце" \
        --subline "Завтра в 17:53 полнолуние в Близнецах" \
        --out out/card.png
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import date
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 1080, 1350
FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
DISPLAY_FONT = FONTS_DIR / "CormorantGaramond-Variable.ttf"
BODY_FONT = FONTS_DIR / "Manrope-Variable.ttf"

MIDNIGHT = (8, 11, 18)
NAVY = (11, 16, 32)
VIOLET = (110, 79, 163)
GOLD = (214, 181, 109)
GOLD_LIGHT = (240, 213, 139)
TEXT = (243, 238, 230)
MUTED = (169, 163, 179)
MOON_SHADOW = (26, 30, 44)

MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)

MOON_CENTER = (WIDTH // 2, 630)
MOON_RADIUS = 200
TEXT_MAX_WIDTH = 900
HEADLINE_TOP = 990
# Области с текстом, где не рисуем звёзды (left, top, right, bottom).
TEXT_ZONES = (
    (80, 90, 1000, 350),
    (80, 860, 1000, 1300),
)


def load_font(path: Path, size: int, weight: int) -> ImageFont.FreeTypeFont:
    if not path.is_file():
        raise FileNotFoundError(f"Нет шрифта {path}")
    font = ImageFont.truetype(str(path), size)
    font.set_variation_by_axes([weight])
    return font


def draw_background(seed: int) -> Image.Image:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), MIDNIGHT)
    draw = ImageDraw.Draw(canvas)
    for y in range(HEIGHT):
        # Вертикальный градиент midnight -> navy -> midnight.
        mix = math.sin(math.pi * y / HEIGHT)
        row = tuple(
            round(MIDNIGHT[channel] + (NAVY[channel] - MIDNIGHT[channel]) * mix)
            for channel in range(3)
        )
        draw.line((0, y, WIDTH, y), fill=row)

    glow = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((560, -260, 1340, 520), fill=tuple(c // 3 for c in VIOLET))
    glow_draw.ellipse((-320, 900, 420, 1600), fill=tuple(c // 5 for c in VIOLET))
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    canvas = ImageChops.add(canvas, glow)

    _draw_stars(canvas, seed)
    return canvas


def _draw_stars(canvas: Image.Image, seed: int) -> None:
    generator = random.Random(seed)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for _ in range(170):
        x, y = generator.uniform(0, WIDTH), generator.uniform(0, HEIGHT)
        if any(left <= x <= right and top <= y <= bottom
               for left, top, right, bottom in TEXT_ZONES):
            continue
        radius = generator.choice((0.8, 1.0, 1.2, 1.6, 2.2))
        color = GOLD_LIGHT if generator.random() < 0.25 else TEXT
        alpha = generator.randint(50, 190)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*color, alpha))
    canvas.paste(layer, (0, 0), layer)


def draw_frame(canvas: Image.Image) -> None:
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle(
        (40, 40, WIDTH - 40, HEIGHT - 40), radius=36, outline=(*GOLD, 70), width=2
    )
    canvas.paste(layer, (0, 0), layer)


def draw_moon(canvas: Image.Image, elongation: float, illumination: float) -> None:
    scale = 2  # рисуем крупнее и уменьшаем: так края сглаживаются
    radius = MOON_RADIUS * scale
    size = radius * 2 + 2
    moon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pixels = moon.load()
    cos_phase = math.cos(math.radians(elongation))
    waxing = elongation < 180
    for py in range(size):
        ny = (py - radius) / radius
        if abs(ny) > 1:
            continue
        half_width = math.sqrt(1 - ny * ny)
        terminator = cos_phase * half_width
        for px in range(size):
            nx = (px - radius) / radius
            if abs(nx) > half_width:
                continue
            lit = nx >= terminator if waxing else nx <= -terminator
            if lit:
                # К краю диска чуть темнее, к центру светлее.
                depth = math.sqrt(max(0.0, 1 - nx * nx - ny * ny))
                shade = 0.78 + 0.22 * depth
                color = tuple(
                    round(GOLD[c] + (GOLD_LIGHT[c] - GOLD[c]) * depth) * shade
                    for c in range(3)
                )
                pixels[px, py] = (*(round(v) for v in color), 255)
            else:
                pixels[px, py] = (*MOON_SHADOW, 255)
    moon = moon.resize((MOON_RADIUS * 2 + 1, MOON_RADIUS * 2 + 1), Image.LANCZOS)

    halo = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    halo_draw = ImageDraw.Draw(halo)
    cx, cy = MOON_CENTER
    halo_radius = MOON_RADIUS + 40
    halo_alpha = round(40 + 90 * illumination)
    halo_draw.ellipse(
        (cx - halo_radius, cy - halo_radius, cx + halo_radius, cy + halo_radius),
        fill=(*GOLD, halo_alpha),
    )
    halo = halo.filter(ImageFilter.GaussianBlur(55))
    canvas.paste(halo, (0, 0), halo)
    canvas.paste(moon, (cx - MOON_RADIUS, cy - MOON_RADIUS), moon)


def draw_spaced_text(
    draw: ImageDraw.ImageDraw, center_x: int, y: int, text: str,
    font: ImageFont.FreeTypeFont, fill: tuple, spacing: int,
) -> None:
    widths = [draw.textlength(char, font=font) for char in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = center_x - total / 2
    for char, width in zip(text, widths):
        draw.text((x, y), char, font=font, fill=fill)
        x += width + spacing


def wrap_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in _glue_short_words(text):
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _glue_short_words(text: str) -> list[str]:
    """Склеивает предлоги и союзы до двух букв со следующим словом."""
    tokens: list[str] = []
    glue_next = False
    for word in text.split():
        if glue_next and tokens:
            tokens[-1] = f"{tokens[-1]} {word}"
        else:
            tokens.append(word)
        glue_next = len(word.strip(",.:;!?«»")) <= 2
    return tokens


def wrap_balanced(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """Перенос с выравниванием длины строк, без одинокого слова внизу."""
    lines = wrap_text(draw, text, font, max_width)
    if len(lines) < 2:
        return lines
    width = max_width
    while width > max_width // 3:
        narrower = wrap_text(draw, text, font, width - 20)
        if len(narrower) > len(lines):
            break
        lines, width = narrower, width - 20
    return lines


def draw_centered_lines(
    draw: ImageDraw.ImageDraw, lines: list[str], top: int,
    font: ImageFont.FreeTypeFont, fill: tuple, line_height: int,
) -> int:
    for index, line in enumerate(lines):
        draw.text(
            (WIDTH / 2, top + index * line_height), line,
            font=font, fill=fill, anchor="ma",
        )
    return top + len(lines) * line_height


def draw_diamond(draw: ImageDraw.ImageDraw, center: tuple[int, int], size: int) -> None:
    x, y = center
    draw.polygon(
        [(x, y - size), (x + size, y), (x, y + size), (x - size, y)], fill=GOLD
    )


def render_card(sky: dict, headline: str, subline: str, footer: str | None) -> Image.Image:
    day = date.fromisoformat(sky["date"])
    phase = sky["moon_phase"]

    canvas = draw_background(seed=day.toordinal())
    draw_frame(canvas)
    draw_moon(canvas, phase["elongation"], phase["illumination_percent"] / 100)

    draw = ImageDraw.Draw(canvas)
    label_font = load_font(BODY_FONT, 30, 700)
    date_font = load_font(DISPLAY_FONT, 104, 700)
    weekday_font = load_font(BODY_FONT, 32, 600)
    phase_font = load_font(BODY_FONT, 28, 600)
    headline_font = load_font(DISPLAY_FONT, 84, 700)
    subline_font = load_font(BODY_FONT, 38, 600)
    footer_font = load_font(BODY_FONT, 26, 600)

    draw_spaced_text(draw, WIDTH // 2, 112, "АСТРОПОГОДА", label_font, GOLD, spacing=8)
    draw.text(
        (WIDTH / 2, 160), f"{day.day} {MONTHS_GENITIVE[day.month - 1]}",
        font=date_font, fill=TEXT, anchor="ma",
    )
    draw.text(
        (WIDTH / 2, 290), WEEKDAYS[day.weekday()],
        font=weekday_font, fill=MUTED, anchor="ma",
    )

    phase_line = f"{phase['name']} · {phase['illumination_percent']}%"
    draw.text(
        (WIDTH / 2, MOON_CENTER[1] + MOON_RADIUS + 44), phase_line,
        font=phase_font, fill=MUTED, anchor="ma",
    )

    draw_diamond(draw, (WIDTH // 2, HEADLINE_TOP - 28), 7)
    headline_lines = wrap_balanced(draw, headline, headline_font, TEXT_MAX_WIDTH)[:2]
    bottom = draw_centered_lines(
        draw, headline_lines, HEADLINE_TOP, headline_font, TEXT, 88
    )
    subline_lines = wrap_balanced(draw, subline, subline_font, TEXT_MAX_WIDTH)[:3]
    draw_centered_lines(draw, subline_lines, bottom + 24, subline_font, TEXT, 52)

    if footer:
        draw.text(
            (WIDTH / 2, HEIGHT - 96), footer,
            font=footer_font, fill=GOLD, anchor="ma",
        )
    return canvas


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Карточка к посту")
    parser.add_argument("--sky", type=Path, default=Path("out/sky.json"))
    parser.add_argument("--headline", required=True, help="до ~25 символов")
    parser.add_argument("--subline", required=True, help="до ~70 символов")
    parser.add_argument("--footer", default=None, help="например, @имя_канала")
    parser.add_argument("--out", type=Path, default=Path("out/card.png"))
    return parser.parse_args()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        sky = json.loads(args.sky.read_text(encoding="utf-8"))
        card = render_card(sky, args.headline, args.subline, args.footer)
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as error:
        print(f"ОШИБКА: {error}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    card.save(args.out, "PNG", optimize=True)
    print(f"Карточка сохранена: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
