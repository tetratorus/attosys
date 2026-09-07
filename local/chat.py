import asyncio
import hmac
import json
import os
import pathlib
import sqlite3
import time
import uuid

from aiohttp import web
import yaml

ROOT = pathlib.Path(os.environ.get("ATTOSYS_ROOT", "/opt/attosys"))
STATE = pathlib.Path(os.environ.get("CHAT_STATE", "/var/lib/atto-chat"))
PORT = int(os.environ.get("CHAT_PORT", "8090"))
STATE.mkdir(parents=True, exist_ok=True)
TOKEN = yaml.safe_load((pathlib.Path(os.environ.get("CREDENTIALS_DIRECTORY", ROOT)) / "secrets.yaml").read_text())["telegram_bot_token"]
db = sqlite3.connect(STATE / "chat.sqlite3")
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS topics(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, name TEXT, mime TEXT, content BLOB);
CREATE TABLE IF NOT EXISTS cursor(offset INTEGER);
INSERT INTO cursor SELECT 0 WHERE NOT EXISTS(SELECT 1 FROM cursor);
""")
wake = asyncio.Event()
polling = False


def ok(result):
    return web.json_response({"ok": True, "result": result})


def error(status, description):
    return web.json_response({"ok": False, "error_code": status, "description": description}, status=status)


async def fields(request):
    return await request.json() if request.content_type == "application/json" else {**request.query, **await request.post()}


def message(data, direction, file=None, kind="document"):
    body = {"chat": {"id": int(data.get("chat_id", -1001)), "type": "supergroup", "title": "Local company"},
            "from": {"id": 42 if direction == "out" else 1, "is_bot": direction == "out", "first_name": "Attobot" if direction == "out" else "CEO"},
            "date": int(time.time())}
    if data.get("message_thread_id") is not None:
        body["message_thread_id"] = int(data["message_thread_id"])
    with db:
        if file:
            file_id = uuid.uuid4().hex
            content = file.file.read()
            name = pathlib.Path(file.filename).name
            db.execute("INSERT INTO files VALUES(?,?,?,?)", (file_id, name, file.content_type, content))
            media = {"file_id": file_id, "file_unique_id": file_id, "file_name": name, "file_size": len(content)}
            body[kind] = [media] if kind == "photo" else media
            body["caption"] = data.get("caption", data.get("text", ""))
        else:
            body["text"] = data.get("text", "")
        cursor = db.execute("INSERT INTO messages(direction,body) VALUES(?, '{}')", (direction,))
        body["message_id"] = cursor.lastrowid
        db.execute("UPDATE messages SET body=? WHERE id=?", (json.dumps(body), cursor.lastrowid))
    wake.set()
    return body


async def bot(request):
    global polling
    if not hmac.compare_digest(request.match_info["token"], TOKEN):
        return error(401, "Unauthorized")
    method = request.match_info["method"]
    data = await fields(request)
    if method == "getMe":
        return ok({"id": 42, "is_bot": True, "username": "local_company_bot", "first_name": "Company", "can_join_groups": True, "can_read_all_group_messages": True})
    if method == "createForumTopic":
        with db:
            topic = db.execute("INSERT INTO topics(name) VALUES(?)", (data["name"],)).lastrowid
        return ok({"message_thread_id": topic, "name": data["name"]})
    if method == "getUpdates":
        if polling:
            return error(409, "Conflict: another getUpdates is active")
        polling = True
        try:
            with db:
                db.execute("UPDATE cursor SET offset=MAX(offset, ?)", (max(0, int(data.get("offset", 0))),))
            deadline = time.monotonic() + min(50, max(0, float(data.get("timeout", 0))))
            while True:
                wake.clear()
                rows = db.execute("SELECT id,body FROM messages WHERE direction='in' AND id >= (SELECT offset FROM cursor) ORDER BY id LIMIT 100").fetchall()
                if rows or time.monotonic() >= deadline:
                    return ok([{"update_id": row[0], "message": json.loads(row[1])} for row in rows])
                try:
                    await asyncio.wait_for(wake.wait(), deadline - time.monotonic())
                except asyncio.TimeoutError:
                    return ok([])
        finally:
            polling = False
    if method == "getFile":
        row = db.execute("SELECT id,name,length(content) FROM files WHERE id=?", (data.get("file_id"),)).fetchone()
        return ok({"file_id": row[0], "file_path": f"{row[0]}/{row[1]}", "file_size": row[2]}) if row else error(404, "file not found")
    if method == "setMessageReaction":
        return ok(True)
    if method == "sendMessage":
        return ok(message(data, "out"))
    for kind in ("document", "photo", "audio", "voice", "video"):
        if method == "send" + kind.title() and isinstance(data.get(kind), web.FileField):
            return ok(message(data, "out", data[kind], kind))
    return error(400, "unsupported method")


async def download(request):
    if request.match_info.get("token") and not hmac.compare_digest(request.match_info["token"], TOKEN):
        return error(401, "Unauthorized")
    row = db.execute("SELECT mime,content FROM files WHERE id=?", (request.match_info["file_id"],)).fetchone()
    if not row:
        return error(404, "file not found")
    return web.Response(body=row[1], content_type="application/octet-stream", headers={"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"})


async def operator(request):
    host = request.headers.get("Host")
    if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}") or request.headers.get("X-Attobot-Lab") != "1":
        return error(403, "local operator header required")
    if request.headers.get("Origin") not in (None, f"http://{host}"):
        return error(403, "cross-origin requests forbidden")
    if request.method == "POST":
        data = await fields(request)
        return ok(message(data, "in", data.get("file")))
    company = yaml.safe_load((ROOT / "company.yaml").read_text())
    rows = db.execute("SELECT direction,body FROM (SELECT * FROM messages ORDER BY id DESC LIMIT 200) ORDER BY id").fetchall()
    return web.json_response({"company": company["name"], "agents": [{"name": f"{company['org']}-{role}", "topic": spec.get("topic_id")} for role, spec in company["agents"].items()],
                              "messages": [{"direction": row[0], **json.loads(row[1])} for row in rows]})


async def page(request):
    return web.Response(text=PAGE, content_type="text/html", headers={"Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'"})


PAGE = """<!doctype html><html><meta charset=utf-8><title>Attosys local company</title>
<style>body{max-width:1000px;margin:30px auto;font:15px system-ui;background:#151b20;color:#e2e9ee}header,form{display:flex;gap:12px;align-items:center}select,button,textarea,input{font:inherit;padding:8px;background:#24313a;color:inherit;border:1px solid #52616b;border-radius:5px}#messages{height:65vh;overflow:auto;margin:20px 0}article{white-space:pre-wrap;padding:12px;border-bottom:1px solid #34424b}small{color:#93a6b4}textarea{flex:1}#notice{min-height:24px;color:#edbd7d}</style>
<header><h2 id=company>Local company</h2><select id=topic></select><small>Real employees · real tools · real model</small></header>
<div id=messages></div><div id=notice></div><form id=compose><textarea id=text placeholder="Talk to this employee"></textarea><input id=file type=file><button>Send</button></form>
<script>
const $=id=>document.getElementById(id);let state,signature='';
function draw(){if(!state)return;const messages=state.messages.filter(m=>String(m.message_thread_id)===$('topic').value);const key=JSON.stringify(messages);if(key===signature)return;signature=key;const panel=$('messages'),stick=panel.scrollHeight-panel.scrollTop-panel.clientHeight<100;panel.replaceChildren();for(const m of messages){const a=document.createElement('article');a.textContent=(m.direction==='in'?'CEO: ':'')+(m.text||m.caption||'');const f=m.document||m.photo?.[0]||m.audio||m.voice||m.video;if(f){const link=document.createElement('a');link.textContent=' Download '+f.file_name;link.href='/files/'+f.file_id;link.download=f.file_name;a.append(link);}panel.append(a);}if(stick)panel.scrollTop=panel.scrollHeight;}
async function refresh(){try{const r=await fetch('/api',{headers:{'X-Attobot-Lab':'1'}});if(!r.ok)throw Error('HTTP '+r.status);state=await r.json();$('company').textContent=state.company;const selected=$('topic').value;for(const a of state.agents){if(!a.topic||Array.from($('topic').options).some(o=>o.value===String(a.topic)))continue;const o=document.createElement('option');o.value=a.topic;o.textContent=a.name;$('topic').append(o);}if(selected)$('topic').value=selected;draw();}catch(e){$('notice').textContent=e.message;}setTimeout(refresh,1000);}
$('topic').onchange=()=>{signature='';draw();};$('compose').onsubmit=async e=>{e.preventDefault();const data=new FormData();data.set('text',$('text').value);data.set('message_thread_id',$('topic').value);if($('file').files[0])data.set('file',$('file').files[0]);try{const r=await fetch('/api',{method:'POST',headers:{'X-Attobot-Lab':'1'},body:data});if(!r.ok)throw Error('HTTP '+r.status);$('text').value='';$('file').value='';$('notice').textContent='Queued';}catch(e){$('notice').textContent=e.message;}};refresh();
</script></html>"""

app = web.Application(client_max_size=16 * 1024 * 1024)
app.router.add_get("/", page)
app.router.add_route("*", "/api", operator)
app.router.add_route("*", "/bot{token}/{method}", bot)
app.router.add_get("/file/bot{token}/{file_id}/{name:.*}", download)
app.router.add_get("/files/{file_id}", download)
if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
