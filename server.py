"""HTTP-сервер мини-CRM: JSON API + статика + запуск Telegram-бота. Только стандартная библиотека."""
import csv
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import bot
import db

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
PASSWORD = os.environ.get("CRM_PASSWORD", "admin")
TRUST_PROXY = os.environ.get("CRM_TRUST_PROXY") == "1"
SESSION_TTL = 14 * 24 * 3600
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

runner = None  # BotRunner, создаётся в main()

CSP = ("default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'")


# ---------- сессии и ограничения ----------

def secret():
    """Ключ подписи сессий. В него замешан пароль: сменили пароль, и все старые сессии перестали действовать."""
    s = os.environ.get("CRM_SECRET") or db.kv_get("session_secret")
    if not s:
        s = secrets.token_hex(32)
        db.kv_set("session_secret", s)
    return hashlib.sha256(s.encode() + b"|" + PASSWORD.encode()).digest()


LOOPBACK = ("127.0.0.1", "::1", "localhost")


def check_startup(host, password):
    """Не даём выставить в сеть CRM с паролем по умолчанию: в ней персональные данные клиентов."""
    if password == "admin" and host not in LOOPBACK:
        raise SystemExit("CRM_PASSWORD не задан: с паролем по умолчанию слушать адрес %s нельзя. "
                         "Задайте свой пароль через CRM_PASSWORD." % host)


def make_session():
    exp = str(int(time.time()) + SESSION_TTL)
    return exp + "." + hmac.new(secret(), exp.encode(), hashlib.sha256).hexdigest()


def valid_session(token):
    try:
        exp, sig = token.split(".", 1)
        ok = hmac.compare_digest(sig.encode(), hmac.new(secret(), exp.encode(), hashlib.sha256).hexdigest().encode())
        return ok and int(exp) > time.time()
    except Exception:
        return False


_hits = defaultdict(deque)


def rate_hit(key, limit, window):
    """True, если действие ещё укладывается в лимит. Счётчики в памяти, после перезапуска обнуляются."""
    if len(_hits) > 5000:  # чтобы словарь не разрастался от случайных адресов
        _hits.clear()
    q, t = _hits[key], time.time()
    while q and q[0] < t - window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(t)
    return True


# ---------- проверка входных данных ----------

class ApiError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


class Raw:
    def __init__(self, body, ctype, headers=()):
        self.body, self.ctype, self.headers = body, ctype, headers


def clean_lead_input(data, partial):
    out = {}
    for k, lim in {"name": 100, "contact": 200, "request": 2000, "note": 4000, "assignee": 40}.items():
        if k in data:
            out[k] = str(data[k]).strip()[:lim]
    if "status" in data:
        if data["status"] not in db.STATUSES:
            raise ApiError(400, "Неизвестный статус")
        out["status"] = data["status"]
    if "due_at" in data:
        v = str(data["due_at"] or "").strip()
        if v:
            # fromisoformat в Python 3.11+ принимает ещё и «20261005» с «2026-W41-1», а сравнения дат в базе строковые
            if not DATE_RE.fullmatch(v):
                raise ApiError(400, "Срок должен быть датой ГГГГ-ММ-ДД")
            try:
                date.fromisoformat(v)
            except ValueError:
                raise ApiError(400, "Срок должен быть датой ГГГГ-ММ-ДД")
        out["due_at"] = v
    if "amount" in data:
        try:
            out["amount"] = int(data["amount"] or 0)
        except (TypeError, ValueError):
            raise ApiError(400, "Сумма должна быть числом")
        if not 0 <= out["amount"] <= 10**10:
            raise ApiError(400, "Сумма вне допустимых пределов")
    if "tags" in data:
        if not isinstance(data["tags"], list):
            raise ApiError(400, "tags должен быть списком")
        out["tags"] = [str(t) for t in data["tags"]][:20]
    if data.get("read"):
        out["read"] = True
    if not partial and not out.get("name"):
        raise ApiError(400, "Укажите имя")
    if "name" in out and not out["name"]:
        raise ApiError(400, "Имя не может быть пустым")
    return out


def csv_safe(v):
    """Защита от формул в Excel: ячейка, начинающаяся с = + - @, получает апостроф."""
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


EXPORT_COLUMNS = [("id", "ID"), ("created_at", "Создан"), ("name", "Имя"), ("contact", "Контакт"),
                  ("request", "Запрос"), ("source", "Источник"), ("status", "Статус"), ("tags", "Теги"),
                  ("assignee", "Ответственный"), ("due_at", "Срок"), ("amount", "Сумма"), ("note", "Заметка")]
IMPORT_ALIASES = {
    "name": ("name", "имя", "фио", "клиент"), "contact": ("contact", "контакт", "телефон", "phone", "email", "e-mail", "почта"),
    "request": ("request", "запрос", "комментарий", "comment", "задача"), "tags": ("tags", "tag", "теги", "тег"),
}


def query_filters(qs):
    g = lambda k: (qs.get(k) or [None])[0]
    status, source = g("status"), g("source")
    if status and status not in db.STATUSES:
        raise ApiError(400, "Неизвестный статус")
    if source and source not in db.SOURCES:
        raise ApiError(400, "Неизвестный источник")
    try:
        off = int(g("tz") or 0)
    except ValueError:
        off = 0
    return dict(tag=g("tag"), q=g("q"), status=status, source=source, assignee=g("assignee"), due=g("due"),
                unread=g("unread") == "1", sort=g("sort") or "updated", offset_min=max(-840, min(840, off)))


# ---------- маршруты ----------

ROUTES = []


def route(method, pattern, auth=True):
    def deco(fn):
        ROUTES.append((method, re.compile(pattern + r"\Z"), fn, auth))
        return fn
    return deco


@route("POST", r"/api/login", auth=False)
def login(req):
    if not rate_hit(("login", req.ip), 10, 300):
        raise ApiError(429, "Слишком много попыток, подождите несколько минут")
    # сравниваем байты: для строк с кириллицей compare_digest бросает TypeError
    if not hmac.compare_digest(str(req.json().get("password", "")).encode(), PASSWORD.encode()):
        time.sleep(0.5)
        raise ApiError(401, "Неверный пароль")
    secure = "; Secure" if req.headers.get("X-Forwarded-Proto") == "https" else ""
    cookie = f"crm_session={make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}{secure}"
    return Raw(b'{"ok": true}', "application/json", [("Set-Cookie", cookie)])


@route("POST", r"/api/logout", auth=False)
def logout(req):
    return Raw(b'{"ok": true}', "application/json",
               [("Set-Cookie", "crm_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")])


@route("POST", r"/api/public/lead", auth=False)
def public_lead(req):
    """Публичная форма заявки: без входа, но с ограничением по IP и скрытым полем-ловушкой для ботов."""
    d = req.json()
    if str(d.get("website", "")).strip():
        return {"ok": True}  # заполнил скрытое поле — это бот, делаем вид, что приняли
    if not rate_hit(("form", req.ip), 5, 600):
        raise ApiError(429, "Слишком много заявок, попробуйте позже")
    name, contact = str(d.get("name", "")).strip()[:100], str(d.get("contact", "")).strip()[:200]
    if not name or not contact:
        raise ApiError(400, "Укажите имя и как с вами связаться")
    ref = bot.clean_ref(str(d.get("ref", "")))
    lead = db.create_lead(name, contact, str(d.get("request", "")).strip()[:2000], "web", tags=[ref] if ref else [])
    bot.notify_new_lead(runner.tg if runner else None, lead)
    return 201, {"ok": True}


@route("GET", r"/api/state")
def state(req):
    off = query_filters(req.qs)["offset_min"]
    return {"bot": runner.status if runner else {"enabled": False, "error": "бот не запущен"},
            "business": db.business_list(), "stats": db.light_stats(off), "rev": db.revision(),
            "managers": [m["name"] for m in db.list_managers()]}


@route("GET", r"/api/stats")
def stats(req):
    f = query_filters(req.qs)
    try:
        days = max(7, min(90, int((req.qs.get("days") or [30])[0])))
    except ValueError:
        days = 30
    return db.full_stats(f["offset_min"], days)


@route("GET", r"/api/settings")
def settings(req):
    return {"rules": db.list_rules(), "managers": db.list_managers(),
            "notify": {"connected": bool(db.kv_get("notify_chat_id")), "code": db.connect_code()},
            "bot": {"username": runner.status.get("username") if runner else None},
            "business": db.business_list()}


@route("POST", r"/api/notify/code")
def notify_code(req):
    return {"code": db.new_connect_code()}


@route("POST", r"/api/notify/disconnect")
def notify_disconnect(req):
    db.kv_delete("notify_chat_id")
    return {"ok": True}


@route("GET", r"/api/tags")
def tags_list(req):
    return db.list_tags()


@route("PUT", r"/api/tags/(\d{1,15})")
def tag_update(req, tid):
    d = req.json()
    name = color = None
    if "name" in d:
        name = " ".join(str(d["name"]).split())[:32]
        if not name:
            raise ApiError(400, "Название тега не может быть пустым")
    if "color" in d:
        color = str(d["color"])
        if not COLOR_RE.fullmatch(color):
            raise ApiError(400, "Цвет должен быть вида #RRGGBB")
    tag = db.update_tag(int(tid), name, color)
    if not tag:
        raise ApiError(404, "Тег не найден")
    return tag


@route("DELETE", r"/api/tags/(\d{1,15})")
def tag_delete(req, tid):
    if not db.delete_tag(int(tid)):
        raise ApiError(404, "Тег не найден")
    return {"ok": True}


@route("POST", r"/api/rules")
def rule_add(req):
    d = req.json()
    rule = db.add_rule(str(d.get("keyword", "")), str(d.get("tag", "")))
    if not rule:
        raise ApiError(400, "Укажите слово и тег")
    return 201, rule


@route("DELETE", r"/api/rules/(\d{1,15})")
def rule_delete(req, rid):
    if not db.delete_rule(int(rid)):
        raise ApiError(404, "Правило не найдено")
    return {"ok": True}


@route("POST", r"/api/managers")
def manager_add(req):
    m = db.add_manager(str(req.json().get("name", "")))
    if not m:
        raise ApiError(400, "Укажите имя")
    return 201, m


@route("DELETE", r"/api/managers/(\d{1,15})")
def manager_delete(req, mid):
    if not db.delete_manager(int(mid)):
        raise ApiError(404, "Не найдено")
    return {"ok": True}


@route("GET", r"/api/leads")
def leads_list(req):
    return db.list_leads(**query_filters(req.qs))


@route("POST", r"/api/leads")
def lead_create(req):
    d = clean_lead_input(req.json(), partial=False)
    lead = db.create_lead(d["name"], d.get("contact", ""), d.get("request", ""), "manual", tags=d.get("tags", []),
                          assignee=d.get("assignee", ""), due_at=d.get("due_at", ""), amount=d.get("amount", 0))
    if d.get("status") and d["status"] != "new" or d.get("note"):
        lead = db.update_lead(lead["id"], {k: d[k] for k in ("status", "note") if k in d})
    return 201, lead


@route("POST", r"/api/leads/bulk")
def lead_bulk(req):
    d = req.json()
    ids = d.get("ids")
    if (not isinstance(ids, list) or not ids or len(ids) > 1000
            or not all(isinstance(i, int) and not isinstance(i, bool) and 0 < i < 10**15 for i in ids)):
        raise ApiError(400, "Выберите лидов")
    action, value = d.get("action"), str(d.get("value", "")).strip()
    if action == "status" and value not in db.STATUSES:
        raise ApiError(400, "Неизвестный статус")
    if action in ("add_tag", "remove_tag") and not value:
        raise ApiError(400, "Укажите тег")
    if action not in ("status", "assignee", "add_tag", "remove_tag", "delete"):
        raise ApiError(400, "Неизвестное действие")
    return {"count": db.bulk(ids, action, value[:40])}


@route("GET", r"/api/leads/(\d{1,15})")
def lead_get(req, lid):
    lead = db.get_lead(int(lid))
    if not lead:
        raise ApiError(404, "Лид не найден")
    return lead


@route("PUT", r"/api/leads/(\d{1,15})")
def lead_update(req, lid):
    lead = db.update_lead(int(lid), clean_lead_input(req.json(), partial=True))
    if not lead:
        raise ApiError(404, "Лид не найден")
    return lead


@route("DELETE", r"/api/leads/(\d{1,15})")
def lead_delete(req, lid):
    if not db.delete_lead(int(lid)):
        raise ApiError(404, "Лид не найден")
    return {"ok": True}


@route("POST", r"/api/leads/(\d{1,15})/reply")
def lead_reply(req, lid):
    """Ответ клиенту прямо из CRM: через бота или от имени подключённого аккаунта (Business)."""
    lead = db.get_lead(int(lid))
    if not lead:
        raise ApiError(404, "Лид не найден")
    text = str(req.json().get("text", "")).strip()
    if not text or len(text) > 4000:
        raise ApiError(400, "Введите текст ответа (до 4000 символов)")
    tg = runner.tg if runner else None
    if lead["source"] == "bot" and lead["tg_chat_id"]:
        extra = {}
    elif lead["source"] == "telegram" and lead["business_connection_id"]:
        extra = {"business_connection_id": lead["business_connection_id"]}
    else:
        raise ApiError(400, "Для этого источника ответ из CRM недоступен")
    if not tg:
        raise ApiError(503, "Бот не запущен")
    try:
        tg.send(lead["tg_chat_id"], text, **extra)
    except Exception as e:
        raise ApiError(502, f"Telegram не принял сообщение: {e}")
    db.add_message(lead["id"], "out", text)
    if lead["status"] == "new":
        db.update_lead(lead["id"], {"status": "in_work"})
    return db.get_lead(lead["id"])


@route("GET", r"/api/export\.csv")
def export_csv(req):
    leads = db.list_leads(**{**query_filters(req.qs), "limit": -1})  # -1: без ограничения, в файле все лиды
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([title for _, title in EXPORT_COLUMNS])
    for l in leads:
        l = {**l, "tags": ", ".join(t["name"] for t in l["tags"]), "source": db.SOURCE_TAGS[l["source"]],
             "status": db.STATUS_LABELS[l["status"]]}
        w.writerow([csv_safe(l[k]) for k, _ in EXPORT_COLUMNS])
    # BOM нужен, чтобы Excel открыл кириллицу правильно
    return Raw(("\ufeff" + buf.getvalue()).encode(), "text/csv; charset=utf-8",
               [("Content-Disposition", f'attachment; filename="leads-{date.today().isoformat()}.csv"')])


@route("POST", r"/api/import")
def import_csv(req):
    raw = req.body(limit=1_000_000)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")  # Excel на русской Windows сохраняет CSV именно в cp1251
    head = text.splitlines()[0] if text.strip() else ""
    delim = max(",;\t", key=head.count)
    try:
        rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
    except csv.Error:
        raise ApiError(400, "Файл не похож на CSV или в нём слишком длинные ячейки")
    if not rows:
        raise ApiError(400, "В файле нет строк")
    if len(rows) > 2000:
        raise ApiError(400, "Не больше 2000 строк за раз")
    cols = {(k or "").strip().lower(): k for k in rows[0]}
    pick = {field: [cols[a] for a in aliases if a in cols] for field, aliases in IMPORT_ALIASES.items()}
    if not pick["name"]:
        raise ApiError(400, "Нужна колонка «Имя» (name)")
    created = skipped = 0
    for r in rows:
        cell = lambda col: (r.get(col) or "").strip()
        name = cell(pick["name"][0])
        if not name:
            skipped += 1
            continue
        # телефон и e-mail бывают в разных колонках: склеиваем, чтобы не потерять ни то ни другое
        contact = ", ".join(dict.fromkeys(v for v in (cell(c) for c in pick["contact"]) if v))
        request = next((v for v in (cell(c) for c in pick["request"]) if v), "")
        tags = ["Импорт"] + [t.strip() for c in pick["tags"] for t in re.split(r"[,;]", cell(c)) if t.strip()]
        db.create_lead(name[:100], contact[:200], request[:2000], "manual", tags=tags)
        created += 1
    return {"created": created, "skipped": skipped}


# ---------- HTTP ----------

class Request:
    def __init__(self, handler, qs):
        self.h, self.qs, self.headers = handler, qs, handler.headers
        # Если запрос пришёл от локального reverse proxy, настоящий адрес клиента в X-Forwarded-For. Берём правую запись:
        # левые клиент может подделать сам, правую дописывает прокси. Без этого лимиты были бы общими на всех.
        peer = handler.client_address[0]
        hops = [p.strip() for p in handler.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        self.ip = hops[-1] if hops and (TRUST_PROXY or peer in LOOPBACK) else peer

    def body(self, limit=100_000):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Некорректный Content-Length")
        if n < 0:
            raise ApiError(400, "Некорректный Content-Length")  # read(-1) повис бы до закрытия соединения
        if n > limit:
            raise ApiError(413, "Слишком большой запрос")
        return self.h.rfile.read(n)

    def json(self):
        try:
            d = json.loads(self.body() or b"{}")
        except ValueError:
            raise ApiError(400, "Некорректный JSON")
        if not isinstance(d, dict):
            raise ApiError(400, "Ожидался объект JSON")
        return d


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniCRM"
    timeout = 30  # таймаут сокета: медленный или молчащий клиент не должен занимать поток бесконечно

    def log_message(self, fmt, *args):
        logging.getLogger("http").debug(fmt, *args)

    def authed(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "crm_session" and valid_session(v):
                return True
        return False

    def reply(self, code, body, ctype, headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.path.split("?")[0] == "/api/public/lead":  # форму можно встроить на чужой сайт, в том числе ответы с ошибками
            self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.reply(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                   [("Cache-Control", "no-store")])

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_HEAD(self):
        self.dispatch("GET")  # reply() не пишет тело для HEAD

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def do_OPTIONS(self):
        # CORS только для публичной формы: её можно встроить на сайт агентства
        self.reply(204, b"", "text/plain", [
            ("Access-Control-Allow-Origin", "*"), ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type")] if self.path == "/api/public/lead" else [])

    def dispatch(self, method):
        url = urlparse(self.path)
        try:
            if url.path.startswith("/api/"):
                self.api(method, url.path, parse_qs(url.query))
            elif method == "GET":
                self.static(url.path)
            else:
                raise ApiError(405, "Метод не поддерживается")
        except ApiError as e:
            self.send_json({"error": e.msg}, e.code)
        except Exception:
            logging.getLogger("http").exception("%s %s", method, self.path)
            self.send_json({"error": "Внутренняя ошибка сервера"}, 500)
        finally:
            db.close()  # поток одноразовый, соединение с базой за ним не должно висеть

    def static(self, path):
        rel = {"/": "index.html", "/form": "form.html"}.get(path) or unquote(path).lstrip("/")
        try:
            full = os.path.realpath(os.path.join(STATIC, rel))
        except ValueError:  # нулевой байт в пути (%00)
            raise ApiError(404, "Не найдено")
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            raise ApiError(404, "Не найдено")
        with open(full, "rb") as f:
            body = f.read()
        headers = [("Content-Security-Policy", CSP + ("; frame-ancestors *" if rel == "form.html" else "; frame-ancestors 'none'")),
                   ("Cache-Control", "no-cache")]  # после обновления CRM браузер не должен показывать старый app.js
        self.reply(200, body, (mimetypes.guess_type(full)[0] or "application/octet-stream") + "; charset=utf-8", headers)

    def api(self, method, path, qs):
        req = Request(self, qs)
        for m, rx, fn, auth in ROUTES:
            match = rx.match(path)
            if m != method or not match:
                continue
            if auth:
                if not self.authed():
                    raise ApiError(401, "Нужен вход")
                origin = (self.headers.get("Origin") or "").split("//")[-1]
                if method != "GET" and origin not in ("", self.headers.get("Host")):
                    raise ApiError(403, "Чужой источник запроса")
            out = fn(req, *match.groups())
            if isinstance(out, Raw):
                return self.reply(200, out.body, out.ctype, out.headers)
            code, obj = out if isinstance(out, tuple) else (200, out)
            return self.send_json(obj, code)
        raise ApiError(404, "Не найдено")


def main():
    global runner
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    if PASSWORD == "admin":
        logging.warning("пароль CRM по умолчанию — admin; задайте свой через CRM_PASSWORD")
    host, port = os.environ.get("CRM_HOST", "127.0.0.1"), int(os.environ.get("CRM_PORT", "8080"))
    check_startup(host, PASSWORD)
    db.conn()
    runner = bot.BotRunner(os.environ.get("BOT_TOKEN", ""))
    runner.start()
    srv = ThreadingHTTPServer((host, port), Handler)
    logging.info("CRM: http://%s:%s", host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()


if __name__ == "__main__":
    main()
