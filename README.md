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

## Services & Apps

There's no "Add Service" form. Instead, each project's "Services & Apps"
section is discovered by SSHing into every repo attached to the project,
reading its top-level `.md` docs, and looking for a fenced code block
labeled `services` containing a JSON list, e.g. in the repo's `README.md`:

    ```services
    [
      {"type": "service", "id": "tradingbot", "name": "Trading Bot Main Engine"},
      {"type": "app", "name": "Grafana", "url": "https://grafana.example.com"}
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
- Docs are trusted content, same trust boundary as everywhere else in this
  app marked "admin-authored": whoever can push to a project's repo can
  declare (and start/stop) services on that project's host.

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
