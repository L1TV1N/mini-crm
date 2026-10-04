"""Регрессионные тесты на баги, найденные при ревью. Каждый тест падал на коде до исправления."""
import json
import os
import socket
import sqlite3
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402
import test_crm  # noqa: E402


class HttpHardening(test_crm.ApiCase):
    def send_raw(self, request: bytes):
        s = socket.create_connection(("127.0.0.1", self.srv.server_address[1]), timeout=3)
        s.sendall(request)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        return data

    def test_negative_content_length_does_not_hang(self):
        r = self.send_raw(b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n")
        self.assertIn(b" 400 ", r.split(b"\r\n")[0])

    def test_garbage_content_length_is_400_not_500(self):
        r = self.send_raw(b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n")
        self.assertIn(b" 400 ", r.split(b"\r\n")[0])

    def test_handler_has_socket_timeout(self):
        self.assertTrue(server.Handler.timeout)  # иначе медленный клиент держит поток вечно

    def test_nul_byte_in_path_is_404(self):
        self.assertEqual(self.raw("GET", "/%00")[0], 404)

    def test_head_request_supported(self):
        self.assertEqual(self.raw("HEAD", "/")[0], 200)

    def test_static_not_cached_forever(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1])
        c.request("GET", "/app.js")
        r = c.getresponse()
        r.read()
        self.assertIn("no-cache", r.getheader("Cache-Control") or "")

    def test_cors_headers_on_public_form_errors(self):
        s, d, _ = self.raw("POST", "/api/public/lead", {"name": "x"})
        self.assertEqual(s, 400)
        c = __import__("http.client").client.HTTPConnection("127.0.0.1", self.srv.server_address[1])
        c.request("POST", "/api/public/lead", json.dumps({"name": "x"}), {"Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "*")

    def test_db_file_is_private(self):
        db.conn()
        self.assertEqual(os.stat(db.DB_PATH).st_mode & 0o077, 0)


class ClientIp(test_crm.ApiCase):
    def post(self, xff):
        return self.raw("POST", "/api/public/lead", {"name": "n", "contact": "c"}, headers={"X-Forwarded-For": xff})[0]

    def test_proxy_header_used_when_peer_is_local_proxy(self):
        # за reverse proxy все запросы приходят с 127.0.0.1: без разбора заголовка лимит общий на всех посетителей
        codes = [self.post(f"203.0.113.{i}") for i in range(1, 8)]
        self.assertEqual(codes, [201] * 7)

    def test_spoofed_leftmost_entry_does_not_bypass_limit(self):
        # клиент может дописать любой адрес слева, настоящий приписывает прокси справа
        codes = [self.post(f"198.51.100.{i}, 203.0.113.9") for i in range(1, 8)]
        self.assertEqual(codes, [201] * 5 + [429] * 2)


class Auth(test_crm.ApiCase):
    def test_non_ascii_password_works(self):
        server.PASSWORD = "пароль123"
        self.assertEqual(self.raw("POST", "/api/login", {"password": "пароль123"})[0], 200)
        self.assertEqual(self.raw("POST", "/api/login", {"password": "парольXXX"})[0], 401)

    def test_changing_password_invalidates_sessions(self):
        server.PASSWORD = "first"
        _, _, cookie = self.raw("POST", "/api/login", {"password": "first"})
        self.assertEqual(self.raw("GET", "/api/leads", cookie=cookie)[0], 200)
        server.PASSWORD = "second"
        self.assertEqual(self.raw("GET", "/api/leads", cookie=cookie)[0], 401)

    def test_refuses_default_password_on_public_host(self):
        with self.assertRaises(SystemExit):
            server.check_startup("0.0.0.0", "admin")
        server.check_startup("127.0.0.1", "admin")
        server.check_startup("0.0.0.0", "strong-pass")


class Validation(test_crm.ApiCase):
    def test_huge_ids_are_404_not_500(self):
        big = "9" * 25
        for m, path in (("GET", f"/api/leads/{big}"), ("PUT", f"/api/leads/{big}"), ("DELETE", f"/api/leads/{big}"),
                        ("POST", f"/api/leads/{big}/reply"), ("PUT", f"/api/tags/{big}"), ("DELETE", f"/api/rules/{big}")):
            self.assertEqual(self.req(m, path, {"text": "x", "name": "x"})[0], 404, (m, path))

    def test_bulk_rejects_out_of_range_ids(self):
        for ids in ([10**30], [-1], [0], [True]):
            self.assertEqual(self.req("POST", "/api/leads/bulk", {"ids": ids, "action": "delete"})[0], 400, ids)

    def test_due_date_must_be_iso(self):
        _, lead = self.req("POST", "/api/leads", {"name": "A"})
        for bad in ("20261005", "2026-W41-1", "2026-10-5", "05.10.2026"):
            self.assertEqual(self.req("PUT", f"/api/leads/{lead['id']}", {"due_at": bad})[0], 400, bad)
        self.assertEqual(self.req("PUT", f"/api/leads/{lead['id']}", {"due_at": "2026-10-05"})[0], 200)

    def test_export_not_truncated_at_1000(self):
        with db.conn() as c:
            c.executemany("INSERT INTO leads(name,source,created_at,updated_at) VALUES(?,?,?,?)",
                          [(f"Лид {i}", "manual", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z") for i in range(1005)])
        _, text = self.req("GET", "/api/export.csv")
        self.assertEqual(len(text.strip().splitlines()), 1006)

    def test_import_cp1251(self):
        data = "Имя;Телефон;Запрос\nИван Петров;+7 900 111;нужен сайт\n".encode("cp1251")
        s, r = self.req("POST", "/api/import", data)
        self.assertEqual((s, r["created"]), (200, 1))
        lead = next(l for l in db.list_leads() if "Иван" in l["name"])
        self.assertEqual((lead["name"], lead["request"]), ("Иван Петров", "нужен сайт"))

    def test_import_keeps_all_contact_columns(self):
        s, r = self.req("POST", "/api/import", "Имя,Телефон,Email\nИван,+7 900 111,iv@x.ru\n")
        lead = db.list_leads()[0]
        self.assertIn("+7 900 111", lead["contact"])
        self.assertIn("iv@x.ru", lead["contact"])

    def test_import_huge_field_is_400(self):
        s, r = self.req("POST", "/api/import", "Имя\n" + "я" * 200000 + "\n")
        self.assertEqual(s, 400)


class Revision(test_crm.Base):
    def test_every_change_changes_revision(self):
        lead = db.create_lead("A")
        seen = {db.revision()}
        for st in ("in_work", "won", "lost"):  # все в пределах одной секунды
            db.update_lead(lead["id"], {"status": st})
            seen.add(db.revision())
        self.assertEqual(len(seen), 4)


class Races(test_crm.Base):
    def test_concurrent_new_tag_creation(self):
        ids = [db.create_lead(f"x{i}")["id"] for i in range(8)]
        errors, barrier = [], threading.Barrier(8)

        def work(i):
            try:
                barrier.wait()
                db.update_lead(i, {"tags": ["Общий-новый"]})
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        ts = [threading.Thread(target=work, args=(i,)) for i in ids]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(db.list_leads(tag="Общий-новый")), 8)

    def test_tag_rename_updates_rules(self):
        db.add_rule("сайт", "Разработка")
        old = next(t for t in db.list_tags() if t["name"] == "Разработка")
        db.update_tag(old["id"], name="Веб")
        self.assertEqual(db.list_rules()[0]["tag"], "Веб")
        self.assertIn("Веб", [t["name"] for t in db.create_lead("a", request="нужен сайт")["tags"]])
        self.assertEqual([t["name"] for t in db.list_tags() if t["name"] == "Разработка"], [])


class BotRegressions(test_crm.Base):
    def say(self, **kw):
        bot.handle_update({"update_id": 1, "message": test_crm.msg(**kw)}, self.tg)

    def test_non_ascii_connect_code_does_not_crash(self):
        db.connect_code()
        self.say(chat=900, text="/connect неверный")  # раньше TypeError в compare_digest
        self.assertIsNone(db.kv_get("notify_chat_id"))
        self.assertTrue(self.tg.sent)

    def test_stale_dialog_restarts(self):
        self.say(text="/start")
        with db.conn() as c:
            c.execute("UPDATE bot_dialogs SET updated_at='2020-01-01T00:00:00Z'")
        self.say(text="Нужен сайт для кофейни")
        d = db.dialog_get(100)
        self.assertEqual((d["step"], d["name"]), ("name", ""))  # не принимаем запрос за имя
        self.assertIn("Как к вам обращаться", self.tg.sent[-1][1])

    def test_unknown_command_not_saved_as_answer(self):
        self.say(text="/start")
        self.say(text="/help")
        self.assertEqual(db.dialog_get(100)["step"], "name")
        self.assertIn("/new", self.tg.sent[-1][1])

    def test_business_owner_message_without_known_connection(self):
        # апдейт business_connection мог прийти до запуска бота, тогда владелец не известен
        m = test_crm.msg(chat=500, text="Добрый день, высылаю смету", uid=7)
        m["business_connection_id"] = "unknown"
        bot.handle_update({"business_message": m}, self.tg)
        self.assertEqual(db.list_leads(), [])

    def test_business_echo_of_own_reply_is_not_duplicated(self):
        # сообщения, отправленные ботом от имени аккаунта, приходят апдейтом с sender_business_bot
        db.business_save("bc", 7, "owner", True)
        lead = db.create_lead("Пётр", "@p", "q", "telegram", tg_chat_id=500, business_connection_id="bc", first_message="вопрос")
        db.add_message(lead["id"], "out", "Ответ из CRM")
        echo = test_crm.msg(chat=500, text="Ответ из CRM", uid=7)
        echo["business_connection_id"] = "bc"
        echo["sender_business_bot"] = {"id": 1, "is_bot": True, "username": "agency_bot"}
        bot.handle_update({"business_message": echo}, self.tg)
        self.assertEqual([m["direction"] for m in db.get_lead(lead["id"])["messages"]], ["in", "out"])

    def test_business_client_message_still_creates_lead(self):
        m = test_crm.msg(chat=500, text="Здравствуйте", uid=500)
        m["business_connection_id"] = "unknown"
        bot.handle_update({"business_message": m}, self.tg)
        self.assertEqual(len(db.list_leads()), 1)


class FakeTelegram(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeTelegram.seen.append((self.path.rsplit("/", 1)[-1], body))
        out = json.dumps({"ok": True, "result": []}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class TelegramClient(unittest.TestCase):
    def test_getupdates_sends_longpoll_timeout_to_telegram(self):
        # раньше timeout уходил в сокет, а Telegram получал короткий опрос и поллер крутился вхолостую
        FakeTelegram.seen = []
        srv = HTTPServer(("127.0.0.1", 0), FakeTelegram)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old, bot.API_BASE = bot.API_BASE, f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            runner = bot.BotRunner("123:abc")
            self.assertEqual(runner._poll_timeout, 25)
            runner.tg.call("getUpdates", timeout=runner._poll_timeout, offset=0)
        finally:
            bot.API_BASE = old
            srv.shutdown()
        self.assertEqual(FakeTelegram.seen[0][1]["timeout"], 25)

    def test_socket_timeout_exceeds_longpoll(self):
        self.assertGreater(bot.SOCKET_TIMEOUT, 25)

    def test_webhook_deleted_before_bot_marked_ready(self):
        class Flaky:
            calls = 0

            def call(self, method, **kw):
                if method == "getMe":
                    return {"username": "b"}
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise RuntimeError("network")
                return True
        runner = bot.BotRunner("123:abc")
        runner.tg = Flaky()
        with self.assertRaises(RuntimeError):
            runner._connect()
        self.assertIsNone(runner.status["username"])  # иначе вебхук так и останется и поллинг будет получать 409
        runner._connect()
        self.assertEqual(runner.status["username"], "b")


if __name__ == "__main__":
    unittest.main()
