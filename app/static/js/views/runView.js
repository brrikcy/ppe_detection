// Run view: live progress while processing, results dashboard when done.

import { api, ServerUnreachable } from "../api.js";
import { timelineChart } from "../charts.js";
import { openLightbox } from "../lightbox.js";
import { fmt, h, icons, plural, statusPill } from "../ui.js";

const POLL_MS = 1000;
const EVENTS_PAGE = 24;

export async function renderRun(root, runId, { onChanged, onDeleted }) {
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
      h("span", { class: "chip" }, m.key_source === "server" ? "Server API key" : "Entered API key")));
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

function renderProgress(root, run) {
  const m = run.meta;
  const pct = Math.round(run.progress_fraction * 100);
  const s = run.summary;
  let page = root.querySelector("[data-view=progress]");
  if (!page) {
    page = h("div", { class: "page", "data-view": "progress" });
    root.replaceChildren(page);
  }
  const previewImg = page.querySelector(".preview img");
  const preview = m.progress.frames_analysed > 0
    ? (previewImg ?? h("img", { alt: "Latest checked frame with detections" }))
    : h("div", { class: "wait" }, m.status === "queued" ? "Waiting for the previous run to finish…" : "Checking the first frame…");
  if (preview.tagName === "IMG") preview.src = api.urls.preview(m.id);

  const totalChecks = Math.ceil((m.options.max_seconds ? Math.min(m.options.max_seconds, m.video.duration_sec) : m.video.duration_sec) / m.options.analysis_interval_sec);
  page.replaceChildren(
    header(run),
    h("section", { class: "card progress-card" },
      h("div", { class: "preview" }, preview),
      h("div", { class: "stats" },
        h("span", { class: "eyebrow" }, m.status === "queued" ? "Queued" : "Processing"),
        h("div", { class: "big-pct" }, `${pct}%`),
        h("div", { class: "bar" }, h("i", { style: `width:${pct}%` })),
        h("dl", { class: "statlist" },
          h("dt", {}, "Frames written"), h("dd", {}, `${m.progress.frames_done.toLocaleString()} / ${m.progress.frames_total.toLocaleString()}`),
          h("dt", {}, "Frames checked"), h("dd", {}, `${m.progress.frames_analysed} / ~${totalChecks}`),
          h("dt", {}, "Workers seen (peak)"), h("dd", {}, s ? s.peak_workers : "–"),
          h("dt", {}, "Checks with violations"), h("dd", {}, s ? `${s.violation_checks} / ${s.worker_checks}` : "–")),
        h("p", { class: "hint" }, "You can leave this page; the run keeps going and appears in the sidebar."))),
    s && s.timeline.length ? timelineCard(s, m, null) : null);
}

// ---------------------------------------------------------------- failed

function renderFailed(root, run, { onDeleted }) {
  root.replaceChildren(h("div", { class: "page" },
    header(run, [deleteButton(run, onDeleted)]),
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
        h("span", {}, h("i", { style: "background:var(--critical)" }), "PPE violation"))),
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

  // Table view of the same data (accessibility + exact numbers).
  const tableWrap = h("div", { class: "table-wrap", hidden: true },
    h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Time"), h("th", {}, "Workers"), h("th", {}, "PPE OK"), h("th", {}, "Violations"))),
      h("tbody", {}, s.timeline.map((p) => h("tr", {},
        h("td", {}, fmt.timePrecise(p.time_sec)), h("td", {}, p.workers),
        h("td", {}, p.workers - p.violations), h("td", {}, p.violations))))));
  const toggle = h("button", { class: "btn btn-ghost table-toggle", type: "button", onclick: () => {
    tableWrap.hidden = !tableWrap.hidden;
    toggle.textContent = tableWrap.hidden ? "Show as table" : "Hide table";
  } }, "Show as table");
  card.append(toggle, tableWrap);
  return card;
}

function byTypeCard(s) {
  const max = Math.max(1, ...s.by_type.map((t) => t.count));
  const body = s.by_type.length
    ? h("div", { class: "hbars" }, s.by_type.map((t) => h("div", { class: "hbar" },
        h("div", { class: "row" }, h("span", {}, t.label), h("b", { class: "num" }, t.count)),
        h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(t.count / max) * 100}%` })))))
    : h("div", { class: "empty-note" }, h("span", { html: icons.check }), "No violations found in any checked frame.");

  const unv = s.unverified.length
    ? [h("div", { class: "divider" }),
       h("h3", {}, "Couldn't verify"),
       h("p", { class: "hint", style: "margin:4px 0 12px" }, "Item not visible (worker cut off or blocked). Not counted as violations."),
       h("div", { class: "hbars" }, s.unverified.map((t) => h("div", { class: "hbar is-warn" },
         h("div", { class: "row" }, h("span", {}, t.label), h("b", { class: "num" }, t.count)),
         h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(t.count / Math.max(1, s.worker_checks)) * 100}%` }))))),
      ]
    : [];

  return h("section", { class: "card card-pad" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "Violations by type"), h("p", {}, "Counted per worker per checked frame."))),
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
        h("span", { class: "time" }, e.track_id != null ? `#${e.track_id} · ${fmt.timePrecise(e.time_sec)}` : fmt.timePrecise(e.time_sec)),
        h("div", { class: "tags" }, e.violations.map((v) => h("span", { class: "tag-bad" }, v)))))));
    more.replaceChildren(list.length > shown
      ? h("button", { class: "btn", type: "button", onclick: () => { shown += EVENTS_PAGE; draw(); } }, `Show more (${list.length - shown} left)`)
      : "");
  }

  if (!s.events.length) {
    return h("section", { class: "card card-pad" },
      h("div", { class: "card-head" }, h("h2", {}, "Violations")),
      h("div", { class: "empty-note" }, h("span", { html: icons.check }), "Every worker in every checked frame had the required PPE."));
  }
  draw();
  return h("section", { class: "card card-pad" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "Violations"), h("p", {}, "One card per flagged worker per checked frame. Click to open; use ← → to browse.")),
      filters),
    grid, more);
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
  // flagged: missing an item in most of their checks; some: flagged in a minority of checks; ok: never.
  const state = (p) => (!p.ok ? "bad" : p.violation_checks ? "some" : "ok");
  const count = (k) => s.people.filter((p) => state(p) === k).length;

  function open(p) {
    const events = s.events.filter((e) => e.track_id === p.id);
    const items = events.length ? eventItems(run, events) : [{
      track_id: p.id, time_sec: p.first_seen_sec, violations: [],
      cropUrl: p.photo ? api.urls.person(run.meta.id, p.photo) : null, frameUrl: null,
    }];
    openLightbox(items, 0, { onShowInVideo: (e) => seekTo(video, e.time_sec) });
  }

  function draw() {
    filters.replaceChildren(...[["all", `All (${s.people.length})`], ["bad", `Flagged (${count("bad")})`],
      ["some", `Occasional (${count("some")})`], ["ok", `Always OK (${count("ok")})`]]
      .map(([k, label]) => h("button", { type: "button", "aria-pressed": String(filter === k),
        onclick: () => { filter = k; draw(); } }, label)));
    const order = { bad: 0, some: 1, ok: 2 };
    const list = s.people.filter((p) => filter === "all" || state(p) === filter)
      .sort((a, b) => order[state(a)] - order[state(b)] || a.id - b.id);
    grid.replaceChildren(...list.map((p) => {
      const items = p.missing.map((t) => h("div", { class: "pm-row" },
        h("span", {}, t.label),
        h("span", { class: "num" }, `${t.count} of ${p.checks}`),
        h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(t.count / p.checks) * 100}%` }))));
      const st = state(p);
      return h("button", { class: `person is-${st}`, type: "button",
        title: `Open images of person #${p.id}`, onclick: () => open(p) },
        h("div", { class: "photo" },
          p.photo ? h("img", { src: api.urls.person(run.meta.id, p.photo), alt: `Person ${p.id}`, loading: "lazy" }) : null,
          h("span", { class: "pid" }, `#${p.id}`)),
        h("div", { class: "body" },
          h("span", { class: `status ${st}` }, p.ok ? "PPE OK" : p.status_label),
          st === "some" ? h("span", { class: "occasional" },
            `Occasional violations: ${p.violation_checks} of ${plural(p.checks, "check")}`) : null,
          h("span", { class: "meta num" },
            `On screen ${fmt.time(p.first_seen_sec)}–${fmt.time(p.last_seen_sec)} · ${plural(p.checks, "check")}`),
          items.length ? h("div", { class: "pm" }, items) : h("span", { class: "meta" }, "All PPE in every check"),
          p.latest_label !== (p.ok ? "PPE OK" : p.status_label)
            ? h("span", { class: "meta" }, `Last check: ${p.latest_label}`) : null));
    }));
  }
  draw();
  return h("section", { class: "card card-pad" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "People"),
        h("p", {}, "Each worker keeps one ID through the video. A person is flagged for an item when it was missing in more than half of their checks; fewer than that shows as occasional.")),
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

  root.replaceChildren(h("div", { class: "page" },
    header(run, [
      h("a", { class: "btn", href: api.urls.video(m.id, true), download: "" }, h("span", { html: icons.download, style: "display:inline-flex" }), "Video"),
      h("a", { class: "btn", href: api.urls.report(m.id), download: "" }, h("span", { html: icons.download, style: "display:inline-flex" }), "Report"),
      deleteButton(run, onDeleted),
    ]),
    h("div", { class: "kpis" },
      kpi("Compliance", compliance == null ? "–" : h("span", {}, (compliance * 100).toFixed(0), h("small", {}, "%")),
        `${s.ok_checks} of ${s.worker_checks} worker checks had all PPE`,
        compliance == null ? null : compliance >= 0.95 ? "good" : "bad"),
      kpi("Violations", String(s.violation_checks),
        s.violation_checks ? `across ${plural(s.frames_with_violation, "checked frame")}` : "none found", s.violation_checks ? "bad" : "good"),
      kpi("Time with a violation", timeShare == null ? "–" : h("span", {}, (timeShare * 100).toFixed(0), h("small", {}, "%")),
        `${s.frames_with_violation} of ${s.frames_analysed} checked frames`),
      s.tracked
        ? kpi("People tracked", String(s.people.length),
            `${s.people.filter((p) => !p.ok).length} flagged · peak ${s.peak_workers} on screen at once`,
            s.people.some((p) => !p.ok) ? "bad" : "good")
        : kpi("Workers on screen", String(s.peak_workers), `peak · ${s.avg_workers} on average`)),
    h("div", { class: "grid-main" },
      h("section", { class: "card player" }, video,
        playerMsg,
        h("div", { class: "player-foot" },
          h("span", {}, "Annotated output"),
          h("span", { class: "num" }, `${s.frames_analysed} frames checked`))),
      byTypeCard(s)),
    peopleCard(run, s, video),
    timelineCard(s, m, video),
    eventsCard(run, s, video)));
}
