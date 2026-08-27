import asyncio
import json
import os

import aiohttp
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

from forwarder import (
    DATA_DIR,
    MEDIA_DIR,
    clean_caption,
    forward_message,
    load_posted,
    load_target_fingerprints,
    make_fingerprint,
    save_posted,
)

MODE_NAMES = {
    "1": "📦 Архив",
    "2": "👁 Мониторинг",
    "3": "📦+👁 Оба",
    "4": "✅ Утверждение",
}

# Постоянная клавиатура снизу
REPLY_KB = {
    "keyboard": [
        [{"text": "📊 Статус"},    {"text": "❓ Помощь"}],
        [{"text": "▶️ Запустить"}, {"text": "⏹ Остановить"}],
        [{"text": "📦 Архив"},     {"text": "👁 Мониторинг"}],
        [{"text": "📦+👁 Оба"},   {"text": "✅ Утверждение"}],
    ],
    "resize_keyboard": True,
    "persistent": True,
    "input_field_placeholder": "Выберите действие...",
}

# Маппинг текста кнопок → команды
BTN_MAP = {
    "📊 Статус":       "/status",
    "❓ Помощь":       "/help",
    "▶️ Запустить":    "/run",
    "⏹ Остановить":   "/stop",
    "📦 Архив":        "/mode 1",
    "👁 Мониторинг":   "/mode 2",
    "📦+👁 Оба":      "/mode 3",
    "✅ Утверждение":  "/mode 4",
}


class Controller:
    def __init__(self, reader, token, source, target, admin_id, delay):
        self.reader   = reader
        self.token    = token
        self.source   = source
        self.target   = target
        self.admin_id = admin_id
        self.delay    = delay
        self.mode     = os.environ.get("MODE", "3")
        self.running  = False
        self.task: asyncio.Task | None = None
        self.pending: dict[str, object] = {}
        self.posted: set = set()
        self.fingerprints: set = set()
        self.stats = {"posted": 0, "errors": 0, "rejected": 0}
        self._http: aiohttp.ClientSession | None = None

    async def initialize(self):
        self.posted       = load_posted()
        self.fingerprints = await load_target_fingerprints(self.reader, self.target)

    # ── Start / Stop ───────────────────────────────────────────────────────────

    async def start(self) -> str:
        if self.running:
            return "already_running"
        if not self.source:
            return "no_source"
        self.running = True
        self.task = asyncio.create_task(self._run())
        return "started"

    async def stop(self) -> str:
        if not self.running:
            return "not_running"
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        return "stopped"

    # ── Mode runners ───────────────────────────────────────────────────────────

    async def _run(self):
        http = self._http
        try:
            if self.mode == "1":
                await self._migrate(http)
            elif self.mode == "2":
                await self._monitor(http)
            elif self.mode == "3":
                await self._migrate(http)
                if self.running:
                    await self._monitor(http)
            elif self.mode == "4":
                await self._approval(http)
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            save_posted(self.posted)

    async def _migrate(self, http):
        print(f"=== Перенос архива из @{self.source} ===")
        count = 0
        async for msg in self.reader.iter_messages(self.source, reverse=True):
            if not self.running:
                break
            if msg.id in self.posted:
                continue
            caption = clean_caption(msg.text or "")
            if not caption and msg.media is None:
                continue
            fp = make_fingerprint(caption)
            if fp and fp in self.fingerprints:
                self.posted.add(msg.id)
                continue
            ok = await forward_message(
                self.reader, http, self.token, msg, self.target, self.delay, source=self.source
            )
            if ok:
                self.posted.add(msg.id)
                if fp:
                    self.fingerprints.add(fp)
                self.stats["posted"] += 1
                count += 1
                if count % 10 == 0:
                    save_posted(self.posted)
            else:
                self.stats["errors"] += 1
        save_posted(self.posted)
        print(f"Перенесено: {count} сообщений.")

    async def _monitor(self, http):
        print(f"=== Мониторинг @{self.source} ===")

        async def handler(event):
            if not self.running:
                return
            msg = event.message
            if msg.id in self.posted:
                return
            caption = clean_caption(msg.text or "")
            if not caption and msg.media is None:
                return
            fp = make_fingerprint(caption)
            if fp and fp in self.fingerprints:
                self.posted.add(msg.id)
                return
            ok = await forward_message(
                self.reader, http, self.token, msg, self.target, self.delay, source=self.source
            )
            if ok:
                self.posted.add(msg.id)
                if fp:
                    self.fingerprints.add(fp)
                self.stats["posted"] += 1
                save_posted(self.posted)
            else:
                self.stats["errors"] += 1

        self.reader.add_event_handler(handler, events.NewMessage(chats=self.source))
        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        finally:
            self.reader.remove_event_handler(handler)

    async def _approval(self, http):
        print(f"=== Режим утверждения @{self.source} ===")

        async def handler(event):
            if not self.running:
                return
            msg = event.message
            if msg.id in self.posted:
                return
            caption = clean_caption(msg.text or "")
            if not caption and msg.media is None:
                return
            self.pending[str(msg.id)] = msg
            await self._send_approval_request(http, msg, caption)

        self.reader.add_event_handler(handler, events.NewMessage(chats=self.source))
        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        finally:
            self.reader.remove_event_handler(handler)

    # ── Approval request ───────────────────────────────────────────────────────

    async def _send_approval_request(self, http, msg, caption):
        if isinstance(msg.media, MessageMediaPhoto):
            media_label = "🖼 Фото\n"
        elif isinstance(msg.media, MessageMediaDocument):
            mime = msg.media.document.mime_type or ""
            media_label = (
                "🎵 Аудио\n" if "audio" in mime else
                "🎬 Видео\n" if "video" in mime else
                "📎 Файл\n"
            )
        else:
            media_label = ""

        preview = (caption[:500] + "…") if len(caption) > 500 else (caption or "‹без текста›")
        text = f"📨 *Новый пост* `#{msg.id}`\n{media_label}\n{preview}"

        keyboard = {"inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"approve:{msg.id}"},
            {"text": "❌ Отклонить",    "callback_data": f"reject:{msg.id}"},
        ]]}
        await _api(http, self.token, "sendMessage", {
            "chat_id":      self.admin_id,
            "text":         text,
            "parse_mode":   "Markdown",
            "reply_markup": keyboard,
        })

    # ── Callback handler ───────────────────────────────────────────────────────

    async def handle_callback(self, http, cb):
        data       = cb.get("data", "")
        cb_id      = cb["id"]
        bot_msg_id = cb.get("message", {}).get("message_id")
        chat_id    = cb.get("message", {}).get("chat", {}).get("id")
        answer     = ""
        refresh_status = False

        if data.startswith("approve:"):
            key = data[8:]
            msg = self.pending.pop(key, None)
            if msg:
                ok = await forward_message(
                    self.reader, http, self.token, msg, self.target, self.delay, source=self.source
                )
                if ok:
                    self.stats["posted"] += 1
                    self.posted.add(msg.id)
                    fp = make_fingerprint(clean_caption(msg.text or ""))
                    if fp:
                        self.fingerprints.add(fp)
                    save_posted(self.posted)
                    answer = "✅ Опубликовано!"
                    await _api(http, self.token, "editMessageText", {
                        "chat_id": chat_id, "message_id": bot_msg_id,
                        "text": f"✅ Опубликовано (пост #{key})",
                    })
                else:
                    self.stats["errors"] += 1
                    answer = "❌ Ошибка публикации"
                    await _api(http, self.token, "editMessageText", {
                        "chat_id": chat_id, "message_id": bot_msg_id,
                        "text": f"❌ Ошибка (пост #{key})",
                    })
            else:
                answer = "⚠️ Уже обработано"

        elif data.startswith("reject:"):
            key = data[7:]
            self.pending.pop(key, None)
            self.stats["rejected"] += 1
            answer = "🗑 Отклонено"
            await _api(http, self.token, "editMessageText", {
                "chat_id": chat_id, "message_id": bot_msg_id,
                "text": f"🗑 Отклонено (пост #{key})",
            })

        elif data.startswith("mode:"):
            new_mode = data[5:]
            if new_mode in MODE_NAMES:
                was_running = self.running
                if was_running:
                    await self.stop()
                self.mode = new_mode
                if was_running:
                    await self.start()
                answer = f"Режим: {MODE_NAMES[new_mode]}"
                refresh_status = True
            else:
                answer = "Неверный режим"

        elif data == "stop_parser":
            res    = await self.stop()
            answer = "⏹ Остановлен" if res == "stopped" else "Уже остановлен"
            refresh_status = True

        elif data == "start_parser":
            res    = await self.start()
            answer = "▶️ Запущен" if res == "started" else "Уже работает"
            refresh_status = True

        elif data == "refresh":
            answer = "🔄 Обновлено"
            refresh_status = True

        else:
            answer = "?"

        if refresh_status and bot_msg_id and chat_id:
            await _api(http, self.token, "editMessageText", {
                "chat_id":      chat_id,
                "message_id":   bot_msg_id,
                "text":         self.status_text(),
                "parse_mode":   "Markdown",
                "reply_markup": self.status_keyboard(),
            })

        await _api(http, self.token, "answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": answer,
        })

    # ── Status text & keyboards ────────────────────────────────────────────────

    def status_text(self) -> str:
        state = "▶️ Работает" if self.running else "⏹ Остановлен"
        mode  = MODE_NAMES.get(self.mode, self.mode)
        lines = [
            "📊 *Статус парсера*", "",
            f"Состояние: {state}",
            f"Режим: {mode}",
            f"Опубликовано сегодня: {self.stats['posted']}",
            f"Ошибок: {self.stats['errors']}",
            f"Отклонено: {self.stats['rejected']}",
        ]
        if self.pending:
            lines.append(f"\n⏳ На утверждении: {len(self.pending)} постов")
        return "\n".join(lines)

    def status_keyboard(self) -> dict:
        def mode_btn(key):
            mark = " ✓" if self.mode == key else ""
            return {"text": MODE_NAMES[key] + mark, "callback_data": f"mode:{key}"}

        toggle = (
            {"text": "⏹ Остановить", "callback_data": "stop_parser"}
            if self.running else
            {"text": "▶️ Запустить",  "callback_data": "start_parser"}
        )
        return {
            "inline_keyboard": [
                [toggle, {"text": "🔄 Обновить", "callback_data": "refresh"}],
                [mode_btn("1"), mode_btn("2")],
                [mode_btn("3"), mode_btn("4")],
            ]
        }


# ── Bot API helper ─────────────────────────────────────────────────────────────

async def _api(http: aiohttp.ClientSession, token: str, method: str, payload: dict):
    async with http.post(
        f"https://api.telegram.org/bot{token}/{method}", json=payload
    ) as resp:
        return await resp.json()


async def _reply(http, token, chat_id, text, inline_kb=None, parse_mode="Markdown"):
    payload = {
        "chat_id":      chat_id,
        "text":         text,
        "parse_mode":   parse_mode,
        "reply_markup": inline_kb if inline_kb else REPLY_KB,
    }
    await _api(http, token, "sendMessage", payload)


# ── Command processor ──────────────────────────────────────────────────────────

async def process_command(ctrl: Controller, http: aiohttp.ClientSession, text: str, chat_id):
    text = BTN_MAP.get(text.strip(), text.strip())

    if text in ("/start", "/status", "/статус"):
        await _api(http, ctrl.token, "sendMessage", {
            "chat_id":      chat_id,
            "text":         ctrl.status_text(),
            "parse_mode":   "Markdown",
            "reply_markup": {
                # Склеиваем inline + постоянную reply-клавиатуру через отдельный вызов
                **ctrl.status_keyboard()
            },
        })
        # Отправляем reply-клавиатуру отдельным «невидимым» способом — просто прикрепим
        # её к статус-сообщению через force_reply нет смысла. Шлём её раз при /start.
        if text == "/start":
            await _api(http, ctrl.token, "sendMessage", {
                "chat_id":      chat_id,
                "text":         "Клавиатура активирована 👇",
                "reply_markup": REPLY_KB,
            })

    elif text in ("/stop", "/стоп"):
        res   = await ctrl.stop()
        reply = "⏹ Парсер остановлен." if res == "stopped" else "Парсер уже не работает."
        await _reply(http, ctrl.token, chat_id, reply)

    elif text in ("/run", "/старт"):
        res = await ctrl.start()
        if res == "started":
            reply = f"▶️ Парсер запущен.\nРежим: {MODE_NAMES[ctrl.mode]}"
        elif res == "no_source":
            reply = "⚠️ SOURCE\\_CHANNEL не задан в .env"
        else:
            reply = "Парсер уже работает."
        await _reply(http, ctrl.token, chat_id, reply)

    elif text.startswith("/mode"):
        parts = text.split()
        if len(parts) < 2 or parts[1] not in MODE_NAMES:
            # Показываем inline-меню выбора режима
            kbd = {"inline_keyboard": [
                [
                    {"text": MODE_NAMES["1"] + (" ✓" if ctrl.mode == "1" else ""), "callback_data": "mode:1"},
                    {"text": MODE_NAMES["2"] + (" ✓" if ctrl.mode == "2" else ""), "callback_data": "mode:2"},
                ],
                [
                    {"text": MODE_NAMES["3"] + (" ✓" if ctrl.mode == "3" else ""), "callback_data": "mode:3"},
                    {"text": MODE_NAMES["4"] + (" ✓" if ctrl.mode == "4" else ""), "callback_data": "mode:4"},
                ],
            ]}
            await _api(http, ctrl.token, "sendMessage", {
                "chat_id":      chat_id,
                "text":         f"Текущий режим: *{MODE_NAMES.get(ctrl.mode, ctrl.mode)}*\n\nВыберите новый режим:",
                "parse_mode":   "Markdown",
                "reply_markup": kbd,
            })
        else:
            new_mode    = parts[1]
            was_running = ctrl.running
            if was_running:
                await ctrl.stop()
            ctrl.mode = new_mode
            reply = f"Режим изменён: *{MODE_NAMES[new_mode]}*"
            if was_running:
                await ctrl.start()
                reply += "\n▶️ Парсер перезапущен."
            await _reply(http, ctrl.token, chat_id, reply)

    elif text == "/pending":
        n     = len(ctrl.pending)
        reply = f"⏳ На утверждении: {n} постов." if n else "Нет постов на утверждении."
        await _reply(http, ctrl.token, chat_id, reply)

    elif text == "/help":
        reply = (
            "📖 *Справка*\n\n"
            "Используй кнопки внизу экрана или команды:\n\n"
            "*Управление:*\n"
            "📊 Статус — состояние парсера\n"
            "▶️ Запустить — включить парсер\n"
            "⏹ Остановить — выключить парсер\n\n"
            "*Режимы* \\(нажми или /mode\\):\n"
            "📦 Архив — перенести все старые посты\n"
            "👁 Мониторинг — только новые посты\n"
            "📦\\+👁 Оба — сначала архив, потом новые\n"
            "✅ Утверждение — каждый пост присылается тебе на одобрение"
        )
        await _api(http, ctrl.token, "sendMessage", {
            "chat_id":      chat_id,
            "text":         reply,
            "parse_mode":   "MarkdownV2",
            "reply_markup": REPLY_KB,
        })


# ── Long-polling loop ──────────────────────────────────────────────────────────

async def poll_loop(ctrl: Controller, http: aiohttp.ClientSession):
    offset = 0
    print("Бот запущен. Жду команды...")
    while True:
        try:
            async with http.get(
                f"https://api.telegram.org/bot{ctrl.token}/getUpdates",
                params={
                    "timeout":          30,
                    "offset":           offset,
                    "allowed_updates":  json.dumps(["message", "callback_query"]),
                },
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                data = await resp.json()

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                if "message" in update:
                    m      = update["message"]
                    sender = str(m.get("from", {}).get("id", ""))
                    if sender != ctrl.admin_id:
                        continue
                    await process_command(ctrl, http, m.get("text", ""), m["chat"]["id"])

                elif "callback_query" in update:
                    cb     = update["callback_query"]
                    sender = str(cb.get("from", {}).get("id", ""))
                    if sender != ctrl.admin_id:
                        continue
                    await ctrl.handle_callback(http, cb)

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[poll] Ошибка: {e}")
            await asyncio.sleep(5)


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
    autostart   = os.environ.get("AUTOSTART", "1")

    missing = [k for k, v in {
        "API_ID": api_id, "API_HASH": api_hash,
        "SESSION_STRING": session_str,
        "TARGET_CHANNEL": target,
        "BOT_TOKEN":      token,
        "ADMIN_ID":       admin_id,
    }.items() if not v]
    if missing:
        print(f"Не заданы переменные: {', '.join(missing)}")
        return

    reader = TelegramClient(
        StringSession(session_str), int(api_id), api_hash,
        timeout=60, request_retries=10, connection_retries=10, retry_delay=5,
    )
    await reader.start()
    print("Telethon подключён.")

    async with aiohttp.ClientSession() as http:
        ctrl = Controller(reader, token, source, target, admin_id, delay)
        ctrl._http = http
        await ctrl.initialize()

        if autostart == "1":
            res = await ctrl.start()
            print(f"Автозапуск: {res} (режим {ctrl.mode})")

        await _api(http, token, "sendMessage", {
            "chat_id":      admin_id,
            "text":         f"🤖 *Бот запущен*\n\n{ctrl.status_text()}",
            "parse_mode":   "Markdown",
            "reply_markup": REPLY_KB,
        })
        await _api(http, token, "sendMessage", {
            "chat_id":      admin_id,
            "text":         ctrl.status_text(),
            "parse_mode":   "Markdown",
            "reply_markup": ctrl.status_keyboard(),
        })

        await poll_loop(ctrl, http)

    await reader.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
