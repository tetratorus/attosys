import json
import os
import pathlib
import pwd
import secrets
import subprocess
import sys
import time
import urllib.request

import yaml

ROOT = pathlib.Path("/opt/attosys")
UNITS = pathlib.Path("/etc/systemd/system")


def run(*args):
    subprocess.run(args, check=True)


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as file:
        file.write(text)
    path.chmod(mode)


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
    write(UNITS / f"{name}.service", f"""[Unit]
After=network.target
[Service]
User={user}
ExecStart={command}
Restart=on-failure
RestartSec=5
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


def stop():
    company = yaml.safe_load((ROOT / "company.yaml").read_text())
    run("systemctl", "stop", *[f"{company['org']}-{role}.service" for role in company["agents"]])


def bootstrap():
    if os.geteuid() != 0:
        sys.exit("bootstrap must run as root inside the disposable container")
    options = json.load(sys.stdin)
    key = options["api_key"]
    if any(character in key for character in "\r\n\x00"):
        sys.exit("invalid API key")
    write(pathlib.Path("/run/attosys/llm.env"), f"ATTOBOT_API_KEY={json.dumps(key)}\nATTOTRAIN_API_KEY={json.dumps(key)}\n", 0o600)
    for user in ("_atto_mux", "_atto_proxy", "_atto_chat"):
        try:
            pwd.getpwnam(user)
        except KeyError:
            run("useradd", "--system", "--no-create-home", user)
    company_path = ROOT / "company.yaml"
    if not company_path.exists():
        company = {"org": "atto", "name": "Local Attosys", "ceo": {"name": "CEO", "telegram_user_id": 1},
                   "telegram_chat_id": "-1001", "telegram_api_base": "http://127.0.0.1:8090", "mux_url": "http://127.0.0.1:8811",
                   "provider": "openai", "model": options.get("model", "gpt-6-astra"), "proxy_url": "http://127.0.0.1:8810",
                   "llm_env_file": "/run/attosys/llm.env", "agent_config": {"provider": "openai_responses", "max_tokens": 4096, "multimodal_support": True},
                   "agents": {role: {"sudo": role == "hr", "description": description} for role, description in {
                       "hr": "Head of HR and Chief of Staff", "sysadmin": "Owns company infrastructure", "labs": "Builds capabilities", "trainer": "Investigates and improves employee behavior"}.items()}}
        write(company_path, yaml.safe_dump(company, sort_keys=False))
        write(ROOT / "secrets.yaml", yaml.safe_dump({"telegram_bot_token": secrets.token_hex(24), "api_key": ""}), 0o600)
    unit("atto-chat", f"/usr/bin/python3 {ROOT}/local/chat.py", "_atto_chat",
         f"StateDirectory=atto-chat\nLoadCredential=secrets.yaml:{ROOT}/secrets.yaml")
    wait("http://127.0.0.1:8090/")
    run("/usr/bin/python3", str(ROOT / "seed.py"))
    unit("atto-proxy", "/usr/local/bin/node /opt/llmproxy/server.js", "_atto_proxy",
         "StateDirectory=atto-proxy\nEnvironment=PORT=8810\nEnvironment=LLMPROXY_DB=/var/lib/atto-proxy/requests.db\nWorkingDirectory=/opt/llmproxy")
    wait("http://127.0.0.1:8810/health")
    unit("atto-mux", f"{ROOT}/venv/bin/python {ROOT}/mux/mux.py", "_atto_mux",
         f"StateDirectory=atto-mux\nEnvironment=MUX_DB=/var/lib/atto-mux/updates.db\nLoadCredential=secrets.yaml:{ROOT}/secrets.yaml")
    run("/usr/bin/python3", str(ROOT / "hire.py"), "hr", "sysadmin", "labs", "trainer")
    for index, role in enumerate(("hr", "sysadmin", "labs", "trainer")):
        user, port = f"atto-{role}", 9229 + index
        unit(f"atto-browser-{role}", f"/usr/bin/chromium --headless --disable-dev-shm-usage --user-data-dir=/home/{user}/browser --remote-debugging-port={port} about:blank", user)
        wait(f"http://127.0.0.1:{port}/json/version")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list") as response:
            targets = json.load(response)
        target = next(target for target in targets if target.get("type") == "page" and target.get("url") == "about:blank")
        subprocess.run(["runuser", "-u", user, "--", "/opt/attobrowser/atto", "attach", target["id"], str(port)], cwd=f"/home/{user}", check=True)
        tools = pathlib.Path(f"/home/{user}/skills/local-tools.md")
        write(tools, "Use `atto` from your home directory for raw CDP commands against your own browser. A tab is already attached. Do not run `atto start` or `atto stop` in Linux; systemd owns browser lifecycle.\n\nTraining: read `/opt/attotrain/README.md`, then use `/opt/attotrain/loop.py` and the current step's TASK.md. Inputs, raw trials, explicit judgments, and validated outputs are files, not a second conversation protocol.\n")
        account = pwd.getpwnam(user)
        os.chown(tools, account.pw_uid, account.pw_gid)
    run("systemd-run", "--unit=atto-discovery-deadline", f"--on-active={int(options.get('duration', 600))}",
        "/usr/bin/python3", str(ROOT / "local/bootstrap.py"), "stop")
    print("Company running. Chat: port 8090. Captures: port 8810. Employee run deadline is active.", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["stop"]:
        stop()
    else:
        bootstrap()
