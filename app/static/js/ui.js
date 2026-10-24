// Small DOM and formatting helpers shared by the views.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const fmt = {
  time(sec) {
    const s = Math.max(0, Math.round(sec));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  },
  timePrecise(sec) {
    const m = Math.floor(sec / 60);
    return `${m}:${(sec - m * 60).toFixed(1).padStart(4, "0")}`;
  },
  // Rough remaining time: "40 s", "3 min", "1 h 5 min".
  duration(sec) {
    if (sec < 60) return `${Math.max(5, Math.round(sec / 5) * 5)} s`;
    if (sec < 3600) return `${Math.round(sec / 60)} min`;
    return `${Math.floor(sec / 3600)} h ${Math.round((sec % 3600) / 60)} min`;
  },
  pct(x, digits = 0) {
    return x == null ? "–" : `${(x * 100).toFixed(digits)}%`;
  },
  bytes(n) {
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  },
  relative(iso) {
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
    return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  },
  dateTime(iso) {
    return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  },
  // Full date and time to the second, e.g. "10/24/2026, 12:00:46 PM".
  stamp(iso) {
    return new Date(iso).toLocaleString(undefined, { year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit" });
  },
};

export function plural(n, word) {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

export const STATUS_LABEL = { queued: "Queued", running: "Running", completed: "Completed", failed: "Failed" };

export function statusPill(status) {
  return h("span", { class: `pill ${status}` }, STATUS_LABEL[status] ?? status);
}

const tip = () => document.getElementById("tooltip");

export function showTooltip(evt, html) {
  const t = tip();
  t.innerHTML = html;
  t.hidden = false;
  const pad = 12;
  const { innerWidth: w, innerHeight: hgt } = window;
  const r = t.getBoundingClientRect();
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + r.width > w - 8) x = evt.clientX - r.width - pad;
  if (y + r.height > hgt - 8) y = evt.clientY - r.height - pad;
  t.style.left = `${x}px`;
  t.style.top = `${y}px`;
}

export function hideTooltip() {
  tip().hidden = true;
}

export const icons = {
  upload: `<svg viewBox="0 0 24 24" fill="none" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4m0 0-4 4m4-4 4 4"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/></svg>`,
  download: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12m0 0-4-4m4 4 4-4"/><path d="M4 18v1a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-1"/></svg>`,
  trash: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>`,
  check: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5 10 17 19 7"/></svg>`,
};
