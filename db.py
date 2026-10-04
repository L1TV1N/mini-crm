"""SQLite-слой мини-CRM: лиды, теги, переписка, история, правила, команда, состояние бота."""
import hashlib
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("CRM_DB", os.path.join(os.path.dirname(__file__), "data", "crm.db"))

STATUSES = ("new", "in_work", "won", "lost")
STATUS_LABELS = {"new": "Новый", "in_work": "В работе", "won": "Успех", "lost": "Отказ"}
OPEN_STATUSES = ("new", "in_work")
SOURCES = ("bot", "telegram", "manual", "web")
# тег, который автоматически получает лид из каждого источника
SOURCE_TAGS = {"bot": "Бот", "telegram": "Telegram", "manual": "Вручную", "web": "Сайт"}
TAG_COLORS = ["#FF4F1A", "#2B59FF", "#0E8A5F", "#D69E00", "#C2185B", "#00899A", "#7A5C3E", "#3B3B38"]
SORTS = {
    "updated": "l.updated_at DESC, l.id DESC",
    "created": "l.id DESC",
    "due": "(l.due_at = ''), l.due_at ASC, l.id DESC",
    "amount": "l.amount DESC, l.id DESC",
    "name": "ulower(l.name) ASC",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    contact     TEXT NOT NULL DEFAULT '',
    request     TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',
    note        TEXT NOT NULL DEFAULT '',
    assignee    TEXT NOT NULL DEFAULT '',
    due_at      TEXT NOT NULL DEFAULT '',
    amount      INTEGER NOT NULL DEFAULT 0,
    tg_user_id  INTEGER,
    tg_chat_id  INTEGER,
    business_connection_id TEXT,
    unread      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_chat ON leads(tg_chat_id, source);
CREATE TABLE IF NOT EXISTS tags (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lead_tags (
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (lead_id, tag_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id   INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    direction TEXT NOT NULL,            -- in / out
    text      TEXT NOT NULL,
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_lead ON messages(lead_id);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL,
    text    TEXT NOT NULL,
    ts      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_lead ON events(lead_id);
-- автотеги: если в имени или запросе есть слово, лид получает тег
CREATE TABLE IF NOT EXISTS rules (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    tag     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS managers (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
-- шаги диалога бота: пока человек отвечает, заявка живёт здесь, в лиды попадает только целиком
CREATE TABLE IF NOT EXISTS bot_dialogs (
    chat_id INTEGER PRIMARY KEY,
    step    TEXT NOT NULL,
    name    TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    ref     TEXT NOT NULL DEFAULT '',
    tg_user_id INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS business_connections (
    id      TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# колонки, появившиеся после первой версии: добавляем в уже существующие базы
MIGRATIONS = {
    "leads": [("assignee", "TEXT NOT NULL DEFAULT ''"), ("due_at", "TEXT NOT NULL DEFAULT ''"),
              ("amount", "INTEGER NOT NULL DEFAULT 0")],
    "bot_dialogs": [("ref", "TEXT NOT NULL DEFAULT ''")],
}

_local = threading.local()
_ready = set()  # базы, в которых схема уже создана: не гоняем CREATE TABLE на каждый запрос


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today(offset_min=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_min)).date().isoformat()


def conn():
    """Своё соединение на поток: сервер и бот работают в разных потоках."""
    c = getattr(_local, "c", None)
    if c is None or getattr(_local, "path", None) != DB_PATH:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        fresh = not os.path.exists(DB_PATH)
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        # встроенный lower() в SQLite не понимает кириллицу, поиск «мар» иначе не найдёт «Мария»
        c.create_function("ulower", 1, lambda v: v.lower() if isinstance(v, str) else v, deterministic=True)
        c.execute("PRAGMA journal_mode=WAL")
        if fresh or DB_PATH not in _ready:
            c.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                for name, ddl in cols:
                    if name not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            c.commit()
            try:  # в базе персональные данные клиентов, другим пользователям системы читать её незачем
                os.chmod(DB_PATH, 0o600)
            except OSError:
                pass
            _ready.add(DB_PATH)
        _local.c, _local.path = c, DB_PATH
    return c


def close():
    """Закрывает соединение текущего потока. Серверу нужно после каждого запроса: потоки одноразовые."""
    c = getattr(_local, "c", None)
    if c is not None:
        c.close()
        _local.c = None


def kv_get(key, default=None):
    r = conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def kv_set(key, value):
    with conn() as c:
        c.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))


def kv_delete(key):
    with conn() as c:
        c.execute("DELETE FROM kv WHERE key=?", (key,))


def log_event(c, lead_id, kind, text):
    c.execute("INSERT INTO events(lead_id,kind,text,ts) VALUES(?,?,?,?)", (lead_id, kind, text, now()))


# ---------- теги ----------

def _clean_tag(name):
    return " ".join(str(name).split())[:32]


def get_or_create_tag(c, name):
    name = _clean_tag(name)
    if not name:
        return None
    r = c.execute("SELECT id FROM tags WHERE ulower(name)=?", (name.lower(),)).fetchone()
    if r:
        return r["id"]
    n = c.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    # OR IGNORE: два потока могли одновременно не найти тег и оба пойти его создавать
    c.execute("INSERT OR IGNORE INTO tags(name,color) VALUES(?,?)", (name, TAG_COLORS[n % len(TAG_COLORS)]))
    return c.execute("SELECT id FROM tags WHERE ulower(name)=?", (name.lower(),)).fetchone()["id"]


def _tag_names(c, lead_id):
    return [r["name"] for r in c.execute(
        "SELECT t.name FROM tags t JOIN lead_tags lt ON lt.tag_id=t.id WHERE lt.lead_id=? ORDER BY t.name", (lead_id,))]


def add_lead_tag(c, lead_id, name):
    tid = get_or_create_tag(c, name)
    if not tid:
        return False
    return c.execute("INSERT OR IGNORE INTO lead_tags(lead_id,tag_id) VALUES(?,?)", (lead_id, tid)).rowcount > 0


def remove_lead_tag(c, lead_id, name):
    return c.execute(
        "DELETE FROM lead_tags WHERE lead_id=? AND tag_id IN (SELECT id FROM tags WHERE ulower(name)=?)",
        (lead_id, _clean_tag(name).lower())).rowcount > 0


def set_lead_tags(c, lead_id, names):
    """Заменяет набор тегов лида и пишет в историю, что добавилось и что убрали."""
    before = {n.lower(): n for n in _tag_names(c, lead_id)}
    want = {}
    for n in names:
        n = _clean_tag(n)
        if n:
            want.setdefault(n.lower(), n)
    for k, n in before.items():
        if k not in want:
            remove_lead_tag(c, lead_id, n)
            log_event(c, lead_id, "tag", f"Тег убран: {n}")
    for k, n in want.items():
        if k not in before:
            add_lead_tag(c, lead_id, n)
            log_event(c, lead_id, "tag", f"Тег добавлен: {n}")


def list_tags():
    rows = conn().execute(
        "SELECT t.id, t.name, t.color, COUNT(lt.lead_id) AS count FROM tags t "
        "LEFT JOIN lead_tags lt ON lt.tag_id=t.id GROUP BY t.id ORDER BY count DESC, t.name").fetchall()
    return [dict(r) for r in rows]


def update_tag(tag_id, name=None, color=None):
    """Переименование и перекраска. Если новое имя уже занято другим тегом, теги сливаются."""
    with conn() as c:
        cur = c.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
        if not cur:
            return None
        if name is not None:
            name = _clean_tag(name)
            twin = c.execute("SELECT id FROM tags WHERE ulower(name)=? AND id<>?", (name.lower(), tag_id)).fetchone()
            if twin:
                c.execute("INSERT OR IGNORE INTO lead_tags(lead_id,tag_id) SELECT lead_id,? FROM lead_tags WHERE tag_id=?",
                          (twin["id"], tag_id))
                c.execute("DELETE FROM tags WHERE id=?", (tag_id,))
                tag_id = twin["id"]
                name = c.execute("SELECT name FROM tags WHERE id=?", (tag_id,)).fetchone()["name"]
            else:
                c.execute("UPDATE tags SET name=? WHERE id=?", (name, tag_id))
            # правила автотегов хранят имя тега текстом: без этого после переименования правило воскресило бы старый тег
            c.execute("UPDATE rules SET tag=? WHERE ulower(tag)=?", (name, cur["name"].lower()))
        if color is not None:
            c.execute("UPDATE tags SET color=? WHERE id=?", (color, tag_id))
        return dict(c.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone())


def delete_tag(tag_id):
    with conn() as c:
        return c.execute("DELETE FROM tags WHERE id=?", (tag_id,)).rowcount > 0


# ---------- правила автотегов и команда ----------

def list_rules():
    return [dict(r) for r in conn().execute("SELECT * FROM rules ORDER BY id")]


def add_rule(keyword, tag):
    keyword, tag = " ".join(keyword.split())[:60], _clean_tag(tag)
    if not keyword or not tag:
        return None
    with conn() as c:
        rid = c.execute("INSERT INTO rules(keyword,tag) VALUES(?,?)", (keyword, tag)).lastrowid
        get_or_create_tag(c, tag)
        return dict(c.execute("SELECT * FROM rules WHERE id=?", (rid,)).fetchone())


def delete_rule(rule_id):
    with conn() as c:
        return c.execute("DELETE FROM rules WHERE id=?", (rule_id,)).rowcount > 0


def list_managers():
    return [dict(r) for r in conn().execute("SELECT * FROM managers ORDER BY name")]


def add_manager(name):
    name = " ".join(name.split())[:40]
    if not name:
        return None
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO managers(name) VALUES(?)", (name,))
        return dict(c.execute("SELECT * FROM managers WHERE name=?", (name,)).fetchone())


def delete_manager(manager_id):
    with conn() as c:
        r = c.execute("SELECT name FROM managers WHERE id=?", (manager_id,)).fetchone()
        if not r:
            return False
        c.execute("UPDATE leads SET assignee='' WHERE assignee=?", (r["name"],))
        c.execute("DELETE FROM managers WHERE id=?", (manager_id,))
        return True


# ---------- лиды ----------

def _attach_tags(c, rows):
    ids = [r["id"] for r in rows]
    by = {i: [] for i in ids}
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        q = ("SELECT lt.lead_id, t.id, t.name, t.color FROM lead_tags lt JOIN tags t ON t.id=lt.tag_id "
             f"WHERE lt.lead_id IN ({','.join('?' * len(part))}) ORDER BY t.name")
        for r in c.execute(q, part):
            by[r["lead_id"]].append({"id": r["id"], "name": r["name"], "color": r["color"]})
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = by[d["id"]]
        out.append(d)
    return out


def create_lead(name, contact="", request="", source="manual", tags=(), tg_user_id=None, tg_chat_id=None,
                business_connection_id=None, first_message=None, assignee="", due_at="", amount=0, auto_tags=True):
    ts = now()
    with conn() as c:
        lid = c.execute(
            "INSERT INTO leads(name,contact,request,source,assignee,due_at,amount,tg_user_id,tg_chat_id,"
            "business_connection_id,unread,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name.strip(), contact.strip(), request.strip(), source, assignee, due_at, amount, tg_user_id, tg_chat_id,
             business_connection_id, 1 if first_message else 0, ts, ts)).lastrowid
        log_event(c, lid, "created", f"Лид создан, источник: {SOURCE_TAGS[source]}")
        add_lead_tag(c, lid, SOURCE_TAGS[source])
        for t in tags:
            add_lead_tag(c, lid, t)
        if auto_tags:
            hay = f"{name} {request}".lower()
            for rule in c.execute("SELECT keyword, tag FROM rules").fetchall():
                if rule["keyword"].lower() in hay and add_lead_tag(c, lid, rule["tag"]):
                    log_event(c, lid, "tag", f"Тег по правилу «{rule['keyword']}»: {rule['tag']}")
        if first_message:
            c.execute("INSERT INTO messages(lead_id,direction,text,ts) VALUES(?,?,?,?)", (lid, "in", first_message, ts))
    return get_lead(lid)


def get_lead(lead_id):
    c = conn()
    r = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not r:
        return None
    d = _attach_tags(c, [r])[0]
    d["messages"] = [dict(m) for m in c.execute(
        "SELECT direction,text,ts FROM messages WHERE lead_id=? ORDER BY id", (lead_id,))]
    d["events"] = [dict(e) for e in c.execute(
        "SELECT kind,text,ts FROM events WHERE lead_id=? ORDER BY id DESC", (lead_id,))]
    d["related"] = related_leads(c, d)
    return d


def related_leads(c, lead):
    """Другие заявки того же человека: по Telegram ID или по одинаковому контакту."""
    conds, args = [], [lead["id"]]
    if lead["tg_user_id"]:
        conds.append("tg_user_id=?")
        args.append(lead["tg_user_id"])
    if lead["contact"]:
        conds.append("ulower(contact)=?")
        args.append(lead["contact"].lower())
    if not conds:
        return []
    rows = c.execute(f"SELECT id,name,source,status,created_at FROM leads WHERE id<>? AND ({' OR '.join(conds)}) "
                     "ORDER BY id DESC LIMIT 10", args).fetchall()
    return [dict(r) for r in rows]


def _filters(f, off):
    sql, args = " WHERE 1=1", []
    if f.get("tag"):
        sql += (" AND l.id IN (SELECT lt.lead_id FROM lead_tags lt JOIN tags t ON t.id=lt.tag_id "
                "WHERE ulower(t.name)=?)")
        args.append(f["tag"].lower())
    for key, col in (("status", "l.status"), ("source", "l.source")):
        if f.get(key):
            sql += f" AND {col}=?"
            args.append(f[key])
    if f.get("assignee"):
        if f["assignee"] == "-":
            sql += " AND l.assignee=''"
        else:
            sql += " AND l.assignee=?"
            args.append(f["assignee"])
    if f.get("unread"):
        sql += " AND l.unread>0"
    due = f.get("due")
    t = today(off)
    if due == "overdue":
        sql += " AND l.due_at<>'' AND l.due_at<? AND l.status IN ('new','in_work')"
        args.append(t)
    elif due == "today":
        sql += " AND l.due_at=?"
        args.append(t)
    elif due == "week":
        sql += " AND l.due_at BETWEEN ? AND ?"
        args += [t, (datetime.fromisoformat(t) + timedelta(days=7)).date().isoformat()]
    elif due == "none":
        sql += " AND l.due_at=''"
    if f.get("q"):
        like = "%" + f["q"].lower().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        sql += (" AND (ulower(l.name) LIKE ? ESCAPE '\\' OR ulower(l.contact) LIKE ? ESCAPE '\\'"
                " OR ulower(l.request) LIKE ? ESCAPE '\\' OR ulower(l.note) LIKE ? ESCAPE '\\')")
        args += [like] * 4
    return sql, args


def list_leads(tag=None, q=None, status=None, source=None, assignee=None, due=None, unread=False,
               sort="updated", offset_min=0, limit=1000):
    where, args = _filters({"tag": tag, "q": q, "status": status, "source": source, "assignee": assignee,
                            "due": due, "unread": unread}, offset_min)
    sql = ("SELECT l.*, (SELECT text FROM messages WHERE lead_id=l.id ORDER BY id DESC LIMIT 1) AS last_message "
           "FROM leads l" + where + " ORDER BY " + SORTS.get(sort, SORTS["updated"]) + " LIMIT ?")
    c = conn()
    return _attach_tags(c, c.execute(sql, [*args, limit]).fetchall())


def update_lead(lead_id, data):
    """Меняет поля лида и пишет в историю то, что реально изменилось."""
    with conn() as c:
        old = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not old:
            return None
        sets = {}
        for k in ("name", "contact", "request", "note", "status", "assignee", "due_at"):
            if k in data and str(data[k]) != old[k]:
                sets[k] = str(data[k])
        if "amount" in data and int(data["amount"]) != old["amount"]:
            sets["amount"] = int(data["amount"])
        if "status" in sets:
            log_event(c, lead_id, "status",
                      f"Статус: {STATUS_LABELS[old['status']]} → {STATUS_LABELS[sets['status']]}")
        if "assignee" in sets:
            log_event(c, lead_id, "assignee", f"Ответственный: {old['assignee'] or 'не назначен'} → "
                                              f"{sets['assignee'] or 'не назначен'}")
        if "due_at" in sets:
            log_event(c, lead_id, "due", f"Срок: {sets['due_at'] or 'снят'}")
        if "amount" in sets:
            log_event(c, lead_id, "amount", f"Сумма: {sets['amount']:,} ₽".replace(",", " "))
        changed = bool(sets)
        if "tags" in data:
            before = _tag_names(c, lead_id)
            set_lead_tags(c, lead_id, data["tags"])
            changed = changed or before != _tag_names(c, lead_id)
        if data.get("read"):
            sets["unread"] = 0
        if changed:  # простое открытие карточки не поднимает лид наверх списка
            sets["updated_at"] = now()
        if sets:
            c.execute("UPDATE leads SET " + ",".join(f"{k}=?" for k in sets) + " WHERE id=?", (*sets.values(), lead_id))
    return get_lead(lead_id)


def delete_lead(lead_id):
    with conn() as c:
        return c.execute("DELETE FROM leads WHERE id=?", (lead_id,)).rowcount > 0


def bulk(ids, action, value=""):
    """Массовое действие над выбранными лидами, возвращает сколько лидов затронуто."""
    n = 0
    with conn() as c:
        for lid in ids:
            old = c.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
            if not old:
                continue
            n += 1
            if action == "delete":
                c.execute("DELETE FROM leads WHERE id=?", (lid,))
                continue
            if action == "status" and old["status"] != value:
                log_event(c, lid, "status", f"Статус: {STATUS_LABELS[old['status']]} → {STATUS_LABELS[value]}")
                c.execute("UPDATE leads SET status=?, updated_at=? WHERE id=?", (value, now(), lid))
            elif action == "assignee" and old["assignee"] != value:
                log_event(c, lid, "assignee", f"Ответственный: {old['assignee'] or 'не назначен'} → {value or 'не назначен'}")
                c.execute("UPDATE leads SET assignee=?, updated_at=? WHERE id=?", (value, now(), lid))
            elif action == "add_tag" and add_lead_tag(c, lid, value):
                log_event(c, lid, "tag", f"Тег добавлен: {_clean_tag(value)}")
                c.execute("UPDATE leads SET updated_at=? WHERE id=?", (now(), lid))
            elif action == "remove_tag" and remove_lead_tag(c, lid, value):
                log_event(c, lid, "tag", f"Тег убран: {_clean_tag(value)}")
                c.execute("UPDATE leads SET updated_at=? WHERE id=?", (now(), lid))
    return n


def add_message(lead_id, direction, text):
    """Входящее увеличивает счётчик непрочитанных, исходящее (ответ менеджера) сбрасывает его."""
    ts = now()
    with conn() as c:
        c.execute("INSERT INTO messages(lead_id,direction,text,ts) VALUES(?,?,?,?)", (lead_id, direction, text, ts))
        if direction == "in":
            c.execute("UPDATE leads SET updated_at=?, unread=unread+1 WHERE id=?", (ts, lead_id))
        else:
            c.execute("UPDATE leads SET updated_at=?, unread=0 WHERE id=?", (ts, lead_id))


def find_business_lead(chat_id):
    return conn().execute(
        "SELECT * FROM leads WHERE source='telegram' AND tg_chat_id=? ORDER BY id DESC LIMIT 1",
        (chat_id,)).fetchone()


def find_bot_lead(chat_id):
    return conn().execute(
        "SELECT * FROM leads WHERE source='bot' AND tg_chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()


def light_stats(off=0):
    c = conn()
    return {
        "total": c.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
        "new": c.execute("SELECT COUNT(*) FROM leads WHERE status='new'").fetchone()[0],
        "unread": c.execute("SELECT COALESCE(SUM(unread),0) FROM leads").fetchone()[0],
        "overdue": c.execute("SELECT COUNT(*) FROM leads WHERE due_at<>'' AND due_at<? AND status IN ('new','in_work')",
                             (today(off),)).fetchone()[0],
        "last_id": c.execute("SELECT COALESCE(MAX(id),0) FROM leads").fetchone()[0],
    }


def revision():
    """Строка, которая меняется при любом изменении данных: по ней страница понимает, что пора обновиться.
    Одного updated_at мало: два изменения в одну секунду дали бы одинаковую строку."""
    r = conn().execute(
        "SELECT (SELECT COUNT(*) FROM leads), (SELECT COALESCE(MAX(updated_at),'') FROM leads), "
        "(SELECT COALESCE(SUM(unread),0) FROM leads), (SELECT COALESCE(MAX(id),0) FROM events), "
        "(SELECT COALESCE(MAX(id),0) FROM messages), "
        "(SELECT COALESCE(group_concat(name||color||id),'') FROM tags)").fetchone()
    return ":".join(str(x) for x in r[:5]) + ":" + hashlib.md5(r[5].encode()).hexdigest()[:8]


def full_stats(off=0, days=30):
    c = conn()
    s = light_stats(off)
    s["by_status"] = {k: 0 for k in STATUSES}
    s["amount"] = {k: 0 for k in STATUSES}
    for r in c.execute("SELECT status, COUNT(*) n, COALESCE(SUM(amount),0) a FROM leads GROUP BY status"):
        s["by_status"][r["status"]], s["amount"][r["status"]] = r["n"], r["a"]
    s["by_source"] = {k: 0 for k in SOURCES}
    for r in c.execute("SELECT source, COUNT(*) n FROM leads GROUP BY source"):
        s["by_source"][r["source"]] = r["n"]
    won, lost = s["by_status"]["won"], s["by_status"]["lost"]
    s["conversion"] = round(100 * won / (won + lost)) if won + lost else None
    s["pipeline"] = s["amount"]["new"] + s["amount"]["in_work"]
    # лиды по дням: сдвиг часового пояса приходит из браузера, чтобы «сегодня» совпадало с тем, что видит человек
    since = datetime.now(timezone.utc) - timedelta(days=days + 1)
    got = {r["d"]: r["n"] for r in c.execute(
        "SELECT date(created_at, ?) d, COUNT(*) n FROM leads WHERE created_at>=? GROUP BY d",
        (f"{off:+d} minutes", since.strftime("%Y-%m-%dT%H:%M:%SZ")))}
    start = datetime.fromisoformat(today(off)).date()
    s["by_day"] = [{"date": (start - timedelta(days=i)).isoformat(),
                    "count": got.get((start - timedelta(days=i)).isoformat(), 0)} for i in range(days - 1, -1, -1)]
    s["period_total"] = sum(d["count"] for d in s["by_day"])
    s["top_tags"] = list_tags()[:8]
    s["by_assignee"] = [dict(r) for r in c.execute(
        "SELECT CASE WHEN assignee='' THEN 'Не назначен' ELSE assignee END name, COUNT(*) n, "
        "SUM(status='won') won FROM leads GROUP BY assignee ORDER BY n DESC")]
    return s


# ---------- диалоги бота и бизнес-подключения ----------

def dialog_get(chat_id):
    r = conn().execute("SELECT * FROM bot_dialogs WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(r) if r else None


def dialog_save(chat_id, step, name="", contact="", tg_user_id=None, ref=""):
    with conn() as c:
        c.execute(
            "INSERT INTO bot_dialogs(chat_id,step,name,contact,ref,tg_user_id,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET step=excluded.step,name=excluded.name,contact=excluded.contact,"
            "ref=excluded.ref,tg_user_id=excluded.tg_user_id,updated_at=excluded.updated_at",
            (chat_id, step, name, contact, ref, tg_user_id, now()))


def dialog_drop(chat_id):
    with conn() as c:
        c.execute("DELETE FROM bot_dialogs WHERE chat_id=?", (chat_id,))


def business_save(conn_id, user_id, username, enabled):
    with conn() as c:
        c.execute(
            "INSERT INTO business_connections(id,user_id,username,enabled,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id,username=excluded.username,"
            "enabled=excluded.enabled,updated_at=excluded.updated_at",
            (conn_id, user_id, username, 1 if enabled else 0, now()))


def business_get(conn_id):
    r = conn().execute("SELECT * FROM business_connections WHERE id=?", (conn_id,)).fetchone()
    return dict(r) if r else None


def business_list():
    return [dict(r) for r in conn().execute("SELECT * FROM business_connections ORDER BY updated_at DESC")]


# ---------- уведомления менеджеру ----------

def connect_code():
    """Одноразовый код: менеджер отправляет боту /connect КОД, и бот начинает присылать ему уведомления."""
    return kv_get("connect_code") or new_connect_code()


def new_connect_code():
    code = f"{secrets.randbelow(900000) + 100000}"
    kv_set("connect_code", code)
    return code
