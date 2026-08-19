from flask import Flask, render_template_string, jsonify, request
import paramiko
import markdown
import os
import re
import shlex
import json
import threading
import tempfile
import logging
from logging.handlers import RotatingFileHandler

app = Flask(__name__)

DEFAULT_SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
SLUG_RE = re.compile(r'^[A-Za-z0-9_-]+$')

# --- Logging ----------------------------------------------------------------
# Local file so the "app" log source in the Tail Logs panel can read it directly
# (no SSH needed -- the dashboard reads its own log off disk). Also mirrored to
# stdout, which systemd already captures into `journalctl -u admin-dashboard`.
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
APP_LOG_PATH = os.path.join(LOG_DIR, 'admin-console.log')

logger = logging.getLogger('admin_console')
logger.setLevel(logging.INFO)
_log_format = logging.Formatter('%(asctime)s %(levelname)s %(message)s')

_file_handler = RotatingFileHandler(APP_LOG_PATH, maxBytes=1_000_000, backupCount=3)
_file_handler.setFormatter(_log_format)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_format)
logger.addHandler(_console_handler)

TAIL_LOG_LINES_DEFAULT = 200
TAIL_LOG_LINES_MAX = 1000
JOURNALCTL_LINES = 200  # fixed -- must match the exact-match sudoers grant
DOC_FILENAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')  # no slashes/dot-dot -- blocks path traversal

# --- Persisted project config -----------------------------------------------
# Projects (host, SSH creds, telemetry, systemd services, code-server/claude
# repos) live in a JSON file the app reads AND writes, so adding a new project,
# repo, or service never requires a code change or redeploy. See
# projects.json.example for the schema and a seeded real-world entry.
CONFIG_PATH = os.environ.get(
    "ADMIN_CONSOLE_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects.json"),
)
_config_lock = threading.Lock()


def load_config():
    """Read projects.json fresh. No module-level cache -- the app runs
    threaded=True, and a shared mutable dict reloaded ad-hoc would race with
    concurrent requests. Missing file -> empty project set, not a crash."""
    if not os.path.exists(CONFIG_PATH):
        return {"version": 1, "projects": {}}
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_config(config):
    """Atomic write: temp file in the same directory, then os.replace()."""
    dir_ = os.path.dirname(CONFIG_PATH) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix='.projects.', suffix='.json.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
            f.write('\n')
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def slugify(name, existing):
    """Deterministic, URL/JS-string-safe id from a display name, deduped
    against a set of already-used ids. Never trust a client-supplied id
    directly -- these end up inside inline onclick="...('{{ id }}')" JS-string
    contexts, which Jinja's HTML autoescaping does not protect."""
    base = re.sub(r'[^A-Za-z0-9]+', '-', name.strip()).strip('-').lower() or 'item'
    slug = base
    n = 2
    while slug in existing:
        slug = f"{base}-{n}"
        n += 1
    return slug

def execute_ssh_cmd(host, user, command, key_path=None, timeout=8):
    """Executes a shell command over SSH on a remote node using key-based auth."""
    key_path = key_path or DEFAULT_SSH_KEY
    logger.info("SSH %s@%s: %s", user, host, command)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs = dict(hostname=host, username=user, timeout=timeout)
        if os.path.exists(key_path):
            connect_kwargs['key_filename'] = key_path
        ssh.connect(**connect_kwargs)
        _stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        output = stdout.read().decode('utf-8', errors='replace')
        err = stderr.read().decode('utf-8', errors='replace')
        ssh.close()
        logger.info("SSH %s@%s: done", user, host)
        return True, (output.strip() or err.strip())
    except Exception as e:
        logger.warning("SSH %s@%s failed: %s", user, host, e)
        return False, str(e)


# --- Platform adapter --------------------------------------------------------
# Every SSH command the dashboard generates (service control, reboot, remote-
# control tool sessions, log tailing, doc listing) is dispatched through
# PLATFORMS[project['platform']] instead of being hardcoded to one OS. "linux"
# covers systemd/bash distros generically (Ubuntu, Debian, Raspberry Pi OS,
# etc. -- there is no per-distro adapter, just per-OS-family). "windows" is
# written from PowerShell/Windows-Service documentation but UNVERIFIED -- no
# Windows host has been available to test it against.

def build_session_id(tool, path):
    """Deterministic, shell-safe identifier for a given tool+path combo."""
    slug = re.sub(r'[^A-Za-z0-9]+', '-', path.strip('/')).strip('-').lower()
    return f"{tool}-{slug}"[:50] or f"{tool}-session"


def build_action_cmd(tool_cfg, action, path_literal, port, session, sync_snippet=None):
    """Compose the final remote shell command for start/stop/restart. Restart is
    generic stop-then-start, which naturally satisfies both tool semantics:
    code-server (stop, start) and claude/tmux (kill session, relaunch fresh)."""
    if action == 'stop':
        return tool_cfg['stop_cmd'].format(path=path_literal, port=port, session=session)

    start = tool_cfg['start_cmd'].format(path=path_literal, port=port, session=session)
    if sync_snippet:
        start = f"{sync_snippet} && {start}"

    if action == 'start':
        return start
    if action == 'restart':
        stop = tool_cfg['stop_cmd'].format(path=path_literal, port=port, session=session)
        return f"{stop}; sleep 1; {start}"
    raise ValueError(action)


# --- Linux adapter (bash/systemd) -- refactor of the original code, byte-
# identical command strings, still the only adapter verified against a live
# host (tbot.tail4c9ea5.ts.net). -------------------------------------------

def resolve_trusted_path_linux(path):
    """Rewrite a leading '~' to '$HOME' and double-quote for safe, still-
    expandable use in a remote bash command. ONLY for trusted, admin-authored
    config strings (a project's repo local_path) -- never client input, which
    must keep using shlex.quote."""
    expanded = re.sub(r'^~(?=/|$)', '$HOME', path)
    return f'"{expanded}"'


def git_sync_cmd_linux(git_repo, quoted_local_path):
    """quoted_local_path must already be shell-safe (output of resolve_trusted_path_linux).
    Clones if .git is missing, else fast-forward pulls. Only for start/restart,
    never stop."""
    repo = shlex.quote(git_repo)
    return (
        f'(test -d {quoted_local_path}/.git '
        f'&& git -C {quoted_local_path} pull --ff-only '
        f'|| git clone {repo} {quoted_local_path})'
    )


# {path} = shell-quoted repo path, {port} = bind port, {session} = sanitized session id
LINUX_REMOTE_TOOLS = {
    "code-server": {
        "label": "code-server (browser IDE)",
        "start_cmd": "mkdir -p ~/.rc-logs && nohup code-server --auth none --bind-addr 0.0.0.0:{port} {path} > ~/.rc-logs/{session}.log 2>&1 & disown",
        "stop_cmd": "pkill -f \"code-server --auth none --bind-addr 0.0.0.0:{port}\" || true",
        "log_cmd": "tail -n {lines} ~/.rc-logs/{session}.log 2>/dev/null || echo '(no log yet -- start the session first)'",
        "default_port": 8443,
    },
    "claude": {
        "label": "Claude Code (tmux session)",
        "start_cmd": "tmux new-session -d -s {session} -c {path} 'claude' 2>&1 || tmux has-session -t {session}",
        "stop_cmd": "tmux kill-session -t {session} 2>/dev/null || true",
        # Snapshot of the tmux pane's scrollback -- avoids tmux pipe-pane's toggle
        # semantics, which would risk silently disabling logging on a re-start.
        "log_cmd": "tmux capture-pane -t {session} -p -S -{lines} 2>/dev/null || echo '(no active session -- start it first)'",
        "default_port": None,
    }
}


# --- Windows adapter (PowerShell via OpenSSH Server) -- UNVERIFIED. Written
# from documentation only; no Windows host has been available to test any of
# this against. Treat every command here as a first draft to validate before
# relying on it. ------------------------------------------------------------

def resolve_trusted_path_windows(path):
    """UNVERIFIED. PowerShell has no POSIX tilde expansion, so a leading '~'
    is expanded server-side (to $env:USERPROFILE) before quoting rather than
    left for the remote shell to resolve. Double-quoted for PowerShell, which
    is safe here because local_path is trusted, admin-authored config, never
    client input."""
    expanded = re.sub(r'^~(?=[\\/]|$)', '$env:USERPROFILE', path)
    return f'"{expanded}"'


def git_sync_cmd_windows(git_repo, quoted_local_path):
    """UNVERIFIED. PowerShell equivalent of git_sync_cmd_linux. Requires
    Git-for-Windows on the remote PATH -- a deployment prerequisite this code
    cannot verify."""
    repo = git_repo.replace("'", "''")  # PowerShell single-quote escaping
    return (
        f'if (Test-Path (Join-Path {quoted_local_path} ".git")) '
        f'{{ git -C {quoted_local_path} pull --ff-only }} '
        f"else {{ git clone '{repo}' {quoted_local_path} }}"
    )


def windows_service_cmd(unit, action):
    """UNVERIFIED. No sudoers equivalent exists on Windows -- the SSH user
    needs its own service-control rights (local security policy / a
    restrictive wrapper), which is a deployment decision outside this code."""
    unit_q = unit.replace("'", "''")
    return {
        "start": f"Start-Service -Name '{unit_q}'",
        "stop": f"Stop-Service -Name '{unit_q}' -Force",
        "restart": f"Restart-Service -Name '{unit_q}' -Force",
    }[action]


def windows_log_cmd(service, lines):
    """UNVERIFIED. Prefers a declared log file (service['log_path']) since
    Windows services don't reliably log under an Event Log Source matching
    their service name; falls back to the Application event log as a
    best-effort default."""
    log_path = service.get('log_path')
    if log_path:
        path_q = log_path.replace("'", "''")
        return f"Get-Content -Path '{path_q}' -Tail {lines}"
    unit_q = service['id'].replace("'", "''")
    return f"Get-EventLog -LogName Application -Source '{unit_q}' -Newest {lines} | Format-Table -AutoSize | Out-String -Width 200"


def windows_read_doc_cmd(path_literal, filename):
    """UNVERIFIED. filename is always DOC_FILENAME_RE-validated before this
    is called (see read_doc route), so no further escaping needed there."""
    return f"Get-Content -Path (Join-Path {path_literal} '{filename}') -Raw"


# code-server is genuinely cross-platform (Node.js-based). "claude" is
# intentionally omitted here: it relies on tmux for session persistence,
# which has no Windows port and no tested equivalent yet.
WINDOWS_REMOTE_TOOLS = {
    "code-server": {
        "label": "code-server (browser IDE)",
        "start_cmd": (
            "New-Item -ItemType Directory -Force -Path (Join-Path {path} '.rc-logs') | Out-Null; "
            "Start-Process -WindowStyle Hidden -FilePath code-server "
            "-ArgumentList '--auth','none','--bind-addr','0.0.0.0:{port}','{path}' "
            "-RedirectStandardOutput (Join-Path {path} '.rc-logs\\{session}.log') "
            "-RedirectStandardError (Join-Path {path} '.rc-logs\\{session}.err.log')"
        ),
        "stop_cmd": (
            "Get-CimInstance Win32_Process -Filter \"Name='code-server.exe'\" "
            "| Where-Object { $_.CommandLine -like '*0.0.0.0:{port}*' } "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        ),
        "log_cmd": "Get-Content -Path (Join-Path {path} '.rc-logs\\{session}.log') -Tail {lines} -ErrorAction SilentlyContinue",
        "default_port": 8443,
    },
}

PLATFORMS = {
    "linux": {
        "label": "Linux",
        "resolve_path": resolve_trusted_path_linux,
        "git_sync_cmd": git_sync_cmd_linux,
        "service_cmd": lambda unit, action: f"sudo systemctl {action} {unit}",
        "reboot_cmd": lambda: "sudo reboot",
        "log_cmd_service": lambda service, lines: f"sudo journalctl -u {shlex.quote(service['id'])} -n {JOURNALCTL_LINES} --no-pager",
        "list_docs_cmd": lambda path_literal: f"find {path_literal} -maxdepth 1 -iname '*.md' -type f 2>/dev/null | sort",
        "read_doc_cmd": lambda path_literal, filename: f"cat {path_literal}/{shlex.quote(filename)}",
        "tools": LINUX_REMOTE_TOOLS,
    },
    "windows": {
        "label": "Windows",
        "resolve_path": resolve_trusted_path_windows,
        "git_sync_cmd": git_sync_cmd_windows,
        "service_cmd": windows_service_cmd,
        "reboot_cmd": lambda: "Restart-Computer -Force",
        "log_cmd_service": windows_log_cmd,
        "list_docs_cmd": lambda path_literal: f"Get-ChildItem -Path {path_literal} -Filter *.md -File | Select-Object -ExpandProperty Name | Sort-Object",
        "read_doc_cmd": windows_read_doc_cmd,
        "tools": WINDOWS_REMOTE_TOOLS,
    },
}


# --- Services & Apps discovery -----------------------------------------------
# Services and apps are no longer entered by hand -- they're declared inside a
# project's own repo docs, in a fenced ```services code block containing a
# JSON list, e.g.:
#   ```services
#   [
#     {"type": "service", "id": "tradingbot", "name": "Trading Bot Main Engine"},
#     {"type": "app", "name": "Dashboard", "url": "https://tbot.example.com"}
#   ]
#   ```
# This is scanned out of the same top-level .md files already exposed via the
# Documentation tab, across every repo on the project. Trust boundary is the
# same as everywhere else marked "trusted, admin-authored" in this file: repo
# docs are only as trustworthy as whoever can push to that repo.
SERVICES_BLOCK_RE = re.compile(r'```services[^\n]*\n(.*?)```', re.DOTALL | re.IGNORECASE)


def parse_services_block(md_text):
    """Extract every ```services fenced JSON list found in one doc's text."""
    entries = []
    for match in SERVICES_BLOCK_RE.finditer(md_text):
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            entries.extend(item for item in data if isinstance(item, dict))
    return entries


def normalize_service_entries(raw_entries):
    """Validate and de-duplicate raw ```services entries. Service ids end up
    unquoted in the Linux systemctl command (see PLATFORMS['linux']['service_cmd']),
    so SLUG_RE is a hard requirement here, not just tidiness."""
    seen = set()
    out = []
    for e in raw_entries:
        etype = e.get('type')
        name = (e.get('name') or '').strip()
        if not name:
            continue
        if etype == 'service':
            sid = (e.get('id') or '').strip()
            if not SLUG_RE.match(sid) or ('service', sid) in seen:
                continue
            seen.add(('service', sid))
            entry = {"type": "service", "id": sid, "name": name}
            log_path = e.get('log_path')
            if isinstance(log_path, str) and log_path.strip():
                entry['log_path'] = log_path.strip()
            out.append(entry)
        elif etype == 'app':
            url = (e.get('url') or '').strip()
            if not url or ('app', name, url) in seen:
                continue
            seen.add(('app', name, url))
            out.append({"type": "app", "name": name, "url": url})
    return out


def discover_project_services(project):
    """Scan every repo's top-level .md docs for ```services blocks and return
    the normalized, de-duplicated list of service/app entries for the project."""
    platform = PLATFORMS[project['platform']]
    key_path = project.get('key_path') or DEFAULT_SSH_KEY
    raw_entries = []
    for repo in project.get('repos', []):
        path_literal = platform['resolve_path'](repo['local_path'])
        list_cmd = platform['list_docs_cmd'](path_literal)
        success, output = execute_ssh_cmd(project['host'], project['user'], list_cmd, key_path=key_path)
        if not success:
            continue
        filenames = sorted({os.path.basename(line.strip().replace('\\', '/')) for line in output.splitlines() if line.strip()})
        for filename in filenames:
            if not DOC_FILENAME_RE.match(filename):
                continue
            read_cmd = platform['read_doc_cmd'](path_literal, filename)
            ok, content = execute_ssh_cmd(project['host'], project['user'], read_cmd, key_path=key_path)
            if ok:
                raw_entries.extend(parse_services_block(content))
    return normalize_service_entries(raw_entries)


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Project Fleet Control</title>
    <style>
        :root {
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --row-bg: #263449;
            --accent: #38bdf8;
            --text-main: #f8fafc;
            --text-sub: #94a3b8;
            --border-color: #334155;
            --radius: 3px;
            --btn-start: #16a34a;
            --btn-stop: #dc2626;
            --btn-restart: #ea580c;
            --btn-reboot: #7f1d1d;
            --success-bg: #14532d;
            --success-text: #bbf7d0;
            --error-bg: #450a0a;
            --error-text: #fecaca;
        }
        * { box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg-color); color: var(--text-main); margin: 0; padding: 20px; }
        .container { max-width: 900px; margin: 0 auto; }
        h1 { border-bottom: 2px solid var(--border-color); padding-bottom: 10px; font-size: 1.8rem; font-weight: 700; }
        .card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: var(--radius); padding: 20px; margin-bottom: 20px; }
        .card-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 12px; margin-bottom: 15px; }
        .status-msg { display: none; margin-bottom: 15px; padding: 10px 14px; border-radius: var(--radius); font-size: 0.85rem; border: 1px solid transparent; }
        .status-msg.status-msg-success { display: block; background: var(--success-bg); color: var(--success-text); border-color: var(--btn-start); }
        .status-msg.status-msg-error { display: block; background: var(--error-bg); color: var(--error-text); border-color: var(--btn-stop); }
        .status-msg a { color: inherit; font-weight: 600; }
        .service-row { display: flex; justify-content: space-between; align-items: center; background: var(--row-bg); border: 1px solid var(--border-color); padding: 10px 15px; border-radius: var(--radius); margin-bottom: 10px; }
        .btn-group button { border: none; padding: 8px 12px; border-radius: var(--radius); font-weight: 700; cursor: pointer; color: white; margin-left: 4px; }
        .btn-group button:hover { filter: brightness(1.15); }
        .btn-group button:active { filter: brightness(0.9); }
        .btn-start { background-color: var(--btn-start); }
        .btn-stop { background-color: var(--btn-stop); }
        .btn-restart { background-color: var(--btn-restart); }
        .btn-reboot-row { text-align: right; margin-top: 12px; }
        .btn-reboot { background-color: var(--btn-reboot); color: var(--error-text); border: none; border-radius: var(--radius); padding: 6px 14px; font-size: 0.8rem; font-weight: 700; cursor: pointer; }
        .btn-reboot:hover { filter: brightness(1.15); }
        .reboot-confirm-row { display: none; margin-top: 10px; padding: 12px; background: var(--error-bg); border: 1px solid var(--btn-reboot); border-radius: var(--radius); text-align: right; font-size: 0.85rem; }
        .reboot-confirm-row.visible { display: block; }
        .reboot-confirm-row span { color: var(--error-text); margin-right: 10px; }
        .log-accordion { margin-top: 15px; border: 1px solid var(--border-color); border-radius: var(--radius); }
        .log-accordion summary { cursor: pointer; padding: 10px 15px; background: var(--row-bg); font-size: 1rem; color: var(--accent); font-weight: 700; list-style: none; }
        .log-accordion summary::-webkit-details-marker { display: none; }
        .log-accordion summary::before { content: "\\25b8  "; }
        .log-accordion[open] summary::before { content: "\\25be  "; }
        .log-accordion-body { padding: 12px 15px; }
        .log-output { background: var(--bg-color); color: #cbd5e1; font-family: "Consolas", "Menlo", monospace; font-size: 0.8rem; padding: 12px; border-radius: var(--radius); border: 1px solid var(--border-color); max-height: 320px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin-top: 10px; }
        .docs-tabs { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }
        .doc-tab { background: var(--row-bg); color: var(--text-sub); border: 1px solid var(--border-color); border-radius: var(--radius); padding: 6px 12px; font-size: 0.8rem; cursor: pointer; font-family: inherit; }
        .doc-tab:hover { color: var(--text-main); }
        .doc-tab-active { background: var(--accent); color: var(--bg-color); border-color: var(--accent); font-weight: 700; }
        .docs-output { background: var(--bg-color); color: var(--text-main); font-size: 0.9rem; padding: 12px 16px; border-radius: var(--radius); border: 1px solid var(--border-color); max-height: 420px; overflow-y: auto; margin-top: 10px; }
        .markdown-body { line-height: 1.55; }
        .markdown-body h1, .markdown-body h2, .markdown-body h3, .markdown-body h4 { color: var(--accent); margin: 1em 0 0.4em; }
        .markdown-body h1 { font-size: 1.4rem; }
        .markdown-body h2 { font-size: 1.2rem; }
        .markdown-body h3 { font-size: 1.05rem; }
        .markdown-body h4 { font-size: 0.95rem; }
        .markdown-body p, .markdown-body ul, .markdown-body ol { margin: 0.6em 0; }
        .markdown-body ul, .markdown-body ol { padding-left: 1.4em; }
        .markdown-body a { color: var(--accent); }
        .markdown-body code { background: var(--row-bg); padding: 2px 5px; border-radius: var(--radius); font-family: "Consolas", "Menlo", monospace; font-size: 0.85em; }
        .markdown-body pre { background: var(--row-bg); border: 1px solid var(--border-color); border-radius: var(--radius); padding: 10px; overflow-x: auto; }
        .markdown-body pre code { background: none; padding: 0; }
        .markdown-body table { border-collapse: collapse; margin: 0.6em 0; }
        .markdown-body th, .markdown-body td { border: 1px solid var(--border-color); padding: 6px 10px; }
        .markdown-body blockquote { border-left: 3px solid var(--accent); margin: 0.6em 0; padding-left: 12px; color: var(--text-sub); }
        .markdown-body hr { border: none; border-top: 1px solid var(--border-color); margin: 1em 0; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Project Fleet & Service Admin</h1>

        <div class="btn-reboot-row" style="text-align: left; margin-bottom: 15px;">
            <button class="btn-start" onclick="toggleAddProjectForm()">+ Add Project</button>
        </div>
        <div id="add-project-form" class="card" style="display: none;">
            <h3 style="margin-top: 0; color: var(--accent);">Add Project</h3>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                <input type="text" id="ap-key" placeholder="Key (e.g. NewBot, no spaces)">
                <select id="ap-platform">
                    {% for pid, platform in platforms.items() %}
                    <option value="{{ pid }}">{{ platform.label }}</option>
                    {% endfor %}
                </select>
                <input type="text" id="ap-host" placeholder="Tailscale host">
                <input type="text" id="ap-user" placeholder="SSH user">
                <input type="text" id="ap-key-path" placeholder="SSH key path (optional)">
                <input type="text" id="ap-hardware" placeholder="Hardware label (optional)">
                <input type="text" id="ap-os-label" placeholder="OS label (e.g. Windows 11, Ubuntu 22.04)">
            </div>
            <div class="btn-group" style="margin-top: 10px; margin-left: 0;">
                <button class="btn-start" onclick="submitAddProject()">Create</button>
                <button class="btn-stop" onclick="toggleAddProjectForm()">Cancel</button>
            </div>
            <div id="status-add-project" class="status-msg"></div>
        </div>

        {% for name, project in projects.items() %}
        <div class="card" id="card-{{ name }}">
            <div class="card-header">
                <div>
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <h2 id="name-display-{{ name }}" style="margin: 0; font-size: 1.3rem;">{{ name }}</h2>
                        <button class="doc-tab" style="padding: 2px 8px; font-size: 0.75rem;" onclick="toggleEditProjectName('{{ name }}')" title="Rename project">&#9998;</button>
                    </div>
                    <div id="name-edit-{{ name }}" class="btn-group" style="display: none; margin: 8px 0; margin-left: 0;">
                        <input type="text" id="name-input-{{ name }}" value="{{ name }}" onkeydown="if (event.key === 'Enter') submitRenameProject('{{ name }}'); if (event.key === 'Escape') toggleEditProjectName('{{ name }}');" style="padding: 6px 8px; border-radius: var(--radius); border: 1px solid var(--border-color); background: var(--bg-color); color: var(--text-main);">
                        <button class="btn-start" onclick="submitRenameProject('{{ name }}')">Save</button>
                        <button class="btn-stop" onclick="toggleEditProjectName('{{ name }}')">Cancel</button>
                    </div>
                    {% set extras = [project.hardware, project.os_label] | select | list %}
                    <small style="color: var(--text-sub);">{{ project.user }}@{{ project.host }}{% if extras %} &middot; {{ extras | join(' · ') }}{% endif %}</small>
                </div>
            </div>

            <div id="status-{{ name }}" class="status-msg"></div>

            <h3 style="font-size: 1rem; color: var(--accent);">Services &amp; Apps</h3>
            <div id="services-apps-{{ name }}">
                <p style="color: var(--text-sub); margin: 0;">Loading from repo docs&hellip;</p>
            </div>

            <h3 style="font-size: 1rem; color: var(--accent); margin-top: 15px;">Remote Control (code-server / claude code)</h3>
            {% for repo in project.repos %}
            <div class="service-row" style="flex-wrap: wrap; gap: 8px;">
                <span><strong>{{ repo.name }}</strong><br><code style="color: var(--text-sub);">{{ repo.local_path }}</code><br><small style="color: var(--text-sub);">{{ repo.git_repo }}</small></span>
                <div class="btn-group" style="display: flex; align-items: center; gap: 4px;">
                    <select id="tool-{{ name }}-{{ repo.id }}">
                        {% for tid, tool in platform_tools[project.platform].items() %}
                        <option value="{{ tid }}">{{ tool.label }}</option>
                        {% endfor %}
                    </select>
                    <button class="btn-start" onclick="remoteControl('{{ name }}', '{{ repo.id }}', document.getElementById('tool-{{ name }}-{{ repo.id }}').value, 'start')">Start</button>
                    <button class="btn-stop" onclick="remoteControl('{{ name }}', '{{ repo.id }}', document.getElementById('tool-{{ name }}-{{ repo.id }}').value, 'stop')">Stop</button>
                    <button class="btn-restart" onclick="remoteControl('{{ name }}', '{{ repo.id }}', document.getElementById('tool-{{ name }}-{{ repo.id }}').value, 'restart')">Restart</button>
                    <button class="btn-stop" onclick="armConfirm(this, () => submitDeleteRepo('{{ name }}', '{{ repo.id }}'))">&times;</button>
                </div>
            </div>
            {% endfor %}
            <button class="doc-tab" onclick="toggleAddRepoForm('{{ name }}')">+ Add Repo</button>
            <div id="add-repo-form-{{ name }}" class="service-row" style="display: none; flex-wrap: wrap; gap: 8px; margin-top: 8px;">
                <input type="text" id="new-repo-name-{{ name }}" placeholder="Display name" style="flex: 1; min-width: 140px; padding: 8px; border-radius: var(--radius); border: 1px solid var(--border-color); background: var(--bg-color); color: var(--text-main);">
                <input type="text" id="new-repo-path-{{ name }}" placeholder="Local path" style="flex: 1; min-width: 180px; padding: 8px; border-radius: var(--radius); border: 1px solid var(--border-color); background: var(--bg-color); color: var(--text-main);">
                <input type="text" id="new-repo-git-{{ name }}" placeholder="GitHub URL" style="flex: 1; min-width: 180px; padding: 8px; border-radius: var(--radius); border: 1px solid var(--border-color); background: var(--bg-color); color: var(--text-main);">
                <div class="btn-group" style="margin-left: 0;">
                    <button class="btn-start" onclick="submitAddRepo('{{ name }}')">Add</button>
                    <button class="btn-stop" onclick="toggleAddRepoForm('{{ name }}')">Cancel</button>
                </div>
            </div>

            <details class="log-accordion" ontoggle="if (this.open) loadDocs('{{ name }}')">
                <summary>Documentation</summary>
                <div class="log-accordion-body">
                    {% if project.repos %}
                    <select id="docs-repo-{{ name }}" onchange="loadDocs('{{ name }}')">
                        {% for repo in project.repos %}
                        <option value="{{ repo.id }}">{{ repo.name }}</option>
                        {% endfor %}
                    </select>
                    <div id="docs-tabs-{{ name }}" class="docs-tabs"></div>
                    <div id="docs-content-{{ name }}" class="docs-output markdown-body">Loading...</div>
                    {% else %}
                    <p style="color: var(--text-sub); margin: 0;">Add a repo first.</p>
                    {% endif %}
                </div>
            </details>

            <details class="log-accordion" ontoggle="if (!this.open) stopTailLogs('{{ name }}')">
                <summary>Logs</summary>
                <div class="log-accordion-body">
                    <div class="service-row" style="flex-wrap: wrap; gap: 8px;">
                        <select id="log-source-{{ name }}">
                            <option value="app">Dashboard App Log</option>
                            {% for repo in project.repos %}
                            {% for tid, tool in platform_tools[project.platform].items() %}
                            <option value="tool:{{ tid }}:{{ repo.id }}">{{ tool.label }} log ({{ repo.name }})</option>
                            {% endfor %}
                            {% endfor %}
                        </select>
                        <button id="tail-btn-{{ name }}" class="btn-start" onclick="toggleTailLogs('{{ name }}')">Tail Logs</button>
                    </div>
                    <pre id="log-output-{{ name }}" class="log-output">Pick a source and click "Tail Logs".</pre>
                </div>
            </details>

            <div class="btn-reboot-row">
                <button class="btn-stop" onclick="armConfirm(this, () => submitDeleteProject('{{ name }}'))" style="margin-right: 8px;">Delete Project</button>
                <button class="btn-reboot" onclick="rebootHost('{{ name }}')">⚠️ Reboot Host</button>
            </div>
            <div id="reboot-confirm-{{ name }}" class="reboot-confirm-row">
                <span>Really reboot {{ name }}? This will interrupt any services running on it until it comes back up.</span>
                <button class="btn-stop" onclick="confirmReboot('{{ name }}')">Confirm Reboot</button>
                <button class="btn-start" onclick="cancelReboot('{{ name }}')">Cancel</button>
            </div>
        </div>
        {% endfor %}
    </div>

    <script>
        function escapeHtml(s) {
            const d = document.createElement('div');
            d.textContent = s;
            return d.innerHTML;
        }

        function makeBtn(text, cls, onclick) {
            const b = document.createElement('button');
            b.textContent = text;
            b.className = cls;
            b.onclick = onclick;
            return b;
        }

        async function loadServicesAndApps(project) {
            const container = document.getElementById(`services-apps-${project}`);
            if (!container) return;
            try {
                const res = await fetch(`/api/services/${project}`);
                const data = await res.json();
                if (!data.success) {
                    container.innerHTML = `<p style="color: var(--text-sub); margin: 0;">Error: ${escapeHtml(data.message)}</p>`;
                    return;
                }
                renderServicesAndApps(project, data.services);
            } catch (e) {
                container.innerHTML = `<p style="color: var(--text-sub); margin: 0;">Fetch failed: ${escapeHtml(String(e))}</p>`;
            }
        }

        function renderServicesAndApps(project, entries) {
            const container = document.getElementById(`services-apps-${project}`);
            container.innerHTML = '';
            if (entries.length === 0) {
                container.innerHTML = '<p style="color: var(--text-sub); margin: 0;">No services or apps declared. Add a fenced <code>```services</code> JSON block to a .md doc in a repo attached to this project.</p>';
            }
            entries.forEach(entry => {
                const row = document.createElement('div');
                row.className = 'service-row';
                const label = document.createElement('span');
                if (entry.type === 'service') {
                    label.innerHTML = `<strong>${escapeHtml(entry.name)}</strong> (<code>${escapeHtml(entry.id)}</code>)`;
                } else {
                    label.innerHTML = `<strong>${escapeHtml(entry.name)}</strong> <small style="color: var(--text-sub);">${escapeHtml(entry.url)}</small>`;
                }
                row.appendChild(label);
                const btnGroup = document.createElement('div');
                btnGroup.className = 'btn-group';
                if (entry.type === 'service') {
                    btnGroup.appendChild(makeBtn('Start', 'btn-start', () => manageService(project, entry.id, 'start')));
                    btnGroup.appendChild(makeBtn('Stop', 'btn-stop', () => manageService(project, entry.id, 'stop')));
                    btnGroup.appendChild(makeBtn('Restart', 'btn-restart', () => manageService(project, entry.id, 'restart')));
                } else {
                    const a = document.createElement('a');
                    a.href = entry.url;
                    a.target = '_blank';
                    a.textContent = 'Open';
                    a.className = 'btn-start';
                    a.style.cssText = 'display: inline-block; padding: 8px 12px; border-radius: var(--radius); font-weight: 700; color: white; text-decoration: none; margin-left: 4px;';
                    btnGroup.appendChild(a);
                }
                row.appendChild(btnGroup);
                container.appendChild(row);
            });
            updateLogSourceOptions(project, entries.filter(e => e.type === 'service'));
        }

        function updateLogSourceOptions(project, serviceEntries) {
            const select = document.getElementById(`log-source-${project}`);
            if (!select) return;
            Array.from(select.querySelectorAll('option[data-dynamic="service"]')).forEach(o => o.remove());
            const insertBefore = select.children[1] || null;
            serviceEntries.forEach(entry => {
                const opt = document.createElement('option');
                opt.value = `service:${entry.id}`;
                opt.textContent = `${entry.name} log`;
                opt.dataset.dynamic = 'service';
                select.insertBefore(opt, insertBefore);
            });
        }

        function showStatus(project, message, isError, url) {
            const el = document.getElementById(`status-${project}`);
            if (!el) return;
            el.innerHTML = '';
            el.className = `status-msg ${isError ? 'status-msg-error' : 'status-msg-success'}`;
            const msgSpan = document.createElement('span');
            msgSpan.textContent = message;
            el.appendChild(msgSpan);
            if (url) {
                el.appendChild(document.createTextNode(' — '));
                const a = document.createElement('a');
                a.href = url;
                a.target = '_blank';
                a.textContent = `Open ${url}`;
                el.appendChild(a);
            }
        }

        async function manageService(target, service, action) {
            const res = await fetch('/api/service', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ target, service, action })
            });
            const data = await res.json();
            showStatus(target, data.message, !data.success);
        }

        async function remoteControl(project, repoId, tool, action) {
            const res = await fetch('/api/remote-control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ project, repo_id: repoId, tool, action })
            });
            const data = await res.json();
            const url = (data.url && (action === 'start' || action === 'restart')) ? data.url : null;
            showStatus(project, data.message, !data.success, url);
        }

        function toggleAddProjectForm() {
            const form = document.getElementById('add-project-form');
            form.style.display = form.style.display === 'none' ? 'block' : 'none';
        }

        async function submitAddProject() {
            const body = {
                key: document.getElementById('ap-key').value.trim(),
                platform: document.getElementById('ap-platform').value,
                host: document.getElementById('ap-host').value.trim(),
                user: document.getElementById('ap-user').value.trim(),
                key_path: document.getElementById('ap-key-path').value.trim(),
                hardware: document.getElementById('ap-hardware').value.trim(),
                os_label: document.getElementById('ap-os-label').value.trim(),
            };
            const res = await fetch('/api/projects', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });
            const data = await res.json();
            if (data.success) {
                location.reload();
            } else {
                showStatus('add-project', data.message, true);
            }
        }

        function toggleEditProjectName(project) {
            const editRow = document.getElementById(`name-edit-${project}`);
            const showing = editRow.style.display === 'none';
            editRow.style.display = showing ? 'flex' : 'none';
            if (showing) {
                const input = document.getElementById(`name-input-${project}`);
                input.value = project;
                input.focus();
                input.select();
            }
        }

        async function submitRenameProject(project) {
            const newKey = document.getElementById(`name-input-${project}`).value.trim();
            if (!newKey || newKey === project) {
                toggleEditProjectName(project);
                return;
            }
            const res = await fetch(`/api/projects/${project}/rename`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ new_key: newKey })
            });
            const data = await res.json();
            if (data.success) location.reload();
            else showStatus(project, data.message, true);
        }

        function armConfirm(btn, action) {
            // Two-click inline confirm for delete buttons: first click arms it
            // (label flips to "Confirm?", auto-disarms after 4s), second click
            // while armed runs the action. Keeps every action inline -- no
            // native confirm()/alert() anywhere in this app.
            if (btn.dataset.armed === '1') {
                clearTimeout(Number(btn.dataset.armTimer));
                delete btn.dataset.armed;
                btn.textContent = btn.dataset.label;
                action();
                return;
            }
            btn.dataset.label = btn.textContent;
            btn.dataset.armed = '1';
            btn.textContent = 'Confirm?';
            btn.dataset.armTimer = setTimeout(() => {
                delete btn.dataset.armed;
                btn.textContent = btn.dataset.label;
            }, 4000);
        }

        async function submitDeleteProject(project) {
            const res = await fetch(`/api/projects/${project}`, { method: 'DELETE' });
            const data = await res.json();
            if (data.success) location.reload();
            else showStatus(project, data.message, true);
        }

        function toggleAddRepoForm(project) {
            const form = document.getElementById(`add-repo-form-${project}`);
            form.style.display = form.style.display === 'none' ? 'flex' : 'none';
        }

        async function submitAddRepo(project) {
            const body = {
                name: document.getElementById(`new-repo-name-${project}`).value.trim(),
                local_path: document.getElementById(`new-repo-path-${project}`).value.trim(),
                git_repo: document.getElementById(`new-repo-git-${project}`).value.trim(),
            };
            const res = await fetch(`/api/projects/${project}/repos`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });
            const data = await res.json();
            if (data.success) location.reload();
            else showStatus(project, data.message, true);
        }

        async function submitDeleteRepo(project, repoId) {
            const res = await fetch(`/api/projects/${project}/repos/${repoId}`, { method: 'DELETE' });
            const data = await res.json();
            if (data.success) location.reload();
            else showStatus(project, data.message, true);
        }

        const logPollers = {};  // project name -> setInterval id, while "Tail Logs" is active

        function logUrl(project, source) {
            if (source === 'app') return `/api/logs/app?lines=200`;
            return `/api/logs/${project}/${encodeURIComponent(source)}?lines=200`;
        }

        async function fetchLogOnce(project) {
            const source = document.getElementById(`log-source-${project}`).value;
            const out = document.getElementById(`log-output-${project}`);
            try {
                const res = await fetch(logUrl(project, source));
                const data = await res.json();
                out.textContent = data.success ? data.output : `Error: ${data.message}`;
            } catch (e) {
                out.textContent = `Fetch failed: ${e}`;
            }
            out.scrollTop = out.scrollHeight;
        }

        function stopTailLogs(project) {
            if (logPollers[project]) {
                clearInterval(logPollers[project]);
                delete logPollers[project];
            }
            const btn = document.getElementById(`tail-btn-${project}`);
            if (btn) {
                btn.textContent = 'Tail Logs';
                btn.classList.remove('btn-stop');
                btn.classList.add('btn-start');
            }
        }

        function toggleTailLogs(project) {
            if (logPollers[project]) {
                stopTailLogs(project);
                return;
            }
            fetchLogOnce(project);
            logPollers[project] = setInterval(() => fetchLogOnce(project), 3000);
            const btn = document.getElementById(`tail-btn-${project}`);
            btn.textContent = 'Stop Tailing';
            btn.classList.remove('btn-start');
            btn.classList.add('btn-stop');
        }

        function rebootHost(target) {
            const row = document.getElementById(`reboot-confirm-${target}`);
            if (row) row.classList.add('visible');
        }

        function cancelReboot(target) {
            const row = document.getElementById(`reboot-confirm-${target}`);
            if (row) row.classList.remove('visible');
        }

        async function confirmReboot(target) {
            cancelReboot(target);
            const res = await fetch('/api/reboot', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ target })
            });
            const data = await res.json();
            showStatus(target, data.message, !data.success);
        }

        const docsCache = {};  // "project:repoId" -> { files: [...], content: { filename: html } }

        async function loadDocs(project) {
            const repoSelect = document.getElementById(`docs-repo-${project}`);
            if (!repoSelect) return;  // no repos yet
            const repoId = repoSelect.value;
            const cacheKey = `${project}:${repoId}`;
            const tabsEl = document.getElementById(`docs-tabs-${project}`);
            const contentEl = document.getElementById(`docs-content-${project}`);
            if (docsCache[cacheKey]) {
                renderDocsTabs(project, repoId);
                return;
            }
            contentEl.textContent = 'Loading...';
            try {
                const res = await fetch(`/api/docs/${project}/${repoId}`);
                const data = await res.json();
                if (!data.success) { contentEl.textContent = `Error: ${data.message}`; return; }
                docsCache[cacheKey] = { files: data.files, content: {} };
                if (data.files.length === 0) {
                    tabsEl.innerHTML = '';
                    contentEl.textContent = "(no .md files found in this repo)";
                    return;
                }
                renderDocsTabs(project, repoId);
                selectDoc(project, repoId, data.files[0]);
            } catch (e) {
                contentEl.textContent = `Fetch failed: ${e}`;
            }
        }

        function renderDocsTabs(project, repoId) {
            const tabsEl = document.getElementById(`docs-tabs-${project}`);
            tabsEl.innerHTML = '';
            docsCache[`${project}:${repoId}`].files.forEach(filename => {
                const btn = document.createElement('button');
                btn.textContent = filename;
                btn.className = 'doc-tab';
                btn.onclick = () => selectDoc(project, repoId, filename);
                tabsEl.appendChild(btn);
            });
        }

        async function selectDoc(project, repoId, filename) {
            const contentEl = document.getElementById(`docs-content-${project}`);
            const cacheKey = `${project}:${repoId}`;
            document.querySelectorAll(`#docs-tabs-${project} .doc-tab`).forEach(btn => {
                btn.classList.toggle('doc-tab-active', btn.textContent === filename);
            });
            if (docsCache[cacheKey].content[filename] !== undefined) {
                contentEl.innerHTML = docsCache[cacheKey].content[filename];
                return;
            }
            contentEl.textContent = 'Loading...';
            try {
                const res = await fetch(`/api/docs/${project}/${repoId}/${encodeURIComponent(filename)}`);
                const data = await res.json();
                const html = data.success ? data.html : `<p>Error: ${escapeHtml(data.message)}</p>`;
                docsCache[cacheKey].content[filename] = html;
                contentEl.innerHTML = html;
            } catch (e) {
                contentEl.textContent = `Fetch failed: ${e}`;
            }
        }

        // Initial load of each project's dynamically-discovered Services & Apps
        {% for name in projects.keys() %}
            loadServicesAndApps('{{ name }}');
        {% endfor %}
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    config = load_config()
    platform_tools = {key: adapter['tools'] for key, adapter in PLATFORMS.items()}
    return render_template_string(HTML_TEMPLATE, projects=config['projects'], platforms=PLATFORMS, platform_tools=platform_tools)

@app.route('/api/services/<project_key>', methods=['GET'])
def list_project_services(project_key):
    """Services & Apps for a project, discovered from its repos' docs -- see
    discover_project_services for the ```services fenced-block convention."""
    config = load_config()
    project = config['projects'].get(project_key)
    if project is None:
        return jsonify({"success": False, "message": "Unknown project"}), 404
    return jsonify({"success": True, "services": discover_project_services(project)})

@app.route('/api/service', methods=['POST'])
def service_action():
    data = request.json
    target, service_id, act = data.get('target'), data.get('service'), data.get('action')
    logger.info("service_action: target=%s service=%s action=%s", target, service_id, act)

    config = load_config()
    project = config['projects'].get(target)
    if project is None or act not in ['start', 'stop', 'restart']:
        logger.warning("service_action: rejected invalid request target=%s action=%s", target, act)
        return jsonify({"success": False, "message": "Invalid request"}), 400

    known_ids = {e['id'] for e in discover_project_services(project) if e['type'] == 'service'}
    if service_id not in known_ids:
        logger.warning("service_action: rejected unknown service=%s for target=%s", service_id, target)
        return jsonify({"success": False, "message": "Unknown service for this project"}), 400

    cmd = PLATFORMS[project['platform']]['service_cmd'](service_id, act)
    success, msg = execute_ssh_cmd(
        project['host'], project['user'], cmd,
        key_path=project.get('key_path') or DEFAULT_SSH_KEY,
    )
    logger.info("service_action: target=%s service=%s action=%s success=%s", target, service_id, act, success)
    return jsonify({"success": success, "message": f"{service_id} {act}: {msg or 'Success'}"})

@app.route('/api/reboot', methods=['POST'])
def reboot_action():
    data = request.json
    target = data.get('target')
    logger.info("reboot_action: target=%s", target)
    config = load_config()
    project = config['projects'].get(target)
    if project is None:
        logger.warning("reboot_action: rejected invalid target=%s", target)
        return jsonify({"success": False, "message": "Invalid target"}), 400

    cmd = PLATFORMS[project['platform']]['reboot_cmd']()
    success, msg = execute_ssh_cmd(
        project['host'], project['user'], cmd,
        key_path=project.get('key_path') or DEFAULT_SSH_KEY,
    )
    logger.info("reboot_action: target=%s success=%s", target, success)
    return jsonify({"success": success, "message": f"Reboot sent: {msg or 'Success'}"})

@app.route('/api/remote-control', methods=['POST'])
def remote_control_action():
    data = request.json or {}
    project_key = data.get('project')
    repo_id = data.get('repo_id')
    tool = data.get('tool', 'code-server')
    action = data.get('action')
    port = data.get('port')
    logger.info("remote_control_action: project=%s repo=%s tool=%s action=%s", project_key, repo_id, tool, action)

    config = load_config()
    project = config['projects'].get(project_key)
    if project is None:
        return jsonify({"success": False, "message": "Unknown project"}), 400

    platform = PLATFORMS[project['platform']]
    if tool not in platform['tools']:
        return jsonify({"success": False, "message": "Unknown tool for this project's platform"}), 400
    if action not in ('start', 'stop', 'restart'):
        return jsonify({"success": False, "message": "Invalid action"}), 400

    repo = next((r for r in project.get('repos', []) if r['id'] == repo_id), None)
    if repo is None:
        return jsonify({"success": False, "message": "Unknown repo"}), 400

    tool_cfg = platform['tools'][tool]
    path_literal = platform['resolve_path'](repo['local_path'])
    session = build_session_id(tool, repo['local_path'])
    sync_snippet = platform['git_sync_cmd'](repo['git_repo'], path_literal) if action in ('start', 'restart') else None

    try:
        port = int(port) if port else tool_cfg.get('default_port')
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid port"}), 400

    cmd = build_action_cmd(tool_cfg, action, path_literal, port, session, sync_snippet)
    success, output = execute_ssh_cmd(
        project['host'], project['user'], cmd,
        key_path=project.get('key_path') or DEFAULT_SSH_KEY,
    )
    logger.info("remote_control_action: project=%s repo=%s tool=%s action=%s success=%s", project_key, repo_id, tool, action, success)

    url = None
    if action in ('start', 'restart') and success and tool_cfg.get('default_port') is not None:
        url = f"http://{project['host']}:{port}"

    return jsonify({
        "success": success,
        "message": f"[{project_key}] {tool} {action} ({repo['name']}): {output or 'OK'}",
        "url": url
    })

@app.route('/api/logs/app', methods=['GET'])
def tail_app_log():
    """The dashboard's own log, read locally off disk -- no SSH involved."""
    lines = request.args.get('lines', default=TAIL_LOG_LINES_DEFAULT, type=int) or TAIL_LOG_LINES_DEFAULT
    lines = max(1, min(lines, TAIL_LOG_LINES_MAX))
    try:
        with open(APP_LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
            tail = f.readlines()[-lines:]
        return jsonify({"success": True, "output": ''.join(tail) or "(no log entries yet)"})
    except FileNotFoundError:
        return jsonify({"success": True, "output": "(no log entries yet)"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/logs/<project_key>/<path:source>', methods=['GET'])
def tail_logs(project_key, source):
    """Fetch recent log output for one of a project's remote log sources.

    source is one of:
      "service:<id>"          -- platform-specific service log (journalctl on
                                  Linux, Get-Content/Get-EventLog on Windows)
      "tool:<tool>:<repo_id>" -- code-server's log file / claude's tmux pane
                                  for a specific repo, via SSH
    """
    lines = request.args.get('lines', default=TAIL_LOG_LINES_DEFAULT, type=int) or TAIL_LOG_LINES_DEFAULT
    lines = max(1, min(lines, TAIL_LOG_LINES_MAX))
    logger.info("tail_logs: project=%s source=%s lines=%d", project_key, source, lines)

    config = load_config()
    project = config['projects'].get(project_key)
    if project is None:
        return jsonify({"success": False, "message": "Unknown project"}), 400
    key_path = project.get('key_path') or DEFAULT_SSH_KEY
    platform = PLATFORMS[project['platform']]

    if source.startswith('service:'):
        unit = source.split(':', 1)[1]
        service = next((e for e in discover_project_services(project) if e['type'] == 'service' and e['id'] == unit), None)
        if service is None:
            return jsonify({"success": False, "message": "Unknown service for this project"}), 400
        # Linux side is a fixed line count -- must match the exact-match sudoers grant (no wildcards).
        cmd = platform['log_cmd_service'](service, lines)
    elif source.startswith('tool:'):
        parts = source.split(':', 2)
        if len(parts) != 3:
            return jsonify({"success": False, "message": "Unknown log source"}), 400
        _, tool, repo_id = parts
        if tool not in platform['tools']:
            return jsonify({"success": False, "message": "Unknown tool for this project's platform"}), 400
        repo = next((r for r in project.get('repos', []) if r['id'] == repo_id), None)
        if repo is None:
            return jsonify({"success": False, "message": "Unknown repo"}), 400
        session = build_session_id(tool, repo['local_path'])
        cmd = platform['tools'][tool]['log_cmd'].format(
            path=platform['resolve_path'](repo['local_path']), session=session, lines=lines,
        )
    else:
        return jsonify({"success": False, "message": "Unknown log source"}), 400

    success, output = execute_ssh_cmd(project['host'], project['user'], cmd, key_path=key_path)
    return jsonify({"success": success, "output": output if success else None, "message": None if success else output})

@app.route('/api/docs/<project_key>/<repo_id>', methods=['GET'])
def list_docs(project_key, repo_id):
    """List the .md files in a repo (top-level only), via SSH."""
    config = load_config()
    project = config['projects'].get(project_key)
    if project is None:
        return jsonify({"success": False, "message": "Unknown project"}), 400
    repo = next((r for r in project.get('repos', []) if r['id'] == repo_id), None)
    if repo is None:
        return jsonify({"success": False, "message": "Unknown repo"}), 400

    platform = PLATFORMS[project['platform']]
    path_literal = platform['resolve_path'](repo['local_path'])
    cmd = platform['list_docs_cmd'](path_literal)
    success, output = execute_ssh_cmd(
        project['host'], project['user'], cmd,
        key_path=project.get('key_path') or DEFAULT_SSH_KEY,
    )
    if not success:
        return jsonify({"success": False, "message": output}), 502

    files = sorted({os.path.basename(line.strip().replace('\\', '/')) for line in output.splitlines() if line.strip()})
    return jsonify({"success": True, "files": files})

@app.route('/api/docs/<project_key>/<repo_id>/<filename>', methods=['GET'])
def read_doc(project_key, repo_id, filename):
    """Read one .md file's raw content from a repo, via SSH."""
    if not DOC_FILENAME_RE.match(filename):
        return jsonify({"success": False, "message": "Invalid filename"}), 400

    config = load_config()
    project = config['projects'].get(project_key)
    if project is None:
        return jsonify({"success": False, "message": "Unknown project"}), 400
    repo = next((r for r in project.get('repos', []) if r['id'] == repo_id), None)
    if repo is None:
        return jsonify({"success": False, "message": "Unknown repo"}), 400

    platform = PLATFORMS[project['platform']]
    path_literal = platform['resolve_path'](repo['local_path'])
    cmd = platform['read_doc_cmd'](path_literal, filename)
    success, output = execute_ssh_cmd(
        project['host'], project['user'], cmd,
        key_path=project.get('key_path') or DEFAULT_SSH_KEY,
    )
    if not success:
        return jsonify({"success": False, "message": output}), 502

    html = markdown.markdown(output, extensions=['fenced_code', 'tables'])
    return jsonify({"success": True, "content": output, "html": html})

# --- Project/repo CRUD (persisted to projects.json) --------------------------
# Services and apps are not persisted here -- see discover_project_services.

@app.route('/api/projects', methods=['POST'])
def create_project():
    data = request.json or {}
    key = (data.get('key') or '').strip()
    platform = data.get('platform')
    host = (data.get('host') or '').strip()
    user = (data.get('user') or '').strip()

    if not SLUG_RE.match(key or ''):
        return jsonify({"success": False, "message": "Key is required and may only contain letters, numbers, - and _"}), 400
    if platform not in PLATFORMS:
        return jsonify({"success": False, "message": "Unknown platform"}), 400
    if not host or not user:
        return jsonify({"success": False, "message": "Host and user are required"}), 400

    with _config_lock:
        config = load_config()
        if key in config['projects']:
            return jsonify({"success": False, "message": f"Project '{key}' already exists"}), 409

        project = {
            "platform": platform,
            "host": host,
            "user": user,
            "key_path": (data.get('key_path') or '').strip() or None,
            "hardware": (data.get('hardware') or '').strip(),
            "os_label": (data.get('os_label') or '').strip(),
            "repos": [],
        }

        config['projects'][key] = project
        save_config(config)

    logger.info("create_project: key=%s platform=%s host=%s", key, platform, host)
    return jsonify({"success": True, "project": project})

@app.route('/api/projects/<project_key>/rename', methods=['POST'])
def rename_project(project_key):
    """The project key IS its display name (there's no separate 'name' field),
    so renaming means moving the dict entry to a new key. Same slug rule as
    create_project -- the key is embedded directly in inline onclick JS."""
    data = request.json or {}
    new_key = (data.get('new_key') or '').strip()
    if not SLUG_RE.match(new_key or ''):
        return jsonify({"success": False, "message": "New name is required and may only contain letters, numbers, - and _"}), 400

    with _config_lock:
        config = load_config()
        if project_key not in config['projects']:
            return jsonify({"success": False, "message": "Unknown project"}), 404
        if new_key != project_key and new_key in config['projects']:
            return jsonify({"success": False, "message": f"Project '{new_key}' already exists"}), 409

        project = config['projects'].pop(project_key)
        config['projects'][new_key] = project
        save_config(config)

    logger.info("rename_project: %s -> %s", project_key, new_key)
    return jsonify({"success": True, "key": new_key})

@app.route('/api/projects/<project_key>', methods=['DELETE'])
def delete_project(project_key):
    with _config_lock:
        config = load_config()
        if project_key not in config['projects']:
            return jsonify({"success": False, "message": "Unknown project"}), 404
        del config['projects'][project_key]
        save_config(config)

    logger.info("delete_project: key=%s", project_key)
    return jsonify({"success": True, "message": "Project removed. This does not stop any running remote-control session on the host."})

@app.route('/api/projects/<project_key>/repos', methods=['POST'])
def add_repo(project_key):
    data = request.json or {}
    name = (data.get('name') or '').strip()
    local_path = (data.get('local_path') or '').strip()
    git_repo = (data.get('git_repo') or '').strip()
    if not name or not local_path or not git_repo:
        return jsonify({"success": False, "message": "Name, local path, and GitHub URL are all required"}), 400

    with _config_lock:
        config = load_config()
        project = config['projects'].get(project_key)
        if project is None:
            return jsonify({"success": False, "message": "Unknown project"}), 404
        existing_ids = {r['id'] for r in project.setdefault('repos', [])}
        repo = {"id": slugify(name, existing_ids), "name": name, "local_path": local_path, "git_repo": git_repo}
        project['repos'].append(repo)
        save_config(config)

    logger.info("add_repo: project=%s repo=%s", project_key, repo['id'])
    return jsonify({"success": True, "repo": repo})

@app.route('/api/projects/<project_key>/repos/<repo_id>', methods=['DELETE'])
def delete_repo(project_key, repo_id):
    with _config_lock:
        config = load_config()
        project = config['projects'].get(project_key)
        if project is None:
            return jsonify({"success": False, "message": "Unknown project"}), 404
        before = len(project.get('repos', []))
        project['repos'] = [r for r in project.get('repos', []) if r['id'] != repo_id]
        if len(project['repos']) == before:
            return jsonify({"success": False, "message": "Unknown repo"}), 404
        save_config(config)

    logger.info("delete_repo: project=%s repo=%s", project_key, repo_id)
    return jsonify({"success": True, "message": "Repo removed. This does not stop any running remote-control session on the host."})

if __name__ == '__main__':
    logger.info("Admin console starting on 0.0.0.0:8080")
    # threaded=True -- the dev server is single-request-at-a-time by default, and
    # SSH-backed routes can take a few seconds each. With more than one browser tab
    # open (this dashboard is meant to be used by more than one person), that
    # serializes every request behind whichever one is currently in flight,
    # including plain telemetry polls. Threading fixes that.
    app.run(host='0.0.0.0', port=8080, threaded=True)
