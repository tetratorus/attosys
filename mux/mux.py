#!/usr/bin/env python3
"""Telegram mux — one bot, many agents.

The whole company runs on a single Telegram bot. Running N agents that each
poll getUpdates with the same token is impossible (getUpdates is a single
consumer queue), so this process owns the one token: it runs the single
getUpdates loop, buckets each update by its forum topic (message_thread_id),
and serves every agent only its own topic.

Each agent's harness points telegram_api_base at this mux and uses its own
agent name as the path token, e.g.

    POST http://127.0.0.1:8811/bot<agent>/getUpdates
    POST http://127.0.0.1:8811/bot<agent>/sendMessage
    GET  http://127.0.0.1:8811/file/bot<agent>/<path>

The mux maps <agent> -> topic_id (from company.yaml), serves that topic's
buffered updates with a per-agent offset, and forwards everything else to the
real Bot API with the real token. Agents never see the token.

Config (env):
    MUX_PORT            default 8811
    ATTOSYS_ROOT        dir holding company.yaml + secrets.yaml (default: parent of this file's dir)
"""
import json
import os
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qsl, urlencode

import requests
import yaml

ROOT = pathlib.Path(os.environ.get("ATTOSYS_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
PORT = int(os.environ.get("MUX_PORT", "8811"))
UPSTREAM = "https://api.telegram.org"
POLL_TIMEOUT = 25

company = yaml.safe_load((ROOT / "company.yaml").read_text())
secrets = yaml.safe_load((ROOT / "secrets.yaml").read_text())
TOKEN = secrets["telegram_bot_token"]
ORG = company["org"]
CHAT_ID = str(company["telegram_chat_id"])

COMPANY_FILE = ROOT / "company.yaml"
THREAD_OF = {}                 # agent name -> thread id (reloaded when company.yaml changes)
BUFFERS = {}                   # thread id -> list of update dicts
LOCK = threading.Lock()
_mtime = 0


def refresh_topics():
    """Reload the agent->topic map when company.yaml changes, so an agent
    hired after startup is routed without restarting the mux. Existing buffers
    are kept; a buffer for each new topic is added."""
    global _mtime
    try:
        m = COMPANY_FILE.stat().st_mtime
    except OSError:
        return
    if m == _mtime:
        return
    try:
        c = yaml.safe_load(COMPANY_FILE.read_text())
    except Exception:
        return
    org = c.get("org", ORG)
    new_map = {f"{org}-{role}": int(spec["topic_id"])
               for role, spec in (c.get("agents") or {}).items()
               if spec.get("topic_id") is not None}
    with LOCK:
        THREAD_OF.clear()
        THREAD_OF.update(new_map)
        for tid in new_map.values():
            BUFFERS.setdefault(tid, [])
    _mtime = m
    print(f"[mux] topic map reloaded: {len(new_map)} agents", flush=True)


refresh_topics()


def tag_outbound(method, ctype, body, tag):
    """Prefix the agent's name onto outbound message text/captions, so every
    message in Telegram is attributable to its agent and a routing mistake
    shows up as the wrong [name] instead of being invisible. Rewrites
    form-urlencoded and JSON sends; multipart media uploads pass through."""
    if not method.startswith("send"):
        return body, ctype
    try:
        if ctype.startswith("application/json"):
            d = json.loads(body or b"{}")
            for k in ("text", "caption"):
                if d.get(k) is not None:
                    d[k] = f"[{tag}] {d[k]}"
                    return json.dumps(d).encode(), ctype
        elif ctype.startswith("application/x-www-form-urlencoded") or not ctype:
            items = parse_qsl(body.decode(), keep_blank_values=True)
            for i, (k, v) in enumerate(items):
                if k in ("text", "caption"):
                    items[i] = (k, f"[{tag}] {v}")
                    return urlencode(items).encode(), "application/x-www-form-urlencoded"
    except Exception:
        pass
    return body, ctype


def _upstream(method, http_method="POST", **kw):
    url = f"{UPSTREAM}/bot{TOKEN}/{method}"
    fn = requests.post if http_method == "POST" else requests.get
    return fn(url, timeout=60, **kw)


def poll_loop():
    """Single getUpdates loop. Fan each update into its topic's buffer."""
    offset = 0
    while True:
        try:
            r = requests.post(f"{UPSTREAM}/bot{TOKEN}/getUpdates",
                              data={"offset": offset, "timeout": POLL_TIMEOUT}, timeout=POLL_TIMEOUT + 20)
            for u in r.json().get("result") or []:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str((msg.get("chat") or {}).get("id") or "") != CHAT_ID:
                    continue
                tid = msg.get("message_thread_id")
                if tid is None or int(tid) not in BUFFERS:
                    continue  # not a topic any agent owns
                with LOCK:
                    BUFFERS[int(tid)].append(u)
        except Exception as e:
            print(f"[mux] poll error: {e}", flush=True)
            time.sleep(3)


def updates_for(thread_id, offset):
    """Return buffered updates for a thread with update_id >= offset.

    A non-zero offset means the agent has acked everything below it (the
    harness advances and persists its offset), so we prune as we go.
    """
    with LOCK:
        buf = BUFFERS.get(thread_id)
        if buf is None:
            return []
        if offset:
            buf[:] = [u for u in buf if u["update_id"] >= offset]
        return list(buf)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _agent_from_path(self, parts):
        # parts like ['bot<agent>', '<method>'] or ['file','bot<agent>',...]
        for p in parts:
            if p.startswith("bot"):
                return p[3:]
        return None

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, http_method):
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        agent = self._agent_from_path(parts)
        method = parts[-1] if parts else ""

        # file download: /file/bot<agent>/<file_path>
        if parts and parts[0] == "file":
            rel = "/".join(parts[2:])
            r = requests.get(f"{UPSTREAM}/file/bot{TOKEN}/{rel}", timeout=60)
            self.send_response(r.status_code)
            self.send_header("Content-Type", r.headers.get("Content-Type", "application/octet-stream"))
            self.send_header("Content-Length", str(len(r.content)))
            self.end_headers()
            self.wfile.write(r.content)
            return

        with LOCK:
            thread_id = THREAD_OF.get(agent) if agent else None
        if thread_id is None:
            self._send(404, {"ok": False, "description": f"unknown agent in path: {path}"})
            return

        if method == "getUpdates":
            body = self._read_body()
            try:
                params = json.loads(body) if body and body[:1] == b"{" else dict(
                    p.split("=", 1) for p in body.decode().split("&") if "=" in p)
            except Exception:
                params = {}
            offset = int(params.get("offset") or 0)
            # Emulate Telegram long-polling so the harness paces itself instead
            # of hot-looping: wait up to `timeout` seconds for the first update.
            timeout = min(float(params.get("timeout") or 0), 50)
            deadline = time.time() + timeout
            while True:
                ups = updates_for(thread_id, offset)
                if ups or time.time() >= deadline:
                    break
                time.sleep(0.4)
            self._send(200, {"ok": True, "result": ups})
            return

        if method == "getMe":
            r = _upstream("getMe", "GET")
            self._send(r.status_code, r.json())
            return

        # send*/setMessageReaction/etc — forward with the real token. The
        # harness already injects chat_id + message_thread_id, so the message
        # lands in the right topic; we swap in the real token and tag the text
        # with the agent's name for attribution.
        ctype = self.headers.get("Content-Type", "")
        body = self._read_body()
        body, ctype = tag_outbound(method, ctype, body, agent)
        try:
            r = requests.post(f"{UPSTREAM}/bot{TOKEN}/{method}", data=body,
                              headers={"Content-Type": ctype} if ctype else None, timeout=60)
            self._send(r.status_code, r.json())
        except Exception as e:
            self._send(502, {"ok": False, "description": str(e)})


def main():
    if not THREAD_OF:
        print("[mux] no agents with topic_id in company.yaml yet — will pick them up as they're hired", flush=True)
    print(f"[mux] {len(THREAD_OF)} agents, chat {CHAT_ID}, listening on 127.0.0.1:{PORT}", flush=True)

    def reloader():  # pick up agents hired after startup, independent of poll cadence
        while True:
            time.sleep(3)
            refresh_topics()

    threading.Thread(target=poll_loop, daemon=True, name="poll").start()
    threading.Thread(target=reloader, daemon=True, name="reload").start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
