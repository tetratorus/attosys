import argparse
import getpass
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCES = {
    "attobot": ["agent.py", "SOUL.md", "opt", "requirements.txt", "lab-constraints.txt"],
    "attosys": ["hire.py", "seed.py", "mux", "templates", "local/chat.py", "local/bootstrap.py"],
    "attotrain": ["*.py", "README.md", "steps", "tools", "tests"],
    "attobrowser": ["atto", "lib", "package.json", "package-lock.json"],
    "llmproxy": ["server.js", "stats-handler.js", "index.html", "package.json", "package-lock.json"],
}


def main():
    parser = argparse.ArgumentParser(description="Boot a disposable company from sibling working checkouts. No host directories are mounted.")
    parser.add_argument("--source-root", type=pathlib.Path, default=ROOT.parent)
    parser.add_argument("--name", default="attosys-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--dns", default=None)
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()
    runtime = shutil.which("container") or "/opt/homebrew/bin/container"
    dns = ["--dns", args.dns] if args.dns else []
    if not args.no_build:
        with tempfile.TemporaryDirectory(prefix=".local-build-", dir=ROOT) as directory:
            context = pathlib.Path(directory).resolve()
            for repo, patterns in SOURCES.items():
                source_root = args.source_root / repo
                if not source_root.is_dir():
                    parser.error(f"missing sibling checkout: {source_root}")
                for pattern in patterns:
                    for source in source_root.glob(pattern):
                        target = context / repo / source.relative_to(source_root)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if source.is_dir():
                            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                        else:
                            shutil.copy2(source, target)
            shutil.copy2(ROOT / "local/Containerfile", context / "Containerfile")
            if not (context / "attobot/lab-constraints.txt").is_file():
                parser.error("build context is missing attobot dependency constraints")
            print(f"Building {sum(path.is_file() for path in context.rglob('*'))} source files", flush=True)
            subprocess.run([runtime, "build", *dns, "--file", "Containerfile", "--tag", "attosys-local", "--progress", "plain", "."], cwd=context, check=True)
    if args.build_only:
        return
    key = os.environ.get("ATTOBOT_API_KEY") or getpass.getpass("OpenAI API key (stdin only; kept in VM tmpfs): ")
    if not key or args.duration < 1:
        parser.error("an API key and positive duration are required")
    subprocess.run([runtime, "run", "--detach", "--name", args.name, *dns, "--cpus", "4", "--memory", "4G", "--cap-add", "SYS_ADMIN",
                    "--tmpfs", "/run", "--tmpfs", "/tmp", "--publish", "127.0.0.1:8090:8090", "--publish", "127.0.0.1:8810:8810", "attosys-local"], check=True)
    try:
        for _ in range(60):
            status = subprocess.run([runtime, "exec", args.name, "systemctl", "is-system-running"], capture_output=True, text=True, timeout=5)
            if status.stdout.strip() in ("running", "degraded"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("systemd did not become ready")
        subprocess.run([runtime, "exec", "--interactive", args.name, "python3", "/opt/attosys/local/bootstrap.py"],
                       input=json.dumps({"api_key": key, "model": args.model, "duration": args.duration}), text=True, check=True, timeout=180)
    except BaseException:
        subprocess.run([runtime, "stop", args.name], check=False)
        raise
    print(f"Container: {args.name}")
    print("Chat: http://127.0.0.1:8090")
    print("Captured model requests: http://127.0.0.1:8810")
    print(f"Inspect: {runtime} exec {args.name} systemctl --failed")
    print(f"Stop: {runtime} stop {args.name}")
    print("Employee services stop at the discovery deadline; container state is retained.")


if __name__ == "__main__":
    main()
