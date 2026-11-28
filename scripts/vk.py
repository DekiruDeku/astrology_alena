#!/usr/bin/env python3
"""Публикует пост «Астропогода» на стене сообщества ВКонтакте.

Переменные окружения:
    VK_GROUP_TOKEN  ключ доступа сообщества с правом на стену (обязателен)
    VK_GROUP_ID     (необязательно) числовой id сообщества без минуса;
                    если не задан, берётся из ключа сообщества
    VK_BOT_LINK     (необязательно) ссылка на бота в конце поста,
                    по умолчанию https://t.me/tarologapp_bot?start=vk

Запуск:
    python scripts/vk.py check
    python scripts/vk.py post --text out/post.md --image out/card.png --dry-run
    python scripts/vk.py post --text out/post.md --image out/card.png

Текст берётся тот же, что для Telegram: **жирный** убирается (ВК его не
показывает), подпись @tarologapp_bot заменяется ссылкой на бота, потому что
«@» во ВКонтакте означает упоминание страницы ВК.

Картинку ключ сообщества загрузить на стену может и не суметь: ВК разрешает
это только ключу пользователя. Тогда пост уходит без картинки, а скрипт
сообщает об этом в выводе.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from send import (
    BOLD_PATTERN,
    POST_SIGNATURE,
    ConfigError,
    _encode_multipart,
    collapse_blank_lines,
    separate_lists,
)

API_URL = "https://api.vk.com/method/{method}"
API_VERSION = "5.199"
DEFAULT_BOT_LINK = "https://t.me/tarologapp_bot?start=vk"
BOT_LINK_PREFIX = "Таро-бот:"
MESSAGE_LIMIT = 16000
REQUEST_TIMEOUT_SECONDS = 60
# Коды ошибок ВК «этому типу ключа метод недоступен» и «нет прав».
FORBIDDEN_FOR_TOKEN_CODES = {5, 7, 15, 27, 1051}


class VkError(RuntimeError):
    """ВК отклонил запрос или недоступен."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Не задана переменная окружения {name}")
    return value


def resolve_group(token: str) -> dict:
    """Сообщество из VK_GROUP_ID, а если его нет, то то, которому принадлежит ключ."""
    raw_group_id = os.environ.get("VK_GROUP_ID", "").strip().lstrip("-")
    params = {}
    if raw_group_id:
        if not raw_group_id.isdigit():
            raise ConfigError("VK_GROUP_ID должен быть числом, например 123456789")
        params["group_id"] = raw_group_id
    response = call_api(token, "groups.getById", params)
    groups = response["groups"] if isinstance(response, dict) else response
    if not groups:
        raise VkError("ВК не вернул сообщество для этого ключа")
    return groups[0]


def markdown_to_vk_text(text: str, bot_link: str) -> str:
    """Текст поста для ВК: без **, с пустыми строками вокруг списков, ссылка на бота в конце."""
    prepared = collapse_blank_lines(separate_lists(text.strip()))
    plain = BOLD_PATTERN.sub(r"\1", prepared)
    lines = plain.rstrip().splitlines()
    while lines and lines[-1].strip() in {POST_SIGNATURE, ""}:
        lines.pop()
    body = "\n".join(lines).rstrip()
    return f"{body}\n\n{BOT_LINK_PREFIX} {bot_link}"


def call_api(token: str, method: str, params: dict) -> dict | list | int:
    """Вызов метода ВК. Ключ идёт в теле запроса, а не в адресе, чтобы не попасть в логи."""
    body = urllib.parse.urlencode({**params, "access_token": token, "v": API_VERSION})
    request = urllib.request.Request(
        API_URL.format(method=method),
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    payload = _read_json(request, f"ВКонтакте недоступен ({method})")
    if "error" in payload:
        error = payload["error"]
        raise VkError(
            f"{method}: [{error.get('error_code')}] {error.get('error_msg', 'неизвестная ошибка')}",
            code=error.get("error_code"),
        )
    return payload["response"]


def _read_json(request: urllib.request.Request, unavailable_message: str) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise VkError(f"{unavailable_message}: HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise VkError(f"{unavailable_message}: {error.__class__.__name__}") from None
    except json.JSONDecodeError:
        raise VkError(f"{unavailable_message}: ответ не JSON") from None


def upload_wall_photo(token: str, group_id: int, image_path: Path) -> str:
    """Загружает картинку для поста и возвращает вложение вида photo<owner>_<id>."""
    server = call_api(token, "photos.getWallUploadServer", {"group_id": group_id})
    body, content_type = _encode_multipart({}, {"photo": image_path})
    request = urllib.request.Request(
        server["upload_url"],
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    uploaded = _read_json(request, "Сервер загрузки фото ВК недоступен")
    if not uploaded.get("photo") or uploaded.get("photo") == "[]":
        raise VkError("Сервер загрузки ВК не принял картинку")
    saved = call_api(token, "photos.saveWallPhoto", {
        "group_id": group_id,
        "photo": uploaded["photo"],
        "server": uploaded["server"],
        "hash": uploaded["hash"],
    })
    photo = saved[0]
    return f"photo{photo['owner_id']}_{photo['id']}"


def try_attach_image(token: str, group_id: int, image_path: Path | None) -> str:
    """Пытается загрузить картинку. При запрете для ключа сообщества возвращает пустую строку."""
    if image_path is None:
        return ""
    if not image_path.is_file():
        raise FileNotFoundError(f"Нет картинки {image_path}")
    try:
        return upload_wall_photo(token, group_id, image_path)
    except VkError as error:
        if error.code not in FORBIDDEN_FOR_TOKEN_CODES:
            raise
        print(f"Картинку загрузить нельзя, пост уйдёт без неё. Ответ ВК: {error}")
        return ""


def post_guid(message: str) -> str:
    """Одинаковый текст даёт одинаковый guid, и ВК не опубликует пост дважды."""
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:32]


def send_post(text_path: Path, image_path: Path | None, dry_run: bool) -> None:
    bot_link = os.environ.get("VK_BOT_LINK", "").strip() or DEFAULT_BOT_LINK
    message = markdown_to_vk_text(text_path.read_text(encoding="utf-8"), bot_link)
    if len(message) > MESSAGE_LIMIT:
        raise VkError(f"Пост длиннее {MESSAGE_LIMIT} символов ({len(message)})")
    print(f"Длина поста для ВК: {len(message)} символов.")
    if dry_run:
        picture = f"с картинкой {image_path}" if image_path else "без картинки"
        print(f"Пробный запуск, во ВКонтакте ничего не опубликовано. Попытка: {picture}.")
        print(message)
        return

    token = require_env("VK_GROUP_TOKEN")
    group_id = int(resolve_group(token)["id"])
    attachment = try_attach_image(token, group_id, image_path)
    params = {
        "owner_id": -group_id,
        "from_group": 1,
        "message": message,
        "guid": post_guid(message),
    }
    if attachment:
        params["attachments"] = attachment
    result = call_api(token, "wall.post", params)
    link = f"https://vk.com/wall-{group_id}_{result['post_id']}"
    picture = "с картинкой" if attachment else "без картинки"
    print(f"Пост опубликован во ВКонтакте ({picture}): {link}")


def check_access() -> None:
    """Проверяет ключ, ничего не публикуя: видит ли он группу и можно ли грузить фото."""
    token = require_env("VK_GROUP_TOKEN")
    group = resolve_group(token)
    group_id = int(group["id"])
    print(f"Ключ работает, сообщество: {group.get('name')} (id {group.get('id')}).")
    try:
        call_api(token, "photos.getWallUploadServer", {"group_id": group_id})
        print("Загрузка картинок на стену разрешена: посты пойдут с карточкой.")
    except VkError as error:
        print(f"Загрузка картинок на стену недоступна: {error}")
        print("Посты пойдут без картинки.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Публикация поста во ВКонтакте")
    commands = parser.add_subparsers(dest="command", required=True)

    post = commands.add_parser("post", help="опубликовать пост на стене сообщества")
    post.add_argument("--text", type=Path, required=True)
    post.add_argument("--image", type=Path, default=None, help="без него уйдёт только текст")
    post.add_argument("--dry-run", action="store_true")

    commands.add_parser("check", help="проверить ключ, ничего не публикуя")
    return parser.parse_args()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        if args.command == "post":
            send_post(args.text, args.image, args.dry_run)
        else:
            check_access()
    except (VkError, ConfigError, FileNotFoundError) as error:
        print(f"ОШИБКА: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
