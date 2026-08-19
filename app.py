from flask import Flask, render_template_string, jsonify, request
import paramiko
import requests
import os
import re
import shlex

app = Flask(__name__)

DEFAULT_SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")

# Configure your target Raspberry Pi(s) running the trading bot
TARGET_PIS = {
    "trading-pi": {
        "ip": "192.168.1.216",  # LAN address -- used for SSH/systemctl control only
        "user": "tbot",         # User running the services
        "api_scheme": "https",
        "api_host": "tbot.tail4c9ea5.ts.net",  # Monitoring API is Tailscale-only (HOST=127.0.0.1 + tailscale serve on the Pi)
        "api_port": 5000,
        "services": [
            {"id": "tradingbot", "name": "Trading Bot Main Engine"},
            {"id": "tradingbot-api", "name": "Trading Bot Monitoring API"},
            {"id": "ngrok-5000", "name": "Ngrok Tunnel (port 5000)"}
        ]
    }
}

def execute_ssh_cmd(host, user, command, key_path=None, timeout=8):
    """Executes a shell command over SSH on a remote node using key-based auth."""
    key_path = key_path or DEFAULT_SSH_KEY
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
        return True, (output.strip() or err.strip())
    except Exception as e:
        return False, str(e)


# --- Remote Control (code-server / claude code) ---------------------------

TARGET_NODES = {
    "blackBox": {
        "host": "100.106.148.3",   # Tailscale IP
        "user": "tone",
        "repos": [
            {"id": "adminConsole", "name": "Admin Console", "path": "/projects/adminConsole"},
        ]
    },
    "trading-pi": {
        "host": "tbot.tail4c9ea5.ts.net",  # MagicDNS name (falls back to LAN IP if unreachable)
        "user": "tbot",
        "repos": [
            {"id": "tradingbot", "name": "Trading Bot", "path": "/home/tbot/tradingbot"},
        ]
    }
}

# Each tool defines how to start/stop a background session for a repo path.
# {path} = shell-quoted repo path, {port} = bind port, {session} = sanitized session id
REMOTE_TOOLS = {
    "code-server": {
        "label": "code-server (browser IDE)",
        "start_cmd": "mkdir -p ~/.rc-logs && nohup code-server --auth none --bind-addr 0.0.0.0:{port} {path} > ~/.rc-logs/{session}.log 2>&1 & disown",
        "stop_cmd": "pkill -f \"code-server --auth none --bind-addr 0.0.0.0:{port}\" || true",
        "default_port": 8443,
    },
    "claude": {
        "label": "Claude Code (tmux session)",
        "start_cmd": "tmux new-session -d -s {session} -c {path} 'claude' 2>&1 || tmux has-session -t {session}",
        "stop_cmd": "tmux kill-session -t {session} 2>/dev/null || true",
        "default_port": None,
    }
}


def build_session_id(tool, path):
    """Deterministic, shell-safe identifier for a given tool+path combo."""
    slug = re.sub(r'[^A-Za-z0-9]+', '-', path.strip('/')).strip('-').lower()
    return f"{tool}-{slug}"[:50] or f"{tool}-session"

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Raspberry Pi Fleet Control</title>
    <style>
        :root {
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --accent: #38bdf8;
            --text-main: #f8fafc;
            --text-sub: #94a3b8;
            --btn-start: #16a34a;
            --btn-stop: #dc2626;
            --btn-restart: #ea580c;
            --btn-reboot: #7f1d1d;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg-color); color: var(--text-main); margin: 0; padding: 20px; }
        .container { max-width: 900px; margin: 0 auto; }
        h1 { border-bottom: 2px solid #334155; padding-bottom: 10px; font-size: 1.8rem; }
        .card { background: var(--card-bg); border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); }
        .card-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; padding-bottom: 12px; margin-bottom: 15px; }
        .status-badge { padding: 4px 10px; border-radius: 20px; font-size: 0.85rem; font-weight: bold; text-transform: uppercase; }
        .status-active { background: #15803d; color: #dcfce7; }
        .status-stopped { background: #991b1b; color: #fee2e2; }
        .status-idle { background: #b45309; color: #fef3c7; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 15px; background: #0f172a; padding: 12px; border-radius: 8px; }
        .stat-item { text-align: center; }
        .stat-value { font-size: 1.1rem; font-weight: bold; color: var(--accent); }
        .stat-label { font-size: 0.75rem; color: var(--text-sub); }
        .service-row { display: flex; justify-content: space-between; align-items: center; background: #334155; padding: 10px 15px; border-radius: 6px; margin-bottom: 10px; }
        .btn-group button { border: none; padding: 8px 12px; border-radius: 4px; font-weight: bold; cursor: pointer; color: white; margin-left: 4px; transition: opacity 0.2s; }
        .btn-group button:hover { opacity: 0.85; }
        .btn-start { background-color: var(--btn-start); }
        .btn-stop { background-color: var(--btn-stop); }
        .btn-restart { background-color: var(--btn-restart); }
        .btn-reboot-row { text-align: right; margin-top: 12px; }
        .btn-reboot { background-color: var(--btn-reboot); color: #fee2e2; border: none; border-radius: 6px; padding: 6px 14px; font-size: 0.8rem; font-weight: 600; cursor: pointer; transition: opacity 0.2s; }
        .btn-reboot:hover { opacity: 0.85; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Raspberry Pi Fleet & Service Admin</h1>
        {% for name, target in targets.items() %}
        <div class="card" id="card-{{ name }}">
            <div class="card-header">
                <div>
                    <h2 style="margin: 0; font-size: 1.3rem;">{{ name }}</h2>
                    <small style="color: var(--text-sub);">IP: {{ target.ip }}</small>
                </div>
                <span id="badge-{{ name }}" class="status-badge status-idle">Checking...</span>
            </div>

            <div class="stats-grid">
                <div class="stat-item"><div class="stat-value" id="mode-{{ name }}">-</div><div class="stat-label">Mode</div></div>
                <div class="stat-item"><div class="stat-value" id="equity-{{ name }}">-</div><div class="stat-label">Equity</div></div>
                <div class="stat-item"><div class="stat-value" id="pnl-{{ name }}">-</div><div class="stat-label">Unrealized PnL</div></div>
                <div class="stat-item"><div class="stat-value" id="pairs-{{ name }}">-</div><div class="stat-label">Active Pairs</div></div>
            </div>

            <h3 style="font-size: 1rem; color: var(--accent);">Managed Systemd Services</h3>
            {% for service in target.services %}
            <div class="service-row">
                <span><strong>{{ service.name }}</strong> (<code>{{ service.id }}</code>)</span>
                <div class="btn-group">
                    <button class="btn-start" onclick="manageService('{{ name }}', '{{ service.id }}', 'start')">Start</button>
                    <button class="btn-stop" onclick="manageService('{{ name }}', '{{ service.id }}', 'stop')">Stop</button>
                    <button class="btn-restart" onclick="manageService('{{ name }}', '{{ service.id }}', 'restart')">Restart</button>
                </div>
            </div>
            {% endfor %}

            <div class="btn-reboot-row">
                <button class="btn-reboot" onclick="rebootHost('{{ name }}')">⚠️ Reboot Host</button>
            </div>
        </div>
        {% endfor %}

        <h1 style="margin-top: 40px;">Remote Control Sessions</h1>
        {% for name, node in nodes.items() %}
        <div class="card">
            <div class="card-header">
                <div>
                    <h2 style="margin: 0; font-size: 1.3rem;">{{ name }}</h2>
                    <small style="color: var(--text-sub);">{{ node.user }}@{{ node.host }}</small>
                </div>
            </div>

            {% for repo in node.repos %}
            <div class="service-row" style="flex-wrap: wrap; gap: 8px;">
                <span><strong>{{ repo.name }}</strong><br><code style="color: var(--text-sub);">{{ repo.path }}</code></span>
                <div class="btn-group" style="display: flex; align-items: center; gap: 4px;">
                    <select id="tool-{{ name }}-{{ repo.id }}">
                        {% for tid, tool in tools.items() %}
                        <option value="{{ tid }}">{{ tool.label }}</option>
                        {% endfor %}
                    </select>
                    <button class="btn-start" onclick="remoteControl('{{ name }}', '{{ repo.path }}', document.getElementById('tool-{{ name }}-{{ repo.id }}').value, 'start')">Start</button>
                    <button class="btn-stop" onclick="remoteControl('{{ name }}', '{{ repo.path }}', document.getElementById('tool-{{ name }}-{{ repo.id }}').value, 'stop')">Stop</button>
                </div>
            </div>
            {% endfor %}

            <h3 style="font-size: 0.95rem; color: var(--accent); margin-top: 15px;">Ad-hoc Repository Path</h3>
            <div class="service-row" style="flex-wrap: wrap; gap: 8px;">
                <input type="text" id="adhoc-path-{{ name }}" placeholder="/home/user/some-repo" style="flex: 1; min-width: 200px; padding: 8px; border-radius: 4px; border: 1px solid #475569; background: #0f172a; color: var(--text-main);">
                <div class="btn-group" style="display: flex; align-items: center; gap: 4px;">
                    <select id="adhoc-tool-{{ name }}">
                        {% for tid, tool in tools.items() %}
                        <option value="{{ tid }}">{{ tool.label }}</option>
                        {% endfor %}
                    </select>
                    <button class="btn-start" onclick="remoteControlAdhoc('{{ name }}')">Start</button>
                    <button class="btn-stop" onclick="remoteControlAdhoc('{{ name }}', 'stop')">Stop</button>
                </div>
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

        async function manageService(target, service, action) {
            const res = await fetch('/api/service', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ target, service, action })
            });
            const data = await res.json();
            alert(data.message);
            setTimeout(() => fetchMetrics(target), 1000);
        }

        async function remoteControl(host, path, tool, action) {
            const res = await fetch('/api/remote-control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ host, path, tool, action })
            });
            const data = await res.json();
            alert(data.message);
            if (data.url && action === 'start') {
                if (confirm(`Open ${data.url} in a new tab?`)) window.open(data.url, '_blank');
            }
        }

        function remoteControlAdhoc(host, action = 'start') {
            const path = document.getElementById(`adhoc-path-${host}`).value.trim();
            const tool = document.getElementById(`adhoc-tool-${host}`).value;
            if (!path) { alert('Enter a repository path first.'); return; }
            remoteControl(host, path, tool, action);
        }

        async function rebootHost(target) {
            if (!confirm(`Are you sure you want to reboot ${target}?`)) return;
            const res = await fetch('/api/reboot', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ target })
            });
            const data = await res.json();
            alert(data.message);
        }

        // Initial Load and Auto-Refresh Telemetry Every 10s
        {% for name in targets.keys() %}
            fetchMetrics('{{ name }}');
            setInterval(() => fetchMetrics('{{ name }}'), 10000);
        {% endfor %}
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, targets=TARGET_PIS, nodes=TARGET_NODES, tools=REMOTE_TOOLS)

@app.route('/api/telemetry/<target>', methods=['GET'])
def get_telemetry(target):
    if target not in TARGET_PIS:
        return jsonify({"success": False, "message": "Target not found"}), 404
    
    scheme = TARGET_PIS[target]['api_scheme']
    host = TARGET_PIS[target]['api_host']
    port = TARGET_PIS[target]['api_port']

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
    
    if target not in TARGET_PIS or act not in ['start', 'stop', 'restart']:
        return jsonify({"success": False, "message": "Invalid request"}), 400
        
    ip = TARGET_PIS[target]['ip']
    user = TARGET_PIS[target]['user']
    cmd = f"sudo systemctl {act} {service}"

    success, msg = execute_ssh_cmd(ip, user, cmd)
    return jsonify({"success": success, "message": f"{service} {act}: {msg or 'Success'}"})

@app.route('/api/reboot', methods=['POST'])
def reboot_action():
    data = request.json
    target = data.get('target')
    if target not in TARGET_PIS:
        return jsonify({"success": False, "message": "Invalid target"}), 400
        
    ip = TARGET_PIS[target]['ip']
    user = TARGET_PIS[target]['user']
    
    success, msg = execute_ssh_cmd(ip, user, "sudo reboot")
    return jsonify({"success": success, "message": f"Reboot sent: {msg or 'Success'}"})

@app.route('/api/remote-control', methods=['POST'])
def remote_control_action():
    data = request.json or {}
    host_key = data.get('host')
    path = (data.get('path') or '').strip()
    tool = data.get('tool', 'code-server')
    action = data.get('action')
    port = data.get('port')

    if host_key not in TARGET_NODES:
        return jsonify({"success": False, "message": "Unknown host"}), 400
    if tool not in REMOTE_TOOLS:
        return jsonify({"success": False, "message": "Unknown tool"}), 400
    if action not in ('start', 'stop'):
        return jsonify({"success": False, "message": "Invalid action"}), 400
    if not path.startswith('/'):
        return jsonify({"success": False, "message": "Repo path must be an absolute path"}), 400

    node = TARGET_NODES[host_key]
    host, user = node['host'], node['user']
    key_path = node.get('key_path', DEFAULT_SSH_KEY)

    tool_cfg = REMOTE_TOOLS[tool]
    quoted_path = shlex.quote(path)
    session = build_session_id(tool, path)

    try:
        port = int(port) if port else tool_cfg.get('default_port')
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid port"}), 400

    template = tool_cfg['start_cmd'] if action == 'start' else tool_cfg['stop_cmd']
    cmd = template.format(path=quoted_path, port=port, session=session)

    success, output = execute_ssh_cmd(host, user, cmd, key_path=key_path)

    url = None
    if action == 'start' and success and tool_cfg.get('default_port') is not None:
        url = f"http://{host}:{port}"

    return jsonify({
        "success": success,
        "message": f"[{host_key}] {tool} {action} '{path}': {output or 'OK'}",
        "url": url
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
