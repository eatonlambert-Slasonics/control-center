from __future__ import annotations

import os
import re

import yaml

CONFIG_PATH = os.environ.get(
    "ADMIN_CONSOLE_CONFIG", os.path.join(os.path.dirname(__file__), "config.yaml")
)

UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
ALLOWED_ACTIONS = {"start", "stop", "restart"}
ALLOWED_KINDS = {"worker", "http"}


class ConfigError(Exception):
    pass


def _validate_service(svc: dict, host_id: str) -> dict:
    unit = svc.get("unit")
    if not unit or not UNIT_RE.match(unit):
        raise ConfigError(f"host '{host_id}': invalid or missing unit name {unit!r}")

    kind = svc.setdefault("kind", "worker")
    if kind not in ALLOWED_KINDS:
        raise ConfigError(f"host '{host_id}/{unit}': invalid kind {kind!r}")

    if kind == "http":
        if not svc.get("port"):
            raise ConfigError(f"host '{host_id}/{unit}': http service missing 'port'")
        svc.setdefault("endpoints", {})

    svc.setdefault("display_name", unit)
    return svc


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    ssh_cfg = raw.get("ssh") or {}
    ssh_cfg.setdefault("key_path", "~/.ssh/id_ed25519")
    ssh_cfg.setdefault("connect_timeout", 6)
    ssh_cfg.setdefault("command_timeout", 12)
    ssh_cfg["key_path"] = os.path.expanduser(ssh_cfg["key_path"])

    hosts: dict[str, dict] = {}
    for host in raw.get("hosts", []) or []:
        host_id = host.get("id")
        if not host_id:
            raise ConfigError("a host entry is missing 'id'")
        if host_id in hosts:
            raise ConfigError(f"duplicate host id '{host_id}'")
        if not host.get("address"):
            raise ConfigError(f"host '{host_id}' missing 'address'")

        host.setdefault("name", host_id)
        host.setdefault("ssh_port", 22)
        host.setdefault("ssh_user", "pi")
        host["id"] = host_id
        host["services"] = [_validate_service(s, host_id) for s in host.get("services", []) or []]

        seen_units = set()
        for svc in host["services"]:
            if svc["unit"] in seen_units:
                raise ConfigError(f"host '{host_id}': duplicate unit '{svc['unit']}'")
            seen_units.add(svc["unit"])

        hosts[host_id] = host

    if not hosts:
        raise ConfigError("config.yaml defines no hosts")

    return {
        "poll_interval_seconds": raw.get("poll_interval_seconds", 8),
        "ssh": ssh_cfg,
        "hosts": hosts,
    }
