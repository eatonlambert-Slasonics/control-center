from flask import Flask, render_template_string, jsonify, request
import paramiko
import requests

app = Flask(__name__)

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

def run_remote_cmd(ip, user, command):
    """Executes a bash command over SSH on a target Pi."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(ip, username=user, timeout=3)
        stdin, stdout, stderr = ssh.exec_command(command)
        output = stdout.read().decode('utf-8')
        err = stderr.read().decode('utf-8')
        ssh.close()
        return True, output.strip() or err.strip()
    except Exception as e:
        return False, str(e)

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
    return render_template_string(HTML_TEMPLATE, targets=TARGET_PIS)

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
    
    success, msg = run_remote_cmd(ip, user, cmd)
    return jsonify({"success": success, "message": f"{service} {act}: {msg or 'Success'}"})

@app.route('/api/reboot', methods=['POST'])
def reboot_action():
    data = request.json
    target = data.get('target')
    if target not in TARGET_PIS:
        return jsonify({"success": False, "message": "Invalid target"}), 400
        
    ip = TARGET_PIS[target]['ip']
    user = TARGET_PIS[target]['user']
    
    success, msg = run_remote_cmd(ip, user, "sudo reboot")
    return jsonify({"success": success, "message": f"Reboot sent: {msg or 'Success'}"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
