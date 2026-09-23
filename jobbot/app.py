"""Сердце бота: юзербот читает каналы, бот-пульт присылает карточки, по кнопке — отклик."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl

from .config import Config
from .db import DB, Vacancy
from .filters import VacancyFilter, detect_lang, extract_contacts, fingerprint
from .letters import LetterWriter
from .webreader import ChannelUnavailable, WebReader

log = logging.getLogger(__name__)

REGIONS = {"georgia": "🇬🇪 Грузия", "ru": "🇷🇺 РФ/СНГ", "eu_remote": "🇪🇺 Европа/удалёнка",
           "design": "🎨 Дизайн", "product": "📦 Продукт", "freelance": "💼 Фриланс",
           "abroad": "🌍 Релокация и зарубеж", "web3": "🪙 Web3 и геймдев"}


async def safe_answer(cb, text: str | None = None) -> None:
    """Если кнопку нажали давно (режим по расписанию), Telegram уже не принимает ответ — это нормально."""
    try:
        await cb.answer(text)
    except Exception:
        pass


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


class JobBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.data_dir / "jobbot.sqlite3")
        self.db.sync_channels(cfg.channels)
        self.filter = VacancyFilter(cfg.keywords)
        self.writer = LetterWriter(cfg)
        # Лёгкий режим: нет ключей my.telegram.org → читаем каналы через веб, письма отправляешь ты сам по ссылке
        self.lite = not (cfg.api_id and cfg.api_hash and cfg.session_string)
        self.client = None if self.lite else TelegramClient(StringSession(cfg.session_string), cfg.api_id, cfg.api_hash)
        self.web = WebReader() if self.lite else None
        self.prefill_ok = True  # влезает ли письмо в ссылку-кнопку
        self.bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
        self.dp = Dispatcher()
        self.owner_id: int = 0
        self.send_lock = asyncio.Lock()
        self.scan_now = asyncio.Event()
        self.entities: dict[str, object] = {}
        self._register_handlers()

    @property
    def pending_edit(self) -> int | None:
        v = self.db.get_flag("pending_edit")
        return int(v) if v else None

    @pending_edit.setter
    def pending_edit(self, vid: int | None) -> None:
        self.db.set_flag("pending_edit", str(vid) if vid else "")

    # ══════════════════════════ запуск ══════════════════════════
    async def _connect(self):
        if self.lite:
            self.owner_id = int(os.environ.get("OWNER_ID") or self.db.get_flag("owner_id", "0") or 0)
            bot_me = await self.bot.get_me()
            log.info("Лёгкий режим. Каналов: %s, письма: %s", len(self.db.active_channels()), self.writer.mode)
            if not self.owner_id:
                log.warning("Владелец ещё не известен — открой бота @%s в Telegram и нажми /start", bot_me.username)
            return bot_me
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise SystemExit("Сессия Telegram недействительна — заново запусти login.py (README, шаг 3)")
        me = await self.client.get_me()
        self.owner_id = me.id
        bot_me = await self.bot.get_me()
        log.info("Подключился. Каналов: %s, письма: %s", len(self.db.active_channels()), self.writer.mode)
        return bot_me

    async def run_once(self) -> None:
        """Один проход для GitHub Actions: нажатые кнопки → новые вакансии → нажатые кнопки → выход."""
        bot_me = await self._connect()
        await self._autostart(bot_me)
        try:
            await self.process_pending_updates()
            if self.db.get_flag("paused") != "1":
                found = await self.scan_all()
                log.info("Проход завершён, новых подходящих: %s", found)
            await self.process_pending_updates()
        finally:
            await self.close()

    async def run_for(self, minutes: float, scan_every: float = 3) -> None:
        """Режим GitHub Actions: работаем `minutes` минут — сразу отвечаем в боте и проверяем каналы
        каждые `scan_every` минут, потом аккуратно выходим (следующий запуск подхватит)."""
        bot_me = await self._connect()
        await self._autostart(bot_me)
        deadline = time.monotonic() + minutes * 60
        next_scan = 0.0
        try:
            while time.monotonic() < deadline:
                if time.monotonic() >= next_scan:
                    if self.db.get_flag("paused") != "1":
                        found = await self.scan_all()
                        log.info("Проход завершён, новых подходящих: %s", found)
                    next_scan = time.monotonic() + scan_every * 60
                left = min(next_scan, deadline) - time.monotonic()
                await self.process_pending_updates(wait=max(1, min(25, int(left))))
        finally:
            await self.close()

    async def close(self) -> None:
        await self.bot.session.close()
        if self.client:
            await self.client.disconnect()
        if self.web:
            await self.web.close()

    async def process_pending_updates(self, wait: int = 0) -> None:
        """Забирает накопившиеся нажатия и сообщения из бота-пульта и обрабатывает их по очереди.
        wait > 0 — ждать новые сообщения до wait секунд (long polling)."""
        offset = int(self.db.get_flag("upd_offset", "0") or 0)
        while True:
            try:
                updates = await self.bot.get_updates(offset=offset or None, timeout=wait, limit=100,
                                                     allowed_updates=["message", "callback_query"])
            except Exception as e:
                log.warning("getUpdates: %s", e)
                await asyncio.sleep(3)
                return
            if not updates:
                break
            wait = 0
            for u in updates:
                try:
                    await self.dp.feed_update(self.bot, u)
                except Exception:
                    log.exception("Не смог обработать нажатие %s", u.update_id)
                offset = u.update_id + 1
                self.db.set_flag("upd_offset", str(offset))

    async def _autostart(self, bot_me) -> None:
        # Бот не может написать первым — поэтому твой аккаунт сам нажимает /start у бота (один раз)
        if self.lite:
            return
        if self.db.get_flag("started") != "1":
            try:
                await self.client.send_message(bot_me.username, "/start")
                self.db.set_flag("started", "1")
            except Exception as e:
                log.warning("Не получилось нажать /start у бота автоматически: %s", e)

    async def run(self) -> None:
        """Постоянный режим (компьютер или сервер)."""
        bot_me = await self._connect()

        await self._autostart(bot_me)
        await asyncio.gather(self.watch_loop(), self.dp.start_polling(self.bot, handle_signals=False))

    # ══════════════════════════ чтение каналов ══════════════════════════
    async def watch_loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            if self.db.get_flag("paused") != "1":
                found = await self.scan_all()
                log.info("Проход по каналам завершён, новых подходящих: %s", found)
            self.scan_now.clear()
            try:
                await asyncio.wait_for(self.scan_now.wait(), timeout=self.cfg.poll_minutes * 60)
            except asyncio.TimeoutError:
                pass

    async def scan_all(self) -> int:
        found = 0
        for ch in self.db.active_channels():
            try:
                found += await self.scan_channel(ch["username"], ch["region"], ch["last_id"])
            except errors.FloodWaitError as e:
                if e.seconds > 300:  # долгое ожидание — не держим запуск, продолжим в следующий раз
                    log.warning("FloodWait %s c — прерываю проход", e.seconds)
                    break
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:
                log.warning("Ошибка в канале %s: %s", ch["username"], e.__class__.__name__)
                self.db.set_channel_error(ch["username"], repr(e))
            await asyncio.sleep(1)  # бережём аккаунт
        return found

    async def _entity(self, username: str):
        if username not in self.entities:
            self.entities[username] = await self.client.get_entity(username)
        return self.entities[username]

    async def scan_channel(self, username: str, region: str, last_id: int) -> int:
        if self.lite:
            return await self.scan_channel_web(username, region, last_id)
        try:
            entity = await self._entity(username)
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, errors.ChannelPrivateError,
                ValueError) as e:
            self.db.set_channel_error(username, f"недоступен: {e.__class__.__name__}", disable=True)
            log.warning("Канал @%s недоступен, выключаю", username)
            return 0

        if last_id == 0:
            since = datetime.now(timezone.utc) - timedelta(hours=self.cfg.backfill_hours)
            msgs = []
            async for m in self.client.iter_messages(entity, limit=300):
                if m.date < since:
                    break
                msgs.append(m)
            if not msgs:  # тихий канал — просто запоминаем, где остановились
                async for m in self.client.iter_messages(entity, limit=1):
                    self.db.set_last_id(username, m.id)
                return 0
        else:
            msgs = [m async for m in self.client.iter_messages(entity, min_id=last_id, limit=300)]
        if not msgs:
            return 0

        found = 0
        is_group = bool(getattr(entity, "megagroup", False))
        for m in sorted(msgs, key=lambda x: x.id):
            if await self.process_message(m, username, region, is_group):
                found += 1
        self.db.set_last_id(username, max(m.id for m in msgs))
        return found

    async def scan_channel_web(self, username: str, region: str, last_id: int) -> int:
        try:
            posts, max_seen = await self.web.fetch_new(username, last_id, self.cfg.backfill_hours)
        except ChannelUnavailable as e:
            self.db.set_channel_error(username, f"недоступен: {e}", disable=True)
            log.warning("Канал @%s недоступен (%s), выключаю", username, e)
            return 0
        found = 0
        for p in posts:
            if await self.process_post(username, region, p.id, p.text, p.links):
                found += 1
        if max_seen > last_id:
            self.db.set_last_id(username, max_seen)
        return found

    async def process_message(self, m, username: str, region: str, is_group: bool) -> bool:
        """Сообщение из Telethon (полный режим)."""
        hidden_urls = [ent.url for ent in (m.entities or []) if isinstance(ent, MessageEntityTextUrl)]

        async def sender_username():
            # В чатах вакансию обычно публикует сам рекрутер — ему и пишем, если в тексте нет другого контакта
            if not is_group:
                return None
            try:
                sender = await m.get_sender()
                if sender is not None and getattr(sender, "username", None) and not getattr(sender, "bot", False):
                    return sender.username
            except Exception:
                pass
            return None
        return await self.process_post(username, region, m.id, m.message or "", hidden_urls, sender_username)

    async def process_post(self, username: str, region: str, msg_id: int, text: str,
                           hidden_urls: list[str], sender_lookup=None) -> bool:
        verdict = self.filter.check(text)
        if not verdict.ok or verdict.score < self.cfg.min_score:
            return False
        fp = fingerprint(text)
        if self.db.seen(fp):
            return False

        contacts = extract_contacts(text, own_username=username, entity_urls=hidden_urls)
        if sender_lookup and not contacts.primary_tg:
            name = await sender_lookup()
            if name:
                contacts.telegram.append(name)
        if self.cfg.only_direct and not contacts.primary_tg:
            return False  # нельзя написать человеку в Telegram напрямую — не присылаем

        lang = detect_lang(text)
        letter = await self.writer.write(text, lang)
        vid = self.db.add_vacancy(
            fingerprint=fp, channel=username, msg_id=msg_id, link=f"https://t.me/{username}/{msg_id}", text=text,
            region=region, lang=lang, score=verdict.score, hits=verdict.hits, contact_tg=contacts.primary_tg,
            contacts={"telegram": contacts.telegram, "emails": contacts.emails, "urls": contacts.urls[:5]},
            letter=letter,
        )
        if vid:
            await self.send_card(vid)
        return bool(vid)

    # ══════════════════════════ карточки ══════════════════════════
    def write_url(self, v: Vacancy) -> str:
        """Ссылка, открывающая чат с рекрутером; письмо уже вставлено в поле ввода."""
        base = f"https://t.me/{v.contact_tg}"
        return f"{base}?text={quote(v.letter)}" if self.prefill_ok else base

    def _keyboard(self, v: Vacancy) -> InlineKeyboardMarkup:
        rows = []
        if v.contact_tg and self.lite:
            rows.append([InlineKeyboardButton(text=f"✍️ Написать @{v.contact_tg}", url=self.write_url(v)),
                         InlineKeyboardButton(text="✅ Отправил", callback_data=f"done:{v.id}")])
        elif v.contact_tg:
            rows.append([InlineKeyboardButton(text=f"✅ Отправить @{v.contact_tg}", callback_data=f"send:{v.id}")])
        elif self.lite:
            rows.append([InlineKeyboardButton(text="✅ Откликнулся", callback_data=f"done:{v.id}")])
        second = [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit:{v.id}")]
        if self.writer.client:
            second.append(InlineKeyboardButton(text="🔁 Переписать", callback_data=f"regen:{v.id}"))
        second.append(InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"skip:{v.id}"))
        rows.append(second)
        rows.append([InlineKeyboardButton(text="🔗 Открыть пост", url=v.link)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _card_text(self, v: Vacancy) -> str:
        excerpt = v.text if len(v.text) <= 1500 else v.text[:1500] + "…"
        c = v.contacts or {}
        if v.contact_tg:
            contact = f"Кому: @{esc(v.contact_tg)}"
            days = self.cfg.limits["same_contact_days"]
            if self.db.contacted_recently(v.contact_tg, days):
                contact += f"\n⚠️ <i>Ему уже писал за последние {days} дней — подумай, стоит ли второй раз</i>"
        else:
            parts = []
            if c.get("telegram"):
                parts.append("бот " + ", ".join("@" + esc(x) for x in c["telegram"]))
            if c.get("emails"):
                parts.append("почта " + ", ".join(esc(x) for x in c["emails"]))
            if c.get("urls"):
                parts.append("ссылка " + esc(c["urls"][0]))
            contact = "Контакта в TG нет — " + ("; ".join(parts) if parts else "смотри пост") + \
                      "\n<i>Скопируй письмо и откликнись там, куда просят в посте</i>"
        hits = ", ".join(v.hits[:5])
        return (f"<b>{REGIONS.get(v.region, v.region)}</b> · @{esc(v.channel)} · балл {v.score}\n"
                f"<i>{esc(hits)}</i>\n\n{esc(excerpt)}\n\n{contact}")

    def _letter_text(self, v: Vacancy, note: str = "") -> str:
        lang = "RU" if v.lang == "ru" else "EN"
        if self.lite:
            hint = ""
            if v.contact_tg and not note:
                hint = ("\n<i>«Написать» откроет чат с уже вставленным письмом — останется нажать «Отправить». "
                        "Потом отметь здесь «✅ Отправил».</i>" if self.prefill_ok else
                        "\n<i>Скопируй письмо (кнопка копирования у блока), нажми «Написать», вставь и отправь. "
                        "Потом отметь «✅ Отправил».</i>")
            return f"✉️ <b>Отклик ({lang})</b>{note}{hint}\n\n<pre>{esc(v.letter)}</pre>"
        return f"✉️ <b>Отклик ({lang})</b>{note}\n\n{esc(v.letter)}"

    async def send_card(self, vid: int) -> None:
        v = self.db.get(vid)
        try:
            if not self.owner_id:
                return  # карточка останется в базе и придёт после /start
            if self.lite:
                await self._send_lite_card(v)
                return
            card = await self.bot.send_message(self.owner_id, self._card_text(v), disable_web_page_preview=True)
            try:
                letter = await self.bot.send_message(self.owner_id, self._letter_text(v),
                                                     reply_markup=self._keyboard(v), disable_web_page_preview=True)
            except TelegramBadRequest:
                if not (self.lite and self.prefill_ok):
                    raise
                self.prefill_ok = False  # письмо слишком длинное для ссылки — даём копировать вручную
                try:
                    letter = await self.bot.send_message(self.owner_id, self._letter_text(v),
                                                         reply_markup=self._keyboard(v), disable_web_page_preview=True)
                finally:
                    self.prefill_ok = True
            self.db.update(vid, card_msg_id=card.message_id, letter_msg_id=letter.message_id)
        except TelegramForbiddenError:
            log.error("Бот не может тебе писать — открой бота в Telegram и нажми /start")

    def _lite_keyboard(self, v: Vacancy) -> InlineKeyboardMarkup:
        rows = []
        if v.contact_tg:
            rows.append([InlineKeyboardButton(text=f"✍️ Написать @{v.contact_tg}", url=self.write_url(v))])
        rows.append([InlineKeyboardButton(text="🔗 Открыть вакансию", url=v.link)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _send_lite_card(self, v: Vacancy) -> None:
        """Лёгкий режим: одно сообщение — текст вакансии и две кнопки."""
        text = esc(v.text if len(v.text) <= 3500 else v.text[:3500] + "…")
        try:
            msg = await self.bot.send_message(self.owner_id, text, reply_markup=self._lite_keyboard(v),
                                              disable_web_page_preview=True)
        except TelegramBadRequest:
            self.prefill_ok = False  # письмо не влезло в ссылку — кнопка просто откроет чат
            try:
                msg = await self.bot.send_message(self.owner_id, text, reply_markup=self._lite_keyboard(v),
                                                  disable_web_page_preview=True)
            finally:
                self.prefill_ok = True
        self.db.update(v.id, card_msg_id=msg.message_id, letter_msg_id=msg.message_id)

    async def refresh_letter(self, v: Vacancy, note: str = "", keyboard: bool = True) -> None:
        if not v.letter_msg_id:
            return
        async def edit():
            await self.bot.edit_message_text(self._letter_text(v, note), chat_id=self.owner_id,
                                             message_id=v.letter_msg_id,
                                             reply_markup=self._keyboard(v) if keyboard else None,
                                             disable_web_page_preview=True)
        try:
            await edit()
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            if not (self.lite and self.prefill_ok and keyboard):
                raise
            self.prefill_ok = False
            try:
                await edit()
            finally:
                self.prefill_ok = True

    async def deliver_pending(self) -> int:
        """Присылает карточки, которые нашлись, пока бот не знал, кому их слать."""
        ids = self.db.undelivered(limit=15)
        for vid in ids:
            await self.send_card(vid)
            await asyncio.sleep(0.5)
        return len(ids)

    # ══════════════════════════ отправка ══════════════════════════
    async def send_application(self, v: Vacancy) -> str:
        """Отправляет отклик от твоего аккаунта. Возвращает текст статуса."""
        lim = self.cfg.limits
        if v.status == "sent":
            return "Уже отправлено"
        if not v.contact_tg:
            return "Нет контакта в Telegram"
        if self.db.contacted_recently(v.contact_tg, lim["same_contact_days"]):
            return f"@{v.contact_tg} уже писали за последние {lim['same_contact_days']} дней — пропускаю, чтобы не спамить"
        if self.db.sends_last_24h() >= lim["max_sends_per_day"]:
            return f"Дневной лимит {lim['max_sends_per_day']} откликов исчерпан. Продолжим завтра"

        async with self.send_lock:
            wait = self.db.last_send_at() + lim["min_delay_seconds"] - time.time()
            if wait > 0:
                await self.refresh_letter(v, note=f" · ⏳ в очереди, ~{int(wait)} с", keyboard=False)
                await asyncio.sleep(wait)
            try:
                await self.client.send_message(v.contact_tg, v.letter, link_preview=False)
                pdf = self.cfg.portfolio_pdf
                if self.cfg.send_pdf and pdf and pdf.exists():
                    await self.client.send_file(v.contact_tg, str(pdf))
            except errors.PeerFloodError:
                self.db.set_flag("paused", "1")
                self.db.update(v.id, status="failed", error="PeerFlood")
                return ("⛔️ Telegram временно ограничил сообщения незнакомым людям (PeerFlood). "
                        "Я поставил бота на паузу. Подожди сутки, проверь @SpamBot, потом /resume")
            except errors.FloodWaitError as e:
                return f"Telegram просит подождать {e.seconds} с — попробуй позже"
            except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
                self.db.update(v.id, status="failed", error="контакт не найден")
                return f"Аккаунт @{v.contact_tg} не найден — откликнись по ссылке в посте"
            except (errors.UserPrivacyRestrictedError, errors.UserIsBlockedError, errors.ChatWriteForbiddenError):
                self.db.update(v.id, status="failed", error="закрыты сообщения")
                return f"@{v.contact_tg} закрыл личные сообщения — откликнись по ссылке в посте"
            self.db.record_send(v.contact_tg)
            self.db.update(v.id, status="sent", sent_at=time.time())
            return "ok"

    # ══════════════════════════ пульт ══════════════════════════
    def _register_handlers(self) -> None:
        dp = self.dp
        is_owner = F.from_user.id.func(lambda uid: uid == self.owner_id)

        @dp.message(Command("start"), F.from_user.id.func(lambda uid: self.lite and not self.owner_id))
        async def claim(msg: Message):
            # Лёгкий режим: первый, кто нажал /start, становится хозяином бота
            self.owner_id = msg.from_user.id
            self.db.set_flag("owner_id", str(self.owner_id))
            await msg.answer(f"Привет! Запомнил тебя как хозяина бота (ID <code>{self.owner_id}</code>). "
                             "Теперь сюда будут приходить вакансии.")
            await start(msg)
            await self.deliver_pending()

        @dp.message(Command("start", "help"), is_owner)
        async def start(msg: Message):
            how = ("Под каждой вакансией: ✍️ «Написать» открывает чат с рекрутером, письмо уже вставлено — "
                   "останется нажать «Отправить». Потом отметь «✅ Отправил». ✏️ — поправить текст, ⏭ — пропустить."
                   if self.lite else
                   "Под каждой вакансией кнопки: ✅ отправить от твоего имени (+PDF), ✏️ поправить текст, "
                   "⏭ пропустить. Без твоего нажатия никому ничего не уходит.")
            await msg.answer(
                "<b>JobBot на связи.</b>\n\n"
                f"Слежу за {len(self.db.active_channels())} каналами. Письма: {self.writer.mode}.\n\n"
                f"{how}\n\n"
                "/stats — статистика\n/scan — проверить каналы сейчас\n/pause и /resume — пауза\n"
                "/channels — список каналов\n/add имя регион — добавить канал (регион: georgia, ru, eu_remote)\n"
                "/remove имя — убрать канал\n/pending — прислать карточки, которые не дошли")

        @dp.message(Command("pending"), is_owner)
        async def pending(msg: Message):
            if not await self.deliver_pending():
                await msg.answer("Недоставленных карточек нет")

        @dp.message(Command("stats"), is_owner)
        async def stats(msg: Message):
            s = self.db.stats()
            lim = self.cfg.limits
            paused = " · ⏸ на паузе" if self.db.get_flag("paused") == "1" else ""
            await msg.answer(
                f"Найдено всего: {sum(v for k, v in s.items() if k != 'sent_24h')}\n"
                f"Ждут решения: {s.get('new', 0)}\nОтправлено: {s.get('sent', 0)}\n"
                f"Пропущено: {s.get('skipped', 0)}\nНе ушло: {s.get('failed', 0)}\n"
                f"За сутки: {s['sent_24h']} из {lim['max_sends_per_day']}{paused}")

        @dp.message(Command("pause"), is_owner)
        async def pause(msg: Message):
            self.db.set_flag("paused", "1")
            await msg.answer("⏸ Пауза: каналы не читаю. /resume — продолжить")

        @dp.message(Command("resume"), is_owner)
        async def resume(msg: Message):
            self.db.set_flag("paused", "0")
            self.scan_now.set()
            await msg.answer("▶️ Продолжаю, сейчас пройдусь по каналам")

        @dp.message(Command("scan"), is_owner)
        async def scan(msg: Message):
            self.scan_now.set()
            await msg.answer("🔎 Проверяю каналы…")

        @dp.message(Command("channels"), is_owner)
        async def channels(msg: Message):
            lines, cur = [], None
            for ch in self.db.all_channels():
                if ch["region"] != cur:
                    cur = ch["region"]
                    lines.append(f"\n<b>{REGIONS.get(cur, cur)}</b>")
                mark = "✅" if ch["enabled"] else "🚫"
                err = f" — {esc(ch['error'])}" if ch["error"] else ""
                lines.append(f"{mark} @{esc(ch['username'])}{err}")
            await msg.answer("\n".join(lines)[:4000])

        @dp.message(Command("add"), is_owner)
        async def add(msg: Message):
            parts = (msg.text or "").split()
            if len(parts) < 2:
                return await msg.answer("Формат: /add имя_канала регион (georgia, ru, eu_remote)")
            region = parts[2] if len(parts) > 2 else "ru"
            name = parts[1].split("/")[-1].lstrip("@")
            self.db.add_channel(name, region)
            self.entities.pop(name, None)
            self.scan_now.set()
            await msg.answer(f"Добавил @{esc(name)} ({REGIONS.get(region, region)}), сейчас проверю")

        @dp.message(Command("remove"), is_owner)
        async def remove(msg: Message):
            parts = (msg.text or "").split()
            if len(parts) < 2:
                return await msg.answer("Формат: /remove имя_канала")
            ok = self.db.disable_channel(parts[1].split("/")[-1])
            await msg.answer("Убрал" if ok else "Такого канала нет в списке")

        @dp.callback_query(F.data.startswith("send:"), is_owner)
        async def on_send(cb: CallbackQuery):
            v = self.db.get(int(cb.data.split(":")[1]))
            await safe_answer(cb, "Отправляю…")
            result = await self.send_application(v)
            v = self.db.get(v.id)
            if result == "ok":
                when = datetime.now().strftime("%H:%M")
                await self.refresh_letter(v, note=f" · ✅ отправлено @{esc(v.contact_tg)} в {when}", keyboard=False)
            else:
                await self.refresh_letter(v)
                await cb.message.answer(result)

        @dp.callback_query(F.data.startswith("done:"), is_owner)
        async def on_done(cb: CallbackQuery):
            v = self.db.get(int(cb.data.split(":")[1]))
            if v.status != "sent":
                if v.contact_tg:
                    self.db.record_send(v.contact_tg)
                self.db.update(v.id, status="sent", sent_at=time.time())
            await safe_answer(cb, "Отмечено 👍")
            when = datetime.now().strftime("%d.%m %H:%M")
            await self.refresh_letter(self.db.get(v.id), note=f" · ✅ отправлено {when}", keyboard=False)

        @dp.callback_query(F.data.startswith("skip:"), is_owner)
        async def on_skip(cb: CallbackQuery):
            v = self.db.get(int(cb.data.split(":")[1]))
            self.db.update(v.id, status="skipped")
            await safe_answer(cb, "Пропущено")
            await self.refresh_letter(self.db.get(v.id), note=" · ⏭ пропущено", keyboard=False)

        @dp.callback_query(F.data.startswith("regen:"), is_owner)
        async def on_regen(cb: CallbackQuery):
            v = self.db.get(int(cb.data.split(":")[1]))
            await safe_answer(cb, "Переписываю…")
            new = await self.writer.write(v.text, v.lang, "напиши другой вариант, не повторяй прошлый: " + v.letter[:300])
            self.db.update(v.id, letter=new)
            await self.refresh_letter(self.db.get(v.id))

        @dp.callback_query(F.data.startswith("edit:"), is_owner)
        async def on_edit(cb: CallbackQuery):
            self.pending_edit = int(cb.data.split(":")[1])
            await safe_answer(cb)
            hint = ("Пришли новый текст письма целиком.\n"
                    "Или начни с «!», чтобы дать указание нейросети — например: <code>! короче и упомяни Figma</code>"
                    if self.writer.client else "Пришли новый текст письма целиком.")
            await cb.message.answer(hint)

        @dp.message(F.text, is_owner)
        async def on_text(msg: Message):
            if self.pending_edit is None or msg.text.startswith("/"):
                return await msg.answer("Команды — /help. Чтобы поправить письмо, нажми ✏️ под ним.")
            v = self.db.get(self.pending_edit)
            self.pending_edit = None
            text = msg.text.strip()
            if text.startswith("!") and self.writer.client:
                await msg.answer("Переписываю…")
                new = await self.writer.write(v.text, v.lang, text[1:].strip() + "\nПрошлый вариант: " + v.letter)
            else:
                new = text
            self.db.update(v.id, letter=new)
            await self.refresh_letter(self.db.get(v.id), note=" · ✏️ изменено")
            await msg.answer("Готово — письмо под вакансией обновлено, жми ✅ когда будешь готов")
