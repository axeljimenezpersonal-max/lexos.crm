/* Habeas Corpus — main.js */
"use strict";

// ── Topbar dropdowns (notifications + user avatar) ────────────────────────────
function toggleTopbarDropdown(id, triggerEl) {
  const dropdown = document.getElementById(id);
  if (!dropdown) return;
  const isOpen = dropdown.classList.contains("open");
  closeAllDropdowns();
  if (!isOpen) dropdown.classList.add("open");
}

function closeAllDropdowns() {
  document.querySelectorAll(".tb-dropdown.open").forEach(d => d.classList.remove("open"));
}

(function () {
  const notifToggle = document.getElementById("notifToggle");
  const tbUserToggle = document.getElementById("tbUserToggle");

  notifToggle?.addEventListener("click", e => {
    e.stopPropagation();
    toggleTopbarDropdown("notifDropdown");
  });
  tbUserToggle?.addEventListener("click", e => {
    e.stopPropagation();
    toggleTopbarDropdown("tbUserDropdown");
  });

  document.addEventListener("click", e => {
    const wraps = document.querySelectorAll(".tb-dd-wrap");
    let inside = false;
    wraps.forEach(w => { if (w.contains(e.target)) inside = true; });
    if (!inside) closeAllDropdowns();
  });
})();

// ── Flash auto-dismiss ────────────────────────────────────────────────────────
(function () {
  document.querySelectorAll(".alert[data-auto-dismiss]").forEach(el => {
    const delay = parseInt(el.dataset.autoDismiss) || 4000;
    setTimeout(() => {
      el.style.transition = "opacity .4s";
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 400);
    }, delay);
  });
})();

// ── Active nav item ───────────────────────────────────────────────────────────
(function () {
  const path = window.location.pathname;
  document.querySelectorAll(".nav-item").forEach(link => {
    const href = link.getAttribute("href");
    if (!href) return;
    if (href === "/" && path === "/") link.classList.add("active");
    else if (href !== "/" && path.startsWith(href)) link.classList.add("active");
  });
})();

// ── Tabs ──────────────────────────────────────────────────────────────────────
(function () {
  document.querySelectorAll("[data-tab-target]").forEach(btn => {
    btn.addEventListener("click", () => {
      const group = btn.dataset.tabGroup || "default";
      document.querySelectorAll(`[data-tab-group="${group}"]`).forEach(b => b.classList.remove("active"));
      document.querySelectorAll(`[data-tab-content="${group}"]`).forEach(c => c.classList.remove("active"));
      btn.classList.add("active");
      const target = document.getElementById(btn.dataset.tabTarget);
      if (target) target.classList.add("active");
    });
  });
  const groups = new Set();
  document.querySelectorAll("[data-tab-group]").forEach(el => groups.add(el.dataset.tabGroup));
  groups.forEach(group => {
    const first = document.querySelector(`[data-tab-group="${group}"]`);
    if (first && !document.querySelector(`[data-tab-group="${group}"].active`)) first.click();
  });
})();

// ── Confirm delete / action ───────────────────────────────────────────────────
document.querySelectorAll("[data-confirm]").forEach(form => {
  form.addEventListener("submit", e => {
    const msg = form.dataset.confirm || "¿Confirmar esta acción?";
    if (!confirm(msg)) e.preventDefault();
  });
});

// ── Relative time ─────────────────────────────────────────────────────────────
function relativeTime(dateStr) {
  const d = new Date(dateStr);
  if (isNaN(d)) return dateStr;
  const diff = Math.floor((Date.now() - d) / 1000);
  if (diff < 60)     return "Ahora mismo";
  if (diff < 3600)   return `Hace ${Math.floor(diff / 60)} min`;
  if (diff < 86400)  return `Hace ${Math.floor(diff / 3600)} h`;
  if (diff < 604800) return `Hace ${Math.floor(diff / 86400)} días`;
  return d.toLocaleDateString("es-MX", { day: "2-digit", month: "short" });
}
document.querySelectorAll("[data-reltime]").forEach(el => {
  el.textContent = relativeTime(el.dataset.reltime);
  el.title = new Date(el.dataset.reltime).toLocaleString("es-MX");
});

// ── Money format ──────────────────────────────────────────────────────────────
function formatMXN(n) {
  return new Intl.NumberFormat("es-MX", { style: "currency", currency: "MXN" }).format(n || 0);
}

// ── Mini week calendar ────────────────────────────────────────────────────────
window.MiniCal = {
  el: null,
  offset: 0,   // week offset from current
  events: [],  // [{date: "YYYY-MM-DD", tipo: "audiencia"}]

  init(containerId, eventsJson) {
    this.el = document.getElementById(containerId);
    if (!this.el) return;
    try { this.events = eventsJson || []; } catch(_) { this.events = []; }
    this.render();
  },

  getWeek(offset) {
    const today = new Date();
    const dow = today.getDay();
    const mon = new Date(today);
    mon.setDate(today.getDate() - ((dow + 6) % 7) + offset * 7);
    const days = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date(mon);
      d.setDate(mon.getDate() + i);
      days.push(d);
    }
    return days;
  },

  fmtDate(d) {
    return d.toISOString().split("T")[0];
  },

  isToday(d) {
    return this.fmtDate(d) === this.fmtDate(new Date());
  },

  getEvents(d) {
    const ds = this.fmtDate(d);
    return this.events.filter(e => (e.fecha_inicio || "").startsWith(ds));
  },

  render() {
    const days = this.getWeek(this.offset);
    const monthName = days[0].toLocaleDateString("es-MX", { month: "long", year: "numeric" });
    const DOWS = ["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"];

    let html = `
      <div class="mc-header">
        <div class="mc-title" style="text-transform:capitalize;">${monthName}</div>
        <div class="mc-nav">
          <button class="mc-nav-btn" onclick="MiniCal.prev()">‹</button>
          <button class="mc-nav-btn" onclick="MiniCal.today()" style="padding:0 8px;width:auto;font-size:.68rem;">Hoy</button>
          <button class="mc-nav-btn" onclick="MiniCal.next()">›</button>
        </div>
      </div>
      <div class="mc-grid">
        ${DOWS.map(d => `<div class="mc-dow">${d}</div>`).join("")}
        ${days.map(d => {
          const evs = this.getEvents(d);
          const todayCls = this.isToday(d) ? " today" : "";
          const dots = evs.map(e => `<span class="mc-dot ${e.tipo || ""}"></span>`).join("");
          return `<div class="mc-day${todayCls}" onclick="MiniCal.clickDay('${this.fmtDate(d)}')" title="${this.fmtDate(d)}">
            <span class="mc-day-num">${d.getDate()}</span>
            <div class="mc-dots">${dots}</div>
          </div>`;
        }).join("")}
      </div>`;

    this.el.innerHTML = html;
  },

  prev()  { this.offset--; this.render(); },
  next()  { this.offset++; this.render(); },
  today() { this.offset = 0; this.render(); },
  clickDay(dateStr) {
    window.location.href = `/calendario/?fecha=${dateStr}`;
  }
};

// ── FAB (Floating Action Button) ──────────────────────────────────────────────
window.initFab = function() {
  const fabMain  = document.getElementById("fabMain");
  const fabItems = document.getElementById("fabItems");
  if (!fabMain || !fabItems) return;

  fabMain.addEventListener("click", () => {
    const open = fabItems.classList.toggle("open");
    fabMain.classList.toggle("open", open);
  });

  document.addEventListener("click", e => {
    const wrap = document.getElementById("fabWrap");
    if (wrap && !wrap.contains(e.target)) {
      fabItems.classList.remove("open");
      fabMain.classList.remove("open");
    }
  });
};

// ── FullCalendar init ─────────────────────────────────────────────────────────
window.initCalendar = function(tenantSlug) {
  const el = document.getElementById("calendar");
  if (!el || typeof FullCalendar === "undefined") return;
  const cal = new FullCalendar.Calendar(el, {
    initialView: "dayGridMonth",
    locale: "es",
    firstDay: 1,
    height: "auto",
    headerToolbar: { left: "prev,next today", center: "title", right: "dayGridMonth,timeGridWeek,listWeek" },
    buttonText: { today: "Hoy", month: "Mes", week: "Semana", list: "Lista" },
    events: `/calendario/eventos${tenantSlug ? "?tenant=" + tenantSlug : ""}`,
    eventClick(info) {
      const e = info.event, p = e.extendedProps;
      const msg = [`Tipo: ${p.tipo || "—"}`, p.asunto ? `Asunto: ${p.asunto}` : "", p.lugar ? `Lugar: ${p.lugar}` : "", `Estado: ${p.estado || "—"}`].filter(Boolean).join("\n");
      alert(`${e.title}\n\n${msg}`);
    },
  });
  cal.render();
};

// ── Chart.js wrappers ─────────────────────────────────────────────────────────
window.LexCharts = {
  colors: {
    accent: "#AD9060", success: "#4F6D8A", warning: "#B08A4A",
    danger: "#B5483F", info: "#3D5A80", muted: "#1C2630",
    text: "#8A9BA8", grid: "#1C2630",
  },
  defaults() {
    if (typeof Chart === "undefined") return;
    Chart.defaults.color = this.colors.text;
    Chart.defaults.borderColor = this.colors.grid;
    Chart.defaults.font.family = "'Inter', sans-serif";
    Chart.defaults.font.size = 11;
  },
  donut(ctx, labels, data, colors) {
    return new Chart(ctx, {
      type: "doughnut",
      data: { labels, datasets: [{ data, backgroundColor: colors || Object.values(this.colors).slice(0,5), borderWidth: 0, hoverOffset: 4 }] },
      options: { cutout: "70%", plugins: { legend: { position: "right", labels: { padding: 12, usePointStyle: true, pointStyleWidth: 8 } } }, maintainAspectRatio: false },
    });
  },
  bar(ctx, labels, datasets) {
    return new Chart(ctx, {
      type: "bar",
      data: { labels, datasets },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: datasets.length > 1 } }, scales: { y: { beginAtZero: true, grid: { color: this.colors.grid } }, x: { grid: { display: false } } } },
    });
  },
  line(ctx, labels, datasets) {
    return new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: datasets.length > 1 } }, scales: { y: { beginAtZero: true, grid: { color: this.colors.grid } }, x: { grid: { display: false } } }, elements: { line: { tension: 0.35 }, point: { radius: 3 } } },
    });
  },
};

document.addEventListener("DOMContentLoaded", () => {
  if (typeof Chart !== "undefined") LexCharts.defaults();
  window.initFab();
});
