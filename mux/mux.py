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
import time
from urllib.parse import urlparse, parse_qsl, urlencode

import aiohttp
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
EVENTS = {}                    # thread id -> asyncio.Event (set when new updates arrive)
_bg_tasks = set()              # strong refs to background asyncio tasks (prevent GC)
_mtime = 0


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


def updates_for(thread_id, offset):
    """Return updates for *thread_id* with update_id >= *offset* (Telegram's
    getUpdates semantics: offset is the first update_id to return, not the
    last to exclude), and prune consumed entries (< offset) from the buffer.
    Single-threaded event loop — no lock needed."""
    buf = BUFFERS.get(thread_id)
    if not buf:
        return []
    kept = [u for u in buf if u["update_id"] >= offset]
    BUFFERS[thread_id] = kept
    return kept


async def poll_loop(session):
    """Single getUpdates task. Fans each update into its topic's buffer and
    signals the per-topic Event so any waiting getUpdates handler wakes up."""
    offset = 0
    while True:
        try:
            async with session.post(
                    f"{UPSTREAM}/bot{TOKEN}/getUpdates",
                    data={"offset": offset, "timeout": POLL_TIMEOUT},
                    timeout=aiohttp.ClientTimeout(total=POLL_TIMEOUT + 20)) as r:
                data = await r.json()
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str((msg.get("chat") or {}).get("id") or "") != CHAT_ID:
                    continue
                tid = msg.get("message_thread_id")
                if tid is None:
                    continue
                tid = int(tid)
                if tid not in BUFFERS:
                    continue  # not a topic any agent owns
                BUFFERS[tid].append(u)
                ev = EVENTS.get(tid)
                if ev is not None:
                    ev.set()
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
            if ev:
                ev.clear()
            return web.json_response({"ok": True, "result": ups})
        if ev is None:
            return web.json_response({"ok": True, "result": []})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if ev:
                ev.clear()
            return web.json_response({"ok": True, "result": []})
        try:
            await asyncio.wait_for(ev.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            if ev:
                ev.clear()
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
    async with session.get(f"{UPSTREAM}/bot{TOKEN}/getFile?file_id={file_path}",
                           timeout=aiohttp.ClientTimeout(total=60)) as r:
        data = await r.json()
    return web.json_response(data, status=r.status)


# We need `web` imported; aiohttp.web is the standard alias.
from aiohttp import web


async def main():
    refresh_topics()
    if not THREAD_OF:
        print("[mux] no agents with topic_id in company.yaml yet — will pick them up as they're hired", flush=True)
    print(f"[mux] {len(THREAD_OF)} agents, chat {CHAT_ID}, listening on 127.0.0.1:{PORT}", flush=True)

    app = web.Application()
    app["session"] = aiohttp.ClientSession()

    # Routes. The agent segment carries the routing key; method is the Bot API
    # call. Paths: /bot<agent>/<method>  and  /file/bot<agent>/<path>
    app.router.add_post("/bot{agent}/getUpdates", handle_get_updates)
    app.router.add_post("/bot{agent}/getMe", handle_get_me)
    app.router.add_post("/bot{agent}/{method:.*}", handle_forward)
    app.router.add_get("/file/bot{agent}/{path:.*}", handle_file)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()

    # Background tasks: the single Telegram poll loop + company.yaml reloader.
    # Stored in a module-level set for strong references — asyncio only holds
    # weak refs, so unreferenced tasks get garbage-collected silently.
    for coro, name in [(poll_loop(app["session"]), "poll"), (reloader(), "reload")]:
        t = asyncio.create_task(coro, name=name)
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)

    # Keep the server running forever.
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        for t in list(_bg_tasks):
            t.cancel()
        await app["session"].close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
