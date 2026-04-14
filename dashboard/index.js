"use strict";

const STATS_POLL_MS   = 5_000;
const RESULTS_POLL_MS = 10_000;
const OUTAGES_POLL_MS = 60_000;
const CURRENT_WINDOW_MS = 10_000; // "is it up right now?" window

let statsHours = 1;

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${resp.status} ${url}`);
  return resp.json();
}

const getStats   = () => fetchJSON(`/api/stats?hours=${statsHours}`);
const getResults = () => fetchJSON(`/api/results?minutes=1440`);  // 24h
const getOutages = () => fetchJSON(`/api/outages?days=7`);

// ---------------------------------------------------------------------------
// Status cards
// ---------------------------------------------------------------------------

function updateCards(results) {
  const now = Date.now();
  const recent = results.filter(r => now - r.ts <= CURRENT_WINDOW_MS);

  for (const type of ["http", "webrtc", "dns"]) {
    const card = document.getElementById(`card-${type}`);
    if (!card) continue;

    const ofType = recent.filter(r => r.type === type);
    const lastOk = [...ofType].reverse().find(r => r.success);
    const last   = [...ofType].reverse().find(() => true); // most recent of any kind

    card.classList.remove("up", "down");
    if (last) card.classList.add(last.success ? "up" : "down");

    const rttEl = card.querySelector(".card-rtt");
    rttEl.textContent = (lastOk && lastOk.rtt_ms != null)
      ? `${Math.round(lastOk.rtt_ms)} ms`
      : "— ms";
  }
}

// ---------------------------------------------------------------------------
// Stats grid
// ---------------------------------------------------------------------------

function updateStats(stats) {
  const grid = document.getElementById("stats-grid");
  const types = { http: "HTTP", webrtc: "WebRTC", dns: "DNS" };

  grid.innerHTML = Object.entries(types).map(([key, label]) => {
    const s = stats[key] || {};
    const val = (v, unit = "") => (v != null ? `${v}${unit}` : "—");
    const minmax = (s.min_rtt != null && s.max_rtt != null)
      ? `${s.min_rtt} / ${s.max_rtt} ms`
      : "—";

    return `
      <div class="stat-group">
        <div class="stat-group-title">${label}</div>
        <div class="stat-row"><span class="stat-lbl">Uptime</span>      <span class="stat-val">${val(s.uptime_pct, "%")}</span></div>
        <div class="stat-row"><span class="stat-lbl">Avg RTT</span>     <span class="stat-val">${val(s.avg_rtt, " ms")}</span></div>
        <div class="stat-row"><span class="stat-lbl">Min / Max</span>   <span class="stat-val">${minmax}</span></div>
        <div class="stat-row"><span class="stat-lbl">Packet loss</span> <span class="stat-val">${val(s.packet_loss_pct, "%")}</span></div>
      </div>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// 24h timeline  (1 cell = 1 minute, newest cell on the right)
// ---------------------------------------------------------------------------

function updateTimeline(results) {
  const container = document.getElementById("timeline");
  const now = Date.now();
  const MINUTES = 24 * 60;
  const MS_PER_MIN = 60_000;

  // Aggregate HTTP + WebRTC results into per-minute buckets
  const buckets = Array.from({ length: MINUTES }, () => ({ ok: 0, total: 0 }));

  for (const r of results) {
    if (r.type !== "http" && r.type !== "webrtc") continue;
    const ageMin = Math.floor((now - r.ts) / MS_PER_MIN);
    if (ageMin < 0 || ageMin >= MINUTES) continue;
    const b = buckets[MINUTES - 1 - ageMin]; // index 0 = oldest minute
    b.total++;
    if (r.success) b.ok++;
  }

  container.innerHTML = "";
  for (let i = 0; i < MINUTES; i++) {
    const b = buckets[i];
    const cell = document.createElement("div");
    cell.className = "t-cell";

    if (b.total > 0) {
      const lossFrac = (b.total - b.ok) / b.total;
      if      (lossFrac < 0.10) cell.classList.add("good");
      else if (lossFrac < 0.50) cell.classList.add("degraded");
      else                       cell.classList.add("down");

      const minutesAgo = MINUTES - 1 - i;
      const ts = new Date(now - minutesAgo * MS_PER_MIN);
      cell.title = `${ts.toLocaleTimeString()} — loss ${Math.round(lossFrac * 100)}% (${b.ok}/${b.total} OK)`;
    }

    container.appendChild(cell);
  }
}

// ---------------------------------------------------------------------------
// Outages table
// ---------------------------------------------------------------------------

function formatDuration(seconds) {
  if (seconds < 60)   return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function updateOutages(outages) {
  const tbody = document.getElementById("outages-body");
  if (!outages.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty">No outages detected in the last 7 days</td></tr>`;
    return;
  }

  tbody.innerHTML = [...outages].reverse().map(o => {
    const start = new Date(o.start).toLocaleString();
    const end   = new Date(o.end).toLocaleString();
    return `<tr>
      <td>${start}</td>
      <td>${end}</td>
      <td>${formatDuration(o.duration_s)}</td>
    </tr>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

function setUpdated() {
  document.getElementById("last-updated").textContent =
    "Updated " + new Date().toLocaleTimeString();
}

async function refreshResults() {
  try {
    const results = await getResults();
    updateCards(results);
    updateTimeline(results);
    setUpdated();
  } catch (e) {
    console.error("Results fetch failed:", e);
  }
}

async function refreshStats() {
  try {
    const stats = await getStats();
    updateStats(stats);
  } catch (e) {
    console.error("Stats fetch failed:", e);
  }
}

async function refreshOutages() {
  try {
    const outages = await getOutages();
    updateOutages(outages);
  } catch (e) {
    console.error("Outages fetch failed:", e);
  }
}

// Window selector buttons
document.querySelectorAll(".win-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".win-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    statsHours = parseInt(btn.dataset.hours, 10);
    refreshStats();
  });
});

// Initial load
refreshResults();
refreshStats();
refreshOutages();

// Polling intervals
setInterval(refreshResults, RESULTS_POLL_MS);
setInterval(refreshStats,   STATS_POLL_MS);
setInterval(refreshOutages, OUTAGES_POLL_MS);
