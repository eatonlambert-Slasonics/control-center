# control-center

A small Flask dashboard for remotely monitoring and controlling systemd
services on a fleet of Raspberry Pis (trading bot hosts, research nodes,
etc.) over SSH and a Tailscale-only HTTP monitoring API.

## Current status

This repo has two generations of the same idea living side by side:

- **`app.py`** -- the app that actually runs today. Self-contained: the page
  is rendered from an inline HTML string, it listens on `0.0.0.0:8080`, and
  projects (host, SSH creds, telemetry, systemd services, git-backed
  code-server/claude remote-control repos) are persisted in `projects.json`
  -- a file `app.py` both reads and writes, so adding/removing a project,
  repo, or service is done entirely through the dashboard's own UI (Add
  Project / Add Repo / Add Service buttons), never by editing Python.
  Command generation for service control, reboot, log tailing, and remote
  control is dispatched through a `PLATFORMS` adapter keyed by each
  project's `platform` field (`linux` | `windows`), so a single project list
  can mix Linux and Windows targets. Deployed via `deploy/admin-dashboard.service`.
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
- A monitoring API reachable over Tailscale exposing `/status` and
  `/portfolio` (optional -- leave `api_host` blank to skip telemetry).
- `git` and (if used) `code-server`/`claude` installed, for repos that use
  remote-control.

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
      "api_scheme": "https",
      "api_host": "tbot.tail4c9ea5.ts.net",
      "api_port": 3000,
      "public_fallback_url": "https://tbot.eatonlambert.online",
      "services": [
        {"id": "tradingbot", "name": "Trading Bot Main Engine"}
      ],
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
      "api_scheme": "http",
      "api_host": "winbox.your-tailnet.ts.net",
      "api_port": 3000,
      "public_fallback_url": null,
      "services": [
        {"id": "SomeWinService", "name": "Some Windows Service", "log_path": "C:\\ProgramData\\SomeService\\logs\\app.log"}
      ],
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
- `services[].log_path` is optional, Windows-only -- if set, log tailing
  reads that file directly (`Get-Content -Tail`) instead of falling back to
  the Windows Event Log, which is a much less reliable default.
- Every `id` (project key, `repo.id`, `service.id`) is validated/generated
  server-side (`^[A-Za-z0-9_-]+$`) -- these get embedded directly into
  inline `onclick` JS in the rendered page, so this isn't just cosmetic.
- Override the config file path with the `ADMIN_CONSOLE_CONFIG` env var
  (see `deploy/admin-dashboard.service`).
- No in-place edit endpoint yet -- delete and re-add to change a project,
  repo, or service.

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
