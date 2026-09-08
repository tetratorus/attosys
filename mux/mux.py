#!/usr/bin/env python3
"""Telegram mux — one bot, many agents (async).

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
buffered updates with a per-agent offset, and forwards everything else to
the real Bot API with the real token. Agents never see the token.

async design: one event loop, one Telegram poll task, one reloader task,
one aiohttp web server. Per-topic asyncio.Event wakes waiting getUpdates
handlers. No threads, no locks, no thread pool to exhaust — scales to as
many agents as the OS lets us hold sockets for.

Config (env):
    MUX_PORT            default 8811
    ATTOSYS_ROOT        dir holding company.yaml + secrets.yaml (default: parent of this file's dir)
"""
import asyncio
import json
import os
import pathlib
import sqlite3
import time
from urllib.parse import urlparse, parse_qsl, urlencode

import aiohttp
import yaml

ROOT = pathlib.Path(os.environ.get("ATTOSYS_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
PORT = int(os.environ.get("MUX_PORT", "8811"))
UPSTREAM = "https://api.telegram.org"
POLL_TIMEOUT = 25

company = yaml.safe_load((ROOT / "company.yaml").read_text())
UPSTREAM = company.get("telegram_api_base", UPSTREAM)
secrets = yaml.safe_load((pathlib.Path(os.environ.get("CREDENTIALS_DIRECTORY", ROOT)) / "secrets.yaml").read_text())
TOKEN = secrets["telegram_bot_token"]
ORG = company["org"]
CHAT_ID = str(company["telegram_chat_id"])

COMPANY_FILE = ROOT / "company.yaml"
THREAD_OF = {}                 # agent name -> thread id (reloaded when company.yaml changes)
BUFFERS = {}                   # thread id -> list of update dicts
EVENTS = {}                    # thread id -> asyncio.Event (set when new updates arrive)
_mtime = 0
DB_PATH = pathlib.Path(os.environ.get("MUX_DB", ROOT / "mux.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DB_PATH.touch(mode=0o600, exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY, topic INTEGER, body TEXT);
CREATE INDEX IF NOT EXISTS updates_topic ON updates(topic, id);
CREATE TABLE IF NOT EXISTS cursor(offset INTEGER);
INSERT INTO cursor SELECT 0 WHERE NOT EXISTS(SELECT 1 FROM cursor);
""")


def refresh_topics():
    """Reload the agent->topic map when company.yaml changes, so an agent
    hired after startup is routed without restarting the mux. Existing buffers
    are kept; a buffer + event for each new topic is added. No lock needed —
    runs on the single event loop thread."""
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
    THREAD_OF.clear()
    THREAD_OF.update(new_map)
    for tid in new_map.values():
        BUFFERS.setdefault(tid, [])
        EVENTS.setdefault(tid, asyncio.Event())
    _mtime = m
    print(f"[mux] topic map reloaded: {len(new_map)} agents", flush=True)


def tag_outbound(method, ctype, body, tag):
    """Prefix the agent's name onto outbound message text/captions, so every
    message in Telegram is attributable to its agent and a routing mistake
    shows up as the wrong <name> instead of being invisible. Angle brackets,
    not square: Telegram's legacy Markdown parser eats [x] when a message is
    sent with parse_mode=Markdown; <x> survives. Rewrites form-urlencoded and
    JSON sends; multipart media uploads pass through."""
    if not method.startswith("send"):
        return body, ctype
    try:
        if ctype.startswith("application/json"):
            d = json.loads(body or b"{}")
            for k in ("text", "caption"):
                if d.get(k) is not None:
                    d[k] = f"<{tag}> {d[k]}"
                    return json.dumps(d).encode(), ctype
        elif ctype.startswith("application/x-www-form-urlencoded") or not ctype:
            items = parse_qsl(body.decode(), keep_blank_values=True)
            for i, (k, v) in enumerate(items):
                if k in ("text", "caption"):
                    items[i] = (k, f"<{tag}> {v}")
                    return urlencode(items).encode(), "application/x-www-form-urlencoded"
    except Exception:
        pass
    return body, ctype


def updates_for(thread_id, offset):
    """Return updates for *thread_id* with update_id >= *offset*, and prune
    consumed entries (update_id < offset) from the buffer. Single-threaded
    event loop — no lock needed."""
    with db:
        db.execute("DELETE FROM updates WHERE topic=? AND id<?", (thread_id, offset))
    kept = [json.loads(row[0]) for row in db.execute("SELECT body FROM updates WHERE topic=? ORDER BY id LIMIT 100", (thread_id,))]
    BUFFERS[thread_id] = kept
    return kept


def persist_updates(updates):
    refresh_topics()
    topics = set()
    with db:
        for update in updates:
            message = update.get("message") or {}
            topic = message.get("message_thread_id")
            if str((message.get("chat") or {}).get("id")) == CHAT_ID and topic in BUFFERS:
                db.execute("INSERT OR IGNORE INTO updates VALUES(?,?,?)", (update["update_id"], topic, json.dumps(update)))
                topics.add(topic)
            db.execute("UPDATE cursor SET offset=MAX(offset, ?)", (update["update_id"] + 1,))
    for topic in topics:
        EVENTS[topic].set()


async def poll_loop(session):
    """Single getUpdates task. Fans each update into its topic's buffer and
    signals the per-topic Event so any waiting getUpdates handler wakes up."""
    while True:
        try:
            offset = db.execute("SELECT offset FROM cursor").fetchone()[0]
            async with session.post(
                    f"{UPSTREAM}/bot{TOKEN}/getUpdates",
                    data={"offset": offset, "timeout": POLL_TIMEOUT},
                    timeout=aiohttp.ClientTimeout(total=POLL_TIMEOUT + 20)) as r:
                data = await r.json()
            if not data.get("ok"):
                raise RuntimeError("upstream getUpdates failed")
            ups = data.get("result", [])
            if ups:
                print(f"[mux:debug] poll got {len(ups)} updates, offset={offset}", flush=True)
                persist_updates(ups)
        except Exception as e:
            print(f"[mux] poll error: {e}", flush=True)
            await asyncio.sleep(5)


async def reloader():
    """Pick up agents hired after startup, independent of poll cadence."""
    while True:
        await asyncio.sleep(3)
        try:
            refresh_topics()
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f'[mux] reloader error: {e}', flush=True)


# ---------- HTTP handlers ----------

async def handle_get_updates(request):
    """Agent long-poll: wait up to `timeout` for updates in this agent's
    topic, then return them. Mirrors Telegram's getUpdates semantics."""
    refresh_topics()
    agent = request.match_info["agent"]
    thread_id = THREAD_OF.get(agent)
    if thread_id is None:
        return web.json_response({"ok": False, "description": f"unknown agent: {agent}"}, status=403)

    body = await request.read()
    try:
        params = dict(p.split("=", 1) for p in body.decode().split("&") if "=" in p)
    except Exception:
        params = {}
    offset = int(params.get("offset") or 0)
    timeout = min(float(params.get("timeout") or 0), 50)

    ev = EVENTS.get(thread_id)
    deadline = time.monotonic() + timeout
    while True:
        ups = updates_for(thread_id, offset)
        if ups:
            print(f"[mux:debug] serving {len(ups)} updates to {agent} (offset={offset})", flush=True)
            return web.json_response({"ok": True, "result": ups})
        if ev is None:
            return web.json_response({"ok": True, "result": []})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return web.json_response({"ok": True, "result": []})
        try:
            await asyncio.wait_for(ev.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return web.json_response({"ok": True, "result": []})
        ev.clear()


async def handle_get_me(request):
    session = request.app["session"]
    async with session.get(f"{UPSTREAM}/bot{TOKEN}/getMe",
                           timeout=aiohttp.ClientTimeout(total=30)) as r:
        data = await r.json()
    return web.json_response(data, status=r.status)


async def handle_forward(request):
    """Forward send*/setMessageReaction/etc to the real Bot API with the real
    token, tagging the text with the agent's name for attribution."""
    refresh_topics()
    agent = request.match_info["agent"]
    if THREAD_OF.get(agent) is None:
        return web.json_response({"ok": False, "description": f"unknown agent: {agent}"}, status=403)
    method = request.match_info["method"]
    ctype = request.headers.get("Content-Type", "")
    body = await request.read()
    if request.method == "GET" and request.query:
        body = urlencode(list(request.query.items())).encode()
        ctype = "application/x-www-form-urlencoded"
    body, ctype = tag_outbound(method, ctype, body, agent)
    session = request.app["session"]
    try:
        async with session.post(f"{UPSTREAM}/bot{TOKEN}/{method}",
                                data=body,
                                headers={"Content-Type": ctype} if ctype else None,
                                timeout=aiohttp.ClientTimeout(total=60)) as r:
            data = await r.json()
        return web.json_response(data, status=r.status)
    except Exception as e:
        return web.json_response({"ok": False, "description": str(e)}, status=502)


async def handle_file(request):
    """File downloads: GET /file/bot<agent>/<path>"""
    agent = request.match_info["agent"]
    if THREAD_OF.get(agent) is None:
        return web.json_response({"ok": False, "description": f"unknown agent: {agent}"}, status=403)
    file_path = request.match_info["path"]
    session = request.app["session"]
    async with session.get(f"{UPSTREAM}/file/bot{TOKEN}/{file_path}",
                           timeout=aiohttp.ClientTimeout(total=60)) as r:
        data = await r.read()
    return web.Response(body=data, content_type=r.content_type, status=r.status)


# We need `web` imported; aiohttp.web is the standard alias.
from aiohttp import web


async def main():
    refresh_topics()
    if not THREAD_OF:
        print("[mux] no agents with topic_id in company.yaml yet — will pick them up as they're hired", flush=True)
    print(f"[mux] {len(THREAD_OF)} agents, chat {CHAT_ID}, listening on 127.0.0.1:{PORT}", flush=True)

    app = web.Application(client_max_size=5*1024*1024)  # 5MB
    app["session"] = aiohttp.ClientSession()

    # Routes. The agent segment carries the routing key; method is the Bot API
    # call. Paths: /bot<agent>/<method>  and  /file/bot<agent>/<path>
    app.router.add_post("/bot{agent}/getUpdates", handle_get_updates)
    app.router.add_post("/bot{agent}/getMe", handle_get_me)
    app.router.add_route("*", "/bot{agent}/{method:.*}", handle_forward)
    app.router.add_get("/file/bot{agent}/{path:.*}", handle_file)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT, shutdown_timeout=5)
    await site.start()

    # Background tasks — set BEFORE site.start() to avoid aiohttp 3.14 deprecation
    _bg_tasks = set()
    for coro, name in [(poll_loop(app["session"]), "poll"), (reloader(), "reload")]:
        t = asyncio.create_task(coro, name=name)
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)

    # Signal handlers for crash diagnostics
    import signal
    stopped = asyncio.Event()
    def _log_signal(signum, frame):
        import os, traceback
        print(f"[mux] received signal {signum} ({signal.Signals(signum).name})", flush=True)
        traceback.print_stack(frame)
        stopped.set()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _log_signal, signal.SIGTERM, None)
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, _log_signal, signal.SIGINT, None)

    # Keep the server running forever.
    try:
        await stopped.wait()
    finally:
        for t in list(_bg_tasks):
            t.cancel()
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
        await app["session"].close()
        await runner.cleanup()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
