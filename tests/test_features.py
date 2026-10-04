import os
import sqlite3
import sys
import unittest
from datetime import date, timedelta
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402
import test_crm  # noqa: E402


class BotExtras(test_crm.Base):
    def say(self, **kw):
        bot.handle_update({"update_id": 1, "message": test_crm.msg(**kw)}, self.tg)

    def finish_dialog(self, chat=100):
        for t in ("/start", "Анна", "a@x.ru", "Нужен сайт"):
            self.say(chat=chat, text=t)

    def test_followup_goes_to_thread_and_new_starts_dialog(self):
        self.finish_dialog()
        self.say(text="Ещё добавлю: бюджет 100к")
        leads = db.list_leads()
        self.assertEqual(len(leads), 1)
        full = db.get_lead(leads[0]["id"])
        self.assertEqual(full["messages"][-1]["text"], "Ещё добавлю: бюджет 100к")
        self.assertEqual(full["unread"], 1)
        self.say(text="/new")
        self.assertEqual(db.dialog_get(100)["step"], "name")

    def test_start_payload_becomes_tag(self):
        self.say(text="/start instagram")
        for t in ("Анна", "a@x.ru", "Реклама"):
            self.say(text=t)
        self.assertIn("instagram", [t["name"] for t in db.list_leads()[0]["tags"]])

    def test_connect_notifications(self):
        code = db.connect_code()
        self.say(chat=900, text="/connect 000000")
        self.assertIsNone(db.kv_get("notify_chat_id"))
        self.say(chat=900, text=f"/connect {code}")
        self.assertEqual(db.kv_get("notify_chat_id"), "900")
        self.assertNotEqual(db.connect_code(), code)  # код одноразовый
        self.tg.sent.clear()
        self.finish_dialog(chat=5)
        to_manager = [t for c, t, _ in self.tg.sent if c == 900]
        self.assertEqual(len(to_manager), 1)
        self.assertIn("Новый лид", to_manager[0])
        self.assertIn("Нужен сайт", to_manager[0])

    def test_business_notifies_only_first_unread(self):
        db.kv_set("notify_chat_id", 900)
        bot.handle_update({"business_connection": {"id": "b", "user": {"id": 7}}}, self.tg)
        for text in ("раз", "два", "три"):
            m = test_crm.msg(chat=50, text=text)
            m["business_connection_id"] = "b"
            bot.handle_update({"business_message": m}, self.tg)
        self.assertEqual(len([1 for c, *_ in self.tg.sent if c == 900]), 1)  # лид создан, дальше тишина

    def test_notify_failure_does_not_break_intake(self):
        db.kv_set("notify_chat_id", 900)

        class Broken(test_crm.FakeTG):
            def send(self, chat_id, text, **extra):
                if chat_id == 900:
                    raise RuntimeError("blocked")
                super().send(chat_id, text, **extra)
        self.tg = Broken()
        self.finish_dialog()
        self.assertEqual(len(db.list_leads()), 1)


class DbFeatures(test_crm.Base):
    def test_events_log_changes(self):
        lead = db.create_lead("А", "x", "y")
        db.update_lead(lead["id"], {"status": "in_work", "tags": ["Горячий"], "assignee": "Аня", "amount": 50000,
                                    "due_at": "2026-10-05"})
        texts = [e["text"] for e in db.get_lead(lead["id"])["events"]]
        self.assertIn("Статус: Новый → В работе", texts)
        self.assertIn("Тег добавлен: Горячий", texts)
        self.assertIn("Ответственный: не назначен → Аня", texts)
        self.assertIn("Сумма: 50 000 ₽", texts)
        n = len(texts)
        db.update_lead(lead["id"], {"status": "in_work"})  # ничего не изменилось — новых событий нет
        self.assertEqual(len(db.get_lead(lead["id"])["events"]), n)

    def test_rules_autotag(self):
        db.add_rule("сайт", "Разработка")
        lead = db.create_lead("Олег", "x", "Нужен САЙТ для кофейни")
        self.assertIn("Разработка", [t["name"] for t in lead["tags"]])
        self.assertNotIn("Разработка", [t["name"] for t in db.create_lead("Иван", "x", "реклама")["tags"]])

    def test_filters_due_and_sort(self):
        today = date.today()
        a = db.create_lead("A", amount=100, due_at=(today - timedelta(days=2)).isoformat())
        b = db.create_lead("B", amount=900, due_at=today.isoformat())
        c = db.create_lead("C", amount=500)
        ids = lambda **kw: [l["id"] for l in db.list_leads(offset_min=0, **kw)]
        self.assertEqual(ids(due="overdue"), [a["id"]])
        self.assertEqual(ids(due="today"), [b["id"]])
        self.assertEqual(ids(due="none"), [c["id"]])
        self.assertEqual(ids(sort="amount"), [b["id"], c["id"], a["id"]])
        self.assertEqual(ids(sort="due")[:2], [a["id"], b["id"]])
        db.update_lead(a["id"], {"status": "won"})
        self.assertEqual(ids(due="overdue"), [])  # закрытые лиды не считаются просроченными

    def test_assignee_filter(self):
        a = db.create_lead("A", assignee="Аня")
        b = db.create_lead("B")
        self.assertEqual([l["id"] for l in db.list_leads(assignee="Аня")], [a["id"]])
        self.assertEqual([l["id"] for l in db.list_leads(assignee="-")], [b["id"]])

    def test_bulk(self):
        ids = [db.create_lead(n)["id"] for n in "abc"]
        self.assertEqual(db.bulk(ids + [999], "status", "won"), 3)
        self.assertEqual({l["status"] for l in db.list_leads()}, {"won"})
        db.bulk(ids, "add_tag", "VIP")
        self.assertEqual(len(db.list_leads(tag="vip")), 3)
        db.bulk(ids[:1], "remove_tag", "VIP")
        self.assertEqual(len(db.list_leads(tag="VIP")), 2)
        db.bulk(ids, "delete")
        self.assertEqual(db.list_leads(), [])

    def test_tag_rename_merges(self):
        a, b = db.create_lead("a", tags=["Старый"]), db.create_lead("b", tags=["Новый"])
        old = next(t for t in db.list_tags() if t["name"] == "Старый")
        db.update_tag(old["id"], name="новый")
        self.assertEqual(len(db.list_leads(tag="Новый")), 2)
        self.assertFalse([t for t in db.list_tags() if t["name"] == "Старый"])

    def test_related_leads(self):
        a = db.create_lead("a", "x@y.ru", tg_user_id=5, source="bot", tg_chat_id=5)
        b = db.create_lead("a2", "other", tg_user_id=5, source="bot", tg_chat_id=5)
        c = db.create_lead("a3", "X@y.ru")
        self.assertEqual({r["id"] for r in db.get_lead(a["id"])["related"]}, {b["id"], c["id"]})

    def test_stats(self):
        db.create_lead("a", amount=1000)
        w = db.create_lead("b", amount=3000)
        db.update_lead(w["id"], {"status": "won"})
        lost = db.create_lead("c")
        db.update_lead(lost["id"], {"status": "lost"})
        s = db.full_stats(0, 30)
        self.assertEqual((s["total"], s["conversion"], s["pipeline"], s["amount"]["won"]), (3, 50, 1000, 3000))
        self.assertEqual(len(s["by_day"]), 30)
        self.assertEqual(s["by_day"][-1]["count"], 3)
        self.assertEqual(s["by_source"]["manual"], 3)

    def test_manager_delete_clears_assignee(self):
        m = db.add_manager("Аня")
        lead = db.create_lead("a", assignee="Аня")
        db.delete_manager(m["id"])
        self.assertEqual(db.get_lead(lead["id"])["assignee"], "")

    def test_migration_of_old_database(self):
        old = os.path.join(self.tmp.name, "old.db")
        c = sqlite3.connect(old)
        c.executescript("CREATE TABLE leads(id INTEGER PRIMARY KEY, name TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '',"
                        "request TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',"
                        "note TEXT NOT NULL DEFAULT '', tg_user_id INTEGER, tg_chat_id INTEGER, business_connection_id TEXT,"
                        "unread INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
                        "INSERT INTO leads(name,source,created_at,updated_at) VALUES('Старый','bot','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');"
                        "CREATE TABLE bot_dialogs(chat_id INTEGER PRIMARY KEY, step TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',"
                        "contact TEXT NOT NULL DEFAULT '', tg_user_id INTEGER, updated_at TEXT NOT NULL);")
        c.commit()
        c.close()
        db.conn().close()
        db.DB_PATH = old
        lead = db.list_leads()[0]
        self.assertEqual((lead["name"], lead["amount"], lead["assignee"]), ("Старый", 0, ""))
        db.dialog_save(1, "name", ref="x")
        self.assertEqual(db.dialog_get(1)["ref"], "x")


class ApiFeatures(test_crm.ApiCase):
    def test_validation_new_fields(self):
        _, lead = self.req("POST", "/api/leads", {"name": "A"})
        put = lambda body: self.req("PUT", f"/api/leads/{lead['id']}", body)[0]
        self.assertEqual(put({"due_at": "завтра"}), 400)
        self.assertEqual(put({"due_at": "2026-02-30"}), 400)
        self.assertEqual(put({"amount": "много"}), 400)
        self.assertEqual(put({"amount": -5}), 400)
        self.assertEqual(put({"due_at": "2026-10-05", "amount": "12000", "assignee": "Аня"}), 200)
        self.assertEqual(put({"due_at": ""}), 200)

    def test_filters_via_query(self):
        self.req("POST", "/api/leads", {"name": "A", "assignee": "Аня", "amount": 5})
        self.req("POST", "/api/leads", {"name": "B", "amount": 50})
        self.assertEqual([l["name"] for l in self.req("GET", "/api/leads?assignee=%D0%90%D0%BD%D1%8F")[1]], ["A"])
        self.assertEqual([l["name"] for l in self.req("GET", "/api/leads?sort=amount")[1]], ["B", "A"])
        self.assertEqual(self.req("GET", "/api/leads?source=nope")[0], 400)

    def test_bulk_endpoint(self):
        ids = [self.req("POST", "/api/leads", {"name": n})[1]["id"] for n in ("a", "b")]
        s, r = self.req("POST", "/api/leads/bulk", {"ids": ids, "action": "add_tag", "value": "Горячий"})
        self.assertEqual((s, r["count"]), (200, 2))
        self.assertEqual(len(self.req("GET", "/api/leads?tag=" + quote("Горячий"))[1]), 2)
        self.assertEqual(self.req("POST", "/api/leads/bulk", {"ids": [], "action": "delete"})[0], 400)
        self.assertEqual(self.req("POST", "/api/leads/bulk", {"ids": ids, "action": "boom"})[0], 400)
        self.assertEqual(self.req("POST", "/api/leads/bulk", {"ids": ids, "action": "status", "value": "x"})[0], 400)

    def test_tags_rename_color(self):
        _, lead = self.req("POST", "/api/leads", {"name": "a", "tags": ["Тест"]})
        tid = next(t["id"] for t in lead["tags"] if t["name"] == "Тест")
        self.assertEqual(self.req("PUT", f"/api/tags/{tid}", {"color": "red"})[0], 400)
        s, tag = self.req("PUT", f"/api/tags/{tid}", {"name": "Тест2", "color": "#112233"})
        self.assertEqual((s, tag["name"], tag["color"]), (200, "Тест2", "#112233"))
        self.assertEqual(self.req("PUT", f"/api/tags/{tid}", {"name": "  "})[0], 400)
        self.assertEqual(self.req("PUT", "/api/tags/9999", {"name": "x"})[0], 404)

    def test_rules_managers_settings(self):
        s, rule = self.req("POST", "/api/rules", {"keyword": "лого", "tag": "Дизайн"})
        self.assertEqual(s, 201)
        self.assertEqual(self.req("POST", "/api/rules", {"keyword": "", "tag": "x"})[0], 400)
        _, m = self.req("POST", "/api/managers", {"name": "Аня"})
        _, st = self.req("GET", "/api/settings")
        self.assertEqual(([r["keyword"] for r in st["rules"]], [x["name"] for x in st["managers"]]), (["лого"], ["Аня"]))
        self.assertEqual(len(st["notify"]["code"]), 6)
        _, lead = self.req("POST", "/api/leads", {"name": "Нужен логотип"})
        self.assertIn("Дизайн", [t["name"] for t in lead["tags"]])
        self.assertEqual(self.req("DELETE", f"/api/rules/{rule['id']}")[0], 200)
        self.assertEqual(self.req("GET", "/api/state")[1]["managers"], ["Аня"])

    def test_notify_code_rotation_and_disconnect(self):
        old = self.req("GET", "/api/settings")[1]["notify"]["code"]
        new = self.req("POST", "/api/notify/code")[1]["code"]
        self.assertNotEqual(old, new)
        db.kv_set("notify_chat_id", 1)
        self.assertTrue(self.req("GET", "/api/settings")[1]["notify"]["connected"])
        self.req("POST", "/api/notify/disconnect")
        self.assertFalse(self.req("GET", "/api/settings")[1]["notify"]["connected"])

    def test_stats_endpoint(self):
        self.req("POST", "/api/leads", {"name": "a", "amount": 10})
        s, st = self.req("GET", "/api/stats?days=14&tz=180")
        self.assertEqual((s, len(st["by_day"]), st["total"]), (200, 14, 1))

    def test_reply_via_bot_and_business(self):
        class R:
            tg = test_crm.FakeTG()
            status = {}
        server.runner = R
        bot_lead = db.create_lead("Анна", "x", "q", "bot", tg_chat_id=111)
        db.add_message(bot_lead["id"], "in", "вопрос")
        s, after = self.req("POST", f"/api/leads/{bot_lead['id']}/reply", {"text": "Здравствуйте!"})
        self.assertEqual(s, 200)
        self.assertEqual(R.tg.sent[0][:2], (111, "Здравствуйте!"))
        self.assertEqual((after["status"], after["unread"], after["messages"][-1]["direction"]), ("in_work", 0, "out"))
        biz = db.create_lead("Пётр", "x", "q", "telegram", tg_chat_id=222, business_connection_id="bc9", first_message="hi")
        self.assertEqual(self.req("POST", f"/api/leads/{biz['id']}/reply", {"text": "Добрый день"})[0], 200)
        self.assertEqual(R.tg.sent[1][2], {"business_connection_id": "bc9"})
        manual = db.create_lead("Олег")
        self.assertEqual(self.req("POST", f"/api/leads/{manual['id']}/reply", {"text": "hi"})[0], 400)
        self.assertEqual(self.req("POST", f"/api/leads/{bot_lead['id']}/reply", {"text": "  "})[0], 400)

    def test_reply_errors(self):
        lead = db.create_lead("Анна", "x", "q", "bot", tg_chat_id=111)
        self.assertEqual(self.req("POST", f"/api/leads/{lead['id']}/reply", {"text": "hi"})[0], 503)

        class Broken:
            class tg:
                @staticmethod
                def send(*a, **k):
                    raise RuntimeError("Forbidden: bot was blocked by the user")
            status = {}
        server.runner = Broken
        s, r = self.req("POST", f"/api/leads/{lead['id']}/reply", {"text": "hi"})
        self.assertEqual(s, 502)
        self.assertIn("blocked", r["error"])
        self.assertEqual(db.get_lead(lead["id"])["messages"], [])  # не отправилось — в переписку не попало

    def test_public_form(self):
        post = lambda b: self.raw("POST", "/api/public/lead", b)[:2]
        s, r = post({"name": "Мария", "contact": "m@x.ru", "request": "Хочу сайт", "ref": "vk ads!"})
        self.assertEqual((s, r), (201, {"ok": True}))
        lead = db.list_leads()[0]
        self.assertEqual((lead["source"], lead["request"]), ("web", "Хочу сайт"))
        self.assertEqual(sorted(t["name"] for t in lead["tags"]), ["vkads", "Сайт"])
        self.assertEqual(post({"name": "x"})[0], 400)
        self.assertEqual(post({"name": "Бот", "contact": "b", "website": "spam.com"})[0], 200)
        self.assertEqual(len(db.list_leads()), 1)  # ловушка сработала, лид не создан

    def test_public_form_rate_limit(self):
        codes = [self.raw("POST", "/api/public/lead", {"name": "n", "contact": "c"})[0] for _ in range(7)]
        self.assertEqual(codes, [201] * 5 + [429] * 2)

    def test_login_rate_limit(self):
        codes = [self.raw("POST", "/api/login", {"password": "bad"})[0] for _ in range(12)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10:], [429, 429])

    def test_export_csv(self):
        self.req("POST", "/api/leads", {"name": "=HYPERLINK(1)", "contact": "+7 900", "tags": ["VIP"], "amount": 7})
        s, text = self.req("GET", "/api/export.csv")
        self.assertEqual(s, 200)
        self.assertTrue(text.startswith("﻿ID,Создан,Имя"))
        self.assertIn("'=HYPERLINK(1)", text)
        self.assertIn("VIP", text)
        _, filtered = self.req("GET", "/api/export.csv?tag=nope")
        self.assertEqual(len(filtered.strip().splitlines()), 1)

    def test_import_csv(self):
        data = "Имя;Телефон;Запрос;Теги\nИван;+7 900 111;сайт;vip, b2b\n;пустое;нет имени;\nПётр;p@x.ru;лого;\n"
        s, r = self.req("POST", "/api/import", data)
        self.assertEqual((s, r), (200, {"created": 2, "skipped": 1}))
        ivan = next(l for l in db.list_leads() if l["name"] == "Иван")
        self.assertEqual(sorted(t["name"] for t in ivan["tags"]), ["b2b", "vip", "Вручную", "Импорт"])
        self.assertEqual(self.req("POST", "/api/import", "a,b\n1,2\n")[0], 400)
        self.assertEqual(self.req("POST", "/api/import", "")[0], 400)

    def test_security_headers_and_form_page(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1])
        c.request("GET", "/")
        r = c.getresponse()
        r.read()
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1])
        c.request("GET", "/form")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 200)
        self.assertIn("frame-ancestors *", r.getheader("Content-Security-Policy"))


if __name__ == "__main__":
    unittest.main()
