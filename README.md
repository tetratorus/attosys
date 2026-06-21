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

`setup.sh --clean` deletes the recorded set of forum topics and re-creates
them, then re-hires the fleet against the new topic ids. Use it when the
Telegram side has gotten messy (stray topics, manual edits). The mux and
proxy keep running throughout.

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
agent unix users + homes, the shared group, sudoers files, and generated state
(`venv/`, `harness/`, `proxy/`, `shared/`, `handbook.md`, `company.yaml`,
`secrets.yaml`). Idempotent; reads `company.yaml` for the org + agents, or
infers them from installed units if the config is already gone.

```bash
sudo ./uninstall.sh             # remove the runtime; keep the repo
sudo ./uninstall.sh --purge     # also delete /opt/attosys itself
sudo ./uninstall.sh -y          # skip the confirmation prompt
```

The one thing it cannot remove is the Telegram side (forum topics in your
supergroup) — a bot can't bulk-delete topics. Delete them by hand in Telegram,
or just leave the supergroup.
