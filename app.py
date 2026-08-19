from flask import Flask, render_template_string, jsonify, request
import paramiko
import requests
import os
import re
import shlex
import logging
from logging.handlers import RotatingFileHandler

app = Flask(__name__)

DEFAULT_SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")

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

# Each entry is one physical/virtual host + everything the dashboard can do to it:
# telemetry polling, systemd service control, and git-backed remote-control sessions
# (code-server / claude code). Everything is reached over Tailscale.
TARGET_PROJECTS = {
    "Adidas": {
        "host": "tbot.tail4c9ea5.ts.net",   # Tailscale MagicDNS -- SSH + systemctl + reboot
        "user": "tbot",
        "key_path": None,                    # optional per-project SSH key override; None -> DEFAULT_SSH_KEY
        "hardware": "Raspberry Pi 5 x64",     # display-only
        "os": "Linux",                        # display-only

        "api_scheme": "https",
        "api_host": "tbot.tail4c9ea5.ts.net",  # Tailscale-native -- what the dashboard actually polls
        "api_port": 3000,

        # Cloudflare tunnel -- informational only, manual fallback when off-tailnet.
        # Never polled by the backend.
        "public_fallback_url": "https://tbot.eatonlambert.online",

        # "tradingbot-api" is NOT a systemd unit -- api.py runs as a bare background
        # process on this host, not a managed service. Only list real systemd units here.
        "services": [
            {"id": "tradingbot", "name": "Trading Bot Main Engine"}
        ],

        "local_path": "~/adidas",
        "git_repo": "https://github.com/eatonlambert-Slasonics/tbot.git",
    }
}

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


# --- Remote Control (code-server / claude code) ---------------------------

# Each tool defines how to start/stop a background session for a repo path, and
# how to fetch its recent log output ({lines} = clamped int line count).
# {path} = shell-quoted repo path, {port} = bind port, {session} = sanitized session id
REMOTE_TOOLS = {
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


def build_session_id(tool, path):
    """Deterministic, shell-safe identifier for a given tool+path combo."""
    slug = re.sub(r'[^A-Za-z0-9]+', '-', path.strip('/')).strip('-').lower()
    return f"{tool}-{slug}"[:50] or f"{tool}-session"


def resolve_trusted_path(path):
    """Rewrite a leading '~' to '$HOME' and double-quote for safe, still-
    expandable use in a remote shell command. ONLY for trusted, admin-authored
    config strings (TARGET_PROJECTS[...]['local_path']) -- never client input,
    which must keep using shlex.quote."""
    expanded = re.sub(r'^~(?=/|$)', '$HOME', path)
    return f'"{expanded}"'


def git_sync_cmd(git_repo, quoted_local_path):
    """quoted_local_path must already be shell-safe (output of resolve_trusted_path).
    Clones if .git is missing, else fast-forward pulls. Only for project-mode
    start/restart, never stop, never ad-hoc paths."""
    repo = shlex.quote(git_repo)
    return (
        f'(test -d {quoted_local_path}/.git '
        f'&& git -C {quoted_local_path} pull --ff-only '
        f'|| git clone {repo} {quoted_local_path})'
    )


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
        .status-badge { padding: 3px 10px; border-radius: var(--radius); font-size: 0.85rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.02em; }
        .status-active { background: var(--success-bg); color: var(--success-text); }
        .status-stopped { background: var(--error-bg); color: var(--error-text); }
        .status-idle { background: #78350f; color: #fef3c7; }
        .status-msg { display: none; margin-bottom: 15px; padding: 10px 14px; border-radius: var(--radius); font-size: 0.85rem; border: 1px solid transparent; }
        .status-msg.status-msg-success { display: block; background: var(--success-bg); color: var(--success-text); border-color: var(--btn-start); }
        .status-msg.status-msg-error { display: block; background: var(--error-bg); color: var(--error-text); border-color: var(--btn-stop); }
        .status-msg a { color: inherit; font-weight: 600; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1px; margin-bottom: 15px; background: var(--border-color); border: 1px solid var(--border-color); border-radius: var(--radius); overflow: hidden; }
        .stat-item { text-align: center; background: var(--bg-color); padding: 12px; }
        .stat-value { font-size: 1.1rem; font-weight: bold; color: var(--accent); }
        .stat-label { font-size: 0.75rem; color: var(--text-sub); }
        .api-links { font-size: 0.8rem; color: var(--text-sub); margin-top: 2px; }
        .api-links a { color: var(--accent); margin-right: 10px; }
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
    </style>
</head>
<body>
    <div class="container">
        <h1>Project Fleet & Service Admin</h1>
        {% for name, project in projects.items() %}
        <div class="card" id="card-{{ name }}">
            <div class="card-header">
                <div>
                    <h2 style="margin: 0; font-size: 1.3rem;">{{ name }}</h2>
                    <small style="color: var(--text-sub);">{{ project.user }}@{{ project.host }} &middot; {{ project.hardware }} &middot; {{ project.os }}</small>
                    <div class="api-links">
                        API: <a href="{{ project.api_scheme }}://{{ project.api_host }}:{{ project.api_port }}/status" target="_blank">/status</a> <a href="{{ project.api_scheme }}://{{ project.api_host }}:{{ project.api_port }}/portfolio" target="_blank">/portfolio</a>
                        {% if project.public_fallback_url %}
                        &middot; Public fallback (off-tailnet only): <a href="{{ project.public_fallback_url }}" target="_blank">{{ project.public_fallback_url }}</a>
                        {% endif %}
                    </div>
                </div>
                <span id="badge-{{ name }}" class="status-badge status-idle">Checking...</span>
            </div>

            <div id="status-{{ name }}" class="status-msg"></div>

            <div class="stats-grid">
                <div class="stat-item"><div class="stat-value" id="mode-{{ name }}">-</div><div class="stat-label">Mode</div></div>
                <div class="stat-item"><div class="stat-value" id="equity-{{ name }}">-</div><div class="stat-label">Equity</div></div>
                <div class="stat-item"><div class="stat-value" id="pnl-{{ name }}">-</div><div class="stat-label">Unrealized PnL</div></div>
                <div class="stat-item"><div class="stat-value" id="pairs-{{ name }}">-</div><div class="stat-label">Active Pairs</div></div>
            </div>

            <h3 style="font-size: 1rem; color: var(--accent);">Managed Systemd Services</h3>
            {% for service in project.services %}
            <div class="service-row">
                <span><strong>{{ service.name }}</strong> (<code>{{ service.id }}</code>)</span>
                <div class="btn-group">
                    <button class="btn-start" onclick="manageService('{{ name }}', '{{ service.id }}', 'start')">Start</button>
                    <button class="btn-stop" onclick="manageService('{{ name }}', '{{ service.id }}', 'stop')">Stop</button>
                    <button class="btn-restart" onclick="manageService('{{ name }}', '{{ service.id }}', 'restart')">Restart</button>
                </div>
            </div>
            {% endfor %}

            <h3 style="font-size: 1rem; color: var(--accent); margin-top: 15px;">Remote Control (code-server / claude code)</h3>
            <div class="service-row" style="flex-wrap: wrap; gap: 8px;">
                <span><strong>Project repo</strong><br><code style="color: var(--text-sub);">{{ project.local_path }}</code><br><small style="color: var(--text-sub);">{{ project.git_repo }}</small></span>
                <div class="btn-group" style="display: flex; align-items: center; gap: 4px;">
                    <select id="tool-{{ name }}">
                        {% for tid, tool in tools.items() %}
                        <option value="{{ tid }}">{{ tool.label }}</option>
                        {% endfor %}
                    </select>
                    <button class="btn-start" onclick="remoteControl('{{ name }}', '', document.getElementById('tool-{{ name }}').value, 'start')">Start</button>
                    <button class="btn-stop" onclick="remoteControl('{{ name }}', '', document.getElementById('tool-{{ name }}').value, 'stop')">Stop</button>
                    <button class="btn-restart" onclick="remoteControl('{{ name }}', '', document.getElementById('tool-{{ name }}').value, 'restart')">Restart</button>
                </div>
            </div>

            <h3 style="font-size: 0.95rem; color: var(--accent); margin-top: 15px;">Ad-hoc Repository Path</h3>
            <div class="service-row" style="flex-wrap: wrap; gap: 8px;">
                <input type="text" id="adhoc-path-{{ name }}" placeholder="/home/user/some-repo" style="flex: 1; min-width: 200px; padding: 8px; border-radius: var(--radius); border: 1px solid var(--border-color); background: var(--bg-color); color: var(--text-main);">
                <div class="btn-group" style="display: flex; align-items: center; gap: 4px;">
                    <select id="adhoc-tool-{{ name }}">
                        {% for tid, tool in tools.items() %}
                        <option value="{{ tid }}">{{ tool.label }}</option>
                        {% endfor %}
                    </select>
                    <button class="btn-start" onclick="remoteControlAdhoc('{{ name }}', 'start')">Start</button>
                    <button class="btn-stop" onclick="remoteControlAdhoc('{{ name }}', 'stop')">Stop</button>
                    <button class="btn-restart" onclick="remoteControlAdhoc('{{ name }}', 'restart')">Restart</button>
                </div>
            </div>

            <details class="log-accordion" ontoggle="if (this.open) loadDocs('{{ name }}')">
                <summary>Documentation</summary>
                <div class="log-accordion-body">
                    <div id="docs-tabs-{{ name }}" class="docs-tabs"></div>
                    <pre id="docs-content-{{ name }}" class="log-output">Loading...</pre>
                </div>
            </details>

            <details class="log-accordion" ontoggle="if (!this.open) stopTailLogs('{{ name }}')">
                <summary>Logs</summary>
                <div class="log-accordion-body">
                    <div class="service-row" style="flex-wrap: wrap; gap: 8px;">
                        <select id="log-source-{{ name }}">
                            <option value="app">Dashboard App Log</option>
                            {% for service in project.services %}
                            <option value="service:{{ service.id }}">{{ service.name }} (systemd)</option>
                            {% endfor %}
                            {% for tid, tool in tools.items() %}
                            <option value="tool:{{ tid }}">{{ tool.label }} log</option>
                            {% endfor %}
                        </select>
                        <button id="tail-btn-{{ name }}" class="btn-start" onclick="toggleTailLogs('{{ name }}')">Tail Logs</button>
                    </div>
                    <pre id="log-output-{{ name }}" class="log-output">Pick a source and click "Tail Logs".</pre>
                </div>
            </details>

            <div class="btn-reboot-row">
                <button class="btn-reboot" onclick="rebootHost('{{ name }}')">⚠️ Reboot Host</button>
            </div>
            <div id="reboot-confirm-{{ name }}" class="reboot-confirm-row">
                <span>Really reboot {{ name }}? This will interrupt the trading bot until it comes back up.</span>
                <button class="btn-stop" onclick="confirmReboot('{{ name }}')">Confirm Reboot</button>
                <button class="btn-start" onclick="cancelReboot('{{ name }}')">Cancel</button>
            </div>
        </div>
        {% endfor %}
    </div>

    <script>
        async function fetchMetrics(name) {
            try {
                const res = await fetch(`/api/telemetry/${name}`);
                const data = await res.json();
                if (data.success) {
                    const status = data.status.status || 'unknown';
                    const badge = document.getElementById(`badge-${name}`);
                    badge.innerText = status;
                    badge.className = `status-badge status-${status}`;
                    
                    document.getElementById(`mode-${name}`).innerText = data.status.mode || 'N/A';
                    document.getElementById(`pairs-${name}`).innerText = (data.status.trading_pairs || []).length;
                    
                    if (data.portfolio) {
                        document.getElementById(`equity-${name}`).innerText = `$${data.portfolio.equity.toFixed(2)}`;
                        document.getElementById(`pnl-${name}`).innerText = `$${data.portfolio.unrealized_pnl.toFixed(2)}`;
                    }
                }
            } catch (e) {
                console.error("Telemetry fetch failed", e);
            }
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
            setTimeout(() => fetchMetrics(target), 1000);
        }

        async function remoteControl(project, path, tool, action) {
            const res = await fetch('/api/remote-control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ project, path, tool, action })
            });
            const data = await res.json();
            const url = (data.url && (action === 'start' || action === 'restart')) ? data.url : null;
            showStatus(project, data.message, !data.success, url);
        }

        function remoteControlAdhoc(project, action = 'start') {
            const path = document.getElementById(`adhoc-path-${project}`).value.trim();
            const tool = document.getElementById(`adhoc-tool-${project}`).value;
            if (!path) { showStatus(project, 'Enter a repository path first.', true); return; }
            remoteControl(project, path, tool, action);
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

        const docsCache = {};  // project name -> { files: [...], content: { filename: text } }

        async function loadDocs(project) {
            const tabsEl = document.getElementById(`docs-tabs-${project}`);
            const contentEl = document.getElementById(`docs-content-${project}`);
            if (docsCache[project]) {
                renderDocsTabs(project);
                return;
            }
            contentEl.textContent = 'Loading...';
            try {
                const res = await fetch(`/api/docs/${project}`);
                const data = await res.json();
                if (!data.success) { contentEl.textContent = `Error: ${data.message}`; return; }
                docsCache[project] = { files: data.files, content: {} };
                if (data.files.length === 0) {
                    tabsEl.innerHTML = '';
                    contentEl.textContent = "(no .md files found in this project's repo)";
                    return;
                }
                renderDocsTabs(project);
                selectDoc(project, data.files[0]);
            } catch (e) {
                contentEl.textContent = `Fetch failed: ${e}`;
            }
        }

        function renderDocsTabs(project) {
            const tabsEl = document.getElementById(`docs-tabs-${project}`);
            tabsEl.innerHTML = '';
            docsCache[project].files.forEach(filename => {
                const btn = document.createElement('button');
                btn.textContent = filename;
                btn.className = 'doc-tab';
                btn.onclick = () => selectDoc(project, filename);
                tabsEl.appendChild(btn);
            });
        }

        async function selectDoc(project, filename) {
            const contentEl = document.getElementById(`docs-content-${project}`);
            document.querySelectorAll(`#docs-tabs-${project} .doc-tab`).forEach(btn => {
                btn.classList.toggle('doc-tab-active', btn.textContent === filename);
            });
            if (docsCache[project].content[filename] !== undefined) {
                contentEl.textContent = docsCache[project].content[filename];
                return;
            }
            contentEl.textContent = 'Loading...';
            try {
                const res = await fetch(`/api/docs/${project}/${encodeURIComponent(filename)}`);
                const data = await res.json();
                const text = data.success ? data.content : `Error: ${data.message}`;
                docsCache[project].content[filename] = text;
                contentEl.textContent = text;
            } catch (e) {
                contentEl.textContent = `Fetch failed: ${e}`;
            }
        }

        // Initial Load and Auto-Refresh Telemetry Every 10s
        {% for name in projects.keys() %}
            fetchMetrics('{{ name }}');
            setInterval(() => fetchMetrics('{{ name }}'), 10000);
        {% endfor %}
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, projects=TARGET_PROJECTS, tools=REMOTE_TOOLS)

@app.route('/api/telemetry/<target>', methods=['GET'])
def get_telemetry(target):
    if target not in TARGET_PROJECTS:
        return jsonify({"success": False, "message": "Target not found"}), 404

    scheme = TARGET_PROJECTS[target]['api_scheme']
    host = TARGET_PROJECTS[target]['api_host']
    port = TARGET_PROJECTS[target]['api_port']

    try:
        status_res = requests.get(f"{scheme}://{host}:{port}/status", timeout=4).json()
        portfolio_res = requests.get(f"{scheme}://{host}:{port}/portfolio", timeout=4).json()
        return jsonify({"success": True, "status": status_res, "portfolio": portfolio_res})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "status": {"status": "stopped"}})

@app.route('/api/service', methods=['POST'])
def service_action():
    data = request.json
    target, service, act = data.get('target'), data.get('service'), data.get('action')
    logger.info("service_action: target=%s service=%s action=%s", target, service, act)

    if target not in TARGET_PROJECTS or act not in ['start', 'stop', 'restart']:
        logger.warning("service_action: rejected invalid request target=%s action=%s", target, act)
        return jsonify({"success": False, "message": "Invalid request"}), 400

    project = TARGET_PROJECTS[target]
    if service not in {s['id'] for s in project.get('services', [])}:
        logger.warning("service_action: rejected unknown service=%s for target=%s", service, target)
        return jsonify({"success": False, "message": "Unknown service for this project"}), 400

    cmd = f"sudo systemctl {act} {service}"
    success, msg = execute_ssh_cmd(
        project['host'], project['user'], cmd,
        key_path=project.get('key_path') or DEFAULT_SSH_KEY,
    )
    logger.info("service_action: target=%s service=%s action=%s success=%s", target, service, act, success)
    return jsonify({"success": success, "message": f"{service} {act}: {msg or 'Success'}"})

@app.route('/api/reboot', methods=['POST'])
def reboot_action():
    data = request.json
    target = data.get('target')
    logger.info("reboot_action: target=%s", target)
    if target not in TARGET_PROJECTS:
        logger.warning("reboot_action: rejected invalid target=%s", target)
        return jsonify({"success": False, "message": "Invalid target"}), 400

    project = TARGET_PROJECTS[target]
    success, msg = execute_ssh_cmd(
        project['host'], project['user'], "sudo reboot",
        key_path=project.get('key_path') or DEFAULT_SSH_KEY,
    )
    logger.info("reboot_action: target=%s success=%s", target, success)
    return jsonify({"success": success, "message": f"Reboot sent: {msg or 'Success'}"})

@app.route('/api/remote-control', methods=['POST'])
def remote_control_action():
    data = request.json or {}
    project_key = data.get('project')
    path = (data.get('path') or '').strip()
    tool = data.get('tool', 'code-server')
    action = data.get('action')
    port = data.get('port')
    logger.info("remote_control_action: project=%s tool=%s action=%s path=%s", project_key, tool, action, path or '(project repo)')

    if project_key not in TARGET_PROJECTS:
        return jsonify({"success": False, "message": "Unknown project"}), 400
    if tool not in REMOTE_TOOLS:
        return jsonify({"success": False, "message": "Unknown tool"}), 400
    if action not in ('start', 'stop', 'restart'):
        return jsonify({"success": False, "message": "Invalid action"}), 400

    project = TARGET_PROJECTS[project_key]
    host, user = project['host'], project['user']
    key_path = project.get('key_path') or DEFAULT_SSH_KEY
    tool_cfg = REMOTE_TOOLS[tool]
    sync_snippet = None

    if path:
        # Ad-hoc mode: client-supplied path. Conservative -- absolute paths only,
        # always shell-quoted, no tilde support, never git-synced.
        if not path.startswith('/'):
            return jsonify({"success": False, "message": "Repo path must be an absolute path"}), 400
        path_literal = shlex.quote(path)
        session = build_session_id(tool, path)
    else:
        # Project mode: trusted, admin-authored config. Supports '~' and git sync.
        if not project.get('local_path') or not project.get('git_repo'):
            return jsonify({"success": False, "message": "Project has no configured local_path/git_repo"}), 400
        path_literal = resolve_trusted_path(project['local_path'])
        session = build_session_id(tool, project['local_path'])
        if action in ('start', 'restart'):
            sync_snippet = git_sync_cmd(project['git_repo'], path_literal)

    try:
        port = int(port) if port else tool_cfg.get('default_port')
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid port"}), 400

    cmd = build_action_cmd(tool_cfg, action, path_literal, port, session, sync_snippet)
    success, output = execute_ssh_cmd(host, user, cmd, key_path=key_path)
    logger.info("remote_control_action: project=%s tool=%s action=%s success=%s", project_key, tool, action, success)

    url = None
    if action in ('start', 'restart') and success and tool_cfg.get('default_port') is not None:
        url = f"http://{host}:{port}"

    return jsonify({
        "success": success,
        "message": f"[{project_key}] {tool} {action} {'(' + path + ')' if path else '(project repo)'}: {output or 'OK'}",
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
      "service:<id>" -- journalctl for a declared systemd service, via SSH+sudo
      "tool:<id>"    -- code-server's log file / claude's tmux pane, via SSH
    """
    lines = request.args.get('lines', default=TAIL_LOG_LINES_DEFAULT, type=int) or TAIL_LOG_LINES_DEFAULT
    lines = max(1, min(lines, TAIL_LOG_LINES_MAX))
    logger.info("tail_logs: project=%s source=%s lines=%d", project_key, source, lines)

    if project_key not in TARGET_PROJECTS:
        return jsonify({"success": False, "message": "Unknown project"}), 400
    project = TARGET_PROJECTS[project_key]
    key_path = project.get('key_path') or DEFAULT_SSH_KEY

    if source.startswith('service:'):
        unit = source.split(':', 1)[1]
        if unit not in {s['id'] for s in project.get('services', [])}:
            return jsonify({"success": False, "message": "Unknown service for this project"}), 400
        # Fixed line count -- must match the exact-match sudoers grant (no wildcards).
        cmd = f"sudo journalctl -u {shlex.quote(unit)} -n {JOURNALCTL_LINES} --no-pager"
    elif source.startswith('tool:'):
        tool = source.split(':', 1)[1]
        if tool not in REMOTE_TOOLS:
            return jsonify({"success": False, "message": "Unknown tool"}), 400
        if not project.get('local_path'):
            return jsonify({"success": False, "message": "Project has no configured local_path"}), 400
        session = build_session_id(tool, project['local_path'])
        cmd = REMOTE_TOOLS[tool]['log_cmd'].format(session=session, lines=lines)
    else:
        return jsonify({"success": False, "message": "Unknown log source"}), 400

    success, output = execute_ssh_cmd(project['host'], project['user'], cmd, key_path=key_path)
    return jsonify({"success": success, "output": output if success else None, "message": None if success else output})

@app.route('/api/docs/<project_key>', methods=['GET'])
def list_docs(project_key):
    """List the .md files in a project's repo (top-level only), via SSH."""
    if project_key not in TARGET_PROJECTS:
        return jsonify({"success": False, "message": "Unknown project"}), 400
    project = TARGET_PROJECTS[project_key]
    if not project.get('local_path'):
        return jsonify({"success": False, "message": "Project has no configured local_path"}), 400

    path_literal = resolve_trusted_path(project['local_path'])
    cmd = f"find {path_literal} -maxdepth 1 -iname '*.md' -type f 2>/dev/null | sort"
    success, output = execute_ssh_cmd(
        project['host'], project['user'], cmd,
        key_path=project.get('key_path') or DEFAULT_SSH_KEY,
    )
    if not success:
        return jsonify({"success": False, "message": output}), 502

    files = sorted({os.path.basename(line.strip()) for line in output.splitlines() if line.strip()})
    return jsonify({"success": True, "files": files})

@app.route('/api/docs/<project_key>/<filename>', methods=['GET'])
def read_doc(project_key, filename):
    """Read one .md file's raw content from a project's repo, via SSH."""
    if project_key not in TARGET_PROJECTS:
        return jsonify({"success": False, "message": "Unknown project"}), 400
    if not DOC_FILENAME_RE.match(filename):
        return jsonify({"success": False, "message": "Invalid filename"}), 400

    project = TARGET_PROJECTS[project_key]
    if not project.get('local_path'):
        return jsonify({"success": False, "message": "Project has no configured local_path"}), 400

    path_literal = resolve_trusted_path(project['local_path'])
    cmd = f"cat {path_literal}/{shlex.quote(filename)}"
    success, output = execute_ssh_cmd(
        project['host'], project['user'], cmd,
        key_path=project.get('key_path') or DEFAULT_SSH_KEY,
    )
    if not success:
        return jsonify({"success": False, "message": output}), 502

    return jsonify({"success": True, "content": output})

if __name__ == '__main__':
    logger.info("Admin console starting on 0.0.0.0:8080")
    # threaded=True -- the dev server is single-request-at-a-time by default, and
    # SSH-backed routes can take a few seconds each. With more than one browser tab
    # open (this dashboard is meant to be used by more than one person), that
    # serializes every request behind whichever one is currently in flight,
    # including plain telemetry polls. Threading fixes that.
    app.run(host='0.0.0.0', port=8080, threaded=True)
