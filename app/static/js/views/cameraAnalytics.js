// Analytics for the camera wall, shown below the violation list: totals, violations over time,
// workers on screen against workers missing PPE, violations by type and per camera.

import { fmt, h, plural } from "../ui.js";

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clock = (t, bucket) => new Date(t * 1000).toLocaleTimeString(undefined,
  bucket < 60 ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" });

const W = 640, H = 190, PAD = { l: 34, r: 10, t: 10, b: 24 };
const niceMax = (v) => (v <= 4 ? 4 : v <= 8 ? 8 : Math.ceil(v / 5) * 5);

// Shared frame: horizontal grid lines with values, and time labels along the bottom.
function frame(points, bucket, max) {
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const y = (v) => PAD.t + ih - (v / max) * ih;
  let out = "";
  for (const v of [0, max / 2, max]) {
    out += `<line class="an-grid" x1="${PAD.l}" x2="${W - PAD.r}" y1="${y(v)}" y2="${y(v)}"/>`
      + `<text class="an-tick" x="${PAD.l - 6}" y="${y(v) + 4}" text-anchor="end">${Math.round(v * 10) / 10}</text>`;
  }
  const idx = points.length <= 2 ? points.map((_, i) => i) : [0, Math.floor((points.length - 1) / 2), points.length - 1];
  for (const i of idx) {
    const x = PAD.l + (points.length === 1 ? iw / 2 : (i / (points.length - 1)) * iw);
    out += `<text class="an-tick" x="${x}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === points.length - 1 ? "end" : "middle"}">${clock(points[i].t, bucket)}</text>`;
  }
  return { out, iw, ih, y };
}

function barsChart(tl) {
  const pts = tl.points;
  const max = niceMax(Math.max(1, ...pts.map((p) => p.violations)));
  const f = frame(pts, tl.bucket_sec, max);
  const slot = f.iw / pts.length, bw = Math.max(2, Math.min(28, slot * 0.7));
  const bars = pts.map((p, i) => {
    if (!p.violations) return "";
    const x = PAD.l + i * slot + (slot - bw) / 2;
    return `<rect class="an-bar" x="${x}" y="${f.y(p.violations)}" width="${bw}" height="${PAD.t + f.ih - f.y(p.violations)}" rx="2">`
      + `<title>${esc(clock(p.t, tl.bucket_sec))}: ${plural(p.violations, "violation")}</title></rect>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Violations over time">${f.out}${bars}</svg>`;
}

function linesChart(tl) {
  const pts = tl.points;
  const max = niceMax(Math.max(1, ...pts.map((p) => Math.max(p.workers ?? 0, p.missing ?? 0))));
  const f = frame(pts, tl.bucket_sec, max);
  const x = (i) => PAD.l + (pts.length === 1 ? f.iw / 2 : (i / (pts.length - 1)) * f.iw);
  const line = (key, cls) => {
    const have = pts.map((p, i) => [i, p[key]]).filter(([, v]) => v != null);
    if (!have.length) return "";
    const d = have.map(([i, v]) => `${x(i)},${f.y(v)}`).join(" ");
    const dots = have.length <= 12 ? have.map(([i, v]) => `<circle class="${cls}-dot" cx="${x(i)}" cy="${f.y(v)}" r="3"/>`).join("") : "";
    return `<polyline class="an-line ${cls}" points="${d}"/>${dots}`;
  };
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Workers on screen and missing PPE over time">${f.out}${line("workers", "an-workers")}${line("missing", "an-missing")}</svg>`;
}

function hbars(rows, { valueText, barClass = "an-hbar" }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return h("ul", { class: "an-hbars" }, rows.map((r) => h("li", {},
    h("span", { class: "an-hl", title: r.label }, r.label),
    h("span", { class: "an-htrack" }, h("i", { class: barClass, style: `width:${(r.value / max) * 100}%` })),
    h("span", { class: "an-hv num" }, valueText(r)))));
}

/** Returns {el, update(analytics)}. */
export function createAnalytics() {
  const since = h("p", { class: "hint" });
  const kpis = h("div", { class: "an-kpis" });
  const empty = h("p", { class: "cam-feed-empty", hidden: true }, "Analytics appear once cameras have been running for a moment.");
  const timeBars = h("div", { class: "an-chart" });
  const timeLines = h("div", { class: "an-chart" });
  const byType = h("div");
  const byCamera = h("div");
  const legend = h("p", { class: "an-legend" },
    h("span", { class: "an-key an-workers" }, "Workers on screen"), h("span", { class: "an-key an-missing" }, "Missing PPE"));

  const panel = (title, hint, ...body) => h("div", { class: "an-panel" }, h("h3", {}, title),
    hint ? h("p", { class: "hint" }, hint) : null, ...body);
  const grid = h("div", { class: "an-grid-wrap" },
    panel("Violations over time", null, timeBars),
    panel("Workers on screen vs missing PPE", null, timeLines, legend),
    panel("Violations by type", null, byType),
    panel("By camera", "Violations found, and the share of worker checks that were compliant.", byCamera));
  const el = h("section", { class: "card card-pad cam-analytics", "aria-label": "Analytics" },
    h("div", { class: "cam-side-head" }, h("h2", {}, "Analytics")), since, kpis, empty, grid);

  const kpi = (label, value, note, tone) => h("div", { class: `an-kpi${tone ? ` is-${tone}` : ""}` },
    h("span", { class: "eyebrow" }, label), h("b", { class: "num" }, value), h("span", { class: "an-note" }, note ?? ""));

  function update(a) {
    if (!a) return;
    const k = a.kpis;
    const has = a.timeline.points.length > 0;
    empty.hidden = has;
    grid.hidden = !has;
    kpis.hidden = !has;
    since.textContent = has ? `Since ${fmt.stamp(new Date(a.since * 1000).toISOString())} · ${fmt.time(a.duration_sec)} monitored`
      : "";
    kpis.replaceChildren(
      kpi("Violations", String(k.violations), k.per_hour != null ? `${k.per_hour} an hour` : "", k.violations ? "bad" : null),
      kpi("Compliance", k.compliance == null ? "–" : `${Math.round(k.compliance * 100)}%`, `${plural(k.checks, "worker check")}`),
      kpi("Workers tracked", String(k.workers_tracked), `up to ${k.peak_workers} on screen at once`),
      kpi("Peak missing PPE", String(k.peak_missing), "workers at the same time", k.peak_missing ? "bad" : null),
      kpi("Most violations", k.worst_camera ?? "–", k.worst_camera ? "camera" : "none yet"));
    if (!has) return;
    timeBars.innerHTML = barsChart(a.timeline);
    timeLines.innerHTML = linesChart(a.timeline);
    byType.replaceChildren(a.by_type.length
      ? hbars(a.by_type.map((t) => ({ label: t.label, value: t.count })), { valueText: (r) => String(r.value) })
      : h("p", { class: "cam-feed-empty" }, "No violations yet."));
    byCamera.replaceChildren(a.by_camera.length
      ? hbars(a.by_camera.map((c) => ({ label: c.name, value: c.violations, comp: c.compliance })), {
        valueText: (r) => `${r.value}${r.comp == null ? "" : ` · ${Math.round(r.comp * 100)}%`}` })
      : h("p", { class: "cam-feed-empty" }, "No cameras yet."));
  }
  return { el, update };
}
