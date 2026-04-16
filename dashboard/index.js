"use strict";

// ── Constants ──
const DATA_POLL_MS     = 5_000;   // background data fetch — matches bucket size so at most 1 grey cell
const STATUS_POLL_MS   = 30_000;  // status bar + stats header refresh interval
const BUCKET_SECS      = 5;       // seconds averaged into each grid cell
const CELLS_PER_ROW    = 60;      // cells per row
const ROW_SECS         = BUCKET_SECS * CELLS_PER_ROW;  // 300s = 5 min per row
const MAX_GRID_ROWS    = 288;     // cap = 288 × 5 min = 24 h of history
const RTT_GREEN_MAX    = 100;   // ms — ITU-T G.1010 interactive threshold
const RTT_YELLOW_MAX   = 300;   // ms — noticeable lag in real-time apps

// ── Session start (used to grow the grid over time) ──
const pageLoadMs = Date.now();

// ── State ──
let activeTab      = "realtime";
let activeProbe    = "http";
let histProbe      = "http";
let statsHours     = 1;
let resultsCache   = [];
let hourlyCache    = null;
let dailyCache     = null;
let lastStatusTime = 0;
let fetching       = false;
let backendOnline  = null;   // null = unknown (initial), true = online, false = offline

// ── Backend connection badge ──
function renderConnBadge() {
  const badge = document.getElementById("conn-badge");
  if (!badge) return;
  badge.className = "conn-badge";
  if (backendOnline === null) {
    badge.classList.add("conn-unknown");
    badge.textContent = "Connecting…";
  } else if (backendOnline) {
    badge.classList.add("conn-online");
    badge.textContent = "Online";
  } else {
    badge.classList.add("conn-offline");
    badge.textContent = "Offline";
  }
}

// ── API helper ──
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

// ── 5s bucket → CSS color class ──
// b = { ok, total, rtts[] } — averaged over the 5-second window
function bucketClass(b) {
  if (!b || b.total === 0) return "cell-empty";
  const lossFrac = (b.total - b.ok) / b.total;
  if (lossFrac >= 0.5) return "cell-red";
  if (b.rtts.length === 0) return "cell-red";
  const avg = b.rtts.reduce((a, c) => a + c, 0) / b.rtts.length;
  if (avg < RTT_GREEN_MAX)  return "cell-green";
  if (avg < RTT_YELLOW_MAX) return "cell-yellow";
  return "cell-orange";
}

// ── Status bar ──
function renderStatusBar() {
  for (const type of ["http", "webrtc", "dns"]) {
    const ind = document.getElementById(`ind-${type}`);
    if (!ind) continue;

    const ofType = resultsCache.filter(r => r.type === type);
    const last   = ofType.length ? ofType[ofType.length - 1] : null;
    const lastOk = [...ofType].reverse().find(r => r.success);

    const dot = ind.querySelector(".probe-dot");
    dot.className = "probe-dot" + (last ? (last.success ? " dot-up" : " dot-down") : "");

    ind.querySelector(".probe-rtt").textContent =
      (lastOk && lastOk.rtt_ms != null) ? `${Math.round(lastOk.rtt_ms)} ms` : "— ms";
  }
}

// ── Stats header ──
function renderStatsHeader() {
  const rows   = resultsCache.filter(r => r.type === activeProbe);
  const lastOk = [...rows].reverse().find(r => r.success && r.rtt_ms != null);
  const okRtts = rows.filter(r => r.success && r.rtt_ms != null).map(r => r.rtt_ms);
  const total  = rows.length;
  const failed = rows.filter(r => !r.success).length;

  const fms = v => `${Math.round(v)} ms`;

  document.getElementById("sh-last").textContent = lastOk ? fms(lastOk.rtt_ms) : "—";
  document.getElementById("sh-min").textContent  = okRtts.length ? fms(Math.min(...okRtts)) : "—";
  document.getElementById("sh-max").textContent  = okRtts.length ? fms(Math.max(...okRtts)) : "—";
  document.getElementById("sh-avg").textContent  = okRtts.length
    ? fms(okRtts.reduce((a, b) => a + b, 0) / okRtts.length) : "—";
  document.getElementById("sh-loss").textContent = total > 0
    ? `${((failed / total) * 100).toFixed(2)}%` : "—";
}

// ── Period bucket → CSS color class (shared by hourly + daily) ──
// p = { avg_rtt, uptime_pct, packet_loss_pct, ... }
function periodClass(p) {
  if (!p) return "cell-empty";
  if (p.packet_loss_pct >= 50) return "cell-red";
  if (p.avg_rtt == null)       return "cell-red";
  if (p.avg_rtt < RTT_GREEN_MAX)  return "cell-green";
  if (p.avg_rtt < RTT_YELLOW_MAX) return "cell-yellow";
  return "cell-orange";
}

// ── Dynamic row count ──
// Starts at 12 rows (1 h) on first load, grows by 1 row (5 min) per ROW_SECS elapsed,
// capping at MAX_GRID_ROWS (24 h).  Tied to real elapsed time so the grid only
// expands as the session ages — not artificially on page refresh.
function getGridRows() {
  const elapsed = (Date.now() - pageLoadMs) / 1000;
  return Math.min(MAX_GRID_ROWS, 12 + Math.floor(elapsed / ROW_SECS));
}

// ── 5-second averaged grid ──
// Each cell = BUCKET_SECS (5s) average · each row = CELLS_PER_ROW × 5s = 5 min
function renderGrid() {
  const container  = document.getElementById("grid");
  const nowMs      = Date.now();
  const nowSec     = nowMs / 1000;

  // Current row = which 5-minute block we're in
  const currentRow = Math.floor(nowSec / ROW_SECS);
  // Current cell column within the top row (0–59)
  const currentCol = Math.floor((nowSec % ROW_SECS) / BUCKET_SECS);

  // Build bucket map: bucketKey → { ok, total, rtts[] }
  // bucketKey = Math.floor(ts_seconds / BUCKET_SECS)  — unique per 5s window
  const bucketMap = {};
  for (const r of resultsCache) {
    if (r.type !== activeProbe) continue;
    const key = Math.floor(r.ts / (BUCKET_SECS * 1000));
    if (!bucketMap[key]) bucketMap[key] = { ok: 0, total: 0, rtts: [] };
    bucketMap[key].total++;
    if (r.success) {
      bucketMap[key].ok++;
      if (r.rtt_ms != null) bucketMap[key].rtts.push(r.rtt_ms);
    }
  }

  container.innerHTML = "";

  // Update subtitle label ("last hour" → "last 2 hours" → … → "last 24 hours")
  const rows      = getGridRows();
  const totalMins = rows * CELLS_PER_ROW * BUCKET_SECS / 60;
  const subEl     = document.getElementById("grid-subtitle");
  if (subEl) {
    if (totalMins <= 60) {
      subEl.textContent = "last hour";
    } else {
      const h = totalMins / 60;
      subEl.textContent = `last ${Number.isInteger(h) ? h : h.toFixed(1)} hours`;
    }
  }

  for (let rowIdx = 0; rowIdx < rows; rowIdx++) {
    const rowNum   = currentRow - rowIdx;
    const rowStart = rowNum * ROW_SECS * 1000;   // ms

    const rowEl = document.createElement("div");
    rowEl.className = "g-row";

    const labelEl = document.createElement("span");
    labelEl.className   = "g-label";
    labelEl.textContent = new Date(rowStart).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit",
    });
    rowEl.appendChild(labelEl);

    const cellsEl = document.createElement("div");
    cellsEl.className = "g-cells";

    for (let col = 0; col < CELLS_PER_ROW; col++) {
      const bucketKey = rowNum * CELLS_PER_ROW + col;
      const b         = bucketMap[bucketKey];
      const cell      = document.createElement("div");
      cell.className  = "g-cell";

      const isFuture = rowIdx === 0 && col > currentCol;
      if (isFuture) {
        cell.classList.add("cell-empty", "cell-future");
      } else if (b) {
        cell.classList.add(bucketClass(b));
        const ts    = new Date(rowStart + col * BUCKET_SECS * 1000).toLocaleTimeString();
        const tsEnd = new Date(rowStart + (col + 1) * BUCKET_SECS * 1000).toLocaleTimeString();
        const avg   = b.rtts.length
          ? `avg ${Math.round(b.rtts.reduce((a, c) => a + c, 0) / b.rtts.length)} ms`
          : "timeout";
        const loss  = Math.round((b.total - b.ok) / b.total * 100);
        cell.dataset.tip = `${ts}–${tsEnd} · ${avg} · ${loss}% loss (${b.ok}/${b.total})`;
      } else {
        cell.classList.add("cell-empty");
      }

      cellsEl.appendChild(cell);
    }

    rowEl.appendChild(cellsEl);
    container.appendChild(rowEl);
  }
}

// ── 1-second ticker: status countdown only ──
// Grid re-render is driven by fetchData (every 5 s).  currentCol only
// advances on 5-second boundaries anyway (BUCKET_SECS = 5), so there is
// no visual benefit to rebuilding the DOM every second.
setInterval(() => {
  const remaining = Math.max(0, STATUS_POLL_MS - (Date.now() - lastStatusTime));
  const secs = Math.ceil(remaining / 1000);
  const el   = document.getElementById("refresh-countdown");
  if (el) el.textContent = secs <= 0 ? "Checking…" : `Refreshing in ${secs}s`;
}, 1000);

// ── Background data fetch (every 5s) ──
// First call: fetches the last 62 minutes to pre-fill the grid.
// Subsequent calls: fetch only a 3-minute overlap window and merge new rows
// into the existing cache.  This keeps each incremental request tiny (~40 KB)
// while the in-memory cache grows up to 24 h as the session ages.
async function fetchData() {
  if (fetching) return;
  fetching = true;
  try {
    const isFirst = resultsCache.length === 0;
    const minutes = isFirst ? 62 : 3;
    const fresh   = await fetchJSON(`/api/results?minutes=${minutes}`);

    if (isFirst) {
      resultsCache = fresh;
    } else {
      // Merge: add only rows not already in cache (dedup on ts + type)
      const seen = new Set(resultsCache.map(r => `${r.ts}|${r.type}`));
      for (const r of fresh) {
        const key = `${r.ts}|${r.type}`;
        if (!seen.has(key)) { resultsCache.push(r); seen.add(key); }
      }
      // Prune to last 24 h so memory stays bounded
      const cutoff = Date.now() - 24 * 60 * 60 * 1000;
      if (resultsCache.length > 10_000) {
        resultsCache = resultsCache.filter(r => r.ts >= cutoff);
      }
    }

    if (backendOnline !== true)  { backendOnline = true;  renderConnBadge(); }
    renderGrid();
    renderStatusBar();
  } catch (e) {
    console.error("Data fetch failed:", e);
    if (backendOnline !== false) { backendOnline = false; renderConnBadge(); }
  } finally {
    fetching = false;
  }
}

// ── Status bar + stats header refresh (every 30s) ──
function refreshStatus() {
  if (resultsCache.length === 0) return;
  renderStatusBar();
  renderStatsHeader();
  lastStatusTime = Date.now();
}

// ── Historical tab ──
async function loadHistoryTab() {
  try {
    const [stats, outages, hourly, daily] = await Promise.all([
      fetchJSON(`/api/stats?hours=${statsHours}`),
      fetchJSON("/api/outages?days=7"),
      fetchJSON("/api/hourly?hours=24"),
      fetchJSON("/api/daily?days=30"),
    ]);
    renderStats(stats);
    renderOutages(outages);
    hourlyCache = hourly;
    dailyCache  = daily;
    renderHourly(hourly);
    renderDaily(daily);
  } catch (e) {
    console.error("History fetch failed:", e);
  }
}

// ── Hourly strip (24 cells, one per hour) ──
function renderHourly(data) {
  const HOUR_MS = 3_600_000;
  const now     = Date.now();
  const thisHour = Math.floor(now / HOUR_MS) * HOUR_MS;

  // Build hour map: hour_ts → probe stats
  const hourMap = {};
  for (const d of data) hourMap[d.ts] = d[histProbe];

  const strip = document.getElementById("hourly-strip");
  strip.innerHTML = "";

  // Cells row
  const cellsEl = document.createElement("div");
  cellsEl.className = "h-cells";

  // Labels row
  const labelsEl = document.createElement("div");
  labelsEl.className = "h-labels";

  for (let i = 23; i >= 0; i--) {
    const hourTs = thisHour - i * HOUR_MS;
    const stats  = hourMap[hourTs];
    const hour   = new Date(hourTs).getHours();  // local hour 0-23

    const cell = document.createElement("div");
    cell.className = "h-cell " + (stats ? periodClass(stats) : "cell-empty");
    if (stats) {
      const label = new Date(hourTs).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const avg   = stats.avg_rtt != null ? `avg ${Math.round(stats.avg_rtt)} ms` : "no data";
      cell.dataset.tip = `${label} · ${avg} · ${stats.uptime_pct ?? "—"}% up · ${stats.packet_loss_pct ?? "—"}% loss`;
    }
    cellsEl.appendChild(cell);

    const lbl = document.createElement("div");
    lbl.className   = "h-label";
    // Show label every 3 hours; keep others blank so they don't crowd
    lbl.textContent = hour % 3 === 0
      ? new Date(hourTs).toLocaleTimeString([], { hour: "numeric", hour12: true }).replace(":00", "")
      : "";
    labelsEl.appendChild(lbl);
  }

  strip.appendChild(cellsEl);
  strip.appendChild(labelsEl);
}

// ── Daily calendar (GitHub-style week grid) ──
function renderDaily(data) {
  const DAY_MS = 86_400_000;
  const now    = Date.now();
  const todayUTC = Math.floor(now / DAY_MS) * DAY_MS;

  // Build day map: day_ts (UTC midnight) → probe stats
  const dayMap = {};
  for (const d of data) dayMap[d.ts] = d[histProbe];

  const cal = document.getElementById("daily-calendar");
  cal.innerHTML = "";

  // Find the Monday on or before (today − 29 days)
  const oldestDay = todayUTC - 29 * DAY_MS;
  const oldestDOW = new Date(oldestDay).getUTCDay();   // 0=Sun
  const daysToMon = (oldestDOW + 6) % 7;              // 0 if already Monday
  const calStart  = oldestDay - daysToMon * DAY_MS;
  const totalWeeks = Math.ceil((todayUTC - calStart) / DAY_MS / 7) + 1;

  // Column headers
  const header = document.createElement("div");
  header.className = "cal-header";
  header.innerHTML = '<div></div>' +
    ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
      .map(d => `<div class="cal-day-header">${d}</div>`)
      .join("");
  cal.appendChild(header);

  for (let week = 0; week < totalWeeks; week++) {
    const weekStart = calStart + week * 7 * DAY_MS;
    if (weekStart > todayUTC) break;

    const rowEl = document.createElement("div");
    rowEl.className = "cal-row";

    // Row label: month/day of the Monday of this week
    const lblEl = document.createElement("div");
    lblEl.className   = "cal-row-label";
    lblEl.textContent = new Date(weekStart).toLocaleDateString([], { month: "short", day: "numeric" });
    rowEl.appendChild(lblEl);

    for (let dow = 0; dow < 7; dow++) {
      const dayTs = weekStart + dow * DAY_MS;
      const cell  = document.createElement("div");
      cell.className = "cal-cell";

      if (dayTs > todayUTC || dayTs < oldestDay) {
        cell.classList.add("out-of-range");
      } else {
        const stats = dayMap[dayTs];
        cell.classList.add(stats ? periodClass(stats) : "cell-empty");
        if (stats) {
          const date = new Date(dayTs).toLocaleDateString([], { month: "short", day: "numeric" });
          const avg  = stats.avg_rtt != null ? `avg ${Math.round(stats.avg_rtt)} ms` : "no data";
          cell.dataset.tip = `${date} · ${avg} · ${stats.uptime_pct ?? "—"}% up · ${stats.packet_loss_pct ?? "—"}% loss`;
        }
      }

      rowEl.appendChild(cell);
    }
    cal.appendChild(rowEl);
  }
}

function renderStats(stats) {
  const grid  = document.getElementById("stats-grid");
  const types = { http: "HTTP", webrtc: "WebRTC", dns: "DNS" };
  grid.innerHTML = Object.entries(types).map(([key, label]) => {
    const s = stats[key] || {};
    const v = (val, unit = "") => val != null ? `${val}${unit}` : "—";
    const minmax = (s.min_rtt != null && s.max_rtt != null)
      ? `${s.min_rtt} / ${s.max_rtt} ms` : "—";
    return `
      <div class="stat-group">
        <div class="stat-group-title">${label}</div>
        <div class="stat-row"><span class="stat-lbl">Uptime</span>      <span class="stat-val">${v(s.uptime_pct, "%")}</span></div>
        <div class="stat-row"><span class="stat-lbl">Avg RTT</span>     <span class="stat-val">${v(s.avg_rtt, " ms")}</span></div>
        <div class="stat-row"><span class="stat-lbl">Min / Max</span>   <span class="stat-val">${minmax}</span></div>
        <div class="stat-row"><span class="stat-lbl">Packet loss</span> <span class="stat-val">${v(s.packet_loss_pct, "%")}</span></div>
      </div>`;
  }).join("");
}

function formatDuration(s) {
  if (s < 60)   return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function renderOutages(outages) {
  const tbody = document.getElementById("outages-body");
  if (!outages.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty">No outages in the last 7 days</td></tr>`;
    return;
  }
  tbody.innerHTML = [...outages].reverse().map(o => `
    <tr>
      <td>${new Date(o.start).toLocaleString()}</td>
      <td>${new Date(o.end).toLocaleString()}</td>
      <td>${formatDuration(o.duration_s)}</td>
    </tr>`).join("");
}

// ── Tab switching ──
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    activeTab = btn.dataset.tab;
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.add("hidden"));
    document.getElementById(`tab-${activeTab}`).classList.remove("hidden");
    if (activeTab === "history") loadHistoryTab();
  });
});

// ── Probe toggle (HTTP / WebRTC grid) ──
document.querySelectorAll(".probe-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".probe-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    activeProbe = btn.dataset.probe;
    renderStatsHeader();
    renderGrid();
  });
});

// ── Historical probe toggle (HTTP / WebRTC — applies to hourly + daily) ──
document.querySelectorAll(".hist-probe-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".hist-probe-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    histProbe = btn.dataset.probe;
    if (hourlyCache) renderHourly(hourlyCache);
    if (dailyCache)  renderDaily(dailyCache);
  });
});

// ── Window selector (historical tab) ──
document.querySelectorAll(".win-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".win-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    statsHours = parseInt(btn.dataset.hours, 10);
    loadHistoryTab();
  });
});

// ── Tooltip — document-level delegation covers grid + hourly + daily ──
{
  const tip = document.getElementById("tip");

  document.addEventListener("mousemove", e => {
    const cell = e.target.closest("[data-tip]");
    if (!cell) { tip.classList.remove("visible"); return; }
    tip.textContent = cell.dataset.tip;
    tip.classList.add("visible");
    const x = e.clientX + 14;
    const y = e.clientY - 32;
    tip.style.left = Math.min(x, window.innerWidth  - tip.offsetWidth  - 8) + "px";
    tip.style.top  = Math.max(8, y) + "px";
  });

  document.addEventListener("mouseleave", () => tip.classList.remove("visible"));
}

// ── Boot ──
(async () => {
  await fetchData();
  renderStatusBar();
  renderStatsHeader();
  renderGrid();
  lastStatusTime = Date.now();
})();
setInterval(fetchData, DATA_POLL_MS);
setInterval(refreshStatus, STATUS_POLL_MS);
