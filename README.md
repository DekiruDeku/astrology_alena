# astro-channel

Каждое утро в 9:00 по Москве облачная рутина Claude пишет пост
«Астропогода» с карточкой и отправляет его в Telegram-канал.

## Как устроено

| Файл | Что делает |
|---|---|
| `scripts/sky.py` | Берёт позиции планет из NASA JPL Horizons и считает знаки, фазы Луны, смены знаков, развороты и аспекты |
| `scripts/card.py` | Рисует карточку 1080×1350 в стиле приложения |
| `scripts/send.py` | Отправляет пост в канал или пишет администратору о сбое |
| `INSTRUCTIONS.md` | Пошаговая задача для Claude, профиль канала и формат поста |
| `.claude/skills/` | Скиллы che-telegram-content-creator и humanizer-ru (MIT) |
| `assets/fonts/` | Manrope и Cormorant Garamond (SIL OFL) |

## Переменные окружения

| Переменная | Обязательна | Пример |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | да | токен от @BotFather |
| `TELEGRAM_CHANNEL_ID` | да | `@my_channel` или `-1001234567890` |
| `TELEGRAM_ADMIN_CHAT_ID` | нет | твой числовой id; бот пишет туда о сбоях |
| `CHANNEL_FOOTER` | нет | подпись внизу карточки, например `@my_channel` |

Чтобы бот мог писать в личку о сбоях, сначала напиши ему `/start`.

## Ручной запуск

```bash
pip install -r requirements.txt
python scripts/sky.py
python scripts/card.py --headline "Луна в Тельце" --subline "Завтра полнолуние"
python scripts/send.py post --text out/post.md --image out/card.png --dry-run
```

На Windows добавляй `-X utf8`: `py -X utf8 scripts/sky.py`.

## Облачное окружение

Сетевой доступ нужен к `ssd.jpl.nasa.gov`, `api.telegram.org` и `pypi.org`.
