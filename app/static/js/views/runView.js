// Run view: live progress while processing, results dashboard when done.

import { api, ServerUnreachable } from "../api.js";
import { timelineChart } from "../charts.js";
import { openLightbox } from "../lightbox.js";
import { openProfile } from "../profile.js";
import { fmt, h, icons, plural, statusPill } from "../ui.js";

const POLL_MS = 1000;
const EVENTS_PAGE = 24;

let tuningDefaults = null; // developer-settings defaults, to mark runs made with custom settings

export async function renderRun(root, runId, { onChanged, onDeleted }) {
  if (!tuningDefaults) tuningDefaults = await api.dev.get().then((s) => s.defaults).catch(() => null);
  let timer = null;
  let disposed = false;
  let lastStatus = null;

  async function tick() {
    if (disposed) return;
    let run;
    try {
      run = await api.getRun(runId);
    } catch (e) {
      if (e instanceof ServerUnreachable) {
        // Keep polling: the server may be restarting.
        root.replaceChildren(h("div", { class: "page" }, h("div", { class: "alert", role: "alert" },
          h("h3", {}, "Server not reachable"), h("p", {}, e.message), h("p", { class: "hint" }, "Retrying every few seconds…"))));
        timer = setTimeout(tick, 3000);
        return;
      }
      root.replaceChildren(notFound(e.message));
      return;
    }
    if (disposed) return;
    const status = run.meta.status;
    if (status === "queued" || status === "running") {
      renderProgress(root, run);
      timer = setTimeout(tick, POLL_MS);
    } else if (status === "completed") {
      renderDashboard(root, run, { onDeleted });
    } else {
      renderFailed(root, run, { onDeleted });
    }
    if (lastStatus && lastStatus !== status) onChanged();
    lastStatus = status;
  }

  await tick();
  return () => { disposed = true; clearTimeout(timer); };
}

// ---------------------------------------------------------------- shared bits

function header(run, actions = []) {
  const m = run.meta;
  const items = m.options.required_ppe.map((k) => k[0].toUpperCase() + k.slice(1)).join(" + ");
  return h("div", { class: "run-head" },
    h("div", { class: "title" },
      h("span", { class: "eyebrow" }, "Inspection"),
      h("h1", {}, m.name),
      h("div", { class: "sub" },
        statusPill(m.status),
        h("span", {}, fmt.dateTime(m.created_at)),
        h("span", {}, "·"),
        h("span", { class: "num" }, `${m.video.width}×${m.video.height} · ${fmt.time(m.video.duration_sec)}`))),
    h("div", { class: "actions" }, actions),
    h("div", { class: "chips", style: "flex-basis:100%" },
      h("span", { class: "chip" }, `Checks: ${items}`),
      h("span", { class: "chip" }, `Every ${m.options.analysis_interval_sec}s`),
      m.options.max_seconds ? h("span", { class: "chip" }, `First ${m.options.max_seconds}s only`) : null,
      h("span", { class: "chip" }, m.key_source === "server" ? "Server API key" : "Entered API key"),
      m.moondream_endpoint ? h("span", { class: "chip", title: `Endpoint: ${m.moondream_endpoint}` },
        `Model: ${m.options.tuning.moondream_model}`) : null,
      customChip(m.options.tuning),
      m.rerun_of ? h("a", { class: "chip chip-link", href: `#run/${encodeURIComponent(m.rerun_of)}` }, "Re-run of an earlier run") : null));
}

// "Run again": inspect the same video as a new run, with the current developer settings
// or the ones this run used. The panel asks for a key only if the server has none.
function rerunButton(run) {
  const wrap = h("div", { class: "rerun" });
  const btn = h("button", { class: "btn", type: "button", "aria-expanded": "false", onclick: toggle }, "Run again");
  let panel = null;

  async function toggle() {
    if (panel) return closePanel();
    const [config, dev] = await Promise.all([api.config(), api.dev.get().catch(() => null)]);
    const n = dev?.changes.length ?? 0;
    const radio = (value, label, note, checked) => h("label", { class: "rerun-opt" },
      h("input", { type: "radio", name: "rerun-settings", value, checked }),
      h("span", {}, h("b", {}, label), h("span", { class: "hint" }, note)));
    const key = h("input", { class: "input", type: "password", id: "rerun-key", autocomplete: "off",
      placeholder: config.server_key_configured ? "Empty = the server's key" : "Moondream API key" });
    const error = h("p", { class: "form-error", hidden: true });
    const start = h("button", { class: "btn btn-primary", type: "button", onclick: go }, "Start");
    panel = h("div", { class: "rerun-panel card", role: "dialog", "aria-label": "Run this video again" },
      h("h3", {}, "Run this video again"),
      h("p", { class: "hint" }, "A new run with the same PPE items, check interval and time limit. This run stays as it is."),
      h("fieldset", { class: "rerun-opts" }, h("legend", { class: "label" }, "Settings"),
        radio("current", "Current developer settings",
          n ? `${n} change${n === 1 ? "" : "s"} from the defaults, as saved now` : "The defaults (nothing changed)", true),
        radio("original", "This run's settings", "Exactly as this run was made", false)),
      h("label", { class: "label", for: "rerun-key" }, "Moondream API key"), key,
      error,
      h("div", { class: "rerun-actions" }, h("button", { class: "btn btn-ghost", type: "button", onclick: closePanel }, "Cancel"), start));
    wrap.append(panel);
    btn.setAttribute("aria-expanded", "true");
    document.addEventListener("keydown", onKey);
    panel.querySelector("input[type=radio]:checked").focus();

    async function go() {
      error.hidden = true;
      start.disabled = true;
      start.textContent = "Checking key…";
      try {
        const settings = panel.querySelector("input[name=rerun-settings]:checked").value;
        const created = await api.rerun(run.meta.id, { settings, api_key: key.value.trim() || null });
        closePanel();
        location.hash = `#run/${encodeURIComponent(created.meta.id)}`;
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
        start.disabled = false;
        start.textContent = "Start";
      }
    }
  }
  function onKey(e) { if (e.key === "Escape") closePanel(); }
  function closePanel() {
    panel?.remove();
    panel = null;
    btn.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", onKey);
  }
  wrap.append(btn);
  return wrap;
}

// Shows which developer settings this run used, when they differ from the defaults.
function customChip(t) {
  if (!t) return null;
  const changed = [];
  const walk = (a, b, prefix) => {
    for (const k of Object.keys(a)) {
      if (a[k] && typeof a[k] === "object" && b?.[k] && typeof b[k] === "object") walk(a[k], b[k], `${prefix}${k}.`);
      else if (JSON.stringify(a[k]) !== JSON.stringify(b?.[k])) changed.push(`${prefix}${k}: ${JSON.stringify(a[k])}`);
    }
  };
  walk(t, tuningDefaults ?? t, "");
  if (!changed.length) return null;
  return h("span", { class: "chip chip-dev", title: changed.join("\n") }, `Custom settings (${changed.length})`);
}

function deleteButton(run, onDeleted) {
  return h("button", { class: "btn btn-danger", type: "button", onclick: async (e) => {
    const btn = e.currentTarget;
    if (btn.dataset.confirm !== "1") {
      btn.dataset.confirm = "1";
      btn.lastChild.textContent = "Click again to delete";
      setTimeout(() => { if (btn.isConnected) { btn.dataset.confirm = ""; btn.lastChild.textContent = "Delete"; } }, 3000);
      return;
    }
    btn.disabled = true;
    try {
      await api.deleteRun(run.meta.id);
      onDeleted();
    } catch (err) {
      btn.disabled = false;
      btn.lastChild.textContent = err.message;
    }
  } }, h("span", { html: icons.trash, style: "display:inline-flex" }), h("span", {}, "Delete"));
}

function notFound(message) {
  return h("div", { class: "page" }, h("div", { class: "card empty-state" },
    h("h2", {}, "Run not found"),
    h("p", {}, message),
    h("a", { class: "btn btn-primary", href: "#new" }, "New inspection")));
}

// ---------------------------------------------------------------- progress

// While a run is processing, the page is built once and then updated in place every poll:
// numbers change, new violations and people slide in, and the preview swaps only once the
// next frame has loaded, so nothing flickers or jumps.
function renderProgress(root, run) {
  let page = root.querySelector("[data-view=progress]");
  if (!page || page.dataset.run !== run.meta.id) {
    page = buildLive(run);
    root.replaceChildren(page);
  }
  page.update(run);
}

// Swaps an <img> to `url` once the new image has loaded, instead of blanking while it loads.
function swapImage(img, url) {
  const next = new Image();
  next.onload = () => { img.src = url; img.classList.add("is-loaded"); };
  next.src = url;
}

function bump(el, text) {
  if (el.textContent === text) return;
  el.textContent = text;
  el.classList.remove("bump");
  void el.offsetWidth; // restart the animation
  el.classList.add("bump");
}

function buildLive(run) {
  const m = run.meta;
  const id = m.id;
  const duration = m.options.max_seconds ? Math.min(m.options.max_seconds, m.video.duration_sec) : m.video.duration_sec;
  const page = h("div", { class: "page", "data-view": "progress", "data-run": id });
  const headSlot = h("div");

  // Preview with a live badge
  const previewImg = h("img", { alt: "Latest checked frame with detections" });
  const waiting = h("div", { class: "wait" });
  const clock = h("span", { class: "num" });
  const badge = h("span", { class: "live-badge" }, h("i"), h("span", { class: "live-text" }, "Live"), clock);
  const preview = h("div", { class: "preview live-preview" }, previewImg, waiting, badge);

  // Progress and counters
  const stage = h("span", { class: "eyebrow" });
  const pct = h("div", { class: "big-pct num" });
  const bar = h("i");
  const eta = h("p", { class: "hint live-eta" });
  const activity = h("p", { class: "live-activity" });
  const counter = (label) => {
    const value = h("b", { class: "num" }, "0");
    const note = h("span");
    return { value, note, el: h("div", { class: "live-stat" }, h("span", { class: "eyebrow" }, label), value, note) };
  };
  const cViol = counter("Violations"), cPeople = counter("People"), cChecks = counter("Worker checks"), cComp = counter("Compliance");
  cViol.el.classList.add("is-bad");

  // Chart, violations feed, people strip
  const chartSlot = h("div", { class: "chart-wrap" });
  const chartCard = h("section", { class: "card card-pad" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "Compliance over time"), h("p", {}, "Workers per checked frame, filled in as the video is inspected.")),
      h("div", { class: "legend" },
        h("span", {}, h("i", { style: "background:var(--good)" }), "PPE OK"),
        h("span", {}, h("i", { style: "background:var(--critical)" }), "PPE violation"),
        h("span", {}, h("i", { style: "background:var(--warning)" }), "Item not visible"),
        h("span", {}, h("i", { style: "background:var(--muted)" }), "Not checked"))),
    chartSlot);
  const feed = h("div", { class: "events live-feed" });
  const feedEmpty = h("div", { class: "empty-note" }, h("span", { html: icons.check }),
    "No violations so far. Every worker checked so far has the required PPE.");
  const feedCount = h("span", { class: "pill failed", hidden: true });
  const feedCard = h("section", { class: "card card-pad" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "Violations found so far ", feedCount),
        h("p", {}, "Newest first. A violation appears once it has been seen in two checks in a row. Click one to open it."))),
    feedEmpty, feed);
  const strip = h("div", { class: "live-people" });
  const stripCard = h("section", { class: "card card-pad" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "People found so far"),
      h("p", {}, "Each worker keeps one ID. Click a person to see every photo of them taken so far."))),
    strip);

  page.append(headSlot,
    h("section", { class: "card progress-card" }, preview,
      h("div", { class: "stats" }, stage, pct, h("div", { class: "bar" }, bar), eta, activity,
        h("div", { class: "live-stats" }, cViol.el, cPeople.el, cChecks.el, cComp.el),
        h("p", { class: "hint" }, "You can leave this page; the run keeps going and appears in the sidebar."))),
    chartCard, feedCard, stripCard);

  const cards = new Map(); // incident key -> {el, img, time}
  const chips = new Map(); // person id -> {el, status}
  let lastFrames = -1, lastPoints = -1, lastStatus = null;
  const started = { t: performance.now(), f: run.progress_fraction };

  page.update = (run) => {
    const m = run.meta;
    const s = run.summary;
    if (m.status !== lastStatus) {
      headSlot.replaceChildren(header(run));
      lastStatus = m.status;
    }
    const frac = run.progress_fraction;
    const queued = m.status === "queued";
    stage.textContent = queued ? "Queued" : "Inspecting";
    pct.textContent = `${Math.round(frac * 100)}%`;
    bar.style.width = `${frac * 100}%`;
    clock.textContent = ` ${fmt.time(frac * duration)} / ${fmt.time(duration)}`;
    badge.hidden = queued || !m.progress.frames_done;
    // Time left, from the speed since this page opened.
    const done = frac - started.f, secs = (performance.now() - started.t) / 1000;
    eta.textContent = queued ? "Waiting for the previous run to finish."
      : done > 0.03 && secs > 4 ? `About ${fmt.duration((secs / done) * (1 - frac))} left`
        : "Estimating time left…";

    // The preview is refreshed as frames are written, so it plays along with the inspection.
    if (m.progress.frames_done !== lastFrames && m.progress.frames_done > 0) {
      lastFrames = m.progress.frames_done;
      swapImage(previewImg, api.urls.preview(id));
      waiting.hidden = true;
    }
    // What the pipeline is doing right now, so a slow Moondream answer doesn't look like a freeze.
    const checking = m.progress.checking ?? 0;
    activity.hidden = queued || lastFrames < 1;
    activity.classList.toggle("is-checking", checking > 0);
    activity.replaceChildren(h("span", { class: "spinner small", "aria-hidden": "true" }),
      checking ? `Asking Moondream about ${plural(checking, "worker")} at ${fmt.time(frac * duration)}…`
        : "Tracking workers and drawing the video…");
    if (lastFrames < 1) {
      waiting.hidden = false;
      waiting.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {},
        queued ? "Waiting for the previous run to finish…"
          : "Starting up: loading the person detector and checking the first workers with Moondream…"));
    }
    if (!s) return;

    bump(cViol.value, String(s.events.length));
    cViol.note.textContent = s.events.length ? `${plural(s.violation_checks, "worker check")}` : "none yet";
    cViol.el.classList.toggle("has-value", s.events.length > 0);
    const flagged = s.people.filter((p) => p.state === "flagged").length;
    bump(cPeople.value, String(s.people.length));
    cPeople.note.textContent = flagged ? `${flagged} flagged` : "none flagged";
    bump(cChecks.value, String(s.worker_checks));
    cChecks.note.textContent = `${plural(s.frames_analysed, "frame")}`;
    bump(cComp.value, s.compliance_rate == null ? "–" : `${Math.round(s.compliance_rate * 100)}%`);
    cComp.note.textContent = "of judged checks";

    if (s.timeline.length !== lastPoints) {
      lastPoints = s.timeline.length;
      timelineChart(chartSlot, s.timeline, { duration, interval: m.options.analysis_interval_sec });
    }

    // Violations: add new incidents at the top, update ones that are still going.
    feedEmpty.hidden = s.events.length > 0;
    feedCount.hidden = !s.events.length;
    feedCount.textContent = String(s.events.length);
    const items = eventItems(run, [...s.events].reverse());
    items.forEach((e, i) => {
      const key = `${e.track_id}-${e.time_sec}`;
      let c = cards.get(key);
      if (!c) {
        const img = h("img", { alt: `Worker at ${fmt.timePrecise(e.time_sec)}` });
        const time = h("span", { class: "time" });
        const tags = h("div", { class: "tags" });
        const el = h("button", { class: "event is-new", type: "button", title: "Open image" },
          h("div", { class: "thumb" }, img), h("div", { class: "body" }, time, tags));
        c = { el, img, time, tags, snap: null };
        cards.set(key, c);
      }
      c.el.onclick = () => openLightbox(items, i);
      c.time.textContent = [e.track_id != null ? `#${e.track_id}` : null, incidentTime(e),
        e.checks > 1 ? plural(e.checks, "check") : null].filter(Boolean).join(" · ");
      if (c.tags.textContent !== e.violations.join("")) c.tags.replaceChildren(...e.violations.map((v) => h("span", { class: "tag-bad" }, v)));
      if (e.cropUrl && e.cropUrl !== c.snap) { c.snap = e.cropUrl; swapImage(c.img, e.cropUrl); }
      if (feed.children[i] !== c.el) feed.insertBefore(c.el, feed.children[i] ?? null);
    });

    // People: one chip each, in order of appearance; status colour changes in place.
    const order = [...s.people].sort((a, b) => a.first_seen_sec - b.first_seen_sec || a.id - b.id);
    for (const p of order) {
      let c = chips.get(p.id);
      if (!c) {
        const img = h("img", { alt: `Person ${p.id}` });
        const status = h("span", { class: "status" });
        const el = h("button", { type: "button", class: "live-person is-new", title: `Open person #${p.id}`,
          onclick: () => openProfile(run, p.id) },
          h("div", { class: "photo" }, img, h("span", { class: "pid" }, `#${p.id}`)), status);
        c = { el, img, status, checks: -1 };
        chips.set(p.id, c);
        strip.append(el);
      }
      c.el.className = `live-person is-${{ flagged: "bad", unverified: "unverified", brief: "brief", ok: "ok" }[p.state] ?? "ok"}${c.el.classList.contains("is-new") ? " is-new" : ""}`;
      c.status.textContent = p.state === "flagged" ? p.status_label.replace(/^PPE Violation \(\d+\) - /, "Missing: ") : p.status_label;
      // The photo is replaced when a bigger view of the person is saved; refresh it as checks come in.
      if (p.photo && p.checks !== c.checks) { c.checks = p.checks; swapImage(c.img, `${api.urls.person(id, p.photo)}?v=${p.checks}`); }
    }
    if (!s.people.length && !strip.children.length) strip.replaceChildren(h("p", { class: "hint" }, "Nobody checked yet."));
    else strip.querySelector(":scope > p.hint")?.remove();
  };
  return page;
}

// ---------------------------------------------------------------- failed

function renderFailed(root, run, { onDeleted }) {
  root.replaceChildren(h("div", { class: "page" },
    header(run, [rerunButton(run), deleteButton(run, onDeleted)]),
    h("div", { class: "alert", role: "alert" },
      h("h3", {}, "This run failed"),
      h("p", {}, run.meta.error || "Unknown error.")),
    h("div", {}, h("a", { class: "btn btn-primary", href: "#new" }, "Start a new inspection"))));
}

// ---------------------------------------------------------------- dashboard

function kpi(label, value, note, tone) {
  return h("div", { class: `card kpi ${tone ? `is-${tone}` : ""}` },
    h("span", { class: "eyebrow" }, label),
    h("div", { class: "value" }, value),
    h("span", { class: "note" }, note));
}

function timelineCard(s, m, video) {
  const box = h("div", { class: "chart-wrap" });
  const card = h("section", { class: "card card-pad" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "Compliance over time"),
        h("p", {}, video ? "Workers per checked frame. Click a column to jump the video there." : "Workers per checked frame so far.")),
      h("div", { class: "legend" },
        h("span", {}, h("i", { style: "background:var(--good)" }), "PPE OK"),
        h("span", {}, h("i", { style: "background:var(--critical)" }), "PPE violation"),
        s.timeline.some((p) => p.unverified)
          ? h("span", {}, h("i", { style: "background:var(--warning)" }), "Item not visible") : null,
        s.timeline.some((p) => p.too_far || p.unchecked)
          ? h("span", {}, h("i", { style: "background:var(--muted)" }), "Not checked (too far, new or failed)") : null)),
    box);
  const chart = timelineChart(box, s.timeline, {
    duration: m.options.max_seconds ? Math.min(m.options.max_seconds, m.video.duration_sec) : m.video.duration_sec,
    interval: m.options.analysis_interval_sec,
    onSeek: video ? (t) => { video.currentTime = t + 0.05; video.play().catch(() => {}); } : null,
  });
  if (video) {
    const sync = () => chart.setPlayhead(video.currentTime);
    video.addEventListener("timeupdate", sync);
    video.addEventListener("seeked", sync);
  }
  return card;
}

function byTypeCard(s) {
  const max = Math.max(1, ...s.by_type.map((t) => t.count));
  const body = s.by_type.length
    ? h("div", { class: "hbars" }, s.by_type.map((t) => h("div", { class: "hbar" },
        h("div", { class: "row" }, h("span", {}, t.label), h("b", { class: "num" }, t.count)),
        h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(t.count / max) * 100}%` })))))
    : h("div", { class: "empty-note" }, h("span", { html: icons.check }), "No violations: nobody's status showed missing PPE.");

  // Share of worker checks where an item couldn't be seen; above half, its results say little.
  const share = (t) => t.count / Math.max(1, s.worker_checks);
  const low = s.unverified.filter((t) => share(t) > 0.5).map((t) => t.label);
  const unv = s.unverified.length || s.too_small_checks
    ? [h("div", { class: "divider" }),
       h("h3", {}, "Couldn't verify"),
       h("p", { class: "hint", style: "margin:4px 0 12px" }, "Item not visible (worker cut off or blocked). Not counted as violations, and not as compliant either."),
       low.length ? h("p", { class: "hint warn-note", style: "margin:0 0 12px" },
         `Low coverage: ${low.join(", ")} could only be checked in a minority of checks, so a clean result for ${low.length > 1 ? "them" : "it"} means little.`) : null,
       h("div", { class: "hbars" }, s.unverified.map((t) => h("div", { class: "hbar is-warn" },
         h("div", { class: "row" }, h("span", {}, t.label),
           h("b", { class: "num" }, `${t.count} of ${s.worker_checks} · ${Math.round(share(t) * 100)}%`)),
         h("div", { class: "track" }, h("div", { class: "fill", style: `width:${share(t) * 100}%` }))))),
       s.too_small_checks ? h("p", { class: "hint", style: "margin-top:12px" },
         `${plural(s.too_small_checks, "worker sighting")} too far away to check; left out of all figures.`) : null,
      ]
    : [];

  return h("section", { class: "card card-pad" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "Violations by type"), h("p", {}, "Counted per incident: one person missing the item over consecutive checks."))),
    body, ...unv);
}

function eventsCard(run, s, video) {
  const types = s.by_type.map((t) => t.label);
  let filter = null;
  let shown = EVENTS_PAGE;
  const grid = h("div", { class: "events" });
  const more = h("div", { class: "more-row" });
  const filters = h("div", { class: "filters", role: "group", "aria-label": "Filter violations" });

  function draw() {
    const list = s.events.filter((e) => !filter || e.violations.includes(filter));
    filters.replaceChildren(...[null, ...types].map((t) => h("button", {
      type: "button", "aria-pressed": String(filter === t),
      onclick: () => { filter = t; shown = EVENTS_PAGE; draw(); },
    }, t ?? `All (${s.events.length})`)));
    const items = eventItems(run, list);
    const showInVideo = (e) => seekTo(video, e.time_sec);
    grid.replaceChildren(...list.slice(0, shown).map((e, i) => h("button", {
      class: "event", type: "button", title: "Open image",
      onclick: () => openLightbox(items, i, { onShowInVideo: showInVideo }),
    },
      h("div", { class: "thumb" }, e.snapshot
        ? h("img", { src: api.urls.snapshot(run.meta.id, e.snapshot), alt: `Worker at ${fmt.timePrecise(e.time_sec)}`, loading: "lazy" })
        : h("span", { class: "hint" }, "No snapshot")),
      h("div", { class: "body" },
        h("span", { class: "time" }, [e.track_id != null ? `#${e.track_id}` : null, incidentTime(e),
          e.checks > 1 ? plural(e.checks, "check") : null].filter(Boolean).join(" · ")),
        h("div", { class: "tags" }, e.violations.map((v) => h("span", { class: "tag-bad" }, v)))))));
    more.replaceChildren(list.length > shown
      ? h("button", { class: "btn", type: "button", onclick: () => { shown += EVENTS_PAGE; draw(); } }, `Show more (${list.length - shown} left)`)
      : "");
  }

  if (!s.events.length) {
    return h("section", { class: "card card-pad" },
      h("div", { class: "card-head" }, h("h2", {}, "Violations")),
      h("div", { class: "empty-note" }, h("span", { html: icons.check }), "No worker's status showed missing PPE at any point."));
  }
  draw();
  return h("section", { class: "card card-pad" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "Violations"), h("p", {}, "One card per incident: a person whose status showed missing PPE, from the first to the last check in a row. Click to open; use ← → to browse.")),
      filters),
    grid, more);
}

function incidentTime(e) {
  return e.end_sec != null && e.end_sec > e.time_sec
    ? `${fmt.timePrecise(e.time_sec)}–${fmt.timePrecise(e.end_sec)}` : fmt.timePrecise(e.time_sec);
}

function seekTo(video, t) {
  video.currentTime = t + 0.05;
  video.scrollIntoView({ behavior: "smooth", block: "center" });
  video.play().catch(() => {});
}

function eventItems(run, events) {
  return events.map((e) => ({
    ...e,
    cropUrl: e.snapshot ? api.urls.snapshot(run.meta.id, e.snapshot) : null,
    frameUrl: e.frame_image ? api.urls.frame(run.meta.id, e.frame_image) : null,
  }));
}

function peopleCard(run, s, video) {
  if (!s.tracked) {
    return h("section", { class: "card card-pad" },
      h("div", { class: "card-head" }, h("h2", {}, "People")),
      h("p", { class: "hint" }, "This run was made before person tracking was added. Run the video again to see each worker's ID and status."));
  }
  let filter = "all";
  const grid = h("div", { class: "people" });
  const filters = h("div", { class: "filters", role: "group", "aria-label": "Filter people" });
  const state = (p) => p.state ?? (p.ok ? "ok" : "flagged"); // older servers sent only ok
  const count = (k) => s.people.filter((p) => state(p) === k).length;
  const LABELS = [["flagged", "Flagged"], ["unverified", "Not verified"], ["brief", "Seen briefly"], ["ok", "PPE OK"]];
  const statusClass = { flagged: "bad", unverified: "unverified", brief: "brief", ok: "ok" };
  const cardClass = { flagged: "bad", unverified: "unverified", brief: "brief", ok: "ok" };

  // The card opens the person's profile: every photo of them, with counts per class.
  const open = (p) => openProfile(run, p.id, { onShowInVideo: (e) => seekTo(video, e.time_sec) });

  function draw() {
    filters.replaceChildren(...[["all", `All (${s.people.length})`],
      ...LABELS.filter(([k]) => count(k)).map(([k, label]) => [k, `${label} (${count(k)})`])]
      .map(([k, label]) => h("button", { type: "button", "aria-pressed": String(filter === k),
        onclick: () => { filter = k; draw(); } }, label)));
    const order = { flagged: 0, unverified: 1, brief: 2, ok: 3 };
    const list = s.people.filter((p) => filter === "all" || state(p) === filter)
      .sort((a, b) => order[state(a)] - order[state(b)] || a.id - b.id);
    grid.replaceChildren(...list.map((p) => {
      const st = state(p);
      const items = p.missing.map((t) => h("div", { class: "pm-row" },
        h("span", {}, t.label),
        h("span", { class: "num" }, `${t.count} of ${p.checks}`),
        h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(t.count / Math.max(1, p.checks)) * 100}%` }))));
      const detail = {
        flagged: p.incidents ? `${plural(p.incidents, "incident")} · ${fmt.time(p.violation_sec ?? 0)} in violation` : null,
        unverified: `An item couldn't be seen in ${p.unverified_checks} of ${plural(p.checks, "check")}`,
        brief: `Not checked often enough for a verdict (${plural(p.checks, "check")})`,
        ok: p.violation_checks ? `${plural(p.violation_checks, "single-check misread")} ignored by the vote` : null,
      }[st];
      return h("button", { class: `person is-${cardClass[st]}`, type: "button",
        title: `Open the profile of person #${p.id}`, onclick: () => open(p) },
        h("div", { class: "photo" },
          p.photo ? h("img", { src: api.urls.person(run.meta.id, p.photo), alt: `Person ${p.id}`, loading: "lazy" }) : null,
          h("span", { class: "pid" }, `#${p.id}`)),
        h("div", { class: "body" },
          h("span", { class: `status ${statusClass[st]}` }, p.status_label),
          detail ? h("span", { class: "occasional" }, detail) : null,
          h("span", { class: "meta num" },
            `On screen ${fmt.time(p.first_seen_sec)}–${fmt.time(p.last_seen_sec)} · ${plural(p.checks, "check")}` +
            (p.failed_checks ? ` · ${p.failed_checks} failed` : "")),
          items.length ? h("div", { class: "pm" }, items) : h("span", { class: "meta" }, "No missing item in any check")));
    }));
  }
  draw();
  return h("section", { class: "card card-pad" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "People"),
        h("p", {}, `Each worker keeps one ID through the video. A person is flagged when their status showed an item missing, i.e. it was missing in at least ${run.meta.options.tuning?.confirm_checks ?? 2} of their recent checks, the same rule as the boxes in the video. Bars show single checks.`)),
      filters),
    grid);
}

function renderDashboard(root, run, { onDeleted }) {
  const m = run.meta;
  const s = run.summary;
  const video = h("video", { src: api.urls.video(m.id), poster: api.urls.preview(m.id),
    controls: true, playsinline: true, preload: "metadata" });
  const playerMsg = h("div", { class: "player-msg", hidden: true },
    "This browser can't play the annotated video. Use the Video button above to download it.");
  // The first load can fail while the server is still converting an older video,
  // so retry once before showing the message, and hide it as soon as anything plays.
  let retried = false;
  video.addEventListener("error", () => {
    if (!retried) {
      retried = true;
      setTimeout(() => { video.src = `${api.urls.video(m.id)}?retry=1`; video.load(); }, 2500);
      return;
    }
    playerMsg.hidden = false;
  });
  for (const ev of ["loadeddata", "canplay", "playing"]) video.addEventListener(ev, () => { playerMsg.hidden = true; });

  const compliance = s.compliance_rate;
  const timeShare = s.frames_analysed ? s.frames_with_violation / s.frames_analysed : null;
  const judged = s.ok_checks + s.violation_checks;
  const attempted = s.worker_checks + (s.failed_checks ?? 0);
  const failedShare = attempted ? (s.failed_checks ?? 0) / attempted : 0;
  const notJudged = [s.unverified_checks ? `${s.unverified_checks} not verified` : null,
    s.pending_checks ? `${s.pending_checks} first checks` : null].filter(Boolean).join(", ");
  const flagged = s.people.filter((p) => (p.state ?? (p.ok ? "ok" : "flagged")) === "flagged").length;

  root.replaceChildren(h("div", { class: "page" },
    header(run, [
      h("a", { class: "btn", href: api.urls.video(m.id, true), download: "" }, h("span", { html: icons.download, style: "display:inline-flex" }), "Video"),
      h("a", { class: "btn", href: api.urls.report(m.id), download: "" }, h("span", { html: icons.download, style: "display:inline-flex" }), "Report"),
      rerunButton(run),
      deleteButton(run, onDeleted),
    ]),
    h("div", { class: "kpis" },
      kpi("Compliance", compliance == null ? "–" : h("span", {}, (compliance * 100).toFixed(0), h("small", {}, "%")),
        `${s.ok_checks} of ${judged} judged worker checks had all PPE` + (notJudged ? ` (${notJudged} left out)` : ""),
        compliance == null ? null : compliance >= 0.95 ? "good" : "bad"),
      kpi("Violations", String(s.events.length),
        s.events.length ? `${plural(s.events.length, "incident")} over ${plural(s.violation_checks, "worker check")}` : "none found",
        s.events.length ? "bad" : "good"),
      kpi("Time with a violation", timeShare == null ? "–" : h("span", {}, (timeShare * 100).toFixed(0), h("small", {}, "%")),
        `${s.frames_with_violation} of ${s.frames_analysed} checked frames`),
      s.tracked
        ? kpi("People tracked", String(s.people.length),
            `${flagged} flagged · peak ${s.peak_workers} on screen at once`, flagged ? "bad" : "good")
        : kpi("Workers on screen", String(s.peak_workers), `peak · ${s.avg_workers} on average`)),
    s.failed_checks ? h("div", { class: `warn-banner`, role: failedShare > 0.1 ? "alert" : null },
      h("b", {}, `${plural(s.failed_checks, "worker check")} failed`),
      ` (${Math.round(failedShare * 100)}% of checks). Moondream didn't answer, so ${s.failed_checks === 1 ? "that check says" : "those checks say"} nothing about the worker. ` +
      (failedShare > 0.1 ? "Results for this run are incomplete; check the server log and consider running it again." : "")) : null,
    h("div", { class: "grid-main" },
      h("section", { class: "card player" }, video,
        playerMsg,
        h("div", { class: "player-foot" },
          h("span", {}, "Annotated output"),
          h("span", { class: "num" }, `${s.frames_analysed} frames checked` +
            (s.arrival_checks ? ` · ${plural(s.arrival_checks, "arrival check")}` : "")))),
      byTypeCard(s)),
    peopleCard(run, s, video),
    timelineCard(s, m, video),
    eventsCard(run, s, video)));
}
