# story-passport — паспорт за «250» під сторіс

Людина відповідає на нашу сторіс у Telegram (@godskyiflow) словом-кодом (`250`) →
отримує PDF-паспорт саме того генератора, що був у сторіс, + одне питання.
Фаундеру в «Збережене» падає: «📄 Іван відповів «250» на сторіс → надіслано паспорт …».
Кожна людина отримує паспорт одного товару один раз; повтор — тільки сповіщення фаундеру.

## Перезалити сторіс (раз на добу)
На сервері: `/opt/story-passport/story_passport/repost.sh` — публікує сторіс усіх товарів, де є блок `story`
(тільки один: `repost.sh perkins250`). Нічого більше робити не треба: робот впізнає сторіс за підписом
(`match` у товару), номер сторіс у `config.json` дописувати НЕ потрібно.

## Новий товар → підключити
1. Ролик 720×1280 покласти в `/opt/telegram-mcp/tg-media/white-gensets/reels/price/`, PDF — у `/opt/telegram-mcp/tg-media/passports/`.
2. У `config.json` → `products` новий блок: `title`, `code`, `file`, `caption`, `match` (слово з підпису сторіс) і `story` (`video`, `duration`, `text`).
3. `git` → PR → на сервері `cd /opt/story-passport && git pull && systemctl restart story-passport`, потім `repost.sh <товар>`.

## Перевірка
- `journalctl -u story-passport -n 30` — логи.
- Сухий прогін без відправки: `SP_DRY=1 /opt/story-passport/story_passport/run.sh`.
- Стан (кому вже надіслано): `/var/lib/story-passport/state.json`.

## Зупинити
`systemctl disable --now story-passport`.

## Грабли
- Сесія Telegram та сама, що в telegram-mcp контейнерах, — тому **опитування раз на 30 с**,
  а не push-оновлення (оновлення між кількома підключеннями однієї сесії ділитися не гарантовано).
- Реагує ТІЛЬКИ на відповідь саме на сторіс (`MessageReplyStoryHeader`), не на будь-яке «250» в особистих.
- Сторіс живе 24 год, але відповідь на неї можна дописати й пізніше. Впізнані сторіс запам'ятовуються в `state.json` → `story_map`; `stories` у config — лише старі ручні записи.
- `match` має бути в тексті `story.text`, інакше робот не впізнає сторіс і промовчить. Два товари з однаковим `match` — не можна.
- Telegram не приймає для сторіс 1080×1920 з fast7-пайплайну (`MEDIA_FILE_INVALID`) — перетиснути в 720×1280.
- Розшифрований env живе в /dev/shm ~10 с при старті й стирається.
