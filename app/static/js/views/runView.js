// Run view: one results page that fills in while the run is processing and settles when it's done.

import { api, apiKey, ServerUnreachable } from "../api.js";
import { timelineChart } from "../charts.js";
import { fmt, h, icons, plural, statusPill } from "../ui.js";
import { createDetections } from "./detections.js";

const POLL_MS = 1000;

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
    if (status === "queued" || status === "running" || status === "completed") {
      renderResults(root, run, { onDeleted });
      if (status !== "completed") timer = setTimeout(tick, POLL_MS);
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
      customChip(m.options.tuning),
      m.rerun_of ? h("a", { class: "chip chip-link", href: `#run/${encodeURIComponent(m.rerun_of)}` }, "Re-run of an earlier run") : null));
}

// "Run again": inspect the same video as a new run, with the current developer settings
// or the ones this run used. It uses the API key from Developer options, or the server's.
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
    const noKey = !apiKey.get() && !config.server_key_configured;
    const error = h("p", { class: "form-error", hidden: true });
    const start = h("button", { class: "btn btn-primary", type: "button", onclick: go }, "Start");
    panel = h("div", { class: "rerun-panel card", role: "dialog", "aria-label": "Run this video again" },
      h("h3", {}, "Run this video again"),
      h("p", { class: "hint" }, "A new run with the same PPE items, check interval and time limit. This run stays as it is."),
      h("fieldset", { class: "rerun-opts" }, h("legend", { class: "label" }, "Settings"),
        radio("current", "Current developer settings",
          n ? `${n} change${n === 1 ? "" : "s"} from the defaults, as saved now` : "The defaults (nothing changed)", true),
        radio("original", "This run's settings", "Exactly as this run was made", false)),
      noKey ? h("div", { class: "dev-note" }, "This server has no API key. Add one in ",
        h("a", { href: "#dev" }, "Developer options"), " first.") : null,
      error,
      h("div", { class: "rerun-actions" }, h("button", { class: "btn btn-ghost", type: "button", onclick: closePanel }, "Cancel"), start));
    wrap.append(panel);
    btn.setAttribute("aria-expanded", "true");
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onOutside, true);
    panel.querySelector("input[type=radio]:checked").focus();

    async function go() {
      error.hidden = true;
      start.disabled = true;
      start.textContent = "Starting…";
      try {
        const settings = panel.querySelector("input[name=rerun-settings]:checked").value;
        const created = await api.rerun(run.meta.id, { settings, api_key: apiKey.get() || null });
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
  // A click anywhere but the panel and its button closes it (the button toggles it itself).
  function onOutside(e) { if (!wrap.contains(e.target)) closePanel(); }
  function closePanel() {
    panel?.remove();
    panel = null;
    btn.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("pointerdown", onOutside, true);
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

// ---------------------------------------------------------------- failed

function renderFailed(root, run, { onDeleted }) {
  root.replaceChildren(h("div", { class: "page" },
    header(run, [rerunButton(run), deleteButton(run, onDeleted)]),
    h("div", { class: "alert", role: "alert" },
      h("h3", {}, "This run failed"),
      h("p", {}, run.meta.error || "Unknown error.")),
    h("div", {}, h("a", { class: "btn btn-primary", href: "#new" }, "Start a new inspection"))));
}

// ---------------------------------------------------------------- results

// One page from the first poll to the end: while the run is processing it shows the live
// video with progress under it, and every figure, the violations list and the chart fill in
// as checks come in. When the run completes, the live view is replaced by the annotated
// video and the progress line by its summary; nothing else moves.
function renderResults(root, run, { onDeleted }) {
  let page = root.querySelector("[data-view=results]");
  if (!page || page.dataset.run !== run.meta.id) {
    page = buildResults(run, { onDeleted });
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

function bump(el) {
  el.classList.remove("bump");
  void el.offsetWidth; // restart the animation
  el.classList.add("bump");
}

// A KPI tile whose label, value and note change in place; the value pulses when it changes.
function kpiTile() {
  const label = h("span", { class: "eyebrow" });
  const value = h("div", { class: "value" });
  const note = h("span", { class: "note" });
  const el = h("div", { class: "card kpi" }, label, value, note);
  let last = null;
  return {
    el,
    set(labelText, text, unit, noteText, tone) {
      label.textContent = labelText;
      const key = `${text}${unit ?? ""}`;
      if (key !== last) {
        if (last !== null) bump(value);
        last = key;
        value.replaceChildren(h("span", {}, text, unit ? h("small", {}, unit) : null));
      }
      note.textContent = noteText;
      el.className = `card kpi${tone ? ` is-${tone}` : ""}`;
    },
  };
}

function legendItems(points) {
  return [
    h("span", {}, h("i", { style: "background:var(--good)" }), "PPE OK"),
    h("span", {}, h("i", { style: "background:var(--critical)" }), "PPE violation"),
    points.some((p) => p.unverified) ? h("span", {}, h("i", { style: "background:var(--warning)" }), "Item not visible") : null,
    points.some((p) => p.too_far || p.unchecked)
      ? h("span", {}, h("i", { style: "background:var(--muted)" }), "Not checked (too far, new or failed)") : null,
  ].filter(Boolean);
}

function byTypeBody(s, onPick, live) {
  const max = Math.max(1, ...s.by_type.map((t) => t.count));
  const body = s.by_type.length
    ? h("div", { class: "hbars" }, s.by_type.map((t) => h("button", { type: "button", class: "hbar is-link",
        title: `Show the ${t.label} violations`, onclick: () => onPick(t.label) },
        h("div", { class: "row" }, h("span", {}, t.label), h("b", { class: "num" }, t.count)),
        h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(t.count / max) * 100}%` })))))
    : h("div", { class: "empty-note" }, h("span", { html: icons.check }),
      live ? "No violations so far." : "No violations: nobody's status showed missing PPE.");

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
  return [body, ...unv].filter(Boolean); // replaceChildren would print null as text
}

function buildResults(run, { onDeleted }) {
  const m = run.meta;
  const id = m.id;
  const duration = m.options.max_seconds ? Math.min(m.options.max_seconds, m.video.duration_sec) : m.video.duration_sec;
  const page = h("div", { class: "page", "data-view": "results", "data-run": id });
  const headSlot = h("div");

  // ---- player: the live view while processing, the annotated video when done ----
  const liveImg = h("img", { class: "player-live", alt: "The video as it is being inspected" });
  const waiting = h("div", { class: "wait" });
  const clock = h("span", { class: "num" });
  const badge = h("span", { class: "live-badge", hidden: true }, h("i"), h("span", { class: "live-text" }, "Live"), clock);
  const playerMsg = h("div", { class: "player-msg", hidden: true },
    "This browser can't play the annotated video. Use the Video button above to download it.");
  const media = h("div", { class: "player-media" }, liveImg, waiting, badge, playerMsg);

  const stage = h("span", { class: "eyebrow" });
  const pct = h("b", { class: "pp-pct num" });
  const eta = h("span", { class: "pp-eta" });
  const bar = h("i");
  const activity = h("p", { class: "live-activity" });
  const progress = h("div", { class: "player-progress" },
    h("div", { class: "pp-row" }, stage, pct, eta), h("div", { class: "bar" }, bar), activity);
  const checkedNote = h("span", { class: "num" });
  const foot = h("div", { class: "player-foot", hidden: true }, h("span", {}, "Annotated output"), checkedNote);

  const watch = h("div", { class: "watch" });
  const setShape = (w, hgt) => {
    if (!w || !hgt) return;
    watch.style.setProperty("--ar", (w / hgt).toFixed(4));
    watch.classList.toggle("is-portrait", hgt > w);
  };
  setShape(m.video.width, m.video.height);

  // ---- metrics beside the video ----
  const kComp = kpiTile(), kViol = kpiTile(), kTime = kpiTile(), kPeople = kpiTile();
  const failedBanner = h("div", { class: "warn-banner", hidden: true });
  const detections = createDetections(run);
  const typeBody = h("div");
  const typeCard = h("section", { class: "card card-pad" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, "Violations by type"), h("p", {}, "Counted per incident: one person missing the item over consecutive checks. Click one to list it."))),
    typeBody);

  watch.append(
    h("section", { class: "card player" }, media, progress, foot),
    h("div", { class: "watch-side" },
      h("div", { class: "kpis" }, kComp.el, kViol.el, kTime.el, kPeople.el),
      failedBanner, typeCard));

  // ---- compliance over time ----
  const chartBox = h("div", { class: "chart-wrap" });
  const chartIntro = h("p");
  const chartLegend = h("div", { class: "legend" });
  const chartCard = h("section", { class: "card card-pad" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Compliance over time"), chartIntro), chartLegend),
    chartBox);

  page.append(headSlot, watch, detections.el, chartCard);

  let video = null;
  let chart = null;
  let chartPoints = [];
  let settling = false; // completed while watching: showing 100% before the video appears
  let lastStatus = null, lastFrames = -1, lastChart = null, lastTypes = null;
  const stream = { on: false, failed: false };
  const started = { t: performance.now(), f: run.progress_fraction };

  function showVideo() {
    video = h("video", { src: api.urls.video(id), poster: api.urls.preview(id),
      controls: true, playsinline: true, preload: "metadata" });
    // The first load can fail while the server is still converting the video,
    // so retry once before showing the message, and hide it as soon as anything plays.
    let retried = false;
    video.addEventListener("error", () => {
      if (!retried) {
        retried = true;
        setTimeout(() => { video.src = `${api.urls.video(id)}?retry=1`; video.load(); }, 2500);
        return;
      }
      playerMsg.hidden = false;
    });
    for (const ev of ["loadeddata", "canplay", "playing"]) video.addEventListener(ev, () => { playerMsg.hidden = true; });
    video.addEventListener("loadedmetadata", () => setShape(video.videoWidth, video.videoHeight));
    const sync = () => chart?.setPlayhead(video.currentTime);
    video.addEventListener("timeupdate", sync);
    video.addEventListener("seeked", sync);
    liveImg.removeAttribute("src"); // closes the live stream
    liveImg.replaceWith(video);
    waiting.hidden = badge.hidden = true;
    detections.setVideo(video);
    drawChart();
  }

  page.update = (run) => {
    const m = run.meta;
    const s = run.summary;
    const done = m.status === "completed";
    if (m.status !== lastStatus) {
      headSlot.replaceChildren(header(run, done ? [
        h("a", { class: "btn", href: api.urls.video(id, true), download: "" }, h("span", { html: icons.download, style: "display:inline-flex" }), "Video"),
        h("a", { class: "btn", href: api.urls.report(id), download: "" }, h("span", { html: icons.download, style: "display:inline-flex" }), "Report"),
        rerunButton(run),
        deleteButton(run, onDeleted),
      ] : []));
      lastStatus = m.status;
    }

    // ---- player and progress ----
    const frac = done ? 1 : run.progress_fraction;
    const queued = m.status === "queued";
    if (done) {
      // Watched to the end: fill the bar to 100% and hold it a moment before settling.
      const watched = lastFrames >= 0 && !video && !settling;
      const settle = () => {
        if (!video) showVideo();
        progress.hidden = true;
        foot.hidden = false;
      };
      if (watched) {
        settling = true;
        stage.textContent = "Done";
        pct.textContent = "100%";
        bar.style.width = "100%";
        eta.textContent = "Finishing the video…";
        activity.hidden = true;
        setTimeout(settle, 1200);
      } else if (!settling) {
        settle();
      }
      checkedNote.textContent = s ? `${s.frames_analysed} frames checked` +
        (s.arrival_checks ? ` · ${plural(s.arrival_checks, "arrival check")}` : "") : "";
    } else {
      stage.textContent = queued ? "Queued" : "Inspecting";
      pct.textContent = `${Math.round(frac * 100)}%`;
      bar.style.width = `${frac * 100}%`;
      clock.textContent = ` ${fmt.time(frac * duration)} / ${fmt.time(duration)}`;
      badge.hidden = queued || !m.progress.frames_done;
      // Time left, from the speed since this page opened.
      const gained = frac - started.f, secs = (performance.now() - started.t) / 1000;
      eta.textContent = queued ? "Waiting for the previous run to finish"
        : gained > 0.03 && secs > 4 ? `About ${fmt.duration((secs / gained) * (1 - frac))} left` : "Estimating time left…";
      // The live view plays along with the inspection: a live run streams every frame (MJPEG),
      // others refresh a still as frames are written. If the stream drops, refresh the still.
      if (m.progress.frames_done !== lastFrames && m.progress.frames_done > 0) {
        lastFrames = m.progress.frames_done;
        if (m.options.live && m.status === "running" && !stream.failed) {
          if (!stream.on) {
            stream.on = true;
            liveImg.onerror = () => { stream.failed = true; liveImg.onerror = null; };
            liveImg.src = api.urls.live(id);
            liveImg.classList.add("is-loaded");
          }
        } else {
          swapImage(liveImg, api.urls.preview(id));
        }
        waiting.hidden = true;
      }
      // What the pipeline is doing right now, so a slow PPE check doesn't look like a freeze.
      const checking = m.progress.checking ?? 0;
      activity.hidden = queued || lastFrames < 1;
      activity.classList.toggle("is-checking", checking > 0);
      activity.replaceChildren(h("span", { class: "spinner small", "aria-hidden": "true" }),
        checking ? `Checking PPE on ${plural(checking, "worker")} at ${fmt.time(frac * duration)}…`
          : "Tracking workers and drawing the video…");
      if (lastFrames < 1) {
        waiting.hidden = false;
        waiting.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {},
          queued ? "Waiting for the previous run to finish…"
            : "Starting up: loading the person detector and checking the first workers…"));
      }
    }

    // ---- figures ----
    const so = done ? "" : " so far";
    if (!s) {
      kComp.set("Compliance", "–", null, "No worker checked yet");
      kViol.set("Violations", "0", null, "none yet");
      kTime.set("Time with a violation", "–", null, "No frame checked yet");
      kPeople.set("People tracked", "0", null, "nobody seen yet");
      typeBody.replaceChildren(h("p", { class: "hint" }, "Appears as workers are checked."));
    } else {
      const compliance = s.compliance_rate;
      const timeShare = s.frames_analysed ? s.frames_with_violation / s.frames_analysed : null;
      const judged = s.ok_checks + s.violation_checks;
      const attempted = s.worker_checks + (s.failed_checks ?? 0);
      const failedShare = attempted ? (s.failed_checks ?? 0) / attempted : 0;
      const notJudged = [s.unverified_checks ? `${s.unverified_checks} not verified` : null,
        s.pending_checks ? `${s.pending_checks} first checks` : null].filter(Boolean).join(", ");
      const flagged = s.people.filter((p) => (p.state ?? (p.ok ? "ok" : "flagged")) === "flagged").length;
      kComp.set("Compliance", compliance == null ? "–" : (compliance * 100).toFixed(0), compliance == null ? null : "%",
        `${s.ok_checks} of ${judged} judged worker checks had all PPE` + (notJudged ? ` (${notJudged} left out)` : ""),
        compliance == null ? null : compliance >= 0.95 ? "good" : "bad");
      kViol.set("Violations", String(s.events.length), null,
        s.events.length ? `${plural(s.events.length, "incident")} over ${plural(s.violation_checks, "worker check")}` : `none found${so}`,
        s.events.length ? "bad" : done ? "good" : null);
      kTime.set("Time with a violation", timeShare == null ? "–" : (timeShare * 100).toFixed(0), timeShare == null ? null : "%",
        `${s.frames_with_violation} of ${s.frames_analysed} checked frames`);
      if (s.tracked || !done) {
        kPeople.set("People tracked", String(s.people.length), null,
          `${flagged} flagged · peak ${s.peak_workers} on screen at once`, flagged ? "bad" : done ? "good" : null);
      } else {
        kPeople.set("Workers on screen", String(s.peak_workers), null, `peak · ${s.avg_workers} on average`);
      }

      failedBanner.hidden = !s.failed_checks;
      failedBanner.setAttribute("role", failedShare > 0.1 ? "alert" : "status");
      if (s.failed_checks) {
        failedBanner.replaceChildren(h("b", {}, `${plural(s.failed_checks, "worker check")} failed`),
          ` (${Math.round(failedShare * 100)}% of checks). The model didn't answer, so ${s.failed_checks === 1 ? "that check says" : "those checks say"} nothing about the worker. ` +
          (failedShare > 0.1 ? "Results for this run are incomplete; consider running it again." : ""));
      }

      const typesKey = JSON.stringify([s.by_type, s.unverified, s.too_small_checks, s.worker_checks, done]);
      if (typesKey !== lastTypes) {
        lastTypes = typesKey;
        typeBody.replaceChildren(...byTypeBody(s, (label) => detections.showItem(label), !done));
      }
    }

    detections.update(run);

    chartPoints = s?.timeline ?? [];
    drawChart();
  };

  // The chart is redrawn as checks come in, and once more with seeking when the video is there.
  function drawChart() {
    const key = `${chartPoints.length}:${!!video}`;
    if (key === lastChart) return;
    lastChart = key;
    chartIntro.textContent = video ? "Workers per checked frame. Click a column to jump the video there."
      : "Workers per checked frame, filled in as the video is inspected.";
    chartLegend.replaceChildren(...legendItems(chartPoints));
    chart = timelineChart(chartBox, chartPoints, {
      duration, interval: run.meta.options.analysis_interval_sec,
      onSeek: video ? (t) => { video.currentTime = t + 0.05; video.play().catch(() => {}); } : null,
    });
    if (video) chart.setPlayhead(video.currentTime);
  }
  return page;
}
