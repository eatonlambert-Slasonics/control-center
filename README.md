# control-center

A small Flask dashboard for remotely monitoring and controlling systemd
services on a fleet of Raspberry Pis (trading bot hosts, research nodes,
etc.) over SSH and a Tailscale-only HTTP monitoring API.

## Current status

This repo has two generations of the same idea living side by side:

- **`app.py`** -- the app that actually runs today. It's self-contained:
  hosts/services are hardcoded in a `TARGET_PROJECTS` dict (one entry per
  project, covering telemetry, systemd services, and git-backed remote-control
  sessions together), the page is rendered from an inline HTML string, and it
  listens on `0.0.0.0:8080`. Deployed via `deploy/admin-dashboard.service`.
- **`config.py` / `config.yaml` / `ssh_client.py` / `http_monitor.py` /
  `templates/` / `static/`** -- an in-progress, config-driven rewrite. Hosts
  and services are defined in `config.yaml` instead of hardcoded, SSH/HTTP
  calls are split into their own modules, and the frontend
  (`templates/index.html` + `static/dashboard.js`) expects a richer API
  (`/api/status/<host>`, `/api/monitor/<host>`, `/api/service/<host>/<unit>/<action>`,
  `/api/logs/<host>/<unit>`). **There is no Flask app wired up to serve these
  routes yet** -- these pieces aren't reachable until that entrypoint exists.

If you're picking this up: the next step for the rewrite is a new app entry
point (e.g. `app_v2.py`) that loads `load_config()` from `config.py` and
implements the routes `dashboard.js` already calls, using `ssh_client.py`
and `http_monitor.py` for the actual work.

## Layout

```
app.py                  Live entrypoint (hardcoded hosts, port 8080)
config.py               Loads and validates config.yaml (rewrite, not yet wired up)
config.yaml             Host/service definitions for the rewrite
ssh_client.py           SSH command runner (systemctl actions, journalctl tail)
http_monitor.py         Polls a service's HTTP endpoints for telemetry
templates/index.html    Frontend for the rewrite
static/dashboard.js     Frontend JS for the rewrite
static/style.css        Frontend styles for the rewrite
test_console.py         Standalone health-check script (SSH + HTTP), independent of app.py
check_tailscale_access.sh  Diagnostic script to run on the dashboard host if it's unreachable over Tailscale
deploy/                 systemd units + config examples for deployment
```

## Running (current, `app.py`)

```bash
pip install -r requirements.txt
python app.py
```

Serves on `http://0.0.0.0:8080/`. Edit the `TARGET_PROJECTS` dict at the top
of `app.py` to point at your own hosts.

Requirements for each target Pi:
- SSH reachable from the dashboard host over Tailscale, with the dashboard's
  key trusted.
- Passwordless `sudo systemctl {start,stop,restart}` and reboot for the
  managed units (see `deploy/sudoers-admin-console.example`).
- A monitoring API reachable over Tailscale exposing `/status` and
  `/portfolio`.
- `git` and (if used) `code-server`/`claude` installed, for projects that
  configure `local_path`/`git_repo` remote-control.

## Config format (`config.yaml`, for the rewrite)

```yaml
poll_interval_seconds: 8

ssh:
  key_path: ~/.ssh/id_ed25519
  connect_timeout: 6
  command_timeout: 12

hosts:
  - id: trading-node
    name: Trading Node
    address: 192.168.1.50
    ssh_port: 22
    ssh_user: pi
    services:
      - unit: tradingbot.service
        display_name: Trading Bot
        kind: worker

      - unit: tradingbot-api.service
        display_name: Trading API
        kind: http
        port: 5000
        endpoints:
          status: /status
          portfolio: /portfolio
```

- `kind: worker` services are systemd-only (start/stop/restart/logs).
- `kind: http` services additionally get polled on their declared
  `endpoints` for live telemetry.
- Override the config path with the `ADMIN_CONSOLE_CONFIG` env var.

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
