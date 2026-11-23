#!/usr/bin/env python3
"""Отправляет пост с картинкой в Telegram-канал через Bot API.

Переменные окружения:
    TELEGRAM_BOT_TOKEN      токен бота (обязателен)
    TELEGRAM_CHANNEL_ID     @username канала или числовой id -100...
    TELEGRAM_ADMIN_CHAT_ID  (необязательно) куда писать о сбоях

Запуск:
    python scripts/send.py post --text out/post.md --image out/card.png
    python scripts/send.py post --text out/post.md --image out/card.png --dry-run
    python scripts/send.py notify "Пост не вышел: эфемериды недоступны"

В тексте поста поддерживается только **жирный**. Если текст влезает в
подпись к фото (1024 символа), уходит одно сообщение, иначе фото и текст
отдельно.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API_URL = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
REQUEST_TIMEOUT_SECONDS = 60
BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
BULLET = "•"


class TelegramError(RuntimeError):
    """Telegram отклонил запрос или недоступен."""


class ConfigError(RuntimeError):
    """Не заданы нужные переменные окружения."""


def markdown_to_telegram_html(text: str) -> str:
    escaped = html.escape(separate_lists(text.strip()), quote=False)
    return BOLD_PATTERN.sub(r"<b>\1</b>", escaped)


def separate_lists(text: str) -> str:
    """Ставит пустую строку перед списком «•» и после него."""
    lines = text.splitlines()
    result: list[str] = []
    for line in lines:
        is_item = line.lstrip().startswith(BULLET)
        previous = result[-1] if result else ""
        previous_is_item = previous.lstrip().startswith(BULLET)
        if previous.strip() and is_item != previous_is_item:
            result.append("")
        result.append(line)
    return "\n".join(result)


def visible_length(telegram_html: str) -> int:
    """Длина видимого текста в UTF-16 (так считает Telegram)."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", telegram_html))
    return len(plain.encode("utf-16-le")) // 2


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Не задана переменная окружения {name}")
    return value


def call_api(token: str, method: str, fields: dict, files: dict | None = None) -> dict:
    body, content_type = _encode_multipart(fields, files or {})
    request = urllib.request.Request(
        API_URL.format(token=token, method=method),
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        payload = _read_error_payload(error)
    except (urllib.error.URLError, TimeoutError) as error:
        # Текст ошибки не содержит URL с токеном.
        raise TelegramError(f"Telegram недоступен: {error.__class__.__name__}") from None
    if not payload.get("ok"):
        raise TelegramError(
            f"{method}: {payload.get('description', 'неизвестная ошибка')}"
        )
    return payload["result"]


def _read_error_payload(error: urllib.error.HTTPError) -> dict:
    try:
        return json.loads(error.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "description": f"HTTP {error.code}"}


def _encode_multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    for name, path in files.items():
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{path.name}"\r\nContent-Type: {mime_type}\r\n\r\n'.encode("utf-8")
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def send_post(text_path: Path, image_path: Path | None, dry_run: bool) -> None:
    post_html = markdown_to_telegram_html(text_path.read_text(encoding="utf-8"))
    length = visible_length(post_html)
    if length > MESSAGE_LIMIT:
        raise TelegramError(f"Пост длиннее {MESSAGE_LIMIT} символов ({length})")
    if image_path is None:
        send_text_only(post_html, length, dry_run)
        return
    if not image_path.is_file():
        raise FileNotFoundError(f"Нет картинки {image_path}")

    single_message = length <= CAPTION_LIMIT
    plan = "одно сообщение (фото с подписью)" if single_message else "фото, затем текст"
    print(f"Длина поста: {length} символов. Отправка: {plan}.")
    if dry_run:
        print("Пробный запуск, в Telegram ничего не отправлено.")
        print(post_html)
        return

    token = require_env("TELEGRAM_BOT_TOKEN")
    channel = require_env("TELEGRAM_CHANNEL_ID")
    photo_fields = {"chat_id": channel}
    if single_message:
        photo_fields.update({"caption": post_html, "parse_mode": "HTML"})
    call_api(token, "sendPhoto", photo_fields, {"photo": image_path})
    if not single_message:
        call_api(token, "sendMessage", {
            "chat_id": channel,
            "text": post_html,
            "parse_mode": "HTML",
            "link_preview_options": json.dumps({"is_disabled": True}),
        })
    print("Пост отправлен в канал.")


def send_text_only(post_html: str, length: int, dry_run: bool) -> None:
    print(f"Длина поста: {length} символов. Отправка: только текст, без картинки.")
    if dry_run:
        print("Пробный запуск, в Telegram ничего не отправлено.")
        print(post_html)
        return
    token = require_env("TELEGRAM_BOT_TOKEN")
    channel = require_env("TELEGRAM_CHANNEL_ID")
    call_api(token, "sendMessage", {
        "chat_id": channel,
        "text": post_html,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": True}),
    })
    print("Пост отправлен в канал (без картинки).")


def notify_admin(message: str) -> None:
    admin_chat = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    if not admin_chat:
        print("TELEGRAM_ADMIN_CHAT_ID не задан, уведомление пропущено.")
        return
    token = require_env("TELEGRAM_BOT_TOKEN")
    call_api(token, "sendMessage", {"chat_id": admin_chat, "text": message})
    print("Уведомление отправлено.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Отправка поста в Telegram")
    commands = parser.add_subparsers(dest="command", required=True)

    post = commands.add_parser("post", help="отправить пост в канал")
    post.add_argument("--text", type=Path, required=True)
    post.add_argument("--image", type=Path, default=None, help="без него уйдёт только текст")
    post.add_argument("--dry-run", action="store_true")

    notify = commands.add_parser("notify", help="написать администратору")
    notify.add_argument("message")
    return parser.parse_args()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        if args.command == "post":
            send_post(args.text, args.image, args.dry_run)
        else:
            notify_admin(args.message)
    except (TelegramError, ConfigError, FileNotFoundError) as error:
        print(f"ОШИБКА: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
