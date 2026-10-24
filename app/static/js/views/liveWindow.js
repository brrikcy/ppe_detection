// Live inspection window: opens over the run page while a run is processing. It shows the
// annotated video as it's written, the progress, running counts and each violation the
// moment it's found. When the run completes it shows 100%, then closes by itself and leaves
// the finished run page underneath. It can be minimised to a chip and reopened.

import { api } from "../api.js";
import { openLightbox } from "../lightbox.js";
import { fmt, h, plural } from "../ui.js";
import { eventItems } from "./detections.js";

const CLOSE_AFTER_MS = 1600; // how long "Inspection complete" shows before the window closes
const MINIMISE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 12h12"/></svg>`;

function swapImage(img, url) {
  const next = new Image();
  next.onload = () => { img.src = url; img.classList.add("is-loaded"); };
  next.src = url;
}

// Shows a live run's frames in `img` as they arrive. Each frame is answered, and the server waits
// for answers before sending more, so a slow connection skips frames instead of falling behind.
// Stops when `img` leaves the page; `onFail` if the stream can't open or breaks off.
// Returns the WebSocket, to close it sooner.
export function playLive(id, img, onFail) {
  const ws = new WebSocket(api.urls.live(id));
  ws.binaryType = "blob";
  ws.onmessage = (e) => {
    if (!img.isConnected) return ws.close();
    ws.send("ok");
    const prev = img.src;
    img.src = URL.createObjectURL(e.data);
    img.classList.add("is-loaded");
    if (prev.startsWith("blob:")) URL.revokeObjectURL(prev);
  };
  ws.onclose = (e) => { if (!e.wasClean && img.isConnected) onFail(); };
  return ws;
}

/** Returns {update(run), dispose()}; mount it once while the run is queued or running. */
export function createLiveWindow(run) {
  const m = run.meta;
  const id = m.id;
  const duration = m.options.max_seconds ? Math.min(m.options.max_seconds, m.video.duration_sec) : m.video.duration_sec;
  const ar = m.video.width && m.video.height ? m.video.width / m.video.height : 16 / 9;

  // ---- video ----
  const img = h("img", { class: "lw-img", alt: "The video as it is being inspected" });
  const clock = h("span", { class: "num" });
  const badge = h("span", { class: "live-badge", hidden: true }, h("i"), h("span", { class: "live-text" }, "Live"), clock);
  const waiting = h("div", { class: "lw-wait" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span"));
  const flash = h("div", { class: "lw-flash", "aria-hidden": "true" });
  const media = h("div", { class: "lw-media", style: `--ar:${ar.toFixed(4)}` }, img, waiting, badge, flash);

  // ---- progress ----
  const stage = h("span", { class: "lw-stage" });
  const pct = h("b", { class: "lw-pct num" }, "0%");
  const eta = h("span", { class: "lw-eta" });
  const bar = h("i");
  const activity = h("p", { class: "live-activity" });
  const progress = h("div", { class: "lw-progress" },
    h("div", { class: "lw-progress-row" }, stage, pct, eta),
    h("div", { class: "lw-bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100" }, bar),
    activity);

  // ---- counters ----
  const counter = (label, tone) => {
    const value = h("b", { class: "num" }, "0");
    const note = h("span");
    return { value, note, el: h("div", { class: `lw-stat${tone ? ` is-${tone}` : ""}` }, h("span", { class: "eyebrow" }, label), value, note) };
  };
  const cViol = counter("Violations", "bad"), cNow = counter("On screen now"), cPeople = counter("Workers checked"), cComp = counter("Compliance");

  // ---- live feed ----
  const feed = h("ol", { class: "lw-feed", "aria-live": "polite", "aria-label": "Violations found" });
  const feedEmpty = h("div", { class: "lw-feed-empty" });
  const feedCount = h("span", { class: "pill failed", hidden: true });

  const minBtn = h("button", { type: "button", class: "lw-icon", "aria-label": "Minimise", title: "Keep running in the background",
    html: MINIMISE, onclick: () => setOpen(false) });
  const elapsed = h("span", { class: "lw-elapsed num" });
  const title = h("h2", { id: `lw-title-${id}` }, m.name);
  const panel = h("div", { class: "lw-panel", role: "dialog", "aria-modal": "true", "aria-labelledby": title.id },
    h("header", { class: "lw-head" },
      h("div", { class: "lw-title" }, h("span", { class: "eyebrow" }, "Live inspection"), title),
      elapsed, minBtn),
    h("div", { class: "lw-body" },
      h("div", { class: "lw-main" }, media, progress),
      h("aside", { class: "lw-side" },
        h("div", { class: "lw-stats" }, cViol.el, cNow.el, cPeople.el, cComp.el),
        h("div", { class: "lw-feed-head" }, h("h3", {}, "Violations as they're found ", feedCount),
          h("span", { class: "hint" }, "Newest first")),
        h("div", { class: "lw-feed-wrap" }, feedEmpty, feed))));
  const overlay = h("div", { class: "lw-overlay" }, panel);

  // Minimised: a chip in the corner with the progress, to reopen the window.
  const chipPct = h("b", { class: "num" });
  const chipBar = h("i");
  const chip = h("button", { type: "button", class: "lw-chip", hidden: true, onclick: () => setOpen(true) },
    h("span", { class: "lw-chip-dot", "aria-hidden": "true" }), "Live inspection ", chipPct,
    h("span", { class: "lw-chip-bar", "aria-hidden": "true" }, chipBar));

  document.body.append(overlay, chip);
  document.body.classList.add("lw-open-body");
  requestAnimationFrame(() => overlay.classList.add("is-in"));

  const cards = new Map(); // incident key -> {el, img, dur, snap}
  let items = [];
  let open = true;
  let lastFrames = -1, finished = false, disposed = false;
  const stream = { on: false, failed: false, ws: null };
  const started = { t: performance.now(), f: run.progress_fraction };
  const tick = setInterval(() => { elapsed.textContent = fmt.time((performance.now() - started.t) / 1000); }, 500);

  function setOpen(v) {
    open = v;
    overlay.hidden = !v;
    chip.hidden = v || finished;
    document.body.classList.toggle("lw-open-body", v);
    if (v) minBtn.focus();
  }

  function onKey(e) {
    if (e.key === "Escape" && open && !document.querySelector(".lightbox")) setOpen(false);
  }
  document.addEventListener("keydown", onKey);

  function bump(el, text) {
    if (el.textContent === text) return;
    el.textContent = text;
    el.classList.remove("bump");
    void el.offsetWidth;
    el.classList.add("bump");
  }

  function update(run) {
    if (disposed) return;
    const m = run.meta;
    const s = run.summary;
    const done = m.status === "completed";
    const queued = m.status === "queued";
    const frac = done ? 1 : run.progress_fraction;

    stage.textContent = done ? "Inspection complete" : queued ? "Queued" : "Inspecting";
    pct.textContent = chipPct.textContent = `${Math.round(frac * 100)}%`;
    bar.style.width = chipBar.style.width = `${frac * 100}%`;
    bar.parentElement.setAttribute("aria-valuenow", String(Math.round(frac * 100)));
    clock.textContent = ` ${fmt.time(frac * duration)} / ${fmt.time(duration)}`;
    badge.hidden = queued || !m.progress.frames_done || done;
    const gained = frac - started.f, secs = (performance.now() - started.t) / 1000;
    eta.textContent = done ? "Opening the results…" : queued ? "Waiting for the previous run to finish"
      : gained > 0.03 && secs > 4 ? `About ${fmt.duration((secs / gained) * (1 - frac))} left` : "Estimating time left…";

    // The video plays along: a live run streams its frames (WebSocket); otherwise a still refreshes.
    if (!done && m.progress.frames_done !== lastFrames && m.progress.frames_done > 0) {
      lastFrames = m.progress.frames_done;
      if (m.options.live && m.status === "running" && !stream.failed) {
        if (!stream.on) {
          stream.on = true;
          stream.ws = playLive(id, img, () => { stream.failed = true; });
        }
      } else {
        swapImage(img, api.urls.preview(id));
      }
    }
    waiting.hidden = lastFrames >= 1 || done;
    waiting.lastChild.textContent = queued ? "Waiting for the previous run to finish…"
      : "Starting up: loading the person detector and checking the first workers…";
    const checking = m.progress.checking ?? 0;
    activity.hidden = queued || lastFrames < 1 || done;
    activity.classList.toggle("is-checking", checking > 0);
    activity.replaceChildren(h("span", { class: "spinner small", "aria-hidden": "true" }),
      checking ? `Checking PPE on ${plural(checking, "worker")} at ${fmt.time(frac * duration)}…`
        : "Tracking workers and drawing the video…");

    if (s) {
      const now = s.timeline.at(-1);
      bump(cViol.value, String(s.events.length));
      cViol.note.textContent = s.events.length ? `over ${plural(s.violation_checks, "worker check")}` : "none yet";
      cViol.el.classList.toggle("has-value", s.events.length > 0);
      bump(cNow.value, String(now?.workers ?? 0));
      cNow.note.textContent = now ? `${now.violations} missing PPE` : "–";
      cNow.el.classList.toggle("has-bad", (now?.violations ?? 0) > 0);
      bump(cPeople.value, String(s.people.length));
      cPeople.note.textContent = plural(s.worker_checks, "check");
      bump(cComp.value, s.compliance_rate == null ? "–" : `${Math.round(s.compliance_rate * 100)}%`);
      cComp.note.textContent = "of judged checks";
      drawFeed(run, s);
    }
    feedEmpty.hidden = (s?.events.length ?? 0) > 0;
    feedEmpty.textContent = !s || !s.people.length ? "Watching for workers…"
      : `No violations yet. ${plural(s.people.length, "worker")} checked, all with the required PPE.`;

    if (done && !finished) finish();
  }

  // Violations, newest first: new ones slide in and the video flashes red; ongoing ones update.
  function drawFeed(run, s) {
    items = eventItems(run, [...s.events].reverse());
    feedCount.hidden = !items.length;
    feedCount.textContent = String(items.length);
    let fresh = 0;
    items.forEach((e, i) => {
      const key = `${e.track_id}-${e.time_sec}`;
      let c = cards.get(key);
      if (!c) {
        const thumb = h("img", { alt: `Worker #${e.track_id ?? ""} missing ${e.violations.join(", ")}` });
        const what = h("b", { class: "lw-what" });
        const who = h("span", { class: "lw-who" });
        const when = h("span", { class: "lw-when num" });
        const el = h("li", { class: "lw-card is-new" },
          h("button", { type: "button", class: "lw-card-btn", title: "Open the photo" },
            h("span", { class: "lw-thumb" }, thumb), h("span", { class: "lw-card-body" }, what, who, when)));
        c = { el, thumb, what, who, when, snap: null };
        cards.set(key, c);
        fresh++;
      }
      c.el.querySelector("button").onclick = () => openLightbox(items, i);
      c.what.textContent = e.violations.join(" · ");
      c.who.textContent = e.track_id != null ? `Worker #${e.track_id}` : "Worker";
      const seen = (e.end_sec ?? e.time_sec) - e.time_sec;
      c.when.textContent = [
        `at ${fmt.timePrecise(e.time_sec)}`,
        seen > 0 ? `seen for ${seen.toFixed(1)} s` : null,
        e.detected_at ? new Date(e.detected_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : null,
      ].filter(Boolean).join(" · ");
      if (e.cropUrl && e.cropUrl !== c.snap) { c.snap = e.cropUrl; swapImage(c.thumb, e.cropUrl); }
      if (feed.children[i] !== c.el) feed.insertBefore(c.el, feed.children[i] ?? null);
    });
    if (fresh && cards.size > fresh) { // not on the first draw
      flash.classList.remove("is-on");
      void flash.offsetWidth;
      flash.classList.add("is-on");
    }
  }

  // 100%, a moment to read it, then fade out to the finished run page.
  function finish() {
    finished = true;
    panel.classList.add("is-done");
    chip.hidden = true;
    setTimeout(() => {
      overlay.classList.remove("is-in");
      overlay.classList.add("is-out");
      setTimeout(dispose, 450);
    }, open ? CLOSE_AFTER_MS : 0);
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    clearInterval(tick);
    document.removeEventListener("keydown", onKey);
    stream.ws?.close();
    overlay.remove();
    chip.remove();
    document.body.classList.remove("lw-open-body");
  }

  setTimeout(() => minBtn.focus(), 50);
  return { update, dispose, get open() { return open && !disposed; } };
}
