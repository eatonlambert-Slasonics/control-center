from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

TIMEOUT = (3, 5)  # (connect, read) seconds


def poll_endpoint(address: str, port: int, path: str) -> dict:
    """Fetch one JSON endpoint. Never raises -- network failures, timeouts, and bad JSON
    all collapse into a structured {ok: False, error: ...} result."""
    url = f"http://{address}:{port}{path}"
    start = time.monotonic()
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        latency_ms = round((time.monotonic() - start) * 1000)
        try:
            data = resp.json()
        except ValueError:
            data = None
        return {
            "ok": resp.ok,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "data": data,
            "error": None if resp.ok else f"HTTP {resp.status_code}",
        }
    except requests.exceptions.RequestException as e:
        return {"ok": False, "status_code": None, "latency_ms": None, "data": None, "error": str(e)}
    except Exception as e:  # never let a malformed response take the poller down
        log.exception("Unexpected error polling %s", url)
        return {"ok": False, "status_code": None, "latency_ms": None, "data": None, "error": f"unexpected error: {e}"}


def poll_service(host: dict, service: dict) -> dict:
    if service.get("kind") != "http":
        return {}
    address = host["address"]
    port = service["port"]
    return {
        key: poll_endpoint(address, port, path)
        for key, path in service.get("endpoints", {}).items()
    }
