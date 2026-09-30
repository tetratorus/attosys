import asyncio
import functools
import hmac
import json
import os
import pathlib
import sqlite3
import time
import uuid

from aiohttp import web
from markdown_it import MarkdownIt
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
markdown = MarkdownIt('commonmark', {'html': False, 'breaks': True}).enable(['table', 'strikethrough']).disable('image')
render_markdown = functools.lru_cache(maxsize=512)(markdown.render)


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
    messages = [{"direction": row[0], **json.loads(row[1])} for row in rows]
    for item in messages:
        item['html'] = render_markdown(item.get('text') or item.get('caption') or '')
    return web.json_response({"company": company["name"], "agents": [{"name": f"{company['org']}-{role}", "topic": spec.get("topic_id")} for role, spec in company["agents"].items()],
                              "messages": messages})


async def page(request):
    return web.Response(text=PAGE, content_type="text/html", headers={"Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'"})


PAGE = """<!doctype html><html><meta charset=utf-8><title>Attosys local company</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{max-width:1000px;margin:30px auto;padding:0 16px;font:15px system-ui;background:#151b20;color:#e2e9ee}header,form{display:flex;gap:12px;align-items:center}header{flex-wrap:wrap}select,button,textarea{font:inherit;padding:8px;background:#24313a;color:inherit;border:1px solid #52616b;border-radius:5px}#messages{height:65vh;overflow:auto;margin:20px 0}article{padding:12px;border-bottom:1px solid #34424b;overflow-wrap:anywhere}article p{margin:.5em 0}article pre{overflow:auto;padding:12px;background:#0d1216;border-radius:6px;white-space:pre;overflow-wrap:normal}article code{font:13px ui-monospace,monospace;background:#0d1216;padding:2px 4px;border-radius:3px}article pre code{padding:0}article blockquote{border-left:3px solid #52616b;margin:12px 0;padding-left:14px;color:#b4c4d0}article table{border-collapse:collapse;display:block;overflow:auto}article th,article td{border:1px solid #52616b;padding:6px 10px}article a{color:#8cc8ee}article h1,article h2,article h3{line-height:1.3}small{color:#93a6b4}textarea{flex:1;min-width:0;resize:vertical}#notice{min-height:24px;color:#edbd7d}#attachment{margin:8px 0;overflow-wrap:anywhere}#remove{margin-left:8px}button:disabled{opacity:.5}body.dropping{outline:2px dashed #8cc8ee;outline-offset:-6px;background:#1b2933}#hint{display:block;margin-top:8px}</style>
<header><h2 id=company>Local company</h2><select id=topic aria-label="Employee topic"></select><small>Real employees · real tools · real model</small></header>
<div id=messages role=log aria-live=polite></div><div id=notice role=status></div>
<div id=attachment hidden><span id=filename></span><button id=remove type=button aria-label="Remove attachment">Remove</button></div>
<form id=compose><textarea id=text rows=2 aria-label="Message" aria-describedby=hint placeholder="Write a message or drop a file here"></textarea><button id=send>Send</button></form>
<small id=hint>Enter to send · Shift+Enter for a new line · Drop a file anywhere to attach</small>
<script>
const $=id=>document.getElementById(id);let state,signature='',attachment=null,sending=false,dragDepth=0;
function draw(){if(!state)return;const messages=state.messages.filter(m=>String(m.message_thread_id)===$('topic').value);const key=JSON.stringify(messages);if(key===signature)return;signature=key;const panel=$('messages'),stick=panel.scrollHeight-panel.scrollTop-panel.clientHeight<100;panel.replaceChildren();for(const m of messages){const a=document.createElement('article');if(m.direction==='in'){const label=document.createElement('small');label.textContent='CEO';a.append(label);}const content=document.createElement('div');content.innerHTML=m.html||'';for(const link of content.querySelectorAll('a')){link.target='_blank';link.rel='noopener noreferrer';}a.append(content);const f=m.document||m.photo?.[0]||m.audio||m.voice||m.video;if(f){const link=document.createElement('a');link.textContent=' Download '+f.file_name;link.href='/files/'+f.file_id;link.download=f.file_name;a.append(link);}panel.append(a);}if(stick)panel.scrollTop=panel.scrollHeight;}
async function refresh(){try{const r=await fetch('/api',{headers:{'X-Attobot-Lab':'1'}});if(!r.ok)throw Error('HTTP '+r.status);state=await r.json();$('company').textContent=state.company;const selected=$('topic').value;for(const a of state.agents){if(!a.topic||Array.from($('topic').options).some(o=>o.value===String(a.topic)))continue;const o=document.createElement('option');o.value=a.topic;o.textContent=a.name;$('topic').append(o);}if(selected)$('topic').value=selected;draw();}catch(e){$('notice').textContent=e.message;}setTimeout(refresh,1000);}
function attach(file){attachment=file;$('attachment').hidden=!file;$('filename').textContent=file?file.name:'';}
function hasFiles(e){return Array.from(e.dataTransfer?.types||[]).includes('Files');}
$('remove').onclick=()=>{if(!sending)attach(null);};
document.ondragenter=e=>{if(hasFiles(e)){e.preventDefault();dragDepth++;document.body.classList.add('dropping');}};
document.ondragover=e=>{if(hasFiles(e)){e.preventDefault();e.dataTransfer.dropEffect=sending?'none':'copy';}};
document.ondragleave=e=>{if(hasFiles(e)&&--dragDepth<=0){dragDepth=0;document.body.classList.remove('dropping');}};
document.ondrop=e=>{if(!hasFiles(e))return;e.preventDefault();dragDepth=0;document.body.classList.remove('dropping');if(sending)return;const files=e.dataTransfer.files;if(files.length!==1){$('notice').textContent='Attach one file at a time.';return;}attach(files[0]);$('notice').textContent='File attached. Press Enter to send.';$('text').focus();};
$('text').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();$('compose').requestSubmit();}};
$('topic').onchange=()=>{signature='';draw();};
$('compose').onsubmit=async e=>{
  e.preventDefault();if(sending||(!$('text').value.trim()&&!attachment))return;
  if(!$('topic').value){$('notice').textContent='Choose an employee first.';return;}
  const data=new FormData();data.set('text',$('text').value);data.set('message_thread_id',$('topic').value);if(attachment)data.set('file',attachment);
  sending=true;for(const id of ['text','topic','send','remove'])$(id).disabled=true;$('notice').textContent='Sending…';
  try{const r=await fetch('/api',{method:'POST',headers:{'X-Attobot-Lab':'1'},body:data});if(!r.ok)throw Error('HTTP '+r.status);$('text').value='';attach(null);$('notice').textContent='Queued';}
  catch(e){$('notice').textContent='Send failed: '+e.message;}
  finally{sending=false;for(const id of ['text','topic','send','remove'])$(id).disabled=false;$('text').focus();}
};refresh();
</script></html>"""

app = web.Application(client_max_size=16 * 1024 * 1024)
app.router.add_get("/", page)
app.router.add_route("*", "/api", operator)
app.router.add_route("*", "/bot{token}/{method}", bot)
app.router.add_get("/file/bot{token}/{file_id}/{name:.*}", download)
app.router.add_get("/files/{file_id}", download)
if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
