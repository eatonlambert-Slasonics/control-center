(() => {
  "use strict";

  const POLL_INTERVAL_MS = window.POLL_INTERVAL_MS || 8000;
  const FETCH_TIMEOUT_MS = 6000;

  const toastEl = document.getElementById("toast");
  const clockEl = document.getElementById("clock");
  const logModal = document.getElementById("log-modal");
  const logModalTitle = document.getElementById("log-modal-title");
  const logModalBody = document.getElementById("log-modal-body");
  const logModalClose = document.getElementById("log-modal-close");
  const logModalRefresh = document.getElementById("log-modal-refresh");

  let logModalContext = null; // { hostId, unit }

  // ---- helpers -------------------------------------------------------

  async function fetchJSON(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    try {
      const resp = await fetch(url, { ...options, signal: controller.signal });
      let body = null;
      try {
        body = await resp.json();
      } catch (_) {
        // non-JSON or empty body -- fall through with body = null
      }
      return { ok: resp.ok, status: resp.status, body };
    } catch (err) {
      return { ok: false, status: 0, body: null, networkError: err.message || String(err) };
    } finally {
      clearTimeout(timer);
    }
  }

  function showToast(message, kind) {
    toastEl.textContent = message;
    toastEl.className = "toast" + (kind ? ` toast-${kind}` : "");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => toastEl.classList.add("hidden"), 3500);
  }

  function pickField(obj, keys) {
    if (!obj || typeof obj !== "object") return undefined;
    for (const k of keys) {
      if (obj[k] !== undefined && obj[k] !== null) return obj[k];
    }
    return undefined;
  }

  function formatNumber(v, digits = 2) {
    const n = Number(v);
    if (Number.isNaN(n)) return String(v);
    return n.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
  }

  function statusDotClass(state) {
    if (state === "active") return "dot-active";
    if (state === "inactive" || state === "failed" || state === "unreachable") return "dot-inactive";
    if (state === "error") return "dot-inactive";
    return "dot-unknown";
  }

  // ---- systemd status polling ----------------------------------------

  async function refreshHostStatus(hostId) {
    const card = document.querySelector(`.card[data-host-id="${hostId}"]`);
    if (!card) return;

    const { ok, body } = await fetchJSON(`/api/status/${encodeURIComponent(hostId)}`);
    const services = ok && body ? body.services || {} : {};

    card.querySelectorAll(".service-row").forEach((row) => {
      const unit = row.dataset.unit;
      const dot = row.querySelector('[data-role="status-dot"]');
      const state = services[unit] ? services[unit].state : "unreachable";
      dot.className = "status-dot " + statusDotClass(state);
      dot.title = state;
    });
  }

  // ---- live application metrics --------------------------------------

  function renderStatusMetric(container, data) {
    if (!data) return;
    if (!data.ok) {
      container.innerHTML = `<span class="metric metric-error">status: ${escapeHtml(data.error || "unreachable")}</span>`;
      return;
    }
    const d = data.data || {};
    const equity = pickField(d, ["equity", "account_equity", "balance", "total_equity"]);
    const mode = pickField(d, ["mode", "trading_mode", "trade_mode"]);
    const pnl = pickField(d, ["pnl", "pnl_usd", "unrealized_pnl", "total_pnl"]);

    const parts = [];
    if (equity !== undefined) parts.push(`<span class="metric"><span>equity</span><strong>$${formatNumber(equity)}</strong></span>`);
    if (mode !== undefined) parts.push(`<span class="metric"><span>mode</span><strong>${escapeHtml(String(mode))}</strong></span>`);
    if (pnl !== undefined) {
      const cls = Number(pnl) > 0 ? "pnl-positive" : Number(pnl) < 0 ? "pnl-negative" : "";
      parts.push(`<span class="metric ${cls}"><span>pnl</span><strong>${Number(pnl) >= 0 ? "+" : ""}${formatNumber(pnl)}</strong></span>`);
    }
    parts.push(`<span class="metric"><span>latency</span><strong>${data.latency_ms ?? "?"}ms</strong></span>`);

    container.innerHTML = parts.length
      ? parts.join("")
      : `<span class="metric-placeholder">connected, no known fields</span>`;
  }

  function renderPortfolioMetric(container, data) {
    if (!data || !data.ok) return; // status endpoint already communicates the error state
    const d = data.data || {};
    const holdings = pickField(d, ["holdings", "positions", "assets"]);
    const count = Array.isArray(holdings) ? holdings.length : undefined;
    if (count !== undefined) {
      container.insertAdjacentHTML(
        "beforeend",
        `<span class="metric"><span>positions</span><strong>${count}</strong></span>`
      );
    }
  }

  function renderHealthMetric(container, data) {
    if (!data) return;
    const ok = data.ok;
    container.innerHTML =
      `<span class="metric"><span>api</span><strong style="color:${ok ? "var(--green)" : "var(--red)"}">${ok ? "online" : "offline"}</strong></span>` +
      (ok ? `<span class="metric"><span>latency</span><strong>${data.latency_ms ?? "?"}ms</strong></span>` : "");
    if (!ok && data.error) {
      container.insertAdjacentHTML("beforeend", `<span class="metric metric-error">${escapeHtml(data.error)}</span>`);
    }
  }

  function renderTopCoinsMetric(container, data) {
    if (!data || !data.ok) return;
    const d = data.data;
    const list = Array.isArray(d) ? d : pickField(d, ["coins", "top_coins", "results"]);
    if (Array.isArray(list) && list.length) {
      const first = list[0];
      const symbol = typeof first === "object" ? pickField(first, ["symbol", "ticker", "name"]) : first;
      container.insertAdjacentHTML(
        "beforeend",
        `<span class="metric"><span>tracked</span><strong>${list.length}</strong></span>` +
          (symbol ? `<span class="metric"><span>top</span><strong>${escapeHtml(String(symbol))}</strong></span>` : "")
      );
    }
  }

  async function refreshHostMonitor(hostId) {
    const card = document.querySelector(`.card[data-host-id="${hostId}"]`);
    if (!card) return;

    const { ok, body } = await fetchJSON(`/api/monitor/${encodeURIComponent(hostId)}`);
    const monitor = ok && body ? body.monitor || {} : {};

    card.querySelectorAll('.service-row[data-kind="http"]').forEach((row) => {
      const unit = row.dataset.unit;
      const container = row.querySelector('[data-role="metrics"]');
      if (!container) return;

      const endpoints = monitor[unit];
      if (!endpoints || Object.keys(endpoints).length === 0) {
        container.innerHTML = `<span class="metric-placeholder">no endpoint data</span>`;
        return;
      }

      container.innerHTML = "";
      if (endpoints.status) renderStatusMetric(container, endpoints.status);
      if (endpoints.portfolio) renderPortfolioMetric(container, endpoints.portfolio);
      if (endpoints.health) renderHealthMetric(container, endpoints.health);
      if (endpoints.top_coins) renderTopCoinsMetric(container, endpoints.top_coins);

      if (!container.innerHTML) {
        container.innerHTML = `<span class="metric-placeholder">no known fields</span>`;
      }
    });
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ---- service actions -------------------------------------------------

  async function runAction(hostId, unit, action, button) {
    const row = button.closest(".service-row");
    const buttons = row.querySelectorAll("button");
    buttons.forEach((b) => (b.disabled = true));

    const { ok, body, status } = await fetchJSON(
      `/api/service/${encodeURIComponent(hostId)}/${encodeURIComponent(unit)}/${encodeURIComponent(action)}`,
      { method: "POST" }
    );

    buttons.forEach((b) => (b.disabled = false));

    if (ok && body && body.ok) {
      showToast(`${unit}: ${action} succeeded`, "ok");
    } else {
      const reason = (body && body.error) || `HTTP ${status || "network error"}`;
      showToast(`${unit}: ${action} failed -- ${reason}`, "error");
    }

    refreshHostStatus(hostId);
  }

  // ---- logs modal --------------------------------------------------

  async function openLogs(hostId, unit) {
    logModalContext = { hostId, unit };
    logModalTitle.textContent = `${unit} -- last 25 lines`;
    logModalBody.textContent = "loading…";
    logModal.classList.remove("hidden");
    await loadLogs();
  }

  async function loadLogs() {
    if (!logModalContext) return;
    const { hostId, unit } = logModalContext;
    logModalBody.textContent = "loading…";

    const { ok, body } = await fetchJSON(`/api/logs/${encodeURIComponent(hostId)}/${encodeURIComponent(unit)}`);

    if (ok && body && body.ok) {
      logModalBody.textContent = (body.lines || []).join("\n") || "(no log output)";
    } else {
      const reason = (body && body.error) || "could not reach host";
      logModalBody.textContent = `error fetching logs: ${reason}`;
    }
  }

  function closeLogs() {
    logModal.classList.add("hidden");
    logModalContext = null;
  }

  logModalClose.addEventListener("click", closeLogs);
  logModalRefresh.addEventListener("click", loadLogs);
  logModal.addEventListener("click", (e) => {
    if (e.target === logModal) closeLogs();
  });

  // ---- wiring --------------------------------------------------------

  function wireCard(card) {
    const hostId = card.dataset.hostId;
    card.querySelectorAll(".service-row").forEach((row) => {
      const unit = row.dataset.unit;
      row.querySelectorAll(".btn[data-action]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const action = btn.dataset.action;
          if (action === "logs") {
            openLogs(hostId, unit);
          } else {
            runAction(hostId, unit, action, btn);
          }
        });
      });
    });
  }

  function tickClock() {
    clockEl.textContent = new Date().toLocaleTimeString();
  }

  function pollAll() {
    document.querySelectorAll(".card").forEach((card) => {
      const hostId = card.dataset.hostId;
      refreshHostStatus(hostId);
      if (card.querySelector('.service-row[data-kind="http"]')) {
        refreshHostMonitor(hostId);
      }
    });
  }

  function init() {
    document.querySelectorAll(".card").forEach(wireCard);
    tickClock();
    setInterval(tickClock, 1000);
    pollAll();
    setInterval(pollAll, POLL_INTERVAL_MS);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
