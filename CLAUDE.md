# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small Flask dashboard (`app.py`) for remotely monitoring and controlling systemd services on a
fleet of Raspberry Pis (trading bot hosts, research nodes, etc.) over SSH via Tailscale. See
`README.md` for the full user-facing description, layout, config schema, and deployment notes —
this file is the AI-assistant-focused companion: where the real logic lives, what's dead code, and
what to be careful with.

## The one file that matters: `app.py`

Everything live runs out of this single ~2,200-line file. It is self-contained: HTML is rendered
from an inline `HTML_TEMPLATE` string via `render_template_string` (not `templates/`), CSS/JS are
inlined in that same string (not `static/`), and there is no other module it imports from this repo.

Rough map (line numbers as of 2026-10-02 -- re-check if the file has grown):

- **1–65**: imports (Flask, `paramiko`, `markdown`, stdlib), logging setup (`logs/admin-console.log`,
  rotating), constants: `SLUG_RE`, `DOC_FILENAME_RE`, `TAIL_LOG_LINES_*`, `JOURNALCTL_LINES` (must
  match the sudoers grant -- see below), the Logs-panel validators (`DEVICE_ID_RE`, `LOG_LEVEL_RE`,
  `RUN_ID_RE`, `LOGS_SH_LINES_*`), `ACTION_TIMEOUT_DEFAULT`/`_MAX` (45s/180s), and `CONFIG_PATH`
  (from `ADMIN_CONSOLE_CONFIG` env var, defaults to `projects.json` next to `app.py`).
- **68–91**: config persistence — `load_config()` re-reads `projects.json` on every call (no cache;
  the server is threaded), `save_config()` writes atomically (`tempfile.mkstemp` + `os.replace`).
- **93–164**: action step-tracker state — `action_progress.json` (path from
  `ADMIN_CONSOLE_ACTION_PROGRESS`), same atomic-write pattern, deliberately separate from
  `projects.json` because it's runtime progress, not config. `get_step_index` / `advance_step` /
  `reset_step`; `num_steps` itself is the "all done" sentinel, never an array position.
- **166–178**: `slugify()` generates dedupe-safe ids for new projects/repos.
- **179–215**: `execute_ssh_cmd(host, user, command, key_path=None, timeout=8)` — the *only* SSH
  mechanism the live app uses. Raw `paramiko.SSHClient()` with `AutoAddPolicy()` (trust-on-first-use,
  **no host-key verification**), returns `(success, output)`, never raises.
- **216–437**: the `PLATFORMS` adapter — `PLATFORMS["linux"]` / `PLATFORMS["windows"]`, each
  supplying `resolve_path`, `git_sync_cmd`, `service_cmd`, `reboot_cmd`, `log_cmd_service`,
  `list_docs_cmd`, `read_doc_cmd`, `action_cmd` (`cd <repo> && <command>` /
  `Set-Location <repo>; <command>`), and `tools` (remote-control launchers: `code-server` on both
  platforms, `claude` — tmux + `claude --remote-control` — Linux-only). Windows adapter is marked
  **UNVERIFIED** in its own comments; there's no Windows host to test against.
- **438–539**: service/app/action discovery. `SERVICES_BLOCK_RE` finds fenced ` ```services ` JSON
  blocks in each repo's `.md` docs; `normalize_service_entries()` handles three entry types
  (`service`, `app`, `action` -- see below) and validates every `id` against `SLUG_RE` (required —
  ids get interpolated into shell commands and inline onclick JS); `discover_project_services(project)`
  does the actual SSH-and-scan work and is called fresh on every relevant request (not cached —
  expect a burst of SSH round-trips per Services/Apps panel load and per action run).
- **541–1725**: `HTML_TEMPLATE`, the whole UI (Jinja + CSS + inline `<script>`), including the
  ADB and Logs panels.
- **1727–2191**: routes. All JSON-in/JSON-out (`jsonify({"success": bool, "message": str})`), no
  auth, no CSRF, no sessions — access control is "you're on the Tailscale network," per the README.
  Key ones: `POST /api/service` (start/stop/restart, re-validates `service_id` against a fresh
  `discover_project_services()` call before building the command), `POST /api/action` and
  `POST /api/action/reset` (one-shot actions and their step tracker), `POST /api/reboot`,
  `POST /api/remote-control`, `GET /api/logs/app` and `GET /api/logs/<project>/<source>`,
  `GET/POST/DELETE /api/projects*` (project/repo CRUD, plus `.../rename`), `GET /api/docs/...`.
- **2192–2199**: `if __name__ == '__main__'` → `app.run(host='0.0.0.0', port=8080, threaded=True)`.

## Actions, and the panels built on them

A ` ```services ` entry with `"type": "action"` declares a one-shot button: `id` (SLUG_RE),
`name`, `repo_id` (must name a repo already attached to the project), `command`, optional
`timeout` (capped at `ACTION_TIMEOUT_MAX`), optional `steps` (`[{id, label}]`). `/api/action`
re-discovers the entry by id, runs `PLATFORMS[...]['action_cmd'](<repo path>, command)` over
SSH, and returns `"<name>: <stdout>"`. A stepped action's progress advances only on success and
is purely a reminder for the human -- nothing validates what the command actually did.

Two UI panels are built *only* from conventional action ids, with no project-specific backend
code -- keep it that way:

- **ADB panel**: any project declaring `adb-status` (plus `adb-start`, `adb-kill`,
  `adb-restart`, and `-force` twins of kill/restart) gets the panel instead of generic rows.
  The backing script prints one JSON object; the panel's JS parses it out of the action's
  message rather than the backend growing a second response shape.
- **Logs panel**: appears for projects declaring `calibrate-gigbuddy` and/or `adb-status`. Its
  five sources (`gigbuddy-app:`, `gigbuddy-crash:`, `adb-server:`, `action-history:`,
  `action-run:`) go through the existing `/api/logs/<project>/<source>` route and run
  `bash scripts/logs.sh ...` in the repo named by that conventional action's `repo_id`.

GigBuddy's calibration actions (`calibrate-gigbuddy`, `pull-gigbuddy-phone-captures`,
`finish-gigbuddy-calibration`) and the tbot ADB actions live in *those* repos' docs and scripts
(gigbuddy's README, adidas/tbot's API.md) -- changes to what they do belong there, not here.
`FINISH_CALIBRATION_PROMPT.md` and `STEP_TRACKING_PROMPT.md` in this repo are the original
implementation prompts for that work, kept as history; they're already implemented and some
details have since changed, so don't treat them as current docs.

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
  - Logs-panel query params are each checked before use: `device` against `DEVICE_ID_RE`,
    `level` against `LOG_LEVEL_RE`, `run_id` against `RUN_ID_RE`, `name` against `SLUG_RE`, and
    the assembled `logs.sh` arguments are `shlex.quote`d.
- **An action's `command` is run as-is, unvalidated.** It's read from a target repo's docs, so
  anyone who can push to a repo attached to a project can run arbitrary shell commands on that
  project's host by adding a ` ```services ` action. That's the same "docs are trusted,
  admin-authored content" boundary as service ids and paths, but with a much larger blast radius
  -- keep it in mind before attaching a repo other people can push to, and never let any part
  of a request body flow into an action's command.
- If you add a new route that reaches `execute_ssh_cmd`, validate every interpolated value the same
  way — a regex allowlist checked *before* the value is placed into the command string, not after.

## Config, not code, for fleet changes

Adding/removing a project, repo, or SSH target is a `projects.json` change made through the
dashboard's own Add Project / Add Repo UI (or by hand-editing `projects.json`, which is gitignored —
copy `projects.json.example` to start). The only in-place edit is renaming a project
(`POST /api/projects/<key>/rename`); changing anything else about a project or repo means
delete-and-re-add. Services and apps are never stored in `projects.json` at all — they're
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
`parse_services_block`, `normalize_service_entries` -- including the `action`/`steps` branch --,
the step-tracker functions against a temp `ADMIN_CONSOLE_ACTION_PROGRESS`, and the
`SLUG_RE`/`DOC_FILENAME_RE`/`DEVICE_ID_RE`/`RUN_ID_RE` validation) with mocked `paramiko` — the
existing code has no test scaffolding to extend, you'd be starting fresh.

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
