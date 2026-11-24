// Live camera wall: simulated RTSP cameras, each playing its video in a loop with live PPE
// detections. Pick the cameras to start, watch them all at once, click one to enlarge it,
// remove one from the wall. Violations from every camera are listed as they are found.

import { api, apiKey } from "../api.js";
import { openLightbox } from "../lightbox.js";
import { fmt, h, plural } from "../ui.js";
import { createAnalytics } from "./cameraAnalytics.js";

const POLL_MS = 1500;
const ALERT_MS = 5000; // a tile stays red this long after a new violation
const BANNER_MS = 7000; // a violation alert stays on screen this long
const MAX_BANNERS = 3; // alerts shown at once; the rest are counted
const CLOSE_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>`;
const DOWNLOAD_ICON = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12m0 0-4-4m4 4 4-4"/><path d="M4 18v1a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-1"/></svg>`;
const EXPAND_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/></svg>`;
const REMOVE_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>`;

// Shows a camera's frames in `img`. Reconnects while `alive()` says the camera is still wanted.
function playCamera(id, size, img, alive) {
  let ws = null, timer = null, closed = false;
  const open = () => {
    if (closed) return;
    ws = new WebSocket(api.cameras.live(id, size));
    ws.binaryType = "blob";
    ws.onmessage = (e) => {
      if (!img.isConnected) return stop();
      ws.send("ok");
      const prev = img.src;
      img.src = URL.createObjectURL(e.data);
      img.classList.add("is-loaded");
      if (prev.startsWith("blob:")) URL.revokeObjectURL(prev);
    };
    ws.onclose = () => { if (!closed && alive()) timer = setTimeout(open, 2000); };
  };
  const stop = () => { closed = true; clearTimeout(timer); ws?.close(); };
  open();
  return stop;
}

const timeOf = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function toLightboxItems(events) {
  return events.map((e) => ({
    ...e, cropUrl: e.crop_url, frameUrl: e.frame_url, cleanUrl: e.clean_url, frameSize: e.frame_size,
    notes: [`${e.camera_name} · detected ${timeOf(e.detected_at)}`],
  }));
}

export async function renderCameras(view) {
  const config = await api.config();
  let data = await api.cameras.state();
  let disposed = false, timer = null;
  const tiles = new Map(); // camera id -> tile
  const seen = new Set(); // violation ids already shown, to flag the new ones
  let firstPoll = true;
  let filter = null; // camera id the violation list is limited to
  let enlarged = null; // {id, el, stop, close}
  const picked = new Set();

  // ---- violation alert: a red glow around the whole window and a banner, wherever you are on the
  // wall (also over an enlarged camera). It never takes focus or clicks, and goes away by itself.
  const glow = h("div", { class: "cam-glow", "aria-hidden": "true" });
  const banners = h("div", { class: "cam-banners", role: "alert", "aria-live": "assertive" });
  document.body.append(glow, banners);
  function raiseAlert(events) {
    glow.classList.remove("is-on");
    void glow.offsetWidth; // restart the animation for a new alert
    glow.classList.add("is-on");
    clearTimeout(glow.timer);
    glow.timer = setTimeout(() => glow.classList.remove("is-on"), BANNER_MS);
    const shown = events.slice(0, MAX_BANNERS);
    for (const e of shown) {
      const banner = h("div", { class: "cam-banner" },
        h("b", {}, `${e.violations.join(", ")} detected`), ` at ${e.camera_name}`);
      banners.prepend(banner);
      setTimeout(() => banner.remove(), BANNER_MS);
    }
    if (events.length > shown.length) {
      const more = h("div", { class: "cam-banner is-more" }, `+${events.length - shown.length} more violations`);
      banners.prepend(more);
      setTimeout(() => more.remove(), BANNER_MS);
    }
    while (banners.children.length > MAX_BANNERS + 1) banners.lastChild.remove();
  }
  const ppeChoice = new Set(["helmet", "vest"]);
  let interval = "2";

  // ---- header and totals ----
  const stat = (label, tone) => {
    const value = h("b", { class: "num" }, "0");
    return { value, el: h("div", { class: `cam-stat${tone ? ` is-${tone}` : ""}` }, h("span", { class: "eyebrow" }, label), value) };
  };
  const sLive = stat("Cameras live"), sWorkers = stat("Workers on screen"),
    sNow = stat("Missing PPE now", "bad"), sTotal = stat("Violations logged", "bad");

  // With cameras on the wall the page takes the whole window; the menu button brings the sidebar back.
  const shell = document.querySelector(".shell");
  let menuOpen = false;
  const menuBtn = h("button", { type: "button", class: "btn", onclick: () => { menuOpen = !menuOpen; applyFull(); } }, "Menu");
  function applyFull() {
    const full = data.totals.live > 0 && !menuOpen;
    shell?.classList.toggle("cam-full", full);
    menuBtn.hidden = data.totals.live === 0;
    menuBtn.textContent = menuOpen ? "Hide menu" : "Menu";
  }

  const reportBtn = h("a", { class: "btn", href: api.cameras.reportUrl, download: "", title: "Download a PDF report of the detections so far" },
    h("span", { html: DOWNLOAD_ICON }), "Download report");
  const addBtn = h("button", { type: "button", class: "btn btn-primary", onclick: () => setPanel(!panelOpen) }, "Add cameras");
  const stopAllBtn = h("button", { type: "button", class: "btn", onclick: stopAll }, "Remove all");

  // ---- add-cameras panel ----
  const panelError = h("div", { class: "alert", role: "alert", hidden: true });
  const chips = h("div", { class: "cam-picks" });
  const ppeBox = h("div", { class: "cam-ppe" }, config.ppe_items.map((i) => {
    const input = h("input", { type: "checkbox", checked: ppeChoice.has(i.key),
      onchange: () => { input.checked ? ppeChoice.add(i.key) : ppeChoice.delete(i.key); } });
    return h("label", { class: "cam-check" }, input, h("span", {}, i.label));
  }));
  const intervalSel = h("select", { onchange: () => { interval = intervalSel.value; } },
    ["1", "2", "5"].map((v) => h("option", { value: v, selected: v === interval }, `every ${v} s`)));
  const startBtn = h("button", { type: "button", class: "btn btn-primary", onclick: () => startSelected() }, "Start selected");
  const allBtn = h("button", { type: "button", class: "btn", onclick: () => startSelected(true) }, "Start all");
  const panel = h("section", { class: "card card-pad cam-panel", hidden: true },
    h("div", { class: "cam-panel-head" },
      h("div", {}, h("h2", {}, "Choose cameras"),
        h("p", { class: "hint" }, "Each camera plays its video in a loop, like an RTSP feed that never ends.")),
      h("button", { type: "button", class: "cam-x", "aria-label": "Close", html: CLOSE_ICON, onclick: () => setPanel(false) })),
    panelError, chips,
    h("div", { class: "cam-panel-opts" },
      h("div", {}, h("span", { class: "eyebrow" }, "Required PPE"), ppeBox),
      h("label", { class: "cam-interval" }, h("span", { class: "eyebrow" }, "Check each worker"), intervalSel)),
    h("div", { class: "cam-panel-actions" }, startBtn, allBtn,
      h("span", { class: "hint" }, "Every camera on the wall adds load: on a CPU-only machine, start only the ones you need.")));
  let panelOpen = false;
  function setPanel(v) {
    panelOpen = v;
    panel.hidden = !v;
    panelError.hidden = true;
    if (v) drawChips();
  }

  function drawChips() {
    chips.replaceChildren(...data.cameras.map((c) => {
      const on = c.active;
      const input = h("input", { type: "checkbox", disabled: on, checked: on || picked.has(c.id),
        onchange: () => { input.checked ? picked.add(c.id) : picked.delete(c.id); } });
      return h("label", { class: `cam-pick${on ? " is-on" : ""}` }, input,
        h("span", { class: "cam-pick-name" }, c.name),
        h("span", { class: "cam-pick-meta num" }, on ? "on the wall" : fmt.time(c.duration_sec)));
    }));
  }

  async function startSelected(all = false) {
    const ids = all ? data.cameras.filter((c) => !c.active).map((c) => c.id) : [...picked];
    panelError.hidden = true;
    if (!ids.length) { showPanelError(all ? "Every camera is already on the wall." : "Pick at least one camera."); return; }
    if (!ppeChoice.size) { showPanelError("Choose at least one PPE item to check."); return; }
    startBtn.disabled = allBtn.disabled = true;
    startBtn.textContent = "Starting…";
    try {
      await api.cameras.start({ camera_ids: ids, api_key: apiKey.get() || null,
        required_ppe: [...ppeChoice].join(","), analysis_interval_sec: Number(interval) });
      picked.clear();
      setPanel(false);
      await poll();
    } catch (e) {
      showPanelError(e.message);
    } finally {
      startBtn.disabled = allBtn.disabled = false;
      startBtn.textContent = "Start selected";
    }
  }
  function showPanelError(msg) {
    panelError.hidden = false;
    panelError.replaceChildren(h("p", {}, msg));
  }

  async function stopAll() {
    if (!data.totals.live || !confirm("Remove every camera from the wall? Violations found so far stay in the list.")) return;
    await api.cameras.stopAll();
    await poll();
  }

  async function remove(id) {
    if (enlarged?.id === id) enlarged.close();
    try { await api.cameras.remove(id); } catch { /* already gone */ }
    await poll();
  }

  // ---- the wall ----
  const wall = h("div", { class: "cam-wall" });
  const wallEmpty = h("div", { class: "cam-empty" },
    h("h2", {}, "No cameras on the wall"),
    h("p", {}, "Add the cameras you want to watch. Each one plays its video in a loop, with workers tracked and PPE checked live."),
    h("button", { type: "button", class: "btn btn-primary", onclick: () => setPanel(true) }, "Add cameras"));

  function makeTile(c) {
    const img = h("img", { class: "cam-img", alt: `${c.name} live view` });
    const wait = h("div", { class: "cam-wait" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {}, "Connecting…"));
    const badge = h("span", { class: "cam-badge" }, h("i"), "LIVE");
    const loop = h("span", { class: "cam-loop num" });
    const info = h("span", { class: "cam-info" });
    const alert = h("span", { class: "cam-alert" });
    const expand = h("button", { type: "button", class: "cam-btn", title: "Enlarge", "aria-label": `Enlarge ${c.name}`, html: EXPAND_ICON,
      onclick: (e) => { e.stopPropagation(); enlarge(c.id); } });
    const rm = h("button", { type: "button", class: "cam-btn", title: "Remove from dashboard", "aria-label": `Remove ${c.name} from the dashboard`,
      html: REMOVE_ICON, onclick: (e) => { e.stopPropagation(); remove(c.id); } });
    const el = h("article", { class: "cam-tile", tabindex: "0", "aria-label": c.name,
      onclick: () => enlarge(c.id), onkeydown: (e) => { if (e.key === "Enter") enlarge(c.id); } },
      h("div", { class: "cam-frame" }, img, wait,
        h("div", { class: "cam-top" }, h("b", { class: "cam-name" }, c.name), badge, h("span", { class: "cam-tools" }, expand, rm))),
      h("div", { class: "cam-foot" }, info, alert, loop));
    const tile = { id: c.id, el, img, wait, badge, info, alert, loop, alertTimer: null };
    tile.stop = playCamera(c.id, "thumb", img, () => tiles.get(c.id) === tile);
    return tile;
  }

  function drawWall() {
    const active = data.cameras.filter((c) => c.active);
    for (const [id, t] of tiles) {
      if (!active.some((c) => c.id === id)) { t.stop(); clearTimeout(t.alertTimer); t.el.remove(); tiles.delete(id); }
    }
    for (const c of active) {
      let t = tiles.get(c.id);
      if (!t) { t = makeTile(c); tiles.set(c.id, t); }
      if (t.el.parentNode !== wall) wall.append(t.el);
      updateTile(t, c);
    }
    // Keep tiles in the catalog's order.
    for (const c of active) wall.append(tiles.get(c.id).el);
    wallEmpty.hidden = active.length > 0;
    wall.hidden = active.length === 0;
    stopAllBtn.hidden = active.length === 0;
  }

  function updateTile(t, c) {
    const s = c.stats ?? {};
    const retrying = c.state === "retrying";
    t.wait.hidden = c.state === "live" && t.img.classList.contains("is-loaded");
    t.wait.lastChild.textContent = retrying ? (c.error ?? "Reconnecting…") : "Connecting…";
    t.wait.classList.toggle("is-error", retrying);
    t.badge.classList.toggle("is-off", c.state !== "live");
    t.badge.lastChild.textContent = c.state === "live" ? "LIVE" : retrying ? "OFFLINE" : "STARTING";
    t.loop.textContent = c.loop ? `Loop ${c.loop}` : "";
    const workers = s.workers_now ?? 0, bad = s.violating_now ?? 0;
    t.info.textContent = c.state !== "live" ? "" : c.checking ? `Checking ${plural(c.checking, "worker")}…`
      : workers ? `${plural(workers, "worker")} on screen` : "No one on screen";
    t.alert.textContent = bad ? `${bad} missing PPE` : "";
    t.el.classList.toggle("has-bad", bad > 0);
  }

  // ---- violations ----
  const feed = h("ol", { class: "cam-feed", "aria-live": "polite", "aria-label": "Violations found" });
  const feedEmpty = h("p", { class: "cam-feed-empty" });
  const filterRow = h("div", { class: "cam-filters" });
  const feedCount = h("span", { class: "pill failed", hidden: true });
  const clearBtn = h("button", { type: "button", class: "btn cam-clear", onclick: async () => {
    if (!confirm("Clear the violation list?")) return;
    await api.cameras.clearViolations();
    seen.clear();
    await poll();
  } }, "Clear");

  function visibleEvents() {
    return filter ? data.violations.filter((e) => e.camera_id === filter) : data.violations;
  }

  function drawFilters() {
    const withEvents = [...new Map(data.violations.map((e) => [e.camera_id, e.camera_name])).entries()];
    if (filter && !withEvents.some(([id]) => id === filter)) filter = null;
    const chip = (id, label, count) => h("button", { type: "button", class: "cam-chip", "aria-pressed": String(filter === id),
      onclick: () => { filter = id; drawFeed(); drawFilters(); } }, label, h("b", { class: "num" }, String(count)));
    filterRow.replaceChildren(chip(null, "All", data.violations.length),
      ...withEvents.map(([id, name]) => chip(id, name, data.violations.filter((e) => e.camera_id === id).length)));
    filterRow.hidden = withEvents.length < 2;
  }

  function drawFeed() {
    const events = visibleEvents();
    const items = toLightboxItems(events);
    feedCount.hidden = !events.length;
    feedCount.textContent = String(events.length);
    feedEmpty.hidden = events.length > 0;
    feedEmpty.textContent = data.totals.live
      ? "No violations yet. They appear here the moment a worker is seen without the required PPE."
      : "Start a camera to begin watching for violations.";
    feed.replaceChildren(...events.map((e, i) => {
      const thumb = e.crop_url ? h("img", { src: e.crop_url, alt: "", loading: "lazy" }) : h("span", { class: "cam-nothumb" });
      return h("li", { class: `cam-card${seen.has(e.id) ? "" : " is-new"}` },
        h("button", { type: "button", class: "cam-card-btn", onclick: () => openLightbox(items, i) },
          h("span", { class: "cam-thumb" }, thumb),
          h("span", { class: "cam-card-body" },
            h("b", { class: "cam-what" }, e.violations.join(" · ")),
            h("span", { class: "cam-where" }, `${e.camera_name}${e.track_id != null ? ` · Worker #${e.track_id}` : ""}`),
            h("span", { class: "cam-when num" }, `${timeOf(e.detected_at)} · loop ${e.loop}${e.resolved ? " · PPE back on" : ""}`))));
    }));
  }

  const analytics = createAnalytics();

  // ---- enlarged camera ----
  function enlarge(id) {
    if (enlarged) enlarged.close();
    const cam = data.cameras.find((c) => c.id === id);
    if (!cam) return;
    const img = h("img", { class: "cam-big-img", alt: `${cam.name} live view` });
    const stats = h("div", { class: "cam-big-stats" });
    const list = h("ol", { class: "cam-feed" });
    const empty = h("p", { class: "cam-feed-empty" }, "No violations from this camera yet.");
    const onKey = (e) => { if (e.key === "Escape" && !document.querySelector(".lightbox")) close(); };
    const close = () => {
      stop();
      document.removeEventListener("keydown", onKey);
      el.remove();
      enlarged = null;
    };
    const el = h("div", { class: "cam-modal", role: "dialog", "aria-modal": "true", "aria-label": `${cam.name} enlarged`,
      onclick: (e) => { if (e.target === el) close(); } },
      h("div", { class: "cam-modal-panel" },
        h("header", { class: "cam-modal-head" },
          h("div", {}, h("span", { class: "eyebrow" }, "RTSP simulation · looping"), h("h2", {}, cam.name)),
          h("div", { class: "cam-modal-actions" },
            h("button", { type: "button", class: "btn", onclick: () => remove(id) }, "Remove from dashboard"),
            h("button", { type: "button", class: "cam-x", "aria-label": "Close", html: CLOSE_ICON, onclick: close }))),
        h("div", { class: "cam-modal-body" },
          h("div", { class: "cam-big" }, img, h("span", { class: "cam-badge" }, h("i"), "LIVE")),
          h("aside", { class: "cam-modal-side" }, stats, h("h3", {}, "Violations from this camera"), empty, list))));
    document.body.append(el);
    document.addEventListener("keydown", onKey);
    const stop = playCamera(id, "full", img, () => enlarged?.id === id);
    enlarged = { id, el, close, stats, list, empty };
    drawEnlarged();
  }

  function drawEnlarged() {
    if (!enlarged) return;
    const cam = data.cameras.find((c) => c.id === enlarged.id);
    if (!cam?.active) { enlarged.close(); return; }
    const s = cam.stats ?? {};
    enlarged.stats.replaceChildren(
      ...[["On screen", s.workers_now ?? 0], ["Missing PPE now", s.violating_now ?? 0],
        ["Compliance", s.compliance == null ? "–" : `${Math.round(s.compliance * 100)}%`], ["Loop", cam.loop || "–"]]
        .map(([k, v]) => h("div", { class: "cam-stat" }, h("span", { class: "eyebrow" }, k), h("b", { class: "num" }, String(v)))));
    const events = data.violations.filter((e) => e.camera_id === cam.id);
    const items = toLightboxItems(events);
    enlarged.empty.hidden = events.length > 0;
    enlarged.list.replaceChildren(...events.map((e, i) => h("li", { class: "cam-card" },
      h("button", { type: "button", class: "cam-card-btn", onclick: () => openLightbox(items, i) },
        h("span", { class: "cam-thumb" }, e.crop_url ? h("img", { src: e.crop_url, alt: "", loading: "lazy" }) : h("span", { class: "cam-nothumb" })),
        h("span", { class: "cam-card-body" }, h("b", { class: "cam-what" }, e.violations.join(" · ")),
          h("span", { class: "cam-where" }, e.track_id != null ? `Worker #${e.track_id}` : "Worker"),
          h("span", { class: "cam-when num" }, `${timeOf(e.detected_at)} · loop ${e.loop}`))))));
  }

  // ---- polling ----
  function bump(el, text) {
    if (el.textContent !== text) el.textContent = text;
  }

  function draw() {
    const t = data.totals;
    bump(sLive.value, String(t.live));
    bump(sWorkers.value, String(t.workers_now));
    bump(sNow.value, String(t.violating_now));
    bump(sTotal.value, String(t.violations));
    sNow.el.classList.toggle("has-value", t.violating_now > 0);
    drawWall();
    drawFilters();
    drawFeed();
    drawEnlarged();
    analytics.update(data.analytics);
    applyFull();
    if (panelOpen) drawChips();

    // A violation found while watching flags its tile and raises the alert.
    const fresh = [];
    for (const e of data.violations) {
      if (!seen.has(e.id) && !firstPoll) {
        fresh.push(e);
        const tile = tiles.get(e.camera_id);
        if (tile) {
          tile.el.classList.add("is-alert");
          clearTimeout(tile.alertTimer);
          tile.alertTimer = setTimeout(() => tile.el.classList.remove("is-alert"), ALERT_MS);
        }
      }
      seen.add(e.id);
    }
    if (fresh.length) raiseAlert(fresh.reverse()); // oldest first, so the newest ends up on top
    firstPoll = false;
  }

  async function poll() {
    clearTimeout(timer);
    try {
      data = await api.cameras.state();
      draw();
    } catch { /* server briefly unreachable: keep what is shown */ }
    if (!disposed) timer = setTimeout(poll, POLL_MS);
  }

  view.replaceChildren(h("div", { class: "page cam-page" },
    h("header", { class: "cam-head" },
      h("div", {}, h("span", { class: "eyebrow" }, "Site monitoring"), h("h1", {}, "Live camera wall"),
        h("p", { class: "hint" }, "Simulated RTSP cameras, each looping its video. Workers are tracked and PPE is checked live.")),
      h("div", { class: "cam-head-actions" }, menuBtn, stopAllBtn, reportBtn, addBtn)),
    h("div", { class: "cam-stats" }, sLive.el, sWorkers.el, sNow.el, sTotal.el),
    panel,
    h("div", { class: "cam-layout" },
      h("section", { class: "cam-main", "aria-label": "Cameras" }, wallEmpty, wall),
      h("section", { class: "card card-pad cam-side", "aria-label": "Violations" },
        h("div", { class: "cam-side-head" }, h("h2", {}, "Violations ", feedCount), clearBtn),
        filterRow, feedEmpty, feed),
      analytics.el)));

  setPanel(!data.totals.live);
  draw();
  timer = setTimeout(poll, POLL_MS);

  return () => {
    disposed = true;
    clearTimeout(timer);
    shell?.classList.remove("cam-full");
    clearTimeout(glow.timer);
    glow.remove();
    banners.remove();
    enlarged?.close();
    for (const t of tiles.values()) { t.stop(); clearTimeout(t.alertTimer); }
    tiles.clear();
  };
}
