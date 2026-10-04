"""Telegram: бот собирает заявку по шагам и ведёт переписку, тот же бот через Telegram Business читает
входящие в обычном аккаунте, а менеджеру приходят уведомления о новых лидах.

Работает через long polling (getUpdates), поэтому домен и https для бота не нужны.
"""
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request

import db

log = logging.getLogger("bot")

API_BASE = os.environ.get("TG_API_BASE", "https://api.telegram.org")
ALLOWED_UPDATES = ["message", "business_connection", "business_message"]
POLL_TIMEOUT = 25      # сколько Telegram держит запрос getUpdates, если новостей нет
SOCKET_TIMEOUT = 40    # таймаут сокета обязан быть больше, иначе длинный опрос рвётся по таймауту
DIALOG_TTL = 24 * 3600  # брошенный на полпути диалог через сутки начинается заново

GREETING = ("Здравствуйте! Я приму вашу заявку и передам её менеджеру агентства.\n\n"
            "Как к вам обращаться?")
ASK_CONTACT = "Приятно познакомиться, {name}! Как с вами связаться — телефон, e-mail или @username?"
ASK_REQUEST = "Принято. Расскажите коротко, что нужно сделать?"
THANKS = ("Спасибо! Заявка №{id} принята, менеджер свяжется с вами.\n\n"
          "Если хотите что-то добавить, просто напишите сюда, сообщение уйдёт менеджеру. "
          "Новая заявка — команда /new.")
RETRY_TEXT = "Пожалуйста, ответьте текстом."
CANCELLED = "Хорошо, отменил. Если понадобится — напишите, начнём заново."
UNKNOWN_COMMAND = "Не знаю такой команды. /new — новая заявка, /cancel — отмена."

KIND_LABELS = {"photo": "фото", "video": "видео", "voice": "голосовое", "audio": "аудио", "document": "файл",
               "sticker": "стикер", "video_note": "кружок", "location": "геопозиция", "contact": "контакт"}


class TelegramAPI:
    def __init__(self, token):
        self.token = token

    def call(self, method, **params):
        """Параметры уходят в Telegram как есть, включая timeout длинного опроса getUpdates."""
        req = urllib.request.Request(
            f"{API_BASE}/bot{self.token}/{method}", data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                data = json.load(e)
            except Exception:
                raise RuntimeError(f"{method}: HTTP {e.code}") from None
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or f"{method}: ошибка")
        return data["result"]

    def send(self, chat_id, text, **extra):
        return self.call("sendMessage", chat_id=chat_id, text=text, **extra)


# ---------- разбор сообщений ----------

def full_name(u):
    return " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or u.get("username") or str(u.get("id"))


def message_text(m):
    if m.get("text"):
        return m["text"]
    for kind, label in KIND_LABELS.items():
        if kind in m:
            return f"[{label}]" + (f" {m['caption']}" if m.get("caption") else "")
    return "[сообщение]"


def with_username(contact, user):
    u = user.get("username")
    if u and f"@{u}".lower() not in contact.lower():
        return f"{contact} (@{u})" if contact else f"@{u}"
    return contact


def clean_ref(raw):
    """Метка из ссылки t.me/бот?start=instagram: превращается в тег лида."""
    return re.sub(r"[^\w.\-]", "", raw or "")[:32]


# ---------- уведомления менеджеру ----------

def notify(tg, text):
    """Пишет менеджеру, чей чат подключён командой /connect. Ошибки не должны ломать приём заявок."""
    cid = db.kv_get("notify_chat_id")
    if not cid or tg is None:
        return
    try:
        tg.send(int(cid), text)
    except Exception as e:
        log.warning("уведомление не отправлено: %s", e)


def notify_new_lead(tg, lead):
    parts = [f"Новый лид №{lead['id']} · {db.SOURCE_TAGS[lead['source']]}", lead["name"]]
    if lead["contact"]:
        parts.append(lead["contact"])
    if lead["request"]:
        parts.append(lead["request"][:300])
    notify(tg, "\n".join(parts))


def notify_message(tg, lead, text):
    notify(tg, f"Сообщение · №{lead['id']} {lead['name']}\n{text[:300]}")


def handle_connect(cid, arg, tg):
    code = db.kv_get("connect_code") or ""
    if arg and code and hmac.compare_digest(arg.strip().encode(), code.encode()):
        db.kv_set("notify_chat_id", cid)
        db.new_connect_code()  # код одноразовый
        tg.send(cid, "Готово. Сюда будут приходить новые заявки и сообщения клиентов.")
    else:
        tg.send(cid, "Код не подошёл. Возьмите актуальный в CRM: Настройки → Уведомления.")


# ---------- заявки через бота ----------

def dialog_is_stale(dialog):
    try:
        t = datetime.strptime(dialog["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return True
    return (datetime.now(timezone.utc) - t).total_seconds() > DIALOG_TTL


def start_dialog(cid, user, tg, ref=""):
    db.dialog_save(cid, "name", tg_user_id=user.get("id"), ref=ref)
    tg.send(cid, GREETING)


def handle_bot_message(m, tg):
    chat = m["chat"]
    if chat.get("type") != "private":
        return
    cid, user = chat["id"], m.get("from") or {}
    if user.get("is_bot"):
        return
    text = (m.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower() if text.startswith("/") else ""

    if cmd == "/connect":
        return handle_connect(cid, arg, tg)
    if cmd == "/cancel":
        db.dialog_drop(cid)
        tg.send(cid, CANCELLED, reply_markup={"remove_keyboard": True})
        return
    if cmd in ("/start", "/new"):
        return start_dialog(cid, user, tg, clean_ref(arg))
    if cmd:
        # неизвестная команда не должна стать «именем» или «запросом»
        tg.send(cid, UNKNOWN_COMMAND)
        return

    dialog = db.dialog_get(cid)
    if dialog and dialog_is_stale(dialog):
        db.dialog_drop(cid)
        dialog = None
    if dialog is None:
        lead = db.find_bot_lead(cid)
        if lead:
            # заявка уже есть: сообщения человека идут в её переписку, а не плодят новые заявки
            first_unread = lead["unread"] == 0
            db.add_message(lead["id"], "in", message_text(m))
            if first_unread:
                notify_message(tg, lead, message_text(m))
            return
        return start_dialog(cid, user, tg)

    step = dialog["step"]
    keep = dict(tg_user_id=dialog["tg_user_id"], ref=dialog["ref"])
    if step == "name":
        if not text or len(text) > 100:
            tg.send(cid, RETRY_TEXT if not text else "Слишком длинно, назовите только имя.")
            return
        db.dialog_save(cid, "contact", name=text, **keep)
        tg.send(cid, ASK_CONTACT.format(name=text), reply_markup={
            "keyboard": [[{"text": "Поделиться номером", "request_contact": True}]],
            "resize_keyboard": True, "one_time_keyboard": True})
    elif step == "contact":
        contact = (m.get("contact") or {}).get("phone_number") or text
        if not contact or len(contact) > 200:
            tg.send(cid, "Напишите телефон, e-mail или @username.")
            return
        db.dialog_save(cid, "request", name=dialog["name"], contact=contact, **keep)
        tg.send(cid, ASK_REQUEST, reply_markup={"remove_keyboard": True})
    elif step == "request":
        if not text:
            tg.send(cid, RETRY_TEXT)
            return
        lead = db.create_lead(
            dialog["name"], with_username(dialog["contact"], user), text[:2000], "bot",
            tags=[dialog["ref"]] if dialog["ref"] else [], tg_user_id=dialog["tg_user_id"], tg_chat_id=cid)
        db.dialog_drop(cid)
        tg.send(cid, THANKS.format(id=lead["id"]))
        notify_new_lead(tg, lead)
        log.info("лид #%s из бота", lead["id"])


# ---------- обычный Telegram через Business ----------

def handle_business_connection(bc):
    user = bc.get("user") or {}
    db.business_save(bc["id"], user.get("id", 0), user.get("username", ""), bc.get("is_enabled", True))
    log.info("business connection %s: %s", bc["id"], "вкл" if bc.get("is_enabled", True) else "выкл")


def handle_business_message(m, tg=None):
    chat, sender = m["chat"], m.get("from") or {}
    if m.get("sender_business_bot"):
        # это сообщение отправил бот от имени аккаунта (в том числе ответ из CRM): оно уже записано при отправке
        return
    bc = db.business_get(m.get("business_connection_id", ""))
    text = message_text(m)
    # В личном чате клиент пишет от своего id, он же id чата. Любой другой отправитель это владелец аккаунта:
    # так он распознаётся и тогда, когда апдейт business_connection пришёл до запуска бота и подключение неизвестно.
    if (bc and sender.get("id") == bc["user_id"]) or sender.get("id") != chat.get("id"):
        # писал сам владелец аккаунта: фиксируем в истории, но лидом это не становится
        lead = db.find_business_lead(chat["id"])
        if lead:
            db.add_message(lead["id"], "out", text)
        return
    if sender.get("is_bot"):
        return
    lead = db.find_business_lead(chat["id"])
    if lead:
        first_unread = lead["unread"] == 0
        db.add_message(lead["id"], "in", text)
        if first_unread:
            notify_message(tg, lead, text)
        return
    who = {**chat, **sender}
    contact = f"@{who['username']}" if who.get("username") else f"Telegram ID {chat['id']}"
    created = db.create_lead(
        full_name(who), contact, text[:2000], "telegram", tg_user_id=sender.get("id"), tg_chat_id=chat["id"],
        business_connection_id=m.get("business_connection_id"), first_message=text)
    notify_new_lead(tg, created)
    log.info("лид #%s из Telegram-аккаунта", created["id"])


def handle_update(update, tg):
    if "message" in update:
        handle_bot_message(update["message"], tg)
    elif "business_connection" in update:
        handle_business_connection(update["business_connection"])
    elif "business_message" in update:
        handle_business_message(update["business_message"], tg)


# ---------- поллер ----------

class BotRunner(threading.Thread):
    def __init__(self, token):
        super().__init__(daemon=True, name="bot")
        self.tg = TelegramAPI(token) if token else None
        self.status = {"enabled": bool(token), "running": False, "username": None, "error": None}
        self._halt = threading.Event()
        self._poll_timeout = POLL_TIMEOUT

    def stop(self):
        self._halt.set()

    def _connect(self):
        """Проверяет токен и снимает вебхук. Бот считается готовым только после обоих шагов, иначе при сбое
        на втором вебхук остался бы навсегда и getUpdates отвечал бы конфликтом."""
        me = self.tg.call("getMe")
        self.tg.call("deleteWebhook")  # если раньше у бота стоял вебхук, getUpdates не отдаст ничего
        self.status["username"] = me.get("username")
        log.info("бот @%s запущен", me.get("username"))

    def run(self):
        if not self.tg:
            self.status["error"] = "BOT_TOKEN не задан, бот выключен"
            return
        delay = 2
        while not self._halt.is_set():
            try:
                if not self.status["username"]:
                    self._connect()
                self.status.update(running=True, error=None)
                offset = int(db.kv_get("tg_offset", 0))
                started = time.monotonic()
                updates = self.tg.call("getUpdates", timeout=self._poll_timeout, offset=offset,
                                       allowed_updates=ALLOWED_UPDATES)
                if not updates and time.monotonic() - started < 1:
                    self._halt.wait(1)  # страховка: если «длинный» опрос вернулся мгновенно, не долбим API вхолостую
                for u in updates:
                    try:
                        handle_update(u, self.tg)
                    except Exception:
                        log.exception("ошибка в апдейте %s", u.get("update_id"))
                    # offset двигаем всегда, иначе один сломанный апдейт заблокирует очередь
                    db.kv_set("tg_offset", u["update_id"] + 1)
                delay = 2
            except Exception as e:
                self.status.update(running=False, error=str(e))
                log.warning("polling: %s, повтор через %ss", e, delay)
                self._halt.wait(delay)
                delay = min(delay * 2, 60)
        self.status["running"] = False
