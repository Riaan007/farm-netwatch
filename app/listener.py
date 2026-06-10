"""ntfy remote-control listener.

Subscribes to the configured ntfy topic's JSON stream and runs any message that
parses as a command (see commands.py), posting the result back to the topic so
it shows on the phone. Lets you ping/port/tracert/scan a device from anywhere by
sending a message to the topic — or by tapping the action buttons on an alert.

Loop-safety: we record the ids of messages we publish and skip them, and our
result messages are prefixed so they never re-parse as a command.
"""
import json
import threading
import time

import requests

import commands
import config
import notify

RESULT_PREFIX = "\U0001F527 "  # 🔧 — ensures our own output is never a command


class NtfyListener:
    def __init__(self):
        self._stop = False
        self._published = []          # recent message ids we sent
        self._pub_set = set()

    def _note(self, msg_id):
        if not msg_id or msg_id == "ok":
            return
        self._published.append(msg_id)
        self._pub_set.add(msg_id)
        if len(self._published) > 200:
            old = self._published.pop(0)
            self._pub_set.discard(old)

    def _handle(self, ev):
        if ev.get("event") != "message":
            return
        if ev.get("id") in self._pub_set:
            return
        text = (ev.get("message") or "").strip()
        result = commands.dispatch(text)
        if not result:
            return
        cfg = config.load()
        mid = notify.push(cfg["alerts"], "Netwatch", RESULT_PREFIX + result, tags=["wrench"])
        self._note(mid)

    def loop(self):
        while not self._stop:
            cfg = config.load()
            alerts = cfg["alerts"]
            topic = alerts.get("ntfy_topic", "").strip()
            if not topic or not alerts.get("allow_commands", True):
                time.sleep(5)
                continue
            server = (alerts.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
            url = f"{server}/{topic}/json"  # no `since` -> only new messages
            try:
                with requests.get(url, stream=True, timeout=(10, 75)) as r:
                    for line in r.iter_lines():
                        if self._stop:
                            return
                        # react to a topic/server change within a keepalive cycle
                        cur = config.load()["alerts"]
                        if (cur.get("ntfy_topic", "").strip() != topic or
                                not cur.get("allow_commands", True)):
                            break
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        self._handle(ev)
            except requests.RequestException:
                time.sleep(5)

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()


listener = NtfyListener()
