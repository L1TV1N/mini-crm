"""Сквозная проверка требований тестового задания: от сообщения боту до лида с тегом в CRM.

Поднимает всё как в бою: поддельный Telegram API по HTTP, настоящий BotRunner с поллером, настоящий HTTP-сервер CRM.
Дальше действует как клиент и менеджер: пишет боту, добавляет лида руками, ставит теги и фильтрует по тегу.
"""
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402
import test_crm  # noqa: E402


class FakeTelegram:
    """Минимальный Bot API: очередь апдейтов, учёт offset, журнал отправленных сообщений."""

    def __init__(self):
        self.updates, self.sent, self.next_id, self.lock = [], [], 1, threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                method = self.path.rsplit("/", 1)[-1]
                if method == "getMe":
                    result = {"id": 1, "username": "agency_test_bot"}
                elif method == "getUpdates":
                    deadline = time.time() + min(body.get("timeout", 0), 0.3)
                    while True:
                        with outer.lock:
                            result = [u for u in outer.updates if u["update_id"] >= body.get("offset", 0)]
                        if result or time.time() >= deadline:
                            break
                        time.sleep(0.05)
                elif method == "sendMessage":
                    with outer.lock:
                        outer.sent.append(body)
                    result = {"message_id": len(outer.sent)}
                else:
                    result = True
                out = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def push(self, **update):
        with self.lock:
            update["update_id"] = self.next_id
            self.next_id += 1
            self.updates.append(update)

    def user_says(self, text, chat=42, first="Мария", username="msokolova"):
        self.push(message={"chat": {"id": chat, "type": "private"},
                           "from": {"id": chat, "first_name": first, "username": username}, "text": text})

    def stop(self):
        self.srv.shutdown()


def wait_for(fn, timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    return fn()


class TzRequirements(test_crm.ApiCase):
    def setUp(self):
        super().setUp()
        self.tg = FakeTelegram()
        self.old_base, bot.API_BASE = bot.API_BASE, self.tg.url
        server.runner = bot.BotRunner("123:test-token")
        server.runner.start()

    def tearDown(self):
        server.runner.stop()
        bot.API_BASE = self.old_base
        self.tg.stop()
        super().tearDown()

    def leads(self, tag=None):
        path = "/api/leads" + (f"?tag={tag}" if tag else "")
        from urllib.parse import quote
        return self.req("GET", "/api/leads" + (f"?tag={quote(tag)}" if tag else ""))[1]

    # ТЗ п.1: человек пишет боту, бот собирает имя, контакт, запрос, и заявка сама появляется в CRM
    def test_1_bot_collects_application_and_it_appears_in_crm(self):
        self.assertTrue(wait_for(lambda: self.req("GET", "/api/state")[1]["bot"]["running"]), "бот не стартовал")
        self.tg.user_says("Здравствуйте")
        self.tg.user_says("Мария Соколова")
        self.tg.user_says("+7 999 123-45-67")
        self.assertEqual(self.leads(), [], "до последнего ответа лида быть не должно")
        self.tg.user_says("Нужна реклама в Instagram для студии йоги")
        lead = wait_for(lambda: (self.leads("Бот") or [None])[0])
        self.assertIsNotNone(lead, "заявка из бота не появилась в CRM")
        self.assertEqual(lead["name"], "Мария Соколова")                       # имя
        self.assertIn("+7 999 123-45-67", lead["contact"])                       # контакт
        self.assertEqual(lead["request"], "Нужна реклама в Instagram для студии йоги")  # запрос
        self.assertEqual(lead["source"], "bot")
        self.assertIn("Бот", [t["name"] for t in lead["tags"]])                  # лид сразу с тегом
        replies = [m["text"] for m in self.tg.sent if m["chat_id"] == 42]
        self.assertEqual(len(replies), 4, replies)                               # приветствие, 2 вопроса, подтверждение
        self.assertIn(f"№{lead['id']}", replies[-1])

    # ТЗ п.2: входящие сообщения в подключённый обычный Telegram попадают в CRM как лиды
    def test_2_incoming_messages_of_connected_account_become_leads(self):
        self.tg.push(business_connection={"id": "bc1", "user": {"id": 7, "username": "owner"}, "is_enabled": True})
        for text in ("Добрый день, нужен логотип", "И сколько это займёт?"):
            self.tg.push(business_message={"business_connection_id": "bc1", "chat": {"id": 77, "type": "private"},
                                           "from": {"id": 77, "first_name": "Пётр", "username": "petr_a"}, "text": text})
        lead = wait_for(lambda: (self.leads("Telegram") or [None])[0])
        self.assertIsNotNone(lead, "сообщение из подключённого Telegram не стало лидом")
        self.assertEqual((lead["name"], lead["contact"], lead["source"]), ("Пётр", "@petr_a", "telegram"))
        full = wait_for(lambda: (lambda l: l if len(l["messages"]) == 2 else None)(self.req("GET", f"/api/leads/{lead['id']}")[1]))
        self.assertIsNotNone(full, "второе сообщение должно попасть в тот же лид")
        self.assertEqual(len(self.leads("Telegram")), 1, "не должно быть дублей")
        # сообщение владельца аккаунта лидом не становится
        self.tg.push(business_message={"business_connection_id": "bc1", "chat": {"id": 77, "type": "private"},
                                       "from": {"id": 7, "first_name": "Я"}, "text": "Отвечу через час"})
        time.sleep(1.5)
        self.assertEqual(len(self.leads()), 1)

    # ТЗ п.3: лида можно завести в CRM руками
    def test_3_manual_lead(self):
        s, lead = self.req("POST", "/api/leads", {"name": "Олег Громов", "contact": "oleg@example.com",
                                                  "request": "Сайт-визитка"})
        self.assertEqual(s, 201)
        self.assertEqual(self.req("GET", f"/api/leads/{lead['id']}")[1]["name"], "Олег Громов")
        self.assertEqual(self.req("PUT", f"/api/leads/{lead['id']}", {"contact": "+7 900 000-00-00"})[1]["contact"],
                         "+7 900 000-00-00")  # и отредактировать

    # ТЗ п.4: лидам можно присваивать теги и видеть лидов по тегу
    def test_4_tags_assign_and_filter(self):
        self.tg.user_says("/start")
        for t in ("Мария", "m@x.ru", "Нужен SMM"):
            self.tg.user_says(t)
        bot_lead = wait_for(lambda: (self.leads("Бот") or [None])[0])
        self.assertIsNotNone(bot_lead)
        _, manual = self.req("POST", "/api/leads", {"name": "Олег", "tags": ["Горячий"]})
        # тег можно присвоить лиду из бота
        self.req("PUT", f"/api/leads/{bot_lead['id']}", {"tags": ["Бот", "Горячий", "SMM"]})
        self.assertEqual({l["id"] for l in self.leads("Горячий")}, {bot_lead["id"], manual["id"]})
        self.assertEqual([l["id"] for l in self.leads("SMM")], [bot_lead["id"]])
        self.assertEqual([l["id"] for l in self.leads("Вручную")], [manual["id"]])
        self.assertEqual({t["name"]: t["count"] for t in self.req("GET", "/api/tags")[1]}["Горячий"], 2)

    # обязательный сквозной сценарий целиком: сообщение боту -> лид с тегом -> другой тег -> фильтр
    def test_end_to_end_from_bot_message_to_tagged_lead(self):
        for t in ("/start", "Анна", "anna@example.com", "Нужен сайт"):
            self.tg.user_says(t, chat=5, first="Анна", username="anna")
        lead = wait_for(lambda: (self.leads("Бот") or [None])[0])
        self.assertIsNotNone(lead)
        self.req("PUT", f"/api/leads/{lead['id']}", {"tags": ["Бот", "Сайт-заказ"]})
        found = self.leads("Сайт-заказ")
        self.assertEqual([(l["name"], l["request"]) for l in found], [("Анна", "Нужен сайт")])


if __name__ == "__main__":
    unittest.main()
