"""Бот за HTTP(S)-прокси: нужен там, где хостинг закрывает Telegram. Код менять не надо, urllib сам берёт HTTPS_PROXY.

Проверяем по-настоящему: TLS-сервер вместо api.telegram.org, прокси с командой CONNECT между ботом и ним.
Главное, что доказывает тест: прокси видит только адрес назначения, а токен в адресе запроса идёт внутри TLS-туннеля.
"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402

TOKEN = "123456:SECRET-TOKEN-VALUE"


class Tunnel(threading.Thread):
    """Минимальный прокси: принимает CONNECT, запоминает адрес и всё, что прошло по туннелю."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.connect_lines, self.raw = [], bytearray()

    def run(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(client,), daemon=True).start()

    def serve(self, client):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(4096)
            if not chunk:
                return
            head += chunk
        line = head.split(b"\r\n")[0].decode()
        self.connect_lines.append(line)
        host, port = line.split()[1].rsplit(":", 1)
        upstream = socket.create_connection((host, int(port)))
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        def pipe(src, dst, record):
            try:
                while True:
                    data = src.recv(4096)
                    if not data:
                        break
                    if record:
                        self.raw.extend(data)
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        threading.Thread(target=pipe, args=(upstream, client, True), daemon=True).start()
        pipe(client, upstream, True)


class FakeApi(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        FakeApi.seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")))
        out = json.dumps({"ok": True, "result": {"username": "agency_bot"}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@unittest.skipUnless(shutil.which("openssl"), "нужен openssl для самоподписанного сертификата")
class ProxyTunnel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cert, key = os.path.join(self.tmp.name, "c.pem"), os.path.join(self.tmp.name, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                        "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                       check=True, capture_output=True)
        FakeApi.seen = []
        self.api = HTTPServer(("127.0.0.1", 0), FakeApi)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.api.socket = ctx.wrap_socket(self.api.socket, server_side=True)
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.proxy = Tunnel()
        self.proxy.start()
        self.saved = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "SSL_CERT_FILE", "NO_PROXY", "no_proxy")}
        os.environ.update(HTTPS_PROXY=f"http://127.0.0.1:{self.proxy.port}", SSL_CERT_FILE=cert)
        for k in ("https_proxy", "NO_PROXY", "no_proxy"):
            os.environ.pop(k, None)
        urllib.request._opener = None  # глобальный opener запоминает прокси при первом вызове
        self.old_base, bot.API_BASE = bot.API_BASE, f"https://localhost:{self.api.server_address[1]}"

    def tearDown(self):
        bot.API_BASE = self.old_base
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        urllib.request._opener = None
        self.api.shutdown()
        self.proxy.sock.close()
        self.tmp.cleanup()

    def test_bot_reaches_api_through_https_proxy_without_code_changes(self):
        me = bot.TelegramAPI(TOKEN).call("getMe")
        self.assertEqual(me["username"], "agency_bot")
        self.assertEqual(FakeApi.seen[0][0], f"/bot{TOKEN}/getMe")  # запрос реально дошёл до «Telegram»
        self.assertEqual(len(self.proxy.connect_lines), 1, "запрос обязан идти через прокси")
        self.assertTrue(self.proxy.connect_lines[0].startswith(f"CONNECT localhost:{self.api.server_address[1]}"))

    def test_proxy_never_sees_the_token(self):
        bot.TelegramAPI(TOKEN).call("getMe")
        self.assertGreater(len(self.proxy.raw), 0)
        self.assertNotIn(TOKEN.encode(), bytes(self.proxy.raw))  # внутри туннеля только шифрованный TLS
        self.assertNotIn(b"getMe", bytes(self.proxy.raw))


if __name__ == "__main__":
    unittest.main()
