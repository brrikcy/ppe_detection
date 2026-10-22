// Run view: live progress while processing, results dashboard when done.

import { api } from "../api.js";
import { timelineChart } from "../charts.js";
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
    grid.replaceChildren(...list.slice(0, shown).map((e) => h("button", {
      class: "event", type: "button", title: "Jump to this moment",
      onclick: () => { video.currentTime = e.time_sec + 0.05; video.scrollIntoView({ behavior: "smooth", block: "center" }); video.play().catch(() => {}); },
    },
      h("div", { class: "thumb" }, e.snapshot
        ? h("img", { src: api.urls.snapshot(run.meta.id, e.snapshot), alt: `Worker at ${fmt.timePrecise(e.time_sec)}`, loading: "lazy" })
        : h("span", { class: "hint" }, "No snapshot")),
      h("div", { class: "body" },
        h("span", { class: "time" }, fmt.timePrecise(e.time_sec)),
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
      h("div", {}, h("h2", {}, "Violations"), h("p", {}, "One card per flagged worker per checked frame. Click to jump the video.")),
      filters),
    grid, more);
}

function renderDashboard(root, run, { onDeleted }) {
  const m = run.meta;
  const s = run.summary;
  const video = h("video", { src: api.urls.video(m.id), poster: api.urls.preview(m.id),
    controls: true, playsinline: true, preload: "metadata" });
  const playerMsg = h("div", { class: "player-msg", hidden: true },
    "This browser can't play the annotated video. Use the Video button above to download it.");
  video.addEventListener("error", () => { playerMsg.hidden = false; });

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
      kpi("Workers on screen", String(s.peak_workers), `peak · ${s.avg_workers} on average`)),
    h("div", { class: "grid-main" },
      h("section", { class: "card player" }, video,
        playerMsg,
        h("div", { class: "player-foot" },
          h("span", {}, "Annotated output"),
          h("span", { class: "num" }, `${s.frames_analysed} frames checked`))),
      byTypeCard(s)),
    timelineCard(s, m, video),
    eventsCard(run, s, video)));
}
