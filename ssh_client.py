from __future__ import annotations

import logging
import shlex

import paramiko

log = logging.getLogger(__name__)


class SSHResult:
    def __init__(self, ok: bool, stdout: str = "", stderr: str = "", error: str = ""):
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.error = error

    def to_dict(self) -> dict:
        return {"ok": self.ok, "stdout": self.stdout, "stderr": self.stderr, "error": self.error or None}


def run_command(host: dict, ssh_cfg: dict, command_parts: list[str]) -> SSHResult:
    """Run a command on a remote host over SSH. Never raises -- always returns an SSHResult,
    so an offline Pi or a broken service never takes the dashboard process down with it."""
    command = " ".join(shlex.quote(p) for p in command_parts)
    client = paramiko.SSHClient()
    try:
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        client.connect(
            hostname=host["address"],
            port=host.get("ssh_port", 22),
            username=host.get("ssh_user", "pi"),
            key_filename=ssh_cfg["key_path"],
            timeout=ssh_cfg["connect_timeout"],
            banner_timeout=ssh_cfg["connect_timeout"],
            auth_timeout=ssh_cfg["connect_timeout"],
        )
        _stdin, stdout, stderr = client.exec_command(command, timeout=ssh_cfg["command_timeout"])
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        return SSHResult(
            ok=(exit_status == 0),
            stdout=out,
            stderr=err,
            error="" if exit_status == 0 else (err.strip() or f"exit status {exit_status}"),
        )
    except paramiko.AuthenticationException as e:
        log.warning("SSH auth failed for host '%s': %s", host.get("id"), e)
        return SSHResult(ok=False, error="SSH authentication failed")
    except paramiko.BadHostKeyException as e:
        log.warning("SSH host key mismatch for host '%s': %s", host.get("id"), e)
        return SSHResult(ok=False, error="SSH host key mismatch (possible spoofing or reimage)")
    except paramiko.SSHException as e:
        log.warning("SSH error for host '%s': %s", host.get("id"), e)
        return SSHResult(ok=False, error=f"SSH error: {e}")
    except (OSError, EOFError) as e:
        log.warning("Connection error for host '%s': %s", host.get("id"), e)
        return SSHResult(ok=False, error=f"connection error: {e}")
    except Exception as e:  # last-resort safety net -- an unexpected error must not crash Flask
        log.exception("Unexpected SSH failure for host '%s'", host.get("id"))
        return SSHResult(ok=False, error=f"unexpected error: {e}")
    finally:
        client.close()


def systemctl_action(host: dict, ssh_cfg: dict, unit: str, action: str) -> SSHResult:
    return run_command(host, ssh_cfg, ["sudo", "-n", "systemctl", action, unit])


def journalctl_tail(host: dict, ssh_cfg: dict, unit: str, lines: int = 25) -> SSHResult:
    return run_command(host, ssh_cfg, ["sudo", "-n", "journalctl", "-u", unit, "-n", str(lines), "--no-pager"])


def service_status(host: dict, ssh_cfg: dict, unit: str) -> SSHResult:
    # `systemctl is-active` is readable without elevated privileges.
    return run_command(host, ssh_cfg, ["systemctl", "is-active", unit])
