"""A fake Telegram Bot API for tests: a tiny HTTP server on 127.0.0.1.

Tests set TELEGRAM_API (or patch tgapi.API) to `fake.url`. It never forwards
anything anywhere; run the suites with `--network none` as well, so no test
can ever post to a real group.

  fake = FakeTelegram(); fake.start()
  fake.script("sendMessage", {"ok": False, "error_code": 429, "description": "Too Many Requests",
                              "parameters": {"retry_after": 3}})   # next call only
  fake.updates.append({...})                                       # served by getUpdates
  fake.calls  -> [(token, method, params), ...]
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijk"
TOKEN2 = "987654321:AAOtherFakeTokenForTests_abcdefghij"


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.scripted = {}            # method -> [responses] (FIFO, one per call)
        self.updates = []
        self.next_mid = 100
        self.bots = {TOKEN: "farm_test_bot", TOKEN2: "ops_test_bot"}
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    params = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    params = {}
                parts = self.path.strip("/").split("/")
                token = parts[0][3:] if parts and parts[0].startswith("bot") else ""
                method = parts[1] if len(parts) > 1 else ""
                body = fake.handle(token, method, params)
                raw = json.dumps(body).encode()
                self.send_response(200 if body.get("ok") else int(body.get("error_code") or 400))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()

    def script(self, method, response):
        with self.lock:
            self.scripted.setdefault(method, []).append(response)

    def sent(self, method="sendMessage"):
        return [p for t, m, p in self.calls if m == method]

    def handle(self, token, method, params):
        with self.lock:
            self.calls.append((token, method, params))
            if self.scripted.get(method):
                return self.scripted[method].pop(0)
            if token not in self.bots:
                return {"ok": False, "error_code": 401, "description": "Unauthorized"}
            if method == "getMe":
                return {"ok": True, "result": {"id": 1, "is_bot": True, "username": self.bots[token]}}
            if method in ("sendMessage", "sendPhoto", "sendDocument"):
                self.next_mid += 1
                return {"ok": True, "result": {"message_id": self.next_mid,
                                               "chat": {"id": params.get("chat_id")}}}
            if method == "editMessageText":
                return {"ok": True, "result": True}
            if method == "getUpdates":
                off = params.get("offset") or 0
                ups = [u for u in self.updates if u["update_id"] >= off]
                return {"ok": True, "result": ups}
            return {"ok": True, "result": True}


def group_message(update_id, chat_id, text, *, title="Test group", kind="group", user=555):
    return {"update_id": update_id,
            "message": {"message_id": update_id, "date": 0, "text": text,
                        "from": {"id": user, "first_name": "T"},
                        "chat": {"id": chat_id, "type": kind, "title": title}}}
