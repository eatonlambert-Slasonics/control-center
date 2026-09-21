# control-center

A small Flask dashboard for remotely monitoring and controlling systemd
services on a fleet of Raspberry Pis (trading bot hosts, research nodes,
etc.) over SSH via Tailscale.

## Current status

This repo has two generations of the same idea living side by side:

- **`app.py`** -- the app that actually runs today. Self-contained: the page
  is rendered from an inline HTML string, it listens on `0.0.0.0:8080`, and
  projects (host, SSH creds, git-backed code-server/claude remote-control
  repos) are persisted in `projects.json` -- a file `app.py` both reads and
  writes, so adding/removing a project or repo is done entirely through the
  dashboard's own UI (Add Project / Add Repo buttons), never by editing
  Python. Services and apps are *not* persisted in `projects.json` -- they're
  discovered by scanning each repo's own `.md` docs for a fenced
  ` ```services ` JSON block (see "Services & Apps" below). Command
  generation for service control, reboot, log tailing, and remote control is
  dispatched through a `PLATFORMS` adapter keyed by each project's
  `platform` field (`linux` | `windows`), so a single project list can mix
  Linux and Windows targets. Deployed via `deploy/admin-dashboard.service`.
- **`config.py` / `config.yaml` / `ssh_client.py` / `http_monitor.py` /
  `templates/` / `static/`** -- a separate, unfinished, unrelated rewrite
  attempt (different fictional fleet, different API shape). **There is no
  Flask app wired up to serve these routes** -- dead code, not used by
  `app.py`, not touched by the `projects.json` work above.

## Layout

```
app.py                  Live entrypoint, port 8080
projects.json            Live project config (gitignored -- see projects.json.example)
projects.json.example    Seed/schema template, copy to projects.json to start
action_progress.json     Per-action step-tracker state (gitignored, created on first use)
config.py, config.yaml, ssh_client.py, http_monitor.py, templates/, static/
                          Dead, unrelated rewrite attempt -- not used by app.py
test_console.py         Standalone health-check script (SSH + HTTP), independent of app.py
check_tailscale_access.sh  Diagnostic script to run on the dashboard host if it's unreachable over Tailscale
deploy/                 systemd units + config examples for deployment
```

## Running

```bash
pip install -r requirements.txt
cp projects.json.example projects.json   # then edit to point at your own hosts, or use the Add Project button
python app.py
```

Serves on `http://0.0.0.0:8080/`. If `projects.json` is missing, the app
still boots with zero projects -- use the "+ Add Project" button in the UI,
or hand-edit the file, then restart.

Requirements for each target host:
- SSH reachable from the dashboard host over Tailscale, with the dashboard's
  key trusted.
- **Linux**: passwordless `sudo systemctl {start,stop,restart}`, `sudo
  reboot`, and `sudo journalctl` for the managed units (see
  `deploy/sudoers-admin-console.example`, exact-match, no wildcards).
- **Windows**: UNVERIFIED -- no Windows host has been available to test
  against. `app.py`'s Windows adapter is written from PowerShell/Windows
  Service documentation only. There is no sudoers equivalent; the SSH user
  needs its own service-control/reboot rights (local security policy, or a
  restrictive wrapper script), which is a deployment decision not designed
  here. `claude` (tmux-based) is Linux-only -- Windows projects only offer
  `code-server`.
- `git` and (if used) `code-server`/`claude` installed, for repos that use
  remote-control.
- **`claude` tool specifically**: starting it launches `claude --remote-control`
  in a detached tmux session, which is what makes the session show up in the
  Claude mobile app / claude.ai (a plain `claude` invocation never registers
  anywhere and will never appear remotely). For that to work, on that host,
  one-time and outside this app: run `claude auth login` interactively so the
  CLI has a stored session, and confirm outbound HTTPS to `api.anthropic.com`
  is reachable (Tailscale doesn't block this by default, but an
  exit-node-only routed box might not have general internet access). If a
  project already has a stale plain-`claude` tmux session from before this
  requirement existed, use **Restart** once (not Start) to kill and relaunch
  it with `--remote-control`.

## Services & Apps

There's no "Add Service" form. Instead, each project's "Services & Apps"
section is discovered by SSHing into every repo attached to the project,
reading its top-level `.md` docs, and looking for a fenced code block
labeled `services` containing a JSON list, e.g. in the repo's `README.md`:

    ```services
    [
      {"type": "service", "id": "tradingbot", "name": "Trading Bot Main Engine"},
      {"type": "app", "name": "Grafana", "url": "https://grafana.example.com"},
      {"type": "action", "id": "calibrate-gigbuddy", "name": "Calibrate GigBuddy",
       "repo_id": "gigbuddy", "command": "bash scripts/calibrate_gigbuddy.sh", "timeout": 60,
       "steps": [{"id": "offer", "label": "Offer screen"},
                 {"id": "arrived", "label": "Arrived at merchant"},
                 {"id": "picked-up", "label": "Mark picked up"}]}
    ]
    ```

- `type: "service"` entries get Start/Stop/Restart buttons wired to
  `PLATFORMS[platform]['service_cmd']` (systemd on Linux, `Start-Service` /
  etc. on Windows) -- `id` must match `^[A-Za-z0-9_-]+$` (validated
  server-side; invalid entries are silently dropped) since it's substituted
  directly into the remote command. An optional `log_path` (Windows only)
  works the same way the old `services[].log_path` config field did. Entries
  are re-validated against a fresh doc scan on every start/stop/restart
  request -- a service id has to currently be declared in a repo's docs to
  be actionable, not just guessed at from the browser.
- `type: "app"` entries just render an "Open" link to `url` -- no remote
  command, for things like a Grafana dashboard or a web UI this dashboard
  doesn't otherwise manage.
- `type: "action"` entries get a single "Run" button that SSHes in, `cd`s
  into `repo_id`'s `local_path` (must name a repo already attached to the
  project), and runs `command` once -- no start/stop/restart semantics, just
  fire-and-report. For scripts that can run long (a remote capture, a sync
  job, etc.), set `timeout` in seconds (default 45s, capped at 180s) --
  `/api/action` passes it straight through to the SSH exec timeout. Meant
  for a script the repo already ships and commits to git, not arbitrary
  ad-hoc commands typed into a doc. The dashboard disables the button and
  shows "Running…" for the duration, since a slow action triggered from a
  phone is exactly the case where a double-tap would otherwise fire it twice.
  - An action can optionally declare a `steps` array (`{"id", "label"}` each)
    -- a purely human-facing reminder of what to go do next before tapping
    Run (e.g. "capture these 3 screens in order"), never validated against
    what the command actually captured or did. The dashboard shows the
    current step's label next to the button ("Capturing: Offer screen (1 of
    3)"), advances it on a **successful** run, and leaves it alone on a
    failed one so a flaky run never forces re-doing a step already
    completed. Once every step has succeeded once, it shows "All screens
    captured -- tap Reset to start over, or Run to capture the last screen
    again" and stops advancing (repeated successful runs of the last step
    just stay there). A small **Reset** button next to the indicator sets it
    back to step 1 with a plain click -- no confirm, since it only resets a
    label, never any data the action already produced. Progress persists
    per `{project}/{action_id}` in `action_progress.json` (gitignored, same
    atomic-write pattern as `projects.json`, created on first use;
    override its path with `ADMIN_CONSOLE_ACTION_PROGRESS`).
- Docs are trusted content, same trust boundary as everywhere else in this
  app marked "admin-authored": whoever can push to a project's repo can
  declare (and start/stop/run) services and actions on that project's host.

## ADB panel

A project whose docs declare an action with `id: "adb-status"` gets a dedicated **ADB**
panel instead of a generic services row -- a server-running indicator, one row per
connected device (friendly name, colored state badge, transport), Start/Restart/Kill/
Refresh buttons, and auto-polling every 15s while the tab is visible (paused when it
isn't). It's built entirely on the existing `type: "action"` mechanism above -- six
ordinary action entries under a naming convention, no new backend route or SSH path:

```services
[
  {"type": "action", "id": "adb-status", "name": "ADB: Refresh Status",
   "repo_id": "adidas-main", "command": "bash scripts/adb_control.sh status", "timeout": 20},
  {"type": "action", "id": "adb-start", "name": "ADB: Start",
   "repo_id": "adidas-main", "command": "bash scripts/adb_control.sh start", "timeout": 60},
  {"type": "action", "id": "adb-kill", "name": "ADB: Kill",
   "repo_id": "adidas-main", "command": "bash scripts/adb_control.sh kill", "timeout": 20},
  {"type": "action", "id": "adb-kill-force", "name": "ADB: Kill (forced)",
   "repo_id": "adidas-main", "command": "bash scripts/adb_control.sh kill --force", "timeout": 20},
  {"type": "action", "id": "adb-restart", "name": "ADB: Restart",
   "repo_id": "adidas-main", "command": "bash scripts/adb_control.sh restart", "timeout": 60},
  {"type": "action", "id": "adb-restart-force", "name": "ADB: Restart (forced)",
   "repo_id": "adidas-main", "command": "bash scripts/adb_control.sh restart --force", "timeout": 60}
]
```

- `adb-status`/`adb-start`/`adb-kill`/`adb-restart` and their `-force` twins are all
  ordinary actions -- `renderServicesAndApps` just recognizes those six conventional ids,
  hides them from the generic list, and renders the panel instead. Any project can get
  the panel by declaring the same six ids (pointed at its own `adb_control.sh`); nothing
  in `app.py` is project-specific.
- Every one of these actions' script prints exactly one JSON object to stdout --
  `{"server_running": bool, "devices": [{"id","state","model","friendly_name",
  "transport"}], "checked_at": ...}` on success, `{"error": "..."}` (nonzero exit) on
  failure or refusal. `/api/action` wraps that as `"<action name>: <stdout>"` same as any
  other action; the panel's JS finds the first `{` in the message and `JSON.parse`s from
  there rather than teaching the backend a second response shape.
- Kill/Restart refuse (leaving everything untouched) while something the script
  considers busy is running, and report why in the `error` field -- the panel shows that
  message with a **Force** button (armed the same two-click way as every other
  confirm in this UI) that retries via the `-force` action id.
- Polling (`adb-status` on a 15s timer) never has side effects by design -- the backing
  script determines whether its connection is up via `pgrep`/`ss` before ever invoking
  `adb`, specifically so a routine status poll can never accidentally start something
  that was deliberately stopped.
- **What "ADB server" means here is host-specific, not generic** -- see
  `scripts/adb_control.sh`'s own header comment in whichever repo declares these actions
  for what it actually starts/kills/restarts on that host. On tbot specifically, there is
  no working local adb server (verified directly: an isolated local adb server there
  finds zero devices via mDNS); what these actions actually manage is the whitebox-relay
  tunnel client, the only thing that's ever actually reached the phones from tbot.

## Config format (`projects.json`)

```json
{
  "version": 1,
  "projects": {
    "Adidas": {
      "platform": "linux",
      "host": "tbot.tail4c9ea5.ts.net",
      "user": "tbot",
      "key_path": null,
      "hardware": "Raspberry Pi 5 x64",
      "os_label": "Raspberry Pi OS",
      "repos": [
        {"id": "adidas-main", "name": "Project repo", "local_path": "~/adidas", "git_repo": "https://github.com/eatonlambert-Slasonics/tbot.git"}
      ]
    },
    "SomeWindowsBox": {
      "platform": "windows",
      "host": "winbox.your-tailnet.ts.net",
      "user": "youruser",
      "key_path": null,
      "hardware": "Mini PC",
      "os_label": "Windows 11",
      "repos": [
        {"id": "app-main", "name": "App repo", "local_path": "C:\\Users\\youruser\\repos\\app", "git_repo": "https://github.com/example/app.git"}
      ]
    }
  }
}
```

- `platform` is the only field the backend dispatches on (`linux` or
  `windows`) -- `os_label` is freeform display text ("Ubuntu 22.04",
  "Windows 11", "Raspberry Pi OS", whatever you want), never parsed.
  "Ubuntu"/"Debian"/"Raspberry Pi OS" are all `platform: "linux"` -- there's
  no per-distro adapter, just per-OS-family (systemd + bash vs. PowerShell).
- Services and apps aren't in this file at all -- see "Services & Apps"
  above.
- Every `id` (project key, `repo.id`) is validated/generated server-side
  (`^[A-Za-z0-9_-]+$`) -- these get embedded directly into inline `onclick`
  JS in the rendered page, so this isn't just cosmetic.
- Override the config file path with the `ADMIN_CONSOLE_CONFIG` env var
  (see `deploy/admin-dashboard.service`).
- No in-place edit endpoint yet -- delete and re-add to change a project or
  repo.

## Deployment

`deploy/` has systemd unit files and setup notes for a Raspberry Pi:

- `admin-dashboard.service` -- runs the dashboard itself.
- `sudoers-admin-console.example` -- exact-match `NOPASSWD` sudoers rules so
  the dashboard can only start/stop/restart/read logs for specific units on
  a managed host, nothing else.

Each file has install instructions in its header comments.

## Diagnostics

If the dashboard isn't reachable over Tailscale, run
`check_tailscale_access.sh` on the dashboard host. It checks whether Flask
is bound to all interfaces, whether `tailscale0` is up with the expected IP,
whether `ufw` is blocking the port, and whether the app responds locally.

## Testing

`test_console.py` is a standalone health-check that exercises real SSH,
sudo, and HTTP behavior against a target Pi and (optionally) the dashboard
API -- it does not import `app.py` or any other module here, so it verifies
actual behavior rather than internal consistency.

```bash
python test_console.py
python test_console.py --host tbot.tail4c9ea5.ts.net --user tbot
python test_console.py --exercise-restart   # also restarts tradingbot-api via the dashboard API
```

Exits `0` if all checks pass, `1` on any hard failure.
