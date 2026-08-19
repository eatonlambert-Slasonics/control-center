#!/usr/bin/env python3
"""
test_console.py -- standalone health-check for the Admin Console + target Pi.

Runs independently of app.py's own code path (it does not import app.py,
ssh_client.py, etc.) so it validates real SSH/sudo/HTTP behavior rather than
just confirming the dashboard agrees with itself.

Exit code: 0 if all checks pass, 1 if any hard failure occurred.

Usage:
    python3 test_console.py
    python3 test_console.py --host tbot.tail4c9ea5.ts.net --user tbot
    python3 test_console.py --exercise-restart   # also restarts tradingbot-api via the dashboard API (see WARNING below)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field

import paramiko
import requests

# ---------------------------------------------------------------------------
# Defaults -- match the hardcoded TARGET_PIS entry in app.py. Override via
# CLI flags or env vars for use against other hosts/targets.
# ---------------------------------------------------------------------------
DEFAULT_PI_HOST = os.environ.get("ADMINCONSOLE_PI_HOST", "tbot.tail4c9ea5.ts.net")
DEFAULT_PI_USER = os.environ.get("ADMINCONSOLE_PI_USER", "tbot")
DEFAULT_PI_API_PORT = int(os.environ.get("ADMINCONSOLE_PI_API_PORT", "5000"))
DEFAULT_SSH_KEY = os.path.expanduser(os.environ.get("ADMINCONSOLE_SSH_KEY", "~/.ssh/id_ed25519"))
DEFAULT_DASHBOARD_URL = os.environ.get("ADMINCONSOLE_DASHBOARD_URL", "http://localhost:8080")
SERVICES = ["tradingbot.service", "tradingbot-api.service"]
DASHBOARD_TARGET_KEY = "trading-pi"  # key in app.py's TARGET_PIS dict

SSH_CONNECT_TIMEOUT = 5
HTTP_TIMEOUT = 4


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    warning: bool = False  # soft failure -- printed differently, doesn't fail the run


@dataclass
class Report:
    results: list = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", warning: bool = False):
        self.results.append(CheckResult(name, ok, detail, warning))
        self._print_last()

    def _print_last(self):
        r = self.results[-1]
        if r.ok:
            tag = "PASS"
        elif r.warning:
            tag = "WARN"
        else:
            tag = "FAIL"
        line = f"[{tag}] {r.name}"
        if r.detail:
            line += f" -- {r.detail}"
        print(line)

    def hard_failures(self):
        return [r for r in self.results if not r.ok and not r.warning]

    def summary(self):
        passed = sum(1 for r in self.results if r.ok)
        warned = sum(1 for r in self.results if r.warning)
        failed = len(self.hard_failures())
        print("\n" + "=" * 60)
        print(f"{passed} passed, {warned} warned, {failed} failed (of {len(self.results)})")
        print("=" * 60)
        return failed == 0


# ---------------------------------------------------------------------------
# a. SSH connectivity
# ---------------------------------------------------------------------------
def check_ssh_connectivity(report: Report, host: str, user: str, key_path: str) -> paramiko.SSHClient | None:
    if not os.path.exists(key_path):
        report.add("SSH key present", False, f"key not found at {key_path}")
        return None
    report.add("SSH key present", True, key_path)

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            hostname=host,
            username=user,
            key_filename=key_path,
            timeout=SSH_CONNECT_TIMEOUT,
            banner_timeout=SSH_CONNECT_TIMEOUT,
            auth_timeout=SSH_CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        report.add("SSH connect (key auth, host key verified)", True, f"{user}@{host}")
        return client
    except paramiko.BadHostKeyException as e:
        report.add(
            "SSH connect (key auth, host key verified)",
            False,
            f"host key mismatch -- possible MITM or Pi was reimaged: {e}",
        )
    except paramiko.AuthenticationException:
        # Retry with an unverified host key policy so we can tell "key rejected"
        # apart from "host key isn't in known_hosts yet".
        client2 = paramiko.SSHClient()
        client2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client2.connect(
                hostname=host, username=user, key_filename=key_path,
                timeout=SSH_CONNECT_TIMEOUT, allow_agent=False, look_for_keys=False,
            )
            client2.close()
            report.add(
                "SSH connect (key auth, host key verified)",
                False,
                "key auth works but host key is not in known_hosts -- run `ssh " + f"{user}@{host}" + "` once manually to trust it",
            )
        except Exception:
            report.add(
                "SSH connect (key auth, host key verified)",
                False,
                "passwordless key auth failed -- check authorized_keys on the Pi and key permissions (600) locally",
            )
    except (socket.timeout, TimeoutError):
        report.add("SSH connect (key auth, host key verified)", False, f"timed out after {SSH_CONNECT_TIMEOUT}s -- Pi unreachable or SSH not listening")
    except (socket.gaierror, OSError) as e:
        report.add("SSH connect (key auth, host key verified)", False, f"network error: {e}")
    except Exception as e:
        report.add("SSH connect (key auth, host key verified)", False, f"unexpected error: {e}")
    return None


# ---------------------------------------------------------------------------
# b. Passwordless sudo for systemctl status
# ---------------------------------------------------------------------------
def _run(client: paramiko.SSHClient, command: str, timeout: int = 10):
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return exit_status, out, err


def check_passwordless_sudo(report: Report, client: paramiko.SSHClient):
    for unit in SERVICES:
        cmd = f"sudo -n systemctl status {unit}"
        try:
            exit_status, out, err = _run(client, cmd)
        except Exception as e:
            report.add(f"passwordless sudo: {unit}", False, f"exec failed: {e}")
            continue

        combined = (out + err).lower()
        if "a password is required" in combined or "no tty present" in combined or "sorry, try again" in combined:
            report.add(
                f"passwordless sudo: {unit}", False,
                "sudo is prompting for a password -- check `sudo visudo` NOPASSWD entry for systemctl",
            )
            continue

        # `systemctl status` returns 0=active, 3=inactive/dead, 4=unit not found.
        # All of those mean sudo itself worked; only genuinely unexpected codes are a hard fail.
        if exit_status in (0, 3):
            state = "active" if exit_status == 0 else "inactive"
            report.add(f"passwordless sudo: {unit}", True, f"sudo OK, unit is {state}")
        elif exit_status == 4:
            report.add(f"passwordless sudo: {unit}", False, f"sudo OK but unit not found on target -- check unit name matches systemd exactly")
        else:
            report.add(f"passwordless sudo: {unit}", False, f"unexpected exit status {exit_status}: {err.strip() or out.strip()}")

    # Also confirm reboot is passwordless without actually rebooting: `sudo -n reboot --help`
    # exercises the sudoers NOPASSWD match for the `reboot` binary without side effects.
    try:
        exit_status, out, err = _run(client, "sudo -n -l reboot")
        combined = (out + err).lower()
        if "a password is required" in combined:
            report.add("passwordless sudo: reboot", False, "sudo would prompt for reboot -- add reboot to NOPASSWD in visudo")
        elif exit_status == 0:
            report.add("passwordless sudo: reboot", True, "NOPASSWD entry confirmed via `sudo -n -l reboot`")
        else:
            report.add("passwordless sudo: reboot", True, "reboot not independently listed by `sudo -l`, likely covered by a broader NOPASSWD:ALL rule", warning=True)
    except Exception as e:
        report.add("passwordless sudo: reboot", False, f"exec failed: {e}")


# ---------------------------------------------------------------------------
# c. Pi monitoring API reachability + JSON validation
# ---------------------------------------------------------------------------
def check_pi_http_api(report: Report, host: str, port: int):
    base = f"http://{host}:{port}"

    # /status -- app.py's JS reads data.status.status, .mode, .trading_pairs
    try:
        resp = requests.get(f"{base}/status", timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            report.add("GET /status", False, f"HTTP {resp.status_code}")
        else:
            try:
                data = resp.json()
            except ValueError:
                report.add("GET /status", False, "response is not valid JSON")
            else:
                missing = [k for k in ("status", "mode", "trading_pairs") if k not in data]
                if missing:
                    report.add("GET /status", True, f"200 OK but missing expected keys: {missing}", warning=True)
                else:
                    report.add("GET /status", True, f"status={data.get('status')!r} mode={data.get('mode')!r}")
    except requests.exceptions.RequestException as e:
        report.add("GET /status", False, str(e))

    # /portfolio -- app.py's JS reads data.portfolio.equity, .unrealized_pnl
    try:
        resp = requests.get(f"{base}/portfolio", timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            report.add("GET /portfolio", False, f"HTTP {resp.status_code}")
        else:
            try:
                data = resp.json()
            except ValueError:
                report.add("GET /portfolio", False, "response is not valid JSON")
            else:
                missing = [k for k in ("equity", "unrealized_pnl") if k not in data]
                if missing:
                    report.add("GET /portfolio", True, f"200 OK but missing expected keys: {missing}", warning=True)
                else:
                    report.add("GET /portfolio", True, f"equity={data.get('equity')} pnl={data.get('unrealized_pnl')}")
    except requests.exceptions.RequestException as e:
        report.add("GET /portfolio", False, str(e))


# ---------------------------------------------------------------------------
# d. Local Flask dashboard responsiveness
# ---------------------------------------------------------------------------
def check_local_dashboard(report: Report, base_url: str):
    try:
        resp = requests.get(base_url + "/", timeout=HTTP_TIMEOUT)
        ok = resp.status_code == 200 and "Raspberry Pi Fleet" in resp.text
        report.add("Local dashboard: GET /", ok, f"HTTP {resp.status_code}")
    except requests.exceptions.RequestException as e:
        report.add("Local dashboard: GET /", False, f"{e} -- is app.py running? (systemctl status admin-dashboard, or check the process on :8080)")
        return  # no point checking the API route if the server isn't even up

    try:
        resp = requests.get(f"{base_url}/api/telemetry/{DASHBOARD_TARGET_KEY}", timeout=HTTP_TIMEOUT + 4)
        if resp.status_code != 200:
            report.add("Local dashboard: GET /api/telemetry", False, f"HTTP {resp.status_code}")
        else:
            data = resp.json()
            if data.get("success"):
                report.add("Local dashboard: GET /api/telemetry", True, "success=true, proxied Pi telemetry OK")
            else:
                report.add(
                    "Local dashboard: GET /api/telemetry", True,
                    f"dashboard is up but reports success=false ({data.get('error')}) -- expected if the Pi/API is down",
                    warning=True,
                )
    except requests.exceptions.RequestException as e:
        report.add("Local dashboard: GET /api/telemetry", False, str(e))


# ---------------------------------------------------------------------------
# optional, opt-in: exercise a real restart through the dashboard's own
# mutating endpoint. Off by default -- this actually bounces a live service.
# ---------------------------------------------------------------------------
def exercise_restart(report: Report, base_url: str):
    unit = "tradingbot-api.service"  # least disruptive of the two -- monitoring API, not the trading loop
    print(f"\n--- WARNING: restarting {unit} via the dashboard API now ---")
    try:
        resp = requests.post(
            f"{base_url}/api/service",
            json={"target": DASHBOARD_TARGET_KEY, "service": unit, "action": "restart"},
            timeout=HTTP_TIMEOUT + 4,
        )
        data = resp.json()
        report.add(f"POST /api/service restart {unit}", data.get("success", False), data.get("message", ""))
    except requests.exceptions.RequestException as e:
        report.add(f"POST /api/service restart {unit}", False, str(e))
        return

    time.sleep(3)
    check_pi_http_api(report, args_host, args_port)  # noqa: F821 -- set in main() before this is called


def main():
    global args_host, args_port
    parser = argparse.ArgumentParser(description="Health-check the Admin Console and its target Pi.")
    parser.add_argument("--host", default=DEFAULT_PI_HOST, help="Target Pi IP/hostname")
    parser.add_argument("--user", default=DEFAULT_PI_USER, help="SSH user on the Pi")
    parser.add_argument("--key", default=DEFAULT_SSH_KEY, help="Path to SSH private key")
    parser.add_argument("--api-port", type=int, default=DEFAULT_PI_API_PORT, help="Pi monitoring API port")
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL, help="Local dashboard base URL")
    parser.add_argument(
        "--exercise-restart", action="store_true",
        help="Also restart tradingbot-api.service via the dashboard's own API and verify it recovers. "
             "This causes real, brief downtime of the monitoring API. Off by default.",
    )
    args = parser.parse_args()
    args_host, args_port = args.host, args.api_port

    report = Report()

    print("== a. SSH connectivity ==")
    client = check_ssh_connectivity(report, args.host, args.user, args.key)

    print("\n== b. Passwordless sudo (systemctl status, reboot) ==")
    if client:
        check_passwordless_sudo(report, client)
        client.close()
    else:
        report.add("passwordless sudo checks", False, "skipped -- SSH connectivity failed above")

    print("\n== c. Pi monitoring API (HTTP + JSON) ==")
    check_pi_http_api(report, args.host, args.api_port)

    print("\n== d. Local Flask dashboard ==")
    check_local_dashboard(report, args.dashboard_url.rstrip("/"))

    if args.exercise_restart:
        print("\n== optional: live restart exercise ==")
        exercise_restart(report, args.dashboard_url.rstrip("/"))

    ok = report.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
