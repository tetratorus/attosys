# attosys

Turn a bare Ubuntu host into an autonomous, agent-only company: a small fleet
of specialist agents, each a Unix user with its own systemd service and its own
Telegram forum topic, governed by an org chart and an employee handbook. The
only human is the CEO — you. The company is built to grow itself: HR hires new
roles as the work demands them.

One agent = one Unix user = one harness process = one forum topic. Agents talk
to you in their topic and to each other by dropping files in each other's mail
inboxes. All LLM traffic flows through a local proxy that logs every request
per agent.

## The genesis fleet

`setup.sh` hires four structural roles — the minimum a self-growing company
needs. Everything else is hired later, by HR, once the company knows what it
does.

| role | what |
|---|---|
| `hr` | Head of HR & Chief of Staff. Headcount, performance, provisioning. The only agent with sudo. |
| `sysadmin` | Owns the substrate: the mux, the proxy, systemd units, the harness, provisioning. |
| `labs` | Builder. Investigates tools and prototypes capabilities the company doesn't yet have. |
| `trainer` | Coaches agents on company principles; audits, training cases. |

## Local company: start, stop and transfer

For a new company, keep `attosys`, `attobot`, `attotrain`, `attobrowser`, and
`llmproxy` as sibling working checkouts. With Apple container on an Apple Silicon
Mac, or rootful Docker on a dedicated Linux host/VM:

```sh
python3 local/up.py start
python3 local/up.py stop
python3 local/up.py save company-snapshot
```

On the receiving host, where that company does not already exist:

```sh
python3 local/up.py restore company-snapshot
python3 local/up.py start
```

The default company name is `atto-company`. `start` builds and provisions only
when that company does not exist; subsequent starts reuse its filesystem and
preserve employee configurations and knowledge. `--idle` starts infrastructure
without employees or an API key. `--build-only` builds an image without starting a
company; `--no-build` uses an already-built image for a new company. `--name` can
select another saved company, but fixed host ports permit only one published
company to run at a time. Legacy discovery containers without maintenance-mode
startup are refused rather than silently modified or deleted.

The guest is Linux with systemd, Unix employee accounts, subconscious processes,
Chromium, a Telegram-compatible local chat transport, the mux, and the LLM proxy.
Model calls are real: the default is `gpt-6-astra` through Responses. Supply
`ATTOBOT_API_KEY` through the environment or the hidden prompt; the launcher sends
it over stdin and keeps it in VM/container tmpfs. A normal stop clears that key.
Infrastructure starts first; employees start only after credentials and browser
connections are ready. Each employee run gets a fresh deadline, defaulting to
900 seconds (`--duration` changes it). The deadline stops company-prefixed worker
services, including new hires, while leaving infrastructure available to inspect.
Unmanaged services need their own deadlines. `stop` shuts down the whole container.

Chat and captured model traffic use host loopback:

```text
http://127.0.0.1:8090
http://127.0.0.1:8810
```

### Matched OS and company-data snapshots

`save` stops the company, boots its existing filesystem in maintenance mode for
export, and leaves the original container stopped and intact. It creates a
private directory containing:

| File | Contents |
|---|---|
| `os.tar` | Linux filesystem, application code, installed tools and dependencies, accounts, system configuration, and files outside the data roots. Not just the original base image. |
| `data.tar` | Employee homes/workspaces (including installed workspace dependencies), company configuration/handbook/shared work, chat, mux queue, and proxy databases. |
| `manifest.json` | Format version, CPU architecture, and checksums binding the two snapshots together. |

Both snapshots are required and restored together. Transfer the entire directory.
On the receiving machine, use this launcher's `local/up.py` and `local/snapshot.py`
(or an attosys checkout) to run `restore`, then `start`. The other sibling source
checkouts are not needed: the OS snapshot contains the actual saved applications.
Restore builds a local image from the saved OS files, restores the data, verifies
it in maintenance mode, and leaves the company stopped until explicitly started.
Existing containers and snapshot destinations are never overwritten. An interrupted
restore remains marked incomplete and cannot be started or saved. Keep the original
snapshot and retry `restore` with a new `--name`; the failed container is retained
for inspection rather than deleted automatically.

Both hosts must support the same Linux guest CPU architecture. This is not an
ARM-to-x86 converter or a migration to a different guest OS. Tools installed by
employees remain present, including native packages and workspace environments.
Browser profiles, `/run`, `/tmp`, and launcher-managed credentials are excluded;
browser connections and local chat credentials are recreated. Unfinished files,
pending mux deliveries, and queued trigger notifications persist, but in-flight
processes do not. There is no exactly-once guarantee for external tool actions.

Snapshots are private, trusted company backups, not automatically sanitized
public exports. Agents may have stored sensitive information elsewhere in their
workspaces or OS files. Additional external mounts are refused because they would
otherwise be missing from the snapshots.

### Runtime and verification

Runtime selection defaults to Apple container on Apple Silicon when available,
and Docker otherwise. Select it explicitly with `--runtime container` or
`--runtime docker`. Apple container mounts no host directories. The Docker backend
uses `SYS_ADMIN`, the host cgroup namespace, and a writable cgroup mount for
systemd; use a dedicated, trusted Linux VM, not a shared multi-tenant host.
Docker/Linux execution must be validated on the receiving setup; the local
verification environment uses Apple container.

If DNS resolution fails, pass `--dns` with a resolver permitted by your network.
This does not change host/VPN configuration. For Docker it configures the company
container, not the Docker build daemon's resolver.

```sh
python3 -m unittest discover -s local -p 'test_[rs]*.py' -v
python3 local/up.py start --build-only --runtime container
ATTOSYS_CONTAINER_TESTS=1 python3 -m unittest discover -s local -p test_lifecycle.py -v
```

The lifecycle test rejects stale images by checking packaged source hashes against
the current sibling checkouts. It uses real isolated containers, installs a native
tool, exercises stop/restart, interrupted restore, and paired restore, and leaves
test containers stopped. It performs a bounded live-model continuation check only
when `ATTOBOT_API_KEY` is supplied.
The lifecycle test also runs the mux HTTP regression inside the image, exercising
its actual aiohttp version. Running `local/test_persistence.py` separately needs
`aiohttp`, `requests`, and PyYAML.

OS export reads the quiescent guest filesystem rather than relying on Apple
container's native filesystem exporter. Transfers use process streams because
Apple container's copy operation does not see guest tmpfs mounts.

Training stays separate: export a captured decision with
`/opt/attotrain/tools/extract_capture.py`, then follow attotrain's README. Preserve
raw evidence and review real repeated trials before deploying a memory; a working
company boot is not proof of self-improvement.

## Setup

You need: an Ubuntu host (22.04+) with root, a Telegram account, and an LLM API
key (any OpenAI-compatible provider).

```bash
sudo git clone https://github.com/tetratorus/attosys /opt/attosys && cd /opt/attosys
sudo ./setup.sh
```

`setup.sh` is guided and idempotent. It will:

1. Install dependencies and build the harness venv.
2. Walk you through configuration — org slug, your name + Telegram id, LLM
   provider/key, and one bot token — with the exact pages to get each value.
3. **The one manual Telegram step** (a bot cannot create a group or list its
   groups): make a supergroup with Topics enabled, and add your bot as admin.
   attosys does everything else — finds the group, creates one topic per agent.
4. Start the mux and the proxy, then hire the fleet.

Each agent boots, reads the handbook, and checks in on its topic. Logs:
`journalctl -u <org>-hr -f`.

### Why one bot

The whole company runs on a **single** Telegram bot. A local **mux** holds the
token, runs the one `getUpdates` loop, and demultiplexes by forum topic so each
agent sees only its own. Outbound messages are tagged with the sending agent's
name. This means you never hand attosys your Telegram *account* — just one bot
token — and there are no per-agent bots to mint or manage.

## How it hangs together

```
/opt/attosys/                  # repo + generated state
  harness/                     # the agent harness (pulled from upstream)
  mux/mux.py                   # one bot -> N agents, demuxed by topic
  proxy/                       # llm proxy: per-agent request logging
  venv/                        # harness python venv
  company.yaml                 # org chart — world-readable, agents consult it
  secrets.yaml                 # bot token + API key — root only, agents never read it
  handbook.md                  # rendered from templates/ on first hire
  shared/                      # group-writable company workspace
  templates/handbook.md        # }
  templates/souls/<role>.md    # } prose templates ({{COMPANY}}, {{company}}, {{CEO}}, {{ROOT}}, {{AGENT}})
/home/<org>-<role>/            # one unix user per agent, home = its workspace
  agent/                       # harness state: SOUL.md, messages.jsonl, LIFE.md,
                               #   MEMORY.md + memory/, triggers/, mail_inbox/
  subconscious/                # sibling watcher agent (reviews the primary's stream)
```

- **Access control is plain Unix.** All agents share one group. Homes are
  `2710` (traversable, not listable), `agent/` dirs `2750` (streams are
  auditable by the fleet), `config.json` `600`, `mail_inbox/` `2770` (anyone can
  drop; sender identity comes from the file's uid — kernel-enforced).
- **Agent-to-agent mail** is the harness's native `mail_inbox/`: drop a markdown
  file in `/home/<agent>/agent/mail_inbox/`; their harness notifies them in
  their topic.
- **Telegram** goes through the mux: each agent's harness points at
  `mux_url` instead of `api.telegram.org`, using its own name as the routing
  key. One topic = one agent.
- **LLM calls** go through the proxy at `/<agent>/<provider>/v1`, which logs
  each request per agent and forwards to the provider.
- **The handbook** (`templates/handbook.md`) is read by every agent on boot and
  is the company's source of truth. The souls and handbook prose shape behavior
  — when adapting them, change names and paths, not phrasing.
- **Growing the company**: add a role to `company.yaml`, run
  `sudo ./seed.py` (creates its topic) and `sudo ./hire.py <role>`. Clone an
  existing role with `labs-2: {soul: labs, ...}`.
- **Firing** is deliberately manual: collect a handover,
  `systemctl disable --now <org>-<role>`, archive `/home/<org>-<role>`, remove
  the unit, the sudoers file if any, and the org-chart entry.

## Uninstall

Remove everything `setup.sh` + `hire.py` created on the host — systemd units,
agent unix users + homes, the shared group, sudoers files, generated state
(`venv/`, `harness/`, `proxy/`, `shared/`, `handbook.md`, `company.yaml`,
`secrets.yaml`), durable topic-id state (`/var/lib/attosys/<org>.yaml`), and
the forum topics it created in Telegram. Idempotent; reads `company.yaml`
for the org + agents, or infers them from installed units if the config is
already gone.

```bash
sudo ./uninstall.sh             # true clean slate: runtime + state + topics gone
sudo ./uninstall.sh --keep-state  # keep topic-id state AND the forum topics (reinstall reuses them)
sudo ./uninstall.sh --purge     # also delete /opt/attosys itself
sudo ./uninstall.sh -y          # skip the confirmation prompt
```

By default the durable topic-id state is dropped AND the forum topics are
deleted from Telegram (using the bot token + the recorded topic_id map, both
read before deletion). Pass `--keep-state` only if you're reinstalling on the
same supergroup and want to reuse the existing topics.

The one thing it cannot remove is the Telegram side (forum topics in your
supergroup) — a bot can't bulk-delete topics. Delete them by hand in Telegram,
or just leave the supergroup.
