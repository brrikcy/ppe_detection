// Violations on the results page: every incident in its own scrollable list (or grid), with
// filters, sorting, a link into the video, the worker's profile and CSV export.

import { api } from "../api.js";
import { openLightbox } from "../lightbox.js";
import { openProfile } from "../profile.js";
import { fmt, h, icons, plural } from "../ui.js";

const VIEW_STORE = "ppe.detectionsView";
const PLAY = `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l11-6.5z"/></svg>`;
const SORT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 10l4-4 4 4M8 14l4 4 4-4"/></svg>`;
const CLOSE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>`;

// resolved: true when a later check found the PPE back on, false when it was still missing at
// the worker's last check, null for runs made before it was recorded.
const OUTCOMES = {
  open: { label: "Unresolved", title: "Still missing at the worker's last check" },
  fixed: { label: "Corrected", title: "A later check found the PPE back on" },
};
const outcomeOf = (e) => (e.resolved == null ? null : e.resolved ? "fixed" : "open");

const SORTS = {
  time: { label: "Video time", key: (e) => e.time_sec },
  worker: { label: "Worker", key: (e) => e.track_id ?? -1 },
  items: { label: "Items missing", key: (e) => e.violations.length },
  duration: { label: "Duration", key: (e) => e.duration },
  checks: { label: "Checks", key: (e) => e.checks },
};

export function eventItems(run, events) {
  return events.map((e) => ({
    ...e,
    cropUrl: e.snapshot ? api.urls.snapshot(run.meta.id, e.snapshot) : null,
    frameUrl: e.frame_image ? api.urls.frame(run.meta.id, e.frame_image) : null,
  }));
}

export function incidentTime(e) {
  return e.end_sec != null && e.end_sec > e.time_sec
    ? `${fmt.timePrecise(e.time_sec)}–${fmt.timePrecise(e.end_sec)}` : fmt.timePrecise(e.time_sec);
}

export function seekTo(video, t) {
  video.currentTime = t + 0.05;
  video.scrollIntoView({ behavior: "smooth", block: "center" });
  video.play().catch(() => {});
}

// "1:20", "80" or "80.5" seconds; null when empty, NaN when unreadable.
function parseTime(text) {
  const t = text.trim();
  if (!t) return null;
  const m = /^(?:(\d+):)?(\d+(?:\.\d+)?)$/.exec(t);
  return m ? Number(m[1] ?? 0) * 60 + Number(m[2]) : NaN;
}

function savedView() {
  try { return localStorage.getItem(VIEW_STORE) === "grid" ? "grid" : "list"; } catch { return "list"; }
}
function rememberView(v) {
  try { localStorage.setItem(VIEW_STORE, v); } catch { /* not remembered */ }
}

/** Returns {el, showItem(label)}; showItem filters to one missing item and scrolls to the list. */
export function detectionsCard(run, s, video) {
  const m = run.meta;
  if (!s.events.length) {
    return { showItem() {}, el: h("section", { class: "card card-pad" },
      h("div", { class: "card-head" }, h("h2", {}, "Violations")),
      h("div", { class: "empty-note" }, h("span", { html: icons.check }), "No worker's status showed missing PPE at any point.")) };
  }

  // Numbered in the order they started, so "No. 3" means the same thing whatever the sort.
  const all = eventItems(run, s.events).map((e, i) => ({ ...e, no: i + 1, duration: (e.end_sec ?? e.time_sec) - e.time_sec }));
  const types = s.by_type.map((t) => t.label);
  const workers = [...new Set(all.map((e) => e.track_id).filter((id) => id != null))].sort((a, b) => a - b);
  const hasOutcome = all.some((e) => e.resolved != null);
  const state = { item: "", outcome: "", worker: "", from: null, to: null, sort: "time", dir: 1, view: savedView() };
  const selected = new Set(); // incident numbers
  const showInVideo = (e) => seekTo(video, e.time_sec);
  let shown = [];

  // ---- filters ----
  const select = (key, label, options) => {
    const el = h("select", { class: "input", onchange: () => { state[key] = el.value; draw(); } },
      h("option", { value: "" }, "All"), options.map(([v, text]) => h("option", { value: v }, text)));
    return { el, field: h("label", { class: "det-field" }, h("span", {}, label), el) };
  };
  const fItem = select("item", "Missing PPE", types.map((t) => [t, t]));
  const fOutcome = hasOutcome ? select("outcome", "Outcome", Object.entries(OUTCOMES).map(([k, o]) => [k, o.label])) : null;
  const fWorker = workers.length ? select("worker", "Worker", workers.map((id) => [String(id), `#${id}`])) : null;
  const timeInput = (key, placeholder) => {
    const el = h("input", { class: "input", inputmode: "decimal", placeholder, "aria-describedby": "det-time-hint",
      oninput: () => {
        const t = parseTime(el.value);
        el.classList.toggle("is-invalid", Number.isNaN(t));
        state[key] = Number.isNaN(t) ? null : t;
        draw();
      } });
    return el;
  };
  const fFrom = timeInput("from", "0:00");
  const fTo = timeInput("to", fmt.time(m.video.duration_sec));
  const sortSelect = h("select", { class: "input", onchange: () => { state.sort = sortSelect.value; draw(); } },
    Object.entries(SORTS).map(([k, o]) => h("option", { value: k }, o.label)));
  const dirBtn = h("button", { type: "button", class: "btn det-dir", onclick: () => { state.dir *= -1; draw(); } });

  const total = h("span", { class: "det-total" });
  const selInfo = h("span", { class: "det-selected", hidden: true });
  const viewBtns = [["list", "List"], ["grid", "Grid"]].map(([v, label]) => h("button", {
    type: "button", "data-view": v, onclick: () => { state.view = v; rememberView(v); draw(); } }, label));
  const exportBtn = h("button", { type: "button", class: "btn", onclick: () => exportCsv() },
    h("span", { html: icons.download, style: "display:inline-flex" }), h("span"));
  const resetBtn = h("button", { type: "button", class: "btn btn-ghost", onclick: reset },
    h("span", { html: CLOSE, style: "display:inline-flex" }), "Reset");

  const filters = h("div", { class: "det-filters" },
    h("div", { class: "det-row" },
      fItem.field, fOutcome?.field, fWorker?.field,
      h("div", { class: "det-field det-range", role: "group", "aria-label": "Video time" },
        h("span", {}, "Video time from – to"),
        h("div", { class: "det-range-inputs" }, fFrom, h("span", { class: "pair-sep" }, "–"), fTo)),
      h("label", { class: "det-field det-sort-field" }, h("span", {}, "Sort by"), h("div", { class: "det-sort" }, sortSelect, dirBtn))),
    h("div", { class: "det-row det-row-2" },
      total, selInfo,
      h("span", { class: "det-spacer" }),
      h("div", { class: "view-switch", role: "group", "aria-label": "Layout" }, h("span", { class: "eyebrow" }, "View"), ...viewBtns),
      exportBtn, resetBtn),
    h("p", { class: "visually-hidden", id: "det-time-hint" }, "Minutes and seconds, like 1:20, or seconds."));

  // ---- list and grid ----
  const scroller = h("div", { class: "det-scroll", tabindex: "0", "aria-label": "Violations" });
  const allBox = h("input", { type: "checkbox", "aria-label": "Select all shown", onchange: () => {
    for (const e of shown) allBox.checked ? selected.add(e.no) : selected.delete(e.no);
    draw();
  } });
  const sortHead = (label, key) => h("th", { "aria-sort": "none", "data-sort": key },
    h("button", { type: "button", class: "det-sort-btn", onclick: () => {
      state.dir = state.sort === key ? -state.dir : 1;
      state.sort = key;
      draw();
    } }, label, h("span", { html: SORT, class: "det-sort-icon" })));
  const heads = [h("th", {}, "No."), sortHead("Missing PPE", "items"), sortHead("Worker", "worker"),
    sortHead("Duration", "duration"), sortHead("Timestamp", "time"), h("th", {}, "Video"), hasOutcome ? h("th", {}, "Outcome") : null];
  const thead = h("thead", {}, h("tr", {}, h("th", { class: "det-sel" }, allBox), heads));
  const tbody = h("tbody");
  const table = h("table", { class: "det-table" }, thead, tbody);
  const grid = h("div", { class: "det-grid" });
  const noMatch = h("div", { class: "det-none" }, h("p", {}, "No violations match these filters."),
    h("button", { type: "button", class: "btn", onclick: reset }, "Reset filters"));
  scroller.append(table, grid, noMatch);

  const rowEls = new Map(); // incident no -> row or tile, to mark the ones playing in the video

  const checkbox = (e) => h("input", { type: "checkbox", checked: selected.has(e.no), "aria-label": `Select violation ${e.no}`,
    onchange: (ev) => { ev.target.checked ? selected.add(e.no) : selected.delete(e.no); draw(); } });
  const tagsFor = (e) => h("div", { class: "tags" }, e.violations.map((v) => h("span", { class: "tag-bad" }, v)));
  const thumb = (e, i, cls) => h("button", { type: "button", class: cls, title: "Open the photos",
    onclick: () => openLightbox(shown, i, { onShowInVideo: showInVideo }) },
    e.cropUrl ? h("img", { src: e.cropUrl, alt: `Worker missing ${e.violations.join(", ")} at ${fmt.timePrecise(e.time_sec)}`, loading: "lazy" })
      : h("span", { class: "hint" }, "No photo"));
  const workerBtn = (e) => e.track_id != null
    ? h("button", { type: "button", class: "det-worker", title: `Open the profile of worker #${e.track_id}`,
      onclick: () => openProfile(run, e.track_id, { onShowInVideo: showInVideo }) }, `#${e.track_id}`)
    : h("span", { class: "hint" }, "–");
  const stampFor = (e) => e.detected_at
    ? h("span", { class: "det-stamp num" }, fmt.stamp(e.detected_at))
    : h("span", { class: "det-stamp is-missing", title: "Recorded for runs made from now on" }, "Not recorded");
  const playBtn = (e) => h("button", { type: "button", class: "det-play", onclick: () => showInVideo(e) },
    h("span", { html: PLAY }), `Play at ${fmt.timePrecise(e.time_sec)}`);
  const outcomePill = (e) => {
    const o = outcomeOf(e);
    return o ? h("span", { class: `det-outcome is-${o}`, title: OUTCOMES[o].title }, OUTCOMES[o].label) : h("span", { class: "hint" }, "–");
  };
  const dot = (e) => h("i", { class: `det-dot is-${outcomeOf(e) ?? "open"}`, "aria-hidden": "true" });

  function listRow(e, i) {
    return h("tr", { class: selected.has(e.no) ? "is-selected" : null },
      h("td", { class: "det-sel" }, checkbox(e)),
      h("td", {}, h("div", { class: "det-no" }, dot(e), h("span", { class: "num" }, String(e.no)))),
      h("td", {}, h("div", { class: "det-detection" }, thumb(e, i, "det-thumb"), tagsFor(e))),
      h("td", {}, workerBtn(e)),
      h("td", {}, h("div", { class: "det-dur" }, h("b", { class: "num" }, `${e.duration.toFixed(1)} s`),
        h("span", {}, plural(e.checks, "check")))),
      h("td", {}, h("div", { class: "det-when" }, stampFor(e), h("span", { class: "num" }, "Video ", h("span", { class: "nowrap" }, incidentTime(e))))),
      h("td", {}, playBtn(e)),
      hasOutcome ? h("td", {}, outcomePill(e)) : null);
  }

  function tile(e, i) {
    return h("article", { class: `det-tile${selected.has(e.no) ? " is-selected" : ""}` },
      h("div", { class: "det-tile-top" },
        thumb(e, i, "det-tile-thumb"),
        h("label", { class: "det-tile-sel" }, checkbox(e)),
        h("span", { class: "det-tile-no num" }, dot(e), `No. ${e.no}`)),
      h("div", { class: "det-tile-body" },
        tagsFor(e),
        h("div", { class: "det-tile-meta" }, workerBtn(e), outcomePill(e)),
        stampFor(e),
        h("span", { class: "hint num" }, `${incidentTime(e)} · ${e.duration.toFixed(1)} s · ${plural(e.checks, "check")}`),
        playBtn(e)));
  }

  function filtered() {
    return all.filter((e) => (!state.item || e.violations.includes(state.item))
      && (!state.outcome || outcomeOf(e) === state.outcome)
      && (!state.worker || String(e.track_id) === state.worker)
      && (state.from == null || (e.end_sec ?? e.time_sec) >= state.from)
      && (state.to == null || e.time_sec <= state.to));
  }

  function draw() {
    const key = SORTS[state.sort].key;
    shown = filtered().sort((a, b) => (key(a) - key(b)) * state.dir || a.no - b.no);
    const isList = state.view === "list";

    sortSelect.value = state.sort;
    dirBtn.textContent = state.dir > 0 ? "↑" : "↓";
    dirBtn.title = state.dir > 0 ? "Ascending" : "Descending";
    dirBtn.setAttribute("aria-label", dirBtn.title);
    for (const th of heads) {
      if (!th?.dataset.sort) continue;
      const on = th.dataset.sort === state.sort;
      th.setAttribute("aria-sort", on ? (state.dir > 0 ? "ascending" : "descending") : "none");
      th.classList.toggle("is-sorted", on);
    }
    viewBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === state.view)));

    total.replaceChildren("Total: ", h("b", { class: "num" }, shown.length === all.length
      ? plural(all.length, "violation") : `${shown.length} of ${plural(all.length, "violation")}`));
    selInfo.hidden = !selected.size;
    selInfo.textContent = `${selected.size} selected`;
    exportBtn.lastChild.textContent = selected.size ? `Export selected (${selected.size})` : "Export shown";
    const picked = shown.filter((e) => selected.has(e.no)).length;
    allBox.checked = shown.length > 0 && picked === shown.length;
    allBox.indeterminate = picked > 0 && picked < shown.length;
    resetBtn.disabled = !state.item && !state.outcome && !state.worker && state.from == null && state.to == null
      && !fFrom.value && !fTo.value && !selected.size && state.sort === "time" && state.dir === 1;

    rowEls.clear();
    table.hidden = !isList || !shown.length;
    grid.hidden = isList || !shown.length;
    noMatch.hidden = shown.length > 0;
    scroller.classList.toggle("is-grid", !isList);
    if (isList) tbody.replaceChildren(...shown.map((e, i) => { const r = listRow(e, i); rowEls.set(e.no, r); return r; }));
    else grid.replaceChildren(...shown.map((e, i) => { const t = tile(e, i); rowEls.set(e.no, t); return t; }));
    markPlaying();
  }

  // Rows whose incident is on screen in the video right now.
  function markPlaying() {
    const t = video.currentTime;
    for (const e of shown) {
      const on = !video.paused && t >= e.time_sec - 0.25 && t <= (e.end_sec ?? e.time_sec) + 0.75;
      rowEls.get(e.no)?.classList.toggle("is-now", on);
    }
  }
  for (const ev of ["timeupdate", "pause", "seeked"]) video.addEventListener(ev, markPlaying);

  function reset() {
    Object.assign(state, { item: "", outcome: "", worker: "", from: null, to: null, sort: "time", dir: 1 });
    for (const el of [fItem.el, fOutcome?.el, fWorker?.el]) if (el) el.value = "";
    for (const el of [fFrom, fTo]) { el.value = ""; el.classList.remove("is-invalid"); }
    selected.clear();
    draw();
  }

  function exportCsv() {
    const rows = selected.size ? all.filter((e) => selected.has(e.no)) : shown;
    const cell = (v) => { const t = String(v ?? ""); return /[",\r\n]/.test(t) ? `"${t.replaceAll('"', '""')}"` : t; };
    const lines = [["No.", "Worker", "Missing PPE", "Start (video)", "End (video)", "Duration (s)", "Checks", "Detected at", "Outcome"],
      ...rows.map((e) => [e.no, e.track_id != null ? `#${e.track_id}` : "", e.violations.join("; "),
        fmt.timePrecise(e.time_sec), fmt.timePrecise(e.end_sec ?? e.time_sec), e.duration.toFixed(1), e.checks,
        e.detected_at ?? "", outcomeOf(e) ? OUTCOMES[outcomeOf(e)].label : ""])];
    const blob = new Blob(["﻿" + lines.map((r) => r.map(cell).join(",")).join("\r\n")], { type: "text/csv" });
    const a = h("a", { href: URL.createObjectURL(blob), download: `${m.name}_violations.csv` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  draw();
  const open = all.filter((e) => e.resolved === false).length;
  const el = h("section", { class: "card card-pad det-card", id: "violations" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "Violations"),
        h("p", {}, "One row per incident: a worker whose status showed missing PPE, from the first to the last check in a row. "
          + "Click a photo to browse them, or Play to watch it in the video above.")),
      hasOutcome ? h("div", { class: "legend" },
        h("span", {}, h("i", { style: "background:var(--critical)" }), `${open} unresolved`),
        h("span", {}, h("i", { style: "background:var(--warning)" }), `${all.length - open} corrected`)) : null),
    filters, scroller);

  return {
    el,
    showItem(label) {
      state.item = label;
      fItem.el.value = label;
      draw();
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    },
  };
}
