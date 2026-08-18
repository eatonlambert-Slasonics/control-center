#!/usr/bin/env bash
# check_tailscale_access.sh -- diagnose why the admin console isn't reachable
# over Tailscale. Run this ON blackBox. Needs sudo for ufw/iptables inspection,
# so run it interactively (it will prompt for your password).

set -uo pipefail

PORT=8080
TS_IP="100.104.42.33"
IFACE="tailscale0"

pass() { echo "[OK]   $1"; }
fail() { echo "[FAIL] $1"; }
info() { echo "[INFO] $1"; }

echo "== 1. Is Flask listening on 0.0.0.0:${PORT}? =="
LISTEN_LINE=$(ss -tlnp 2>/dev/null | awk -v p=":${PORT}" '$4 ~ p')
if [[ -z "$LISTEN_LINE" ]]; then
    fail "Nothing is listening on port ${PORT} at all. Check the service: systemctl status dashboard.service"
else
    echo "$LISTEN_LINE"
    if echo "$LISTEN_LINE" | grep -qE '^\S+\s+\S+\s+\S+\s+(0\.0\.0\.0|\*|\[::\]):'"${PORT}"; then
        pass "Bound to all interfaces (0.0.0.0:${PORT})."
    elif echo "$LISTEN_LINE" | grep -q "127.0.0.1:${PORT}"; then
        fail "Bound to 127.0.0.1 only -- Flask needs host='0.0.0.0' in app.run(). This will not be reachable from Tailscale."
    else
        info "Bound, but check the address above manually -- it's neither 0.0.0.0 nor 127.0.0.1."
    fi
fi

echo
echo "== 2. Is tailscale0 up with the expected IP? =="
if ip -4 addr show "$IFACE" &>/dev/null; then
    ACTUAL_IP=$(ip -4 addr show "$IFACE" | awk '/inet /{print $2}' | cut -d/ -f1)
    if [[ "$ACTUAL_IP" == "$TS_IP" ]]; then
        pass "tailscale0 is up with ${TS_IP}."
    else
        fail "tailscale0 is up but has IP ${ACTUAL_IP}, expected ${TS_IP}. Update TS_IP in this script or check 'tailscale status'."
    fi
else
    fail "tailscale0 interface not found. Is tailscaled running? ('systemctl status tailscaled')"
fi

echo
echo "== 3. Is ufw active, and does it allow tailscale0 / port ${PORT}? =="
if ! command -v ufw &>/dev/null; then
    info "ufw is not installed -- firewall is not the blocker (check iptables/nftables directly, or cloud security groups)."
else
    UFW_STATUS=$(sudo -n ufw status verbose 2>&1)
    if echo "$UFW_STATUS" | grep -qE "password is required|a terminal is required"; then
        info "Can't check ufw rules without a sudo password. Run manually: sudo ufw status verbose"
    elif echo "$UFW_STATUS" | grep -q "Status: inactive"; then
        pass "ufw is inactive -- it is not blocking anything."
    else
        echo "$UFW_STATUS"
        if echo "$UFW_STATUS" | grep -qE "(${PORT}(/tcp)?[[:space:]]+(ALLOW|ALLOW IN)|on ${IFACE}.*ALLOW)"; then
            pass "Found an existing ALLOW rule referencing port ${PORT} or ${IFACE}."
        else
            fail "No ALLOW rule for port ${PORT} or interface ${IFACE} found. This is very likely why Tailscale traffic is being dropped."
            echo "       Fix: sudo ufw allow in on ${IFACE} to any port ${PORT} proto tcp"
        fi
    fi
fi

echo
echo "== 4. Can we reach the app over the Tailscale IP right now? =="
if command -v curl &>/dev/null; then
    if curl -s -o /dev/null -m 3 -w "%{http_code}" "http://${TS_IP}:${PORT}/" | grep -q "200"; then
        pass "curl http://${TS_IP}:${PORT}/ returned 200 -- it's reachable from this box's own Tailscale IP."
    else
        fail "curl to http://${TS_IP}:${PORT}/ did not return 200 (timeout or connection refused/reset)."
        info "If step 1-3 all passed but this still fails, test from a SECOND tailnet device -- loopback-adjacent routing on the same host can mask firewall issues that only bite external traffic."
    fi
fi

echo
echo "== 5. tailscale serve status (optional HTTPS proxy path) =="
if command -v tailscale &>/dev/null; then
    tailscale serve status 2>&1
else
    info "tailscale CLI not found in PATH."
fi

echo
echo "== Summary =="
echo "Run this from a REMOTE tailnet device too:"
echo "  curl -v http://${TS_IP}:${PORT}/"
echo "If step 4 passes locally but the remote curl fails/times out, it's almost certainly ufw's default-deny on tailscale0."
