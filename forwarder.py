import asyncio
import html as _html
import json
import os
import re

import aiohttp
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

DATA_DIR    = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
MEDIA_DIR   = os.environ.get("MEDIA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "media"))
POSTED_FILE = os.path.join(DATA_DIR, "posted.json")

CHANNEL_URL  = os.environ.get("CHANNEL_URL", "https://t.me/wbc_stories")
CHANNEL_NAME = os.environ.get("CHANNEL_NAME", "Канал")

# Footer triggers: cut from the line that contains any of these phrases
_FOOTER_TRIGGERS = ("Реклама и интро вырезаны", "Забустить канал")

# Emoji unicode ranges
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002700-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002500-\U00002BFF"
    "]+",
    re.UNICODE,
)

# Markdown-style links [text](url) and bare URLs
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_URL_RE     = re.compile(r"https?://\S+")


def load_posted() -> set:
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_posted(posted: set):
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(posted), f)


def clean_caption(text: str) -> str:
    """Remove footer lines. Splits by line so the trigger never eats the previous line."""
    if not text:
        return ""
    lines = text.split("\n")
    result = []
    for line in lines:
        if any(trigger in line for trigger in _FOOTER_TRIGGERS):
            break
        result.append(line)
    return "\n".join(result).strip()


def transform_caption(text: str) -> str:
    """
    Apply full style transformation:
    - Remove markdown links [text](url) → keep text, drop URL
    - Remove bare external URLs
    - Replace all emoji with 💀
    - HTML-escape for Bot API parse_mode=HTML
    - Append channel link at the bottom
    """
    if not text:
        return f'<a href="{CHANNEL_URL}">{CHANNEL_NAME}</a>'

    # [text](url) → keep text only
    text = _MD_LINK_RE.sub(lambda m: m.group(1), text)

    # Remove bare URLs
    text = _URL_RE.sub("", text)

    # Replace emoji sequences with 💀
    text = _EMOJI_RE.sub("💀", text)

    # Clean up extra spaces / blank lines left after URL removal
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" \n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    # HTML-escape regular text so < > & don't break HTML mode
    text = _html.escape(text)

    # Append channel link (raw HTML, added after escaping)
    text += f'\n\n<a href="{CHANNEL_URL}">{CHANNEL_NAME}</a>'

    return text


def make_fingerprint(text: str) -> str | None:
    t = text.strip()
    return t[:200] if t else None


async def load_target_fingerprints(reader: TelegramClient, target: str) -> set:
    print(f"Глубокая проверка целевого канала @{target}...")
    fingerprints = set()
    count = 0
    async for msg in reader.iter_messages(target):
        fp = make_fingerprint(clean_caption(msg.text or ""))
        if fp:
            fingerprints.add(fp)
        count += 1
        if count % 200 == 0:
            print(f"  Просмотрено {count} сообщений...")
    print(f"  Готово. Найдено {count} сообщений в целевом канале.\n")
    return fingerprints


# ── Bot API helpers ────────────────────────────────────────────────────────────

async def notify_admin(token: str, admin_id: str, text: str):
    if not admin_id:
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": admin_id, "text": text},
            )
    except Exception:
        pass


async def bot_send_message(session: aiohttp.ClientSession, token: str, chat_id: str, text: str):
    await session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": f"@{chat_id}", "text": text, "parse_mode": "HTML"},
    )


async def bot_send_file(session: aiohttp.ClientSession, token: str, chat_id: str,
                        file_path: str, caption: str, mime: str):
    if "audio" in mime or file_path.endswith(".mp3") or file_path.endswith(".ogg"):
        method, field = "sendAudio", "audio"
    elif "video" in mime or file_path.endswith(".mp4"):
        method, field = "sendVideo", "video"
    else:
        method, field = "sendDocument", "document"

    url  = f"https://api.telegram.org/bot{token}/{method}"
    data = aiohttp.FormData()
    data.add_field("chat_id", f"@{chat_id}")
    if caption:
        data.add_field("caption", caption)
        data.add_field("parse_mode", "HTML")
    data.add_field(field, open(file_path, "rb"), filename=os.path.basename(file_path))

    async with session.post(url, data=data) as resp:
        result = await resp.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("description", "Bot API error"))


async def bot_send_photo(session: aiohttp.ClientSession, token: str, chat_id: str,
                         file_path: str, caption: str):
    url  = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = aiohttp.FormData()
    data.add_field("chat_id", f"@{chat_id}")
    if caption:
        data.add_field("caption", caption)
        data.add_field("parse_mode", "HTML")
    data.add_field("photo", open(file_path, "rb"),
                   filename=os.path.basename(file_path), content_type="image/jpeg")

    async with session.post(url, data=data) as resp:
        result = await resp.json()
        if not result.get("ok"):
            desc = result.get("description", "Bot API error")
            if "photo" in desc.lower() or "too large" in desc.lower() or "wrong" in desc.lower():
                await bot_send_photo_as_document(session, token, chat_id, file_path, caption)
            else:
                raise RuntimeError(desc)


async def bot_send_photo_as_document(session: aiohttp.ClientSession, token: str, chat_id: str,
                                     file_path: str, caption: str):
    url  = f"https://api.telegram.org/bot{token}/sendDocument"
    data = aiohttp.FormData()
    data.add_field("chat_id", f"@{chat_id}")
    if caption:
        data.add_field("caption", caption)
        data.add_field("parse_mode", "HTML")
    data.add_field("document", open(file_path, "rb"),
                   filename=os.path.basename(file_path), content_type="image/jpeg")

    async with session.post(url, data=data) as resp:
        result = await resp.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("description", "Bot API error (document fallback)"))


# ── Core forward logic ─────────────────────────────────────────────────────────

async def forward_message(reader: TelegramClient, http: aiohttp.ClientSession,
                          token: str, msg, target: str,
                          delay: float, admin_id: str = "") -> bool:
    caption = transform_caption(clean_caption(msg.text or ""))
    try:
        if msg.media is None:
            if caption:
                await bot_send_message(http, token, target, caption)

        elif isinstance(msg.media, MessageMediaPhoto):
            tmp = os.path.join(MEDIA_DIR, f"{msg.id}.jpg")
            try:
                await reader.download_media(msg, tmp)
                await bot_send_photo(http, token, target, tmp, caption)
            except (OSError, RuntimeError) as e:
                err = str(e)
                if "No space left" in err or "Too Large" in err or "too large" in err or "wrong" in err.lower():
                    print(f"  [FALLBACK] {msg.id}: {e} → Telethon")
                    await reader.send_file(target, msg.media, caption=caption, parse_mode="html")
                else:
                    raise
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

        elif isinstance(msg.media, MessageMediaDocument):
            doc      = msg.media.document
            mime     = doc.mime_type or ""
            filename = None
            for attr in doc.attributes:
                if type(attr).__name__ == "DocumentAttributeFilename":
                    filename = attr.file_name
                    break
            if not filename:
                ext = ".mp3" if "audio" in mime else ".mp4" if "video" in mime else ".ogg" if "ogg" in mime else ".bin"
                filename = f"{msg.id}{ext}"

            tmp = os.path.join(MEDIA_DIR, filename)
            try:
                await reader.download_media(msg, tmp)
                await bot_send_file(http, token, target, tmp, caption, mime)
            except (OSError, RuntimeError) as e:
                err = str(e)
                if "No space left" in err or "Too Large" in err or "too large" in err:
                    print(f"  [FALLBACK] {msg.id}: {e} → Telethon")
                    await reader.send_file(target, msg.media, caption=caption, parse_mode="html")
                else:
                    raise
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

        else:
            if caption:
                await bot_send_message(http, token, target, caption)

    except Exception as e:
        print(f"  [ОШИБКА] сообщение {msg.id}: {e}")
        await notify_admin(token, admin_id, f"❌ Ошибка при публикации сообщения {msg.id}:\n{e}")
        return False

    await asyncio.sleep(delay)
    return True


# ── Modes ──────────────────────────────────────────────────────────────────────

async def migrate_history(reader, http, token, source, target, posted, fingerprints, delay, admin_id=""):
    print(f"=== Перенос архива из @{source} в @{target} ===\n")
    count = 0
    async for msg in reader.iter_messages(source, reverse=True):
        if msg.id in posted:
            print(f"[{msg.id}] Уже опубликован (ID), пропускаю")
            continue

        caption   = clean_caption(msg.text or "")
        has_media = msg.media is not None
        if not caption and not has_media:
            continue

        fp = make_fingerprint(caption)
        if fp and fp in fingerprints:
            print(f"[{msg.id}] Уже опубликован (текст совпадает), пропускаю")
            posted.add(msg.id)
            continue

        print(f"[{msg.id}] Публикую: {caption[:60] if caption else '<медиа>'}...")
        ok = await forward_message(reader, http, token, msg, target, delay, admin_id)
        if ok:
            posted.add(msg.id)
            if fp:
                fingerprints.add(fp)
            count += 1
            if count % 10 == 0:
                save_posted(posted)

    save_posted(posted)
    print(f"\nПеренесено: {count} сообщений.")


async def monitor_new(reader, http, token, source, target, posted, fingerprints, delay, admin_id=""):
    print(f"\n=== Мониторинг новых сообщений из @{source} ===")
    print("Ожидаю новые посты...\n")

    @reader.on(events.NewMessage(chats=source))
    async def handler(event):
        msg = event.message
        if msg.id in posted:
            return

        caption   = clean_caption(msg.text or "")
        has_media = msg.media is not None
        if not caption and not has_media:
            return

        fp = make_fingerprint(caption)
        if fp and fp in fingerprints:
            print(f"[НОВОЕ {msg.id}] Уже существует в канале, пропускаю.")
            posted.add(msg.id)
            return

        print(f"[НОВОЕ {msg.id}] {caption[:60] if caption else '<медиа>'}...")
        ok = await forward_message(reader, http, token, msg, target, delay, admin_id)
        if ok:
            posted.add(msg.id)
            if fp:
                fingerprints.add(fp)
            save_posted(posted)
            print(f"[{msg.id}] Опубликовано.")

    await reader.run_until_disconnected()


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)

    api_id      = os.environ.get("API_ID", "")
    api_hash    = os.environ.get("API_HASH", "")
    session_str = os.environ.get("SESSION_STRING", "")
    source      = os.environ.get("SOURCE_CHANNEL", "")
    target      = os.environ.get("TARGET_CHANNEL", "")
    token       = os.environ.get("BOT_TOKEN", "")
    delay       = float(os.environ.get("POST_DELAY", "5"))
    admin_id    = os.environ.get("ADMIN_ID", "")
    mode        = os.environ.get("MODE", "3")

    if not api_id or not api_hash:
        print("Укажите API_ID и API_HASH в .env!")
        return
    if not session_str:
        print("Укажите SESSION_STRING в .env!")
        return
    if not target:
        print("Укажите TARGET_CHANNEL в .env!")
        return
    if not token:
        print("Укажите BOT_TOKEN в .env!")
        return

    reader = TelegramClient(
        StringSession(session_str), int(api_id), api_hash,
        timeout=60, request_retries=10, connection_retries=10, retry_delay=5,
    )
    await reader.start()
    print(f"Аккаунт подключён. Режим: {mode}\n")

    posted       = load_posted()
    fingerprints = await load_target_fingerprints(reader, target)

    async with aiohttp.ClientSession() as http:
        if mode == "1":
            await migrate_history(reader, http, token, source, target, posted, fingerprints, delay, admin_id)
        elif mode == "2":
            await monitor_new(reader, http, token, source, target, posted, fingerprints, delay, admin_id)
        elif mode == "3":
            await migrate_history(reader, http, token, source, target, posted, fingerprints, delay, admin_id)
            await monitor_new(reader, http, token, source, target, posted, fingerprints, delay, admin_id)
        else:
            print(f"Неверный MODE={mode!r}. Допустимые значения: 1, 2, 3.")

    await reader.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
