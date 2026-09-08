import json
import os
import pathlib
import pwd
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.request

import yaml
import telegram
from snapshot import MAINTENANCE_TARGET, RESTORE_PENDING

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from services import employee_service

ROOT = pathlib.Path("/opt/attosys")
UNITS = pathlib.Path("/etc/systemd/system")
READY = pathlib.Path("/run/attosys/ready")
ENV_FILE = pathlib.Path("/run/attosys/llm.env")
STATE = ROOT / "local-state.json"
DATABASES = ("/var/lib/atto-chat/chat.sqlite3", "/var/lib/atto-mux/updates.db", "/var/lib/atto-proxy/requests.db")


def run(*args):
    subprocess.run(args, check=True)


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8) + ".tmp")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "w") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    temporary.chmod(mode)
    temporary.replace(path)


def roster(company):
    names = {role: f"{company['org']}-{role}" for role in company['agents']}
    if not names or any(not re.fullmatch(r"[a-z][a-zA-Z0-9_-]{0,31}", name) for name in names.values()):
        raise ValueError("invalid company roster")
    return names


def unit(name, command, user, extra=""):
    account = pwd.getpwnam(user)
    settings = []
    for line in extra.splitlines():
        if line.startswith("LoadCredential="):
            credential, source = line.removeprefix("LoadCredential=").split(":", 1)
            directory = pathlib.Path("/run/attosys/credentials") / user
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chown(directory, account.pw_uid, account.pw_gid)
            write(directory / credential, pathlib.Path(source).read_text(), 0o400)
            os.chown(directory / credential, account.pw_uid, account.pw_gid)
            settings.append(f"Environment=CREDENTIALS_DIRECTORY={directory}")
        else:
            settings.append(line)
    extra = "\n".join(settings)
    path = UNITS / f"{name}.service"
    if not path.exists():
        write(path, f"""[Unit]
After=network.target
[Service]
User={user}
ExecStart={command}
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
UMask=0027
NoNewPrivileges=yes
{extra}
[Install]
WantedBy=multi-user.target
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "start", name)


def wait(url):
    for _ in range(60):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(1)
    raise RuntimeError(f"service did not become ready: {url}")


def owned_units(company):
    result = subprocess.run(["systemctl", "list-units", "--all", "--plain", "--no-legend", "--type=service,timer",
                             "atto-*", f"{company['org']}-*"], check=True, capture_output=True, text=True)
    return sorted({line.split()[0] for line in result.stdout.splitlines() if line.strip()})


def halt(names):
    if names:
        result = subprocess.run(["systemctl", "stop", *sorted(names)], capture_output=True, text=True)
        if result.returncode not in (0, 5):
            raise RuntimeError(result.stderr.strip() or 'service shutdown failed')


def check_databases(checkpoint=False):
    for filename in DATABASES:
        path = pathlib.Path(filename)
        if not path.exists():
            continue
        db = sqlite3.connect(path, timeout=5)
        try:
            if db.execute("PRAGMA quick_check").fetchall() != [('ok',)]:
                raise ValueError(f"database integrity check failed: {path}")
            if checkpoint and db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise ValueError(f"database still has writers: {path}")
        finally:
            db.close()


def stop(full=False):
    READY.unlink(missing_ok=True)
    company_path = ROOT / "company.yaml"
    if not company_path.exists():
        return
    company = yaml.safe_load(company_path.read_text())
    names = owned_units(company)
    halt([name for name in names if name.endswith('.timer')])
    infrastructure = {"atto-chat.service", "atto-mux.service", "atto-proxy.service"}
    browsers = {name for name in names if name.startswith("atto-browser-")}
    workers = {name for name in names if name.endswith('.service')} - infrastructure - browsers
    if not full:
        workers = {name for name in workers if not name.startswith('atto-discovery-deadline')}
    halt(workers)
    if full:
        halt(browsers)
        halt(set(names) & {"atto-mux.service"})
        halt(set(names) & {"atto-chat.service", "atto-proxy.service"})
        run('systemctl', 'isolate', MAINTENANCE_TARGET)
        check_databases(checkpoint=True)
        run("sync")


def prepare():
    company = yaml.safe_load((ROOT / 'company.yaml').read_text())
    for role, user in roster(company).items():
        if not (pathlib.Path('/home') / user / 'agent/config.json').exists():
            continue
        service = UNITS / f'{user}.service'
        if not service.exists():
            write(service, employee_service(ROOT, company, user))
        write(UNITS / f'{user}.service.d/local-ready.conf', f'[Unit]\nConditionPathExists={READY}\n')
        sudoers = pathlib.Path('/etc/sudoers.d') / user
        if company['agents'][role].get('sudo') and not sudoers.exists():
            write(sudoers, f'{user} ALL=(ALL) NOPASSWD:ALL\n', 0o440)
    run('systemctl', 'daemon-reload')


def check():
    state = json.loads(STATE.read_text())
    if state.get('version') != 1:
        raise ValueError("unsupported saved company version")
    company = yaml.safe_load((ROOT / "company.yaml").read_text())
    for user in roster(company).values():
        pwd.getpwnam(user)
        for relative in ("agent/config.json", "agent/SOUL.md", "subconscious/config.json"):
            if not (pathlib.Path('/home') / user / relative).is_file():
                raise ValueError(f"missing employee state: {user}/{relative}")
        if not (UNITS / f"{user}.service").is_file():
            raise ValueError(f"missing employee service: {user}")
    check_databases()
    print(json.dumps({'version': 1, 'employees': list(roster(company).values())}), flush=True)


def bootstrap(options):
    if pathlib.Path(RESTORE_PENDING).exists():
        raise ValueError('restore is incomplete; retry the original snapshot with a new --name')
    key = options.get("api_key") or ""
    workers = options.get("workers", True)
    duration = int(options.get("duration", 900))
    if duration < 1 or any(character in key for character in "\r\n\x00"):
        raise ValueError("invalid key or run duration")
    if workers and not key and not ENV_FILE.is_file():
        raise ValueError("an API key is required to start employees")
    if READY.exists():
        print("Company already running.", flush=True)
        return
    check_databases()
    if key:
        write(ENV_FILE, f"ATTOBOT_API_KEY={json.dumps(key)}\nATTOTRAIN_API_KEY={json.dumps(key)}\n", 0o600)
    for user in ("_atto_mux", "_atto_proxy", "_atto_chat"):
        try:
            pwd.getpwnam(user)
        except KeyError:
            run("useradd", "--system", "--no-create-home", user)
    company_path = ROOT / "company.yaml"
    existing = company_path.exists()
    if existing:
        company = yaml.safe_load(company_path.read_text())
    else:
        company = {"org": "atto", "name": "Local Attosys", "ceo": {"name": "CEO", "telegram_user_id": 1},
                   "mux_url": "http://127.0.0.1:8811",
                   "provider": "openai", "model": options.get("model", "gpt-6-astra"), "proxy_url": "http://127.0.0.1:8810",
                   "llm_env_file": str(ENV_FILE), "agent_config": {"provider": "openai_responses", "max_tokens": 4096, "multimodal_support": True},
                   "agents": {role: {"sudo": role == "hr", "description": description} for role, description in {
                       "hr": "Head of HR and Chief of Staff", "sysadmin": "Owns company infrastructure", "labs": "Builds capabilities", "trainer": "Investigates and improves employee behavior"}.items()}}
    telegram.configure(ROOT, company, options, write, existing=existing)
    names = roster(company)
    prepare()
    if company['telegram_api_base'] == telegram.LOCAL_API:
        unit("atto-chat", f"/usr/bin/python3 {ROOT}/local/chat.py", "_atto_chat",
             f"StateDirectory=atto-chat\nLoadCredential=secrets.yaml:{ROOT}/secrets.yaml")
        wait("http://127.0.0.1:8090/")
        if any(spec.get('topic_id') is None for spec in company['agents'].values()):
            run("/usr/bin/python3", str(ROOT / "seed.py"))
    unit("atto-proxy", "/usr/local/bin/node /opt/llmproxy/server.js", "_atto_proxy",
         "StateDirectory=atto-proxy\nEnvironment=PORT=8810\nEnvironment=LLMPROXY_DB=/var/lib/atto-proxy/requests.db\nWorkingDirectory=/opt/llmproxy")
    wait("http://127.0.0.1:8810/health")
    if options.get('telegram_bot_token'):
        halt(['atto-mux.service'])
    unit("atto-mux", f"{ROOT}/venv/bin/python {ROOT}/mux/mux.py", "_atto_mux",
         f"StateDirectory=atto-mux\nEnvironment=MUX_DB=/var/lib/atto-mux/updates.db\nLoadCredential=secrets.yaml:{ROOT}/secrets.yaml")
    wait(f"http://127.0.0.1:8811/bot{next(iter(names.values()))}/getMe")
    for role, user in names.items():
        write(UNITS / f"{user}.service.d/local-ready.conf", f"[Unit]\nConditionPathExists={READY}\n")
        home = pathlib.Path('/home') / user
        if not (home / 'agent/config.json').exists():
            run("/usr/bin/python3", str(ROOT / "hire.py"), "--no-start", role)
        else:
            pwd.getpwnam(user)
            if not (home / 'subconscious/config.json').is_file() or not (UNITS / f'{user}.service').is_file():
                raise ValueError(f"incomplete existing employee state: {user}")
    state = json.loads(STATE.read_text()) if STATE.exists() else {'version': 1, 'browser_ports': {}}
    if state.get('version') != 1:
        raise ValueError("unsupported company state version")
    ports = state['browser_ports']
    for role, user in names.items():
        if user not in ports:
            ports[user] = max([9228, *ports.values()]) + 1
            write(STATE, json.dumps(state, indent=2) + '\n')
        port = ports[user]
        unit(f"atto-browser-{role}", f"/usr/bin/chromium --headless --disable-dev-shm-usage --user-data-dir=/home/{user}/browser --remote-debugging-port={port} about:blank", user)
        wait(f"http://127.0.0.1:{port}/json/version")
        browser = ["runuser", "-u", user, "--", "/opt/attobrowser/atto"]
        selected = json.loads(subprocess.run([*browser, "state"], cwd=f"/home/{user}", check=True, capture_output=True, text=True).stdout)
        if selected.get('status') != 'available' or selected.get('port') != port:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
            with urllib.request.urlopen(request) as response:
                target = json.load(response)
            subprocess.run([*browser, "attach", target["id"], str(port)], cwd=f"/home/{user}", check=True)
        tools = pathlib.Path(f"/home/{user}/skills/local-tools.md")
        if not tools.exists():
            write(tools, "Use `atto` from your home directory for raw CDP commands against your own browser. A tab is already attached. Do not run `atto start` or `atto stop` in Linux; systemd owns browser lifecycle.\n\nTraining: read `/opt/attotrain/README.md`, then use `/opt/attotrain/loop.py` and the current step's TASK.md. Inputs, raw trials, explicit judgments, and validated outputs are files, not a second conversation protocol.\n")
            account = pwd.getpwnam(user)
            os.chown(tools, account.pw_uid, account.pw_gid)
    write(STATE, json.dumps(state, indent=2) + '\n')
    check()
    if workers:
        subprocess.run(["systemctl", "stop", "atto-discovery-deadline.timer", "atto-discovery-deadline.service"], capture_output=True)
        subprocess.run(["systemctl", "reset-failed", "atto-discovery-deadline.service"], capture_output=True)
        write(READY, "ready\n", 0o600)
        run("systemd-run", "--unit=atto-discovery-deadline", "--timer-property=AccuracySec=1s", f"--on-active={duration}",
            "/usr/bin/python3", str(ROOT / "local/bootstrap.py"), "stop")
        run("systemctl", "daemon-reload")
        run("systemctl", "reset-failed", *[f"{user}.service" for user in names.values()])
        run("systemctl", "start", "multi-user.target", *[f"{user}.service" for user in names.values()])
    print("Company running." if workers else "Company infrastructure ready; employees remain stopped.", flush=True)


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("bootstrap must run as root inside the disposable container")
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    if command == "version":
        print(1)
    elif command in ("stop", "pause"):
        stop(full=command == "pause")
    elif command in ("check", "prepare"):
        if command == "prepare":
            prepare()
        check()
    elif command == "start":
        try:
            bootstrap(json.load(sys.stdin))
        except BaseException:
            READY.unlink(missing_ok=True)
            stop()
            raise
    else:
        sys.exit("expected start, stop, pause, check or version")
