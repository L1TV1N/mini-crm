import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, **extra):
        self.sent.append((chat_id, text, extra))


def msg(chat=100, text=None, uid=None, username="ivan", **kw):
    m = {"chat": {"id": chat, "type": "private"}, "from": {"id": uid or chat, "first_name": "Иван", "username": username}}
    if text is not None:
        m["text"] = text
    m.update(kw)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        self.tg = FakeTG()

    def tearDown(self):
        db.conn().close()
        self.tmp.cleanup()


class BotFlow(Base):
    def say(self, **kw):
        bot.handle_update({"update_id": 1, "message": msg(**kw)}, self.tg)

    def test_full_dialog_creates_tagged_lead(self):
        self.say(text="привет")
        self.say(text="Иван Петров")
        self.say(text="+7 900 111-22-33")
        self.assertEqual(db.list_leads(), [])  # пока диалог не закончен, лида нет
        self.say(text="Нужен сайт для кофейни")
        leads = db.list_leads()
        self.assertEqual(len(leads), 1)
        l = leads[0]
        self.assertEqual((l["name"], l["source"], l["request"]), ("Иван Петров", "bot", "Нужен сайт для кофейни"))
        self.assertIn("+7 900 111-22-33", l["contact"])
        self.assertIn("@ivan", l["contact"])
        self.assertEqual([t["name"] for t in l["tags"]], ["Бот"])
        self.assertIn(f"№{l['id']}", self.tg.sent[-1][1])

    def test_shared_contact_button(self):
        self.say(text="/start")
        self.say(text="Анна")
        self.say(contact={"phone_number": "+79001112233"})
        self.say(text="Реклама")
        self.assertIn("+79001112233", db.list_leads()[0]["contact"])

    def test_cancel_and_restart(self):
        self.say(text="/start")
        self.say(text="Анна")
        self.say(text="/cancel")
        self.assertIsNone(db.dialog_get(100))
        self.say(text="/start")
        self.say(text="Борис")
        self.say(text="b@x.ru")
        self.say(text="Сайт")
        self.assertEqual(db.list_leads()[0]["name"], "Борис")

    def test_non_text_does_not_advance(self):
        self.say(text="/start")
        self.say(photo=[{}])
        self.assertEqual(db.dialog_get(100)["step"], "name")

    def test_two_people_do_not_mix(self):
        for chat, name in ((1, "Аня"), (2, "Боря")):
            self.say(chat=chat, text="/start")
            self.say(chat=chat, text=name)
        self.say(chat=1, text="a@x.ru")
        self.say(chat=2, text="b@x.ru")
        self.say(chat=2, text="запрос Бори")
        self.say(chat=1, text="запрос Ани")
        got = {l["name"]: l["request"] for l in db.list_leads()}
        self.assertEqual(got, {"Аня": "запрос Ани", "Боря": "запрос Бори"})

    def test_group_chat_ignored(self):
        m = msg(text="hi")
        m["chat"]["type"] = "group"
        bot.handle_update({"message": m}, self.tg)
        self.assertEqual(self.tg.sent, [])


class Business(Base):
    def setUp(self):
        super().setUp()
        bot.handle_update({"business_connection": {"id": "bc1", "user": {"id": 7, "username": "owner"},
                                                   "is_enabled": True}}, self.tg)

    def incoming(self, text, chat=500, **kw):
        m = msg(chat=chat, text=text, username="client", **kw)
        m["business_connection_id"] = "bc1"
        bot.handle_update({"business_message": m}, self.tg)

    def test_incoming_becomes_lead_then_appends(self):
        self.incoming("Здравствуйте, сколько стоит?")
        self.incoming("И сроки?")
        leads = db.list_leads()
        self.assertEqual(len(leads), 1)
        self.assertEqual([t["name"] for t in leads[0]["tags"]], ["Telegram"])
        full = db.get_lead(leads[0]["id"])
        self.assertEqual([m["text"] for m in full["messages"]], ["Здравствуйте, сколько стоит?", "И сроки?"])
        self.assertEqual(full["unread"], 2)
        self.assertEqual(full["contact"], "@client")
        self.assertEqual(self.tg.sent, [])  # от имени аккаунта бот ничего не отвечает

    def test_owner_message_is_not_a_lead(self):
        m = msg(chat=600, text="я сам себе", uid=7)
        m["business_connection_id"] = "bc1"
        bot.handle_update({"business_message": m}, self.tg)
        self.assertEqual(db.list_leads(), [])

    def test_owner_reply_goes_to_history(self):
        self.incoming("вопрос")
        m = msg(chat=500, text="ответ", uid=7)
        m["business_connection_id"] = "bc1"
        bot.handle_update({"business_message": m}, self.tg)
        full = db.get_lead(db.list_leads()[0]["id"])
        self.assertEqual([x["direction"] for x in full["messages"]], ["in", "out"])

    def test_media_message(self):
        self.incoming(None, photo=[{}], caption="смотрите")
        self.assertEqual(db.list_leads()[0]["request"], "[фото] смотрите")


class ApiCase(Base):
    """Сервер на временной базе и помощники запросов. Своих тестов нет, чтобы подклассы не гоняли чужие."""


    def setUp(self):
        super().setUp()
        server.PASSWORD = "pw"
        server.runner = None
        server._hits.clear()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.cookie = None

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, login=True):
        if login and not self.cookie:
            self.cookie = self.raw("POST", "/api/login", {"password": "pw"})[2]
        s, d, _ = self.raw(method, path, body, self.cookie if login else None)
        return s, d

    def raw(self, method, path, body=None, cookie=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1])
        h = {"Content-Type": "application/json", **(headers or {})}
        if cookie:
            h["Cookie"] = cookie
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(body)
        c.request(method, path, body.encode() if isinstance(body, str) else body, h)
        r = c.getresponse()
        data = r.read()
        ck = (r.getheader("Set-Cookie") or "").split(";")[0]
        try:
            data = json.loads(data)
        except ValueError:
            data = data.decode("utf-8")
        return r.status, data, ck



class Api(ApiCase):
    def test_auth_required(self):
        self.assertEqual(self.req("GET", "/api/leads", login=False)[0], 401)
        self.assertEqual(self.raw("POST", "/api/login", {"password": "bad"})[0], 401)
        self.assertEqual(self.req("GET", "/api/leads")[0], 200)

    def test_manual_lead_with_tags_and_filter(self):
        s, lead = self.req("POST", "/api/leads", {"name": "Олег", "contact": "o@x.ru", "request": "SEO",
                                                  "tags": ["VIP", "vip", "Срочно", "срочно"]})
        self.assertEqual(s, 201)
        self.assertEqual(sorted(t["name"] for t in lead["tags"]), ["VIP", "Вручную", "Срочно"])
        self.req("POST", "/api/leads", {"name": "Без тегов"})
        _, by_tag = self.req("GET", "/api/leads?tag=VIP")
        self.assertEqual([l["name"] for l in by_tag], ["Олег"])
        _, all_ = self.req("GET", "/api/leads")
        self.assertEqual(len(all_), 2)
        _, tags = self.req("GET", "/api/tags")
        self.assertEqual({t["name"]: t["count"] for t in tags}["Вручную"], 2)

    def test_edit_tags_status_and_delete_tag(self):
        _, lead = self.req("POST", "/api/leads", {"name": "A"})
        s, upd = self.req("PUT", f"/api/leads/{lead['id']}", {"tags": ["Горячий"], "status": "in_work"})
        self.assertEqual((s, upd["status"], [t["name"] for t in upd["tags"]]), (200, "in_work", ["Горячий"]))
        _, tags = self.req("GET", "/api/tags")
        tid = next(t["id"] for t in tags if t["name"] == "Горячий")
        self.assertEqual(self.req("DELETE", f"/api/tags/{tid}")[0], 200)
        self.assertEqual(self.req("GET", f"/api/leads/{lead['id']}")[1]["tags"], [])

    def test_validation(self):
        self.assertEqual(self.req("POST", "/api/leads", {"name": "  "})[0], 400)
        _, lead = self.req("POST", "/api/leads", {"name": "A"})
        self.assertEqual(self.req("PUT", f"/api/leads/{lead['id']}", {"status": "bogus"})[0], 400)
        self.assertEqual(self.req("GET", "/api/leads/999")[0], 404)

    def test_search_and_special_chars(self):
        self.req("POST", "/api/leads", {"name": "Мария", "request": "скидка 50% на баннер"})
        self.assertEqual(len(self.req("GET", "/api/leads?q=50%25")[1]), 1)
        self.assertEqual(len(self.req("GET", "/api/leads?q=%D0%BC%D0%B0%D1%80")[1]), 1)
        self.assertEqual(len(self.req("GET", "/api/leads?q=%25")[1]), 1)  # % — буквальный символ
        self.assertEqual(self.req("GET", "/api/leads?q=zzz")[1], [])

    def test_bot_lead_visible_through_api_and_read_flag(self):
        bot.handle_update({"business_connection": {"id": "b", "user": {"id": 1}}}, self.tg)
        m = msg(chat=9, text="привет")
        m["business_connection_id"] = "b"
        bot.handle_update({"business_message": m}, self.tg)
        _, leads = self.req("GET", "/api/leads?tag=Telegram")
        self.assertEqual(leads[0]["unread"], 1)
        _, after = self.req("PUT", f"/api/leads/{leads[0]['id']}", {"read": True})
        self.assertEqual(after["unread"], 0)
        self.assertEqual(self.req("GET", "/api/state")[1]["stats"]["total"], 1)

    def test_static_and_traversal(self):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1])
        c.request("GET", "/")
        self.assertEqual(c.getresponse().status, 200)
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1])
        c.request("GET", "/..%2Fdb.py")
        self.assertEqual(c.getresponse().status, 404)


if __name__ == "__main__":
    unittest.main()
