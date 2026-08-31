# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small Flask dashboard (`app.py`) for remotely monitoring and controlling systemd services on a
fleet of Raspberry Pis (trading bot hosts, research nodes, etc.) over SSH via Tailscale. See
`README.md` for the full user-facing description, layout, config schema, and deployment notes —
this file is the AI-assistant-focused companion: where the real logic lives, what's dead code, and
what to be careful with.

## The one file that matters: `app.py`

Everything live runs out of this single ~1,380-line file. It is self-contained: HTML is rendered
from an inline `HTML_TEMPLATE` string via `render_template_string` (not `templates/`), CSS/JS are
inlined in that same string (not `static/`), and there is no other module it imports from this repo.

Rough map (line numbers as of this writing — re-check if the file has grown):

- **1–52**: imports (Flask, `paramiko`, `markdown`, stdlib), logging setup (`logs/admin-console.log`,
  rotating), constants: `SLUG_RE = r'^[A-Za-z0-9_-]+$'`, `DOC_FILENAME_RE = r'^[A-Za-z0-9._-]+$'`,
  `TAIL_LOG_LINES_MAX`, `JOURNALCTL_LINES` (must match the sudoers grant — see below), `CONFIG_PATH`
  (from `ADMIN_CONSOLE_CONFIG` env var, defaults to `projects.json` next to `app.py`).
- **55–91**: config persistence — `load_config()` re-reads `projects.json` on every call (no cache;
  the server is threaded), `save_config()` writes atomically (`tempfile.mkstemp` + `os.replace`),
  `slugify()` generates dedupe-safe ids for new projects/repos.
- **93–118**: `execute_ssh_cmd(host, user, command, key_path=None, timeout=8)` — the *only* SSH
  mechanism the live app uses. Raw `paramiko.SSHClient()` with `AutoAddPolicy()` (trust-on-first-use,
  **no host-key verification**), returns `(success, output)`, never raises.
- **121–318**: the `PLATFORMS` adapter — `PLATFORMS["linux"]` / `PLATFORMS["windows"]`, each
  supplying `resolve_path`, `git_sync_cmd`, `service_cmd`, `reboot_cmd`, `log_cmd_service`,
  `list_docs_cmd`, `read_doc_cmd`, `tools` (remote-control launchers: `code-server` on both
  platforms, `claude` — tmux + `claude --remote-control` — Linux-only). Windows adapter is marked
  **UNVERIFIED** in its own comments; there's no Windows host to test against.
- **321–401**: service/app discovery. `SERVICES_BLOCK_RE` finds fenced ` ```services ` JSON blocks
  in each repo's `.md` docs; `normalize_service_entries()` validates every `id` against `SLUG_RE`
  (required — ids get interpolated into shell commands); `discover_project_services(project)` does
  the actual SSH-and-scan work and is called fresh on every relevant request (not cached — expect a
  burst of SSH round-trips per Services/Apps panel load).
- **404–1027**: `HTML_TEMPLATE`, the whole UI (Jinja + CSS + inline `<script>`).
- **1030–1368**: routes. All JSON-in/JSON-out (`jsonify({"success": bool, "message": str})`), no
  auth, no CSRF, no sessions — access control is "you're on the Tailscale network," per the README.
  Key ones: `POST /api/service` (start/stop/restart, re-validates `service_id` against a fresh
  `discover_project_services()` call before building the command), `POST /api/reboot`,
  `POST /api/remote-control`, `GET/POST /api/projects*` (project/repo CRUD), `GET /api/docs/...`.
- **1370–1377**: `app.run(host='0.0.0.0', port=8080, threaded=True)`.

## Dead code — do not extend

`config.py`, `config.yaml`, `ssh_client.py`, `http_monitor.py`, `templates/`, `static/` are an
abandoned parallel rewrite with a **different data model** (`config.yaml`'s `hosts`/`services` with
`.service`-suffixed unit names, vs. the live `projects.json`'s `projects`/`repos` with bare unit
names) and a different frontend (`templates/index.html` + `static/dashboard.js`, never served —
`app.py` never calls `render_template` or references `static/`). Confirmed via grep: `app.py`
imports none of these modules. New features belong in `app.py`, against the live `projects.json`
schema. Don't casually "clean up" by importing `ssh_client.py`'s stricter `RejectPolicy()` host-key
check into `app.py` without calling out the behavior change — see Security below.

## Security-sensitive patterns to preserve

- **No auth on any route.** Every `/api/*` endpoint — including reboot and service control — is
  reachable by anyone who can hit port 8080. This is intentional (Tailscale-network trust model per
  README), but don't "helpfully" widen exposure (binding beyond `0.0.0.0` inside the tailnet is
  already as open as it gets; never suggest port-forwarding, ngrok, or a public listener) without
  flagging that there's zero authentication.
- **Host-key verification is off** (`AutoAddPolicy` in `execute_ssh_cmd`). This is a known,
  already-made tradeoff, not an oversight to silently "fix" — changing it affects every SSH call the
  app makes and needs a deliberate decision, not a drive-by fix.
- **Commands are built with string interpolation, not argument lists.** Safety instead comes from
  validating inputs *before* they're interpolated:
  - Service ids: `SLUG_RE` in `normalize_service_entries`, re-checked against
    `discover_project_services()`'s freshly-discovered ids inside the `/api/service` handler before
    reaching `sudo systemctl {action} {unit}`.
  - Doc filenames: `DOC_FILENAME_RE`, checked in `read_doc()` and `discover_project_services()`
    before hitting `cat`/`Get-Content`.
  - `local_path` / `git_repo` on a project's repos are **trusted, admin-authored config** (only
    settable via the Add Project/Add Repo UI → `projects.json`) — the path-resolution helpers say so
    explicitly in their docstrings. If a future change ever lets either field flow from
    unvalidated request data into the SSH command builders, that trust boundary breaks.
  - `action` values (`start`/`stop`/`restart`) are always membership-checked before dispatch.
- If you add a new route that reaches `execute_ssh_cmd`, validate every interpolated value the same
  way — a regex allowlist checked *before* the value is placed into the command string, not after.

## Config, not code, for fleet changes

Adding/removing a project, repo, or SSH target is a `projects.json` change made through the
dashboard's own Add Project / Add Repo UI (or by hand-editing `projects.json`, which is gitignored —
copy `projects.json.example` to start). There's no in-place edit endpoint: changing a project or
repo means delete-and-re-add. Services and apps are never stored in `projects.json` at all — they're
declared by adding a fenced ` ```services ` JSON block to a `.md` doc in the *target repo*, not this
one (see README's "Services & Apps" section for the exact format).

## Testing

There's no unit-test suite and nothing here runs offline. `test_console.py` is a standalone
health-check (own `paramiko`/`requests` calls, no imports from `app.py`/`ssh_client.py`) that needs
one of: a live, reachable SSH host; the target's HTTP monitoring API; or a locally-running `app.py`.
Its check list (`SERVICES`, `DASHBOARD_TARGET_KEY = "Adidas"`) is hardcoded to the author's real
fleet — treat it as a template for exercising *your* infra, not a generic test you can run as-is
against a fresh checkout. `--exercise-restart` has a real side effect (restarts `tradingbot-api` via
the live dashboard API); don't pass it unless you mean to.

If you add the first real unit tests, prefer testing `app.py`'s pure logic (`slugify`,
`parse_services_block`, `normalize_service_entries`, `SLUG_RE`/`DOC_FILENAME_RE` validation) with
mocked `paramiko` — the existing code has no test scaffolding to extend, you'd be starting fresh.

## Local dev

```bash
pip install -r requirements.txt
cp projects.json.example projects.json   # then edit, or use the Add Project button after starting
python app.py                            # serves http://0.0.0.0:8080/
```

`requirements.txt` pins `Flask`, `paramiko`, `requests`, `PyYAML`, `Markdown` — but `app.py` itself
only needs `Flask`, `paramiko`, and `Markdown`; `requests`/`PyYAML` are pulled in for the dead
`config.py`/`http_monitor.py`/`test_console.py` code paths.

No Python version is pinned anywhere in the repo (no `pyproject.toml`, `Pipfile`, or
`.python-version`) — anything satisfying Flask 3.x/paramiko 3.x works in practice.

## `.claude/settings.local.json` is stale

The checked-in Bash allowlist in `.claude/settings.local.json` was built against the **dead**
`config.py`/`config.yaml` code path (it imports `from config import load_config`, sets
`ADMIN_CONSOLE_CONFIG=/projects/adminConsole/config.yaml`, and hits routes like
`/api/status/trading-node` and `/api/service/trading-node/tradingbot.service/nuke`) — none of these
routes or module paths exist in the live `app.py` (whose routes are `/api/service` etc. with a JSON
body, not path segments). It also includes what look like injection-probe entries (a semicolon and
its URL-encoded form spliced into a service-id path segment) — read as leftover security-testing
scaffolding from that earlier session against the abandoned code path, not as anything to act on.
Don't treat this file as documentation of currently-valid routes or commands; it should probably be
regenerated the next time permissions in this repo are revisited.

## Deployment

`deploy/admin-dashboard.service` runs the dashboard itself (on the dashboard host, as a non-root
user, `NoNewPrivileges=true`). `deploy/sudoers-admin-console.example` is installed on *each managed
Pi* (not the dashboard host) — exact-match `NOPASSWD` rules per unit name, no wildcards. Two things
have to stay in sync if you touch either side: unit names in sudoers must be bare (no `.service`
suffix, matching `projects.json`'s `id` convention — opposite of the dead `config.yaml`'s
`.service`-suffixed convention) and the `journalctl -n 200` count must match `JOURNALCTL_LINES` in
`app.py`, or Tail Logs breaks silently. `check_tailscale_access.sh` runs on the dashboard host to
diagnose reachability (Flask binding, `tailscale0` status, `ufw`) — it's not meant to be copied to a
managed Pi.
