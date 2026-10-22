// Compliance timeline: one stacked column per checked frame
// (workers in compliance at the bottom, workers with violations on top).

import { fmt, hideTooltip, showTooltip } from "./ui.js";

const NS = "http://www.w3.org/2000/svg";
const s = (tag, attrs = {}) => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
};

function niceMax(v) {
  if (v <= 4) return Math.max(1, v);
  const step = v <= 10 ? 2 : v <= 20 ? 5 : 10;
  return Math.ceil(v / step) * step;
}

// Rounded top corners only (data end), square at the baseline.
function topRoundedRect(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h} Z`;
}

/**
 * @param {HTMLElement} container
 * @param {{time_sec:number, workers:number, violations:number}[]} points
 * @param {{duration:number, interval:number, onSeek?:(t:number)=>void}} opts
 * @returns {{setPlayhead:(t:number)=>void}}
 */
export function timelineChart(container, points, { duration, interval, onSeek }) {
  container.replaceChildren();
  const W = 900, H = 220, m = { l: 34, r: 12, t: 12, b: 26 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const total = Math.max(duration, (points.at(-1)?.time_sec ?? 0) + interval);
  const yMax = niceMax(Math.max(1, ...points.map((p) => p.workers)));
  const x = (t) => m.l + (t / total) * iw;
  const y = (v) => m.t + ih - (v / yMax) * ih;
  const slot = (interval / total) * iw;
  const bw = Math.max(2, Math.min(28, slot - 2)); // 2px surface gap between columns

  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img",
    "aria-label": "Workers per checked frame: in compliance versus with violations" });

  const grid = s("g", { class: "grid" });
  const axis = s("g", { class: "axis" });
  const yStep = yMax <= 4 ? 1 : yMax <= 10 ? 2 : 5;
  for (let v = 0; v <= yMax; v += yStep) {
    grid.append(s("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
    const t = s("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end" });
    t.textContent = v;
    axis.append(t);
  }
  const xStep = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600].find((st) => total / st <= 10) ?? 1200;
  for (let t = 0; t <= total + 1e-6; t += xStep) {
    const lbl = s("text", { x: x(t), y: H - 6, "text-anchor": "middle" });
    lbl.textContent = fmt.time(t);
    axis.append(lbl);
  }
  svg.append(grid, axis);

  const bars = s("g");
  for (const p of points) {
    const cx = x(p.time_sec + interval / 2);
    const x0 = cx - bw / 2;
    const ok = p.workers - p.violations;
    if (ok > 0) {
      const top = p.violations > 0 ? 0 : 3;
      const yTop = y(ok);
      bars.append(top
        ? s("path", { d: topRoundedRect(x0, yTop, bw, y(0) - yTop, top), class: "bar-ok" })
        : s("rect", { x: x0, y: yTop, width: bw, height: y(0) - yTop, class: "bar-ok" }));
    }
    if (p.violations > 0) {
      const yTop = y(p.workers);
      const yBot = y(ok) - (ok > 0 ? 2 : 0); // 2px gap between stacked segments
      bars.append(s("path", { d: topRoundedRect(x0, yTop, bw, Math.max(1, yBot - yTop), 3), class: "bar-bad" }));
    }
    // Hit target wider and taller than the mark.
    const hit = s("rect", { x: cx - Math.max(slot, 10) / 2, y: m.t, width: Math.max(slot, 10), height: ih, class: "hit" });
    hit.addEventListener("mousemove", (e) => showTooltip(e,
      `<b>${fmt.timePrecise(p.time_sec)}</b><br>${p.workers} worker${p.workers === 1 ? "" : "s"} seen<br>` +
      `${ok} in compliance · ${p.violations} with violations` + (onSeek ? `<br><span style="opacity:.7">Click to jump here</span>` : "")));
    hit.addEventListener("mouseleave", hideTooltip);
    if (onSeek) hit.addEventListener("click", () => onSeek(p.time_sec));
    bars.append(hit);
  }
  svg.append(bars);

  const playhead = s("line", { class: "playhead", y1: m.t, y2: m.t + ih, x1: m.l, x2: m.l, visibility: "hidden" });
  svg.append(playhead);
  container.append(svg);

  return {
    setPlayhead(t) {
      if (t == null) return playhead.setAttribute("visibility", "hidden");
      const px = x(Math.min(t, total));
      playhead.setAttribute("x1", px);
      playhead.setAttribute("x2", px);
      playhead.setAttribute("visibility", "visible");
    },
  };
}
