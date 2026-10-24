// Violations on the run page: every incident in its own scrollable list (or grid), with
// filters, sorting, a link into the video, the worker's profile and CSV export. It fills in
// while the run is processing and keeps filters, sort, selection and scroll as rows arrive.

import { api } from "../api.js";
import { openLightbox } from "../lightbox.js";
import { openProfile } from "../profile.js";
import { fmt, h, icons, plural } from "../ui.js";

const VIEW_STORE = "ppe.detectionsView";
const PLAY = `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l11-6.5z"/></svg>`;
const SORT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 10l4-4 4 4M8 14l4 4 4-4"/></svg>`;
const CLOSE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>`;

// dir: the first direction when a sort is picked; the biggest violations come first.
const SORTS = {
  time: { label: "Video time", key: (e) => e.time_sec, dir: 1 },
  worker: { label: "Worker", key: (e) => e.track_id ?? -1, dir: 1 },
  items: { label: "Items missing", key: (e) => e.violations.length, dir: -1 },
  duration: { label: "Duration", key: (e) => e.duration, dir: -1 },
  checks: { label: "Checks", key: (e) => e.checks, dir: -1 },
};

export function eventItems(run, events) {
  // Boxes are in the pixels the video was processed at, which live mode makes smaller than the video.
  const frameSize = run.meta.frame_size ?? [run.meta.video.width, run.meta.video.height];
  return events.map((e) => ({
    ...e,
    cropUrl: e.snapshot ? api.urls.snapshot(run.meta.id, e.snapshot) : null,
    frameUrl: e.frame_image ? api.urls.frame(run.meta.id, e.frame_image) : null,
    cleanUrl: e.clean_image ? api.urls.frame(run.meta.id, e.clean_image) : null,
    frameSize, // the lightbox draws just this box on the clean frame
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

/**
 * Returns {el, update(run), setVideo(video), showItem(label)}. update() takes each poll's run;
 * setVideo() is called once the annotated video exists, which enables Play and "Show in video".
 */
export function createDetections(run) {
  const m = run.meta;
  let all = [];
  let types = [];
  let live = true;
  let video = null;
  let lastData = null;
  let seen = 0; // incidents already drawn once; later ones slide in
  const state = { item: "", worker: "", from: null, to: null, sort: "time", dir: 1, view: savedView() };
  const selected = new Set(); // incident numbers
  const showInVideo = (e) => video && seekTo(video, e.time_sec);
  let shown = [];

  // ---- filters ----
  const select = (key, label) => {
    const el = h("select", { class: "input", onchange: () => { state[key] = el.value; draw(); } });
    return { el, field: h("label", { class: "det-field" }, h("span", {}, label), el) };
  };
  // Options change as violations come in; the current choice stays selected.
  const setOptions = (sel, options) => {
    const key = JSON.stringify(options);
    if (sel.el.dataset.options === key) return;
    sel.el.dataset.options = key;
    const value = sel.el.value;
    sel.el.replaceChildren(h("option", { value: "" }, "All"), ...options.map(([v, text]) => h("option", { value: v }, text)));
    sel.el.value = options.some(([v]) => v === value) ? value : "";
  };
  const fItem = select("item", "Missing PPE");
  const fWorker = select("worker", "Worker");
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
  const sortSelect = h("select", { class: "input", onchange: () => {
    state.sort = sortSelect.value;
    state.dir = SORTS[state.sort].dir;
    draw();
  } },
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
      fItem.field, fWorker.field,
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
      state.dir = state.sort === key ? -state.dir : SORTS[key].dir;
      state.sort = key;
      draw();
    } }, label, h("span", { html: SORT, class: "det-sort-icon" })));
  const heads = [h("th", {}, "No."), sortHead("Missing PPE", "items"), sortHead("Worker", "worker"),
    sortHead("Duration", "duration"), sortHead("Timestamp", "time"), h("th", {}, "Video")];
  const thead = h("thead", {}, h("tr", {}, h("th", { class: "det-sel" }, allBox), heads));
  const tbody = h("tbody");
  const table = h("table", { class: "det-table" }, thead, tbody);
  const grid = h("div", { class: "det-grid" });
  const noMatch = h("div", { class: "det-none" }, h("p", {}, "No violations match these filters."),
    h("button", { type: "button", class: "btn", onclick: reset }, "Reset filters"));
  scroller.append(table, grid, noMatch);
  const empty = h("div", { class: "empty-note" }, h("span", { html: icons.check }), h("span"));

  const rowEls = new Map(); // incident no -> row or tile, to mark the ones playing in the video

  const checkbox = (e) => h("input", { type: "checkbox", checked: selected.has(e.no), "aria-label": `Select violation ${e.no}`,
    onchange: (ev) => { ev.target.checked ? selected.add(e.no) : selected.delete(e.no); draw(); } });
  const tagsFor = (e) => h("div", { class: "tags" }, e.violations.map((v) => h("span", { class: "tag-bad" }, v)));
  const thumb = (e, i, cls) => h("button", { type: "button", class: cls, title: "Open the photo",
    onclick: () => openLightbox(shown, i, { onShowInVideo: video ? showInVideo : null }) },
    e.cropUrl ? h("img", { src: e.cropUrl, alt: `Worker missing ${e.violations.join(", ")} at ${fmt.timePrecise(e.time_sec)}`, loading: "lazy" })
      : h("span", { class: "hint" }, "No photo"));
  const workerBtn = (e) => e.track_id != null
    ? h("button", { type: "button", class: "det-worker", title: `Open the profile of worker #${e.track_id}`,
      onclick: () => openProfile(run, e.track_id, { onShowInVideo: video ? showInVideo : null }) }, `#${e.track_id}`)
    : h("span", { class: "hint" }, "–");
  // Time of day first (what tells incidents apart), then the date.
  const stampFor = (e) => {
    if (!e.detected_at) return h("span", { class: "det-stamp is-missing", title: "Recorded for runs made from now on" }, "Not recorded");
    const d = new Date(e.detected_at);
    return h("span", { class: "det-stamp num", title: fmt.stamp(e.detected_at) },
      h("b", {}, d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })), " ",
      h("span", { class: "det-date" }, d.toLocaleDateString(undefined, { year: "numeric", month: "2-digit", day: "2-digit" })));
  };
  const playBtn = (e) => h("button", { type: "button", class: "det-play", disabled: !video,
    title: video ? null : "Available when the inspection finishes", onclick: () => showInVideo(e) },
    h("span", { html: PLAY }), `Play at ${fmt.timePrecise(e.time_sec)}`);
  const rowClass = (e) => [selected.has(e.no) ? "is-selected" : null, e.no > seen ? "is-new" : null].filter(Boolean).join(" ") || null;

  function listRow(e, i) {
    return h("tr", { class: rowClass(e) },
      h("td", { class: "det-sel" }, checkbox(e)),
      h("td", {}, h("div", { class: "det-no num" }, String(e.no))),
      h("td", {}, h("div", { class: "det-detection" }, thumb(e, i, "det-thumb"), tagsFor(e))),
      h("td", {}, workerBtn(e)),
      h("td", {}, h("div", { class: "det-dur" }, h("b", { class: "num" }, `${e.duration.toFixed(1)} s`),
        h("span", {}, plural(e.checks, "check")))),
      h("td", {}, h("div", { class: "det-when" }, stampFor(e), h("span", { class: "num" }, "Video ", h("span", { class: "nowrap" }, incidentTime(e))))),
      h("td", {}, playBtn(e)));
  }

  function tile(e, i) {
    return h("article", { class: `det-tile ${rowClass(e) ?? ""}` },
      h("div", { class: "det-tile-top" },
        thumb(e, i, "det-tile-thumb"),
        h("label", { class: "det-tile-sel" }, checkbox(e)),
        h("span", { class: "det-tile-no num" }, `No. ${e.no}`)),
      h("div", { class: "det-tile-body" },
        tagsFor(e),
        h("div", { class: "det-tile-meta" }, workerBtn(e)),
        stampFor(e),
        h("span", { class: "hint num" }, `${incidentTime(e)} · ${e.duration.toFixed(1)} s · ${plural(e.checks, "check")}`),
        playBtn(e)));
  }

  function filtered() {
    return all.filter((e) => (!state.item || e.violations.includes(state.item))
      && (!state.worker || String(e.track_id) === state.worker)
      && (state.from == null || (e.end_sec ?? e.time_sec) >= state.from)
      && (state.to == null || e.time_sec <= state.to));
  }

  function draw() {
    const key = SORTS[state.sort].key;
    shown = filtered().sort((a, b) => (key(a) - key(b)) * state.dir || a.no - b.no);
    const isList = state.view === "list";

    filters.hidden = scroller.hidden = !all.length;
    empty.hidden = all.length > 0;
    empty.lastChild.textContent = live ? "No violations so far. Every worker checked so far has the required PPE."
      : "No worker's status showed missing PPE at any point.";

    sortSelect.value = state.sort;
    dirBtn.textContent = state.dir > 0 ? "↑" : "↓";
    dirBtn.title = state.dir > 0 ? "Ascending" : "Descending";
    dirBtn.setAttribute("aria-label", dirBtn.title);
    for (const th of heads) {
      if (!th.dataset.sort) continue;
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
    resetBtn.disabled = !state.item && !state.worker && state.from == null && state.to == null
      && !fFrom.value && !fTo.value && !selected.size && state.sort === "time" && state.dir === 1;

    rowEls.clear();
    table.hidden = !isList || !shown.length;
    grid.hidden = isList || !shown.length;
    noMatch.hidden = shown.length > 0;
    scroller.classList.toggle("is-grid", !isList);
    // Replacing the rows keeps the list's scroll position; images come from the cache.
    if (isList) tbody.replaceChildren(...shown.map((e, i) => { const r = listRow(e, i); rowEls.set(e.no, r); return r; }));
    else grid.replaceChildren(...shown.map((e, i) => { const t = tile(e, i); rowEls.set(e.no, t); return t; }));
    seen = Math.max(seen, all.length);
    markPlaying();
  }

  // Rows whose incident is on screen in the video right now.
  function markPlaying() {
    if (!video) return;
    const t = video.currentTime;
    for (const e of shown) {
      const on = !video.paused && t >= e.time_sec - 0.25 && t <= (e.end_sec ?? e.time_sec) + 0.75;
      rowEls.get(e.no)?.classList.toggle("is-now", on);
    }
  }

  function reset() {
    Object.assign(state, { item: "", worker: "", from: null, to: null, sort: "time", dir: 1 });
    for (const el of [fItem.el, fWorker.el]) el.value = "";
    for (const el of [fFrom, fTo]) { el.value = ""; el.classList.remove("is-invalid"); }
    selected.clear();
    draw();
  }

  function exportCsv() {
    const rows = selected.size ? all.filter((e) => selected.has(e.no)) : shown;
    const cell = (v) => { const t = String(v ?? ""); return /[",\r\n]/.test(t) ? `"${t.replaceAll('"', '""')}"` : t; };
    const lines = [["No.", "Worker", "Missing PPE", "Start (video)", "End (video)", "Duration (s)", "Checks", "Detected at"],
      ...rows.map((e) => [e.no, e.track_id != null ? `#${e.track_id}` : "", e.violations.join("; "),
        fmt.timePrecise(e.time_sec), fmt.timePrecise(e.end_sec ?? e.time_sec), e.duration.toFixed(1), e.checks,
        e.detected_at ?? ""])];
    const blob = new Blob(["﻿" + lines.map((r) => r.map(cell).join(",")).join("\r\n")], { type: "text/csv" });
    const a = h("a", { href: URL.createObjectURL(blob), download: `${m.name}_violations.csv` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  const intro = h("p");
  const count = h("span", { class: "pill failed", hidden: true });
  const el = h("section", { class: "card card-pad det-card", id: "violations" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Violations ", count), intro)),
    empty, filters, scroller);

  function update(run) {
    const s = run.summary;
    const wasLive = live;
    live = run.meta.status !== "completed";
    const data = JSON.stringify(s?.events ?? []);
    if (data === lastData && wasLive === live) return;
    const first = lastData === null;
    lastData = data;
    // Numbered in the order they started, so "No. 3" means the same thing whatever the sort.
    all = eventItems(run, s?.events ?? []).map((e, i) => ({ ...e, no: i + 1, duration: (e.end_sec ?? e.time_sec) - e.time_sec }));
    types = s?.by_type.map((t) => t.label) ?? [];
    const workers = [...new Set(all.map((e) => e.track_id).filter((id) => id != null))].sort((a, b) => a - b);
    setOptions(fItem, types.map((t) => [t, t]));
    setOptions(fWorker, workers.map((id) => [String(id), `#${id}`]));
    if (first) seen = all.length; // only violations found while watching slide in

    intro.textContent = live
      ? "Filled in as the video is inspected. A violation appears once it has been seen in two checks in a row; Play works when the inspection finishes."
      : "One row per incident: a worker whose status showed missing PPE, from the first to the last check in a row. Click a photo to see it, or Play to watch it in the video above.";
    count.hidden = !all.length;
    count.textContent = String(all.length);
    draw();
  }

  return {
    el,
    update,
    setVideo(v) {
      video = v;
      for (const ev of ["timeupdate", "pause", "seeked"]) video.addEventListener(ev, markPlaying);
      draw();
    },
    showItem(label) {
      state.item = label;
      fItem.el.value = label;
      draw();
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    },
  };
}
