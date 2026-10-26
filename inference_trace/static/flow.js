// Inference flowchart. #/ uploads a video and lists traces; #/t/<id> shows one trace as a
// flowchart: input -> settings -> tracking -> check schedule -> one check in full (every
// Moondream call, answer and decision per worker and item) -> violations -> outputs.
// Every box is clickable: the panel on the right shows its images, raw data and why.

import { COLOR_CLASS, EXPLAIN, ITEM_RESULT, REASON, STATE, api, boxedImage, file, fmt, h, modelTime, overlapShare, pic, plural, whyItem } from "./shared.js";

const app = document.getElementById("app");
const drawer = document.getElementById("drawer");

// ---------------------------------------------------------------- details drawer

function openDrawer({ title, explain, images = [], fields = [], json }) {
  drawer.replaceChildren(...[
    h("div", { class: "drawer-head" }, h("h2", {}, title),
      h("button", { type: "button", class: "btn btn-sm", onclick: closeDrawer }, "Close")),
    explain ? h("section", {}, h("p", { class: "explain" }, explain)) : null,
    images.length ? h("section", {}, h("div", { class: "pics" }, images)) : null,
    ...fields.filter(([, v]) => v != null && v !== "").map(([k, v]) =>
      h("section", {}, h("span", { class: "eyebrow" }, k), v instanceof Node ? v : h("pre", {}, String(v)))),
    json !== undefined ? h("section", {}, h("span", { class: "eyebrow" }, "Raw data"), h("pre", {}, JSON.stringify(json, null, 2))) : null,
  ].filter(Boolean));
  drawer.hidden = false;
  drawer.scrollTop = 0;
}
function closeDrawer() { drawer.hidden = true; }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// A flowchart box. kind: io | model | decision | skip | result (+ is-* classes).
function node(kind, { k, v, s, cls = "", body, details }) {
  return h("button", { type: "button", class: `node node-${kind} ${cls}`, onclick: () => details && openDrawer(details) },
    k ? h("span", { class: "k" }, k) : null, body ?? null, v != null ? h("span", { class: "v" }, v) : null,
    s ? h("span", { class: "s" }, s) : null);
}
const arrow = (label, taken = true) => h("span", { class: `arrow${taken ? " is-taken" : ""}${label ? " is-label" : ""}`, "aria-hidden": "true" }, label ?? null);
function chain(...nodes) {
  const out = h("div", { class: "chain" });
  nodes.flat().filter(Boolean).forEach((n, i) => {
    if (i && !n.classList.contains("arrow") && !out.lastChild.classList.contains("arrow")) out.append(arrow());
    out.append(n);
  });
  return out;
}
const stage = (n, title, intro, ...body) => h("section", { class: "card stage", id: `stage-${n}` },
  h("div", { class: "stage-head" }, h("span", { class: "stage-num" }, n), h("div", {}, h("h2", {}, title), intro ? h("p", {}, intro) : null)),
  ...body);
const down = () => h("div", { class: "down", "aria-hidden": "true" });

// ---------------------------------------------------------------- router

let stop = () => {};
async function route() {
  stop();
  stop = () => {};
  closeDrawer();
  const walk = location.hash.match(/^#\/t\/([^/?]+)\/walk/);
  const m = location.hash.match(/^#\/t\/([^/?]+)$/);
  try {
    if (walk) {
      const { walkView } = await import("./walk.js");
      stop = await walkView(app, decodeURIComponent(walk[1]));
    } else if (m) await traceView(decodeURIComponent(m[1]));
    else await home();
  } catch (e) {
    app.replaceChildren(h("div", { class: "alert", role: "alert" }, e.message));
  }
}
window.addEventListener("hashchange", route);
route();

// ---------------------------------------------------------------- home

async function home() {
  const config = await api.config();
  const fileInput = h("input", { type: "file", accept: "video/*", class: "input", style: { height: "auto", padding: "8px" }, required: true });
  const items = h("div", { class: "checks" }, config.items.map((it) => h("label", { class: "check" },
    h("input", { type: "checkbox", name: "ppe", value: it.key, checked: config.default_items.includes(it.key) }), it.label)));
  const interval = h("select", { class: "input" }, [0.5, 1, 2, 5].map((v) => h("option", { value: v, selected: v === 1 }, `Every ${v} s`)));
  const maxSec = h("input", { class: "input", type: "number", min: "1", step: "1", placeholder: "Whole video" });
  const changes = config.saved_changes;
  const radio = (value, label, note, checked) => h("label", { class: "radio" },
    h("input", { type: "radio", name: "settings", value, checked }), h("span", {}, h("b", {}, label), h("br"), h("span", { class: "hint" }, note)));
  const key = h("input", { class: "input", type: "password", autocomplete: "off",
    placeholder: config.server_key_configured ? "Empty = the server's key" : "Moondream API key" });
  const bar = h("i");
  const barWrap = h("div", { class: "bar", hidden: true }, bar);
  const err = h("p", { class: "alert", hidden: true });
  const go = h("button", { type: "submit", class: "btn btn-primary" }, "Run inference and build the flowchart");
  const form = h("form", { class: "card form", onsubmit: async (e) => {
    e.preventDefault();
    err.hidden = true;
    const ppe = [...items.querySelectorAll("input:checked")].map((i) => i.value);
    if (!fileInput.files[0]) return showErr("Choose a video.");
    if (!ppe.length) return showErr("Choose at least one PPE item.");
    const body = new FormData();
    body.append("video", fileInput.files[0]);
    body.append("required_ppe", ppe.join(","));
    body.append("interval", interval.value);
    if (Number(maxSec.value) > 0) body.append("max_seconds", maxSec.value);
    body.append("settings_choice", form.querySelector("input[name=settings]:checked").value);
    if (key.value.trim()) body.append("api_key", key.value.trim());
    go.disabled = true;
    go.textContent = "Uploading…";
    barWrap.hidden = false;
    try {
      const { id } = await api.create(body, (f) => (bar.style.width = `${f * 100}%`));
      location.hash = `#/t/${encodeURIComponent(id)}`;
    } catch (ex) {
      showErr(ex.message);
      go.disabled = false;
      go.textContent = "Run inference and build the flowchart";
    }
    function showErr(m) { err.textContent = m; err.hidden = false; }
  } },
    h("div", { class: "field" }, h("span", { class: "label" }, "Video"), fileInput),
    h("div", { class: "field" }, h("span", { class: "label" }, "Required PPE"), items),
    h("div", { class: "field" }, h("span", { class: "label" }, "Check a frame"), interval),
    h("div", { class: "field" }, h("span", { class: "label" }, "Only the first … seconds"), maxSec),
    h("div", { class: "field" }, h("span", { class: "label" }, "Settings"),
      radio("saved", "As in the main app", changes.length ? `Developer options as saved: ${changes.length} change(s) from the defaults (${changes.join(", ")})` : "Developer options as saved (the defaults)", true),
      radio("defaults", "Defaults", "Every confirmation step on: shows the full decision tree with detect and re-checks", false)),
    h("div", { class: "field" }, h("span", { class: "label" }, "Moondream API key (optional)"), key),
    go, barWrap, err);

  const list = h("div", { class: "trace-list" });
  app.replaceChildren(h("div", { class: "home" },
    h("div", {}, h("h1", {}, "See inside an inference run"),
      h("p", { class: "hint", style: { marginBottom: "16px" } },
        "Runs a video through the same pipeline as PPE Inspector and records every step: the frames checked, each worker's crop, every prompt sent to Moondream and its answer, each decision, the vote and what's drawn in the video. The result is an interactive flowchart."),
      form),
    h("div", {}, h("h2", { style: { margin: "6px 0 10px" } }, "Traces"), h("div", { class: "card" }, list))));

  async function refresh() {
    const traces = await api.list().catch(() => []);
    list.replaceChildren(...(traces.length ? traces.map((t) => h("a", { class: "trace-row", href: `#/t/${encodeURIComponent(t.id)}` },
      h("span", {}, h("b", {}, t.name), h("br"), h("span", { class: "hint" }, fmt.stamp(t.created_at),
        t.status === "completed" ? ` · ${plural(t.violations, "violation")}` : "")),
      h("span", { class: `pill ${t.status}` }, t.status),
      h("button", { type: "button", class: "btn btn-sm btn-ghost", title: "Delete", onclick: async (e) => {
        e.preventDefault();
        if (!confirm(`Delete the trace of ${t.name}?`)) return;
        await api.del(t.id).catch((ex) => alert(ex.message));
        refresh();
      } }, "Delete"))) : [h("p", { class: "hint" }, "No traces yet.")]));
    if (traces.some((t) => t.status === "running" || t.status === "queued")) timer = setTimeout(refresh, 2000);
  }
  let timer = null;
  stop = () => clearTimeout(timer);
  refresh();
}

// ---------------------------------------------------------------- trace view

async function traceView(id) {
  const view = { check: null, worker: null, all: false, follow: true };
  let t = await api.get(id);
  let timer = null;
  stop = () => clearTimeout(timer);

  function select(check, worker) {
    view.check = check;
    view.worker = worker ?? null;
    view.follow = false;
    render();
    document.getElementById("stage-5")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function render() {
    const y = window.scrollY;
    const running = t.status === "running" || t.status === "queued";
    if (view.check == null || (running && view.follow)) view.check = t.checks.length ? t.checks.length - 1 : null;
    app.replaceChildren(...[
      header(t),
      running ? runningNote(t) : null,
      t.status === "failed" ? h("p", { class: "alert", style: { marginBottom: "16px" } }, `This run failed: ${t.error}`) : null,
      overview(t),
      legend(),
      stageInput(t), down(),
      stageSettings(t), down(),
      stageTracking(t, select), down(),
      stageSchedule(t, view, select), down(),
      stageCheck(t, view, render), down(),
      stageViolations(t, select), down(),
      stageOutputs(t)].filter(Boolean)); // replaceChildren would print null as text
    window.scrollTo(0, y);
  }

  async function poll() {
    try { t = await api.get(id); } catch { /* try again */ }
    render();
    if (t.status === "running" || t.status === "queued") timer = setTimeout(poll, 1500);
  }
  render();
  if (t.status === "running" || t.status === "queued") timer = setTimeout(poll, 1500);

  document.onkeydown = (e) => {
    if (!/^#\/t\/[^/?]+$/.test(location.hash) || e.target.closest?.("input, select, textarea")) return; // flowchart only
    if (e.key === "ArrowRight" && view.check != null && view.check < t.checks.length - 1) select(view.check + 1);
    if (e.key === "ArrowLeft" && view.check > 0) select(view.check - 1);
  };
}

function header(t) {
  return h("div", { class: "trace-head" },
    h("div", {}, h("span", { class: "eyebrow" }, "Inference trace"), h("h1", {}, t.name),
      h("div", { class: "sub" }, h("span", { class: `pill ${t.status}` }, t.status), h("span", {}, fmt.stamp(t.created_at)),
        h("span", {}, `${t.video.width}×${t.video.height} · ${t.video.fps} fps · ${fmt.tp(t.video.duration_sec)}`),
        h("span", {}, `${t.settings.required_ppe.map((i) => t.settings.items[i].replace(/^No /, "")).join(" + ")} · every ${t.settings.interval_sec} s`))),
    h("div", { class: "actions" },
      h("a", { class: "btn btn-primary", href: `#/t/${encodeURIComponent(t.id)}/walk` }, "Step through ▶"),
      h("a", { class: "btn", href: "#/" }, "All traces"),
      t.outputs?.video ? h("a", { class: "btn", href: file(t, t.outputs.video), download: "" }, "Annotated video") : null,
      t.outputs?.report ? h("a", { class: "btn", href: file(t, t.outputs.report), download: "" }, "Report JSON") : null,
      h("a", { class: "btn", href: `/api/traces/${encodeURIComponent(t.id)}`, download: `${t.name}_trace.json` }, "Trace JSON")));
}

function runningNote(t) {
  const p = t.progress?.total ? t.progress.done / t.progress.total : 0;
  return h("div", { class: "card running-note" },
    h("div", { style: { display: "flex", justifyContent: "space-between" } },
      h("b", {}, t.status === "queued" ? "Queued" : `Running the pipeline… ${fmt.pct(p)}`),
      h("span", { class: "hint" }, `${plural(t.checks.length, "check")} recorded so far · the flowchart fills in as they happen`)),
    h("div", { class: "bar" }, h("i", { style: { width: `${p * 100}%` } })));
}

function overview(t) {
  const checked = t.checks.flatMap((c) => c.workers.filter((w) => w.checked));
  const calls = (t.calls?.query ?? 0) + (t.calls?.detect ?? 0);
  const regular = t.checks.filter((c) => !c.arrival).length;
  const stat = (v, l) => h("div", { class: "card stat" }, h("b", { class: "num" }, v), h("span", {}, l));
  return h("div", { class: "stats" },
    stat(t.frames_written ?? t.progress?.done ?? 0, "frames processed"),
    stat(`${regular} + ${t.checks.length - regular}`, "regular + arrival checks"),
    stat(checked.length, "worker checks"),
    stat(calls, `Moondream calls (${t.calls?.query ?? 0} query · ${t.calls?.detect ?? 0} detect)`),
    stat(calls ? fmt.ms(Math.round(t.calls.ms / calls)) : "–", "average per call"),
    stat(t.summary ? t.summary.events.length : "…", "violations"));
}

function legend() {
  return h("div", { class: "legend" }, h("span", { class: "eyebrow" }, "Shapes"),
    node("io", { v: "Data / image" }), node("model", { v: "Moondream call" }), node("decision", { v: "Decision" }),
    node("skip", { v: "Skipped (setting off)" }), node("result", { v: "Result", cls: "is-ok" }),
    h("span", { class: "hint" }, "Click any box for its details · ← → move between checks"));
}

// ---------------------------------------------------------------- 1. input

function stageInput(t) {
  const s = t.settings;
  return stage(1, "Input video", "The uploaded video is decoded frame by frame; some frames are sampled for PPE checks.",
    chain(
      node("io", { k: "Upload", v: t.name, s: `${t.video.width}×${t.video.height}`, details: { title: "Upload", explain: EXPLAIN.upload, json: t.video } }),
      node("io", { k: "Decode", v: `${t.video.frames} frames`, s: `${t.video.fps} fps · ${fmt.tp(t.video.duration_sec)}${s.max_seconds ? ` · first ${s.max_seconds} s` : ""}`,
        details: { title: "Decode", explain: EXPLAIN.decode, fields: [["Processed at", (t.frame_size ?? [t.video.width, t.video.height]).join("×")]] } }),
      node("decision", { k: "Sample", v: `Every ${s.check_every_frames} frames`, s: `= every ${s.interval_sec} s, plus arrival checks`,
        details: { title: "Which frames are checked", explain: EXPLAIN.sample } })));
}

// ---------------------------------------------------------------- 2. settings

function stageSettings(t) {
  const s = t.settings, tun = s.tuning;
  const sw = (on, label, explain) => node(on ? "decision" : "skip", { k: on ? "On" : "Off", v: label, s: explain,
    details: { title: label, explain: `${explain} This setting is ${on ? "on" : "off"} for this run.` } });
  return stage(2, "Settings and prompts", `${s.choice === "saved" ? "Developer options as saved in the main app" : "Default settings"}. These are the exact prompts sent to Moondream (${s.model} at ${s.endpoint_host}).`,
    h("div", { class: "chain", style: { gap: "10px" } }, s.required_ppe.map((item) => {
      const it = tun.items[item];
      return node("io", { k: s.items[item], v: h("q", {}, s.questions[item]),
        s: `detect “${it.detect_term}”${it.body_part ? ` · body part “${it.body_part}”` : ""} · region ${it.region_top}–${it.region_bottom} of the box`,
        details: { title: `${s.items[item]}: prompts`, explain: "The question for this item is the template with the item's wording filled in.",
          fields: [["Question sent with each worker crop", s.questions[item]], ["Detect term (to confirm a “no”)", it.detect_term],
            ["Re-check question for a detected box", s.verify_questions[item]], ["Body part (if the item isn't found)", it.body_part ?? "–"],
            ["Where on the worker (fraction of box height)", `${it.region_top} to ${it.region_bottom}`]], json: it } });
    })),
    s.multi_question && tun.one_query_per_worker ? h("div", { style: { marginTop: "10px" } },
      node("model", { k: "One question for all items", v: h("q", {}, s.multi_question), details: { title: "Combined question", explain: EXPLAIN.combined, fields: [["Prompt", s.multi_question]] } })) : null,
    h("h3", { style: { margin: "16px 0 8px" } }, "Decision rules"),
    h("div", { class: "chain", style: { gap: "8px" } },
      sw(tun.one_query_per_worker, "Ask about all items at once", "One question per worker instead of one per item."),
      sw(tun.confirm_no_with_detect, "Confirm “no” with detect", "A “no” only counts if detect can't find the item on the worker."),
      sw(tun.verify_detected_box, "Re-check a detected box", "A detected box must be confirmed by asking about it alone."),
      sw(tun.check_body_part, "Not visible when the body part can't be seen", "If the head/hand/foot isn't found, the item is “not visible”, not missing."),
      sw(tun.confirm_yes_on_overlap, `Confirm “yes” when overlapping (≥ ${fmt.pct(tun.overlap_threshold)})`, "A “yes” for a worker overlapping a neighbour must be confirmed by detect."),
      node("io", { k: "Unclear answer", v: `counts as ${tun.unclear_answer}`, details: { title: "Unclear answers", explain: EXPLAIN.read } }),
      node("io", { k: "Vote", v: `${tun.confirm_checks} of last ${tun.vote_window} checks`, s: "for an item to show as missing", details: { title: "Voting", explain: EXPLAIN.vote } }),
      node("io", { k: "Too far", v: `< ${fmt.pct(tun.min_check_height_frac)} of frame height or < ${tun.min_check_height_px} px`, details: { title: "Minimum size", explain: EXPLAIN.gate_too_far } })),
    s.changes.length ? h("p", { class: "hint", style: { marginTop: "10px" } }, `Changed from the defaults: ${s.changes.join(", ")}`) : null);
}

// ---------------------------------------------------------------- 3. tracking

function stageTracking(t, select) {
  const tun = t.settings.tuning;
  const total = Math.max(1, t.frames_written ?? t.progress?.total ?? t.video.frames);
  const x = (f) => `${(f / total) * 100}%`;
  const dots = {};
  t.checks.forEach((c) => c.workers.forEach((w) => {
    if (w.track_id == null) return;
    (dots[w.track_id] ??= []).push(h("button", { type: "button", class: `lane-dot dot-${w.state}`, style: { left: x(c.frame) },
      title: `Check ${c.index + 1} at ${fmt.tp(c.time_sec)}: ${STATE[w.state]?.[0] ?? w.state}`, onclick: () => select(c.index, w.track_id) }));
  }));
  const lanes = t.tracks.map((tr) => h("div", { class: "lane" }, h("span", {}, `#${tr.id}`),
    h("div", { class: "lane-track" },
      h("span", { class: "lane-bar", style: { left: x(tr.first_frame), width: `calc(${x(tr.last_frame - tr.first_frame + 1)})` },
        title: `On screen ${fmt.tp(tr.first_frame / t.video.fps)}–${fmt.tp(tr.last_frame / t.video.fps)} (${tr.frames} frames)` }),
      dots[tr.id] ?? [])));
  const ticks = Math.min(10, Math.ceil(total / t.video.fps));
  return stage(3, "Person detection and tracking", "Every frame: find people, give each one an ID and follow them. Only established tracks are checked.",
    chain(
      node("io", { k: "Detector", v: tun.person_model || "YOLO11s (server default)", s: `${tun.detect_fps ? `${tun.detect_fps}× a second` : "every frame"} · ${tun.detector_imgsz}px · conf ${tun.detector_conf}`,
        details: { title: "Person detector", explain: EXPLAIN.detector } }),
      node("io", { k: "Tracker", v: "BoT-SORT", s: `IDs kept ${tun.hidden_sec} s when hidden · tracks shown after ${tun.min_track_age_sec} s${tun.fixed_camera ? " · fixed camera" : ""}`,
        details: { title: "Tracker", explain: EXPLAIN.tracker } }),
      node("result", { cls: "is-ok", k: "Tracks", v: plural(t.tracks.length, "person"), details: { title: "Tracks", explain: EXPLAIN.tracks, json: t.tracks } })),
    h("div", { style: { marginTop: "14px" } },
      h("div", { class: "gantt" }, lanes.length ? lanes : h("p", { class: "hint" }, "Nobody tracked yet.")),
      h("div", { class: "lane-axis" }, Array.from({ length: ticks + 1 }, (_, i) => h("span", { style: { left: `${(i / ticks) * 100}%` } }, fmt.time((i / ticks) * total / t.video.fps)))),
      h("div", { class: "gantt-legend" }, Object.entries(STATE).map(([k, [label]]) => h("span", {}, h("i", { class: `dot-${k}` }), label)))));
}

// ---------------------------------------------------------------- 4. schedule

function stageSchedule(t, view, select) {
  const strip = h("div", { class: "schedule" });
  t.checks.forEach((c, i) => {
    if (i) strip.append(arrow());
    const bad = c.workers.filter((w) => w.state === "violation").length;
    const ok = c.workers.filter((w) => w.state === "ok").length;
    strip.append(h("button", { type: "button", class: `sched-node${c.arrival ? " is-arrival" : ""}`, "aria-pressed": String(view.check === i),
      onclick: () => select(i) },
      h("img", { src: pic(t, c.annotated), alt: "", loading: "lazy" }),
      h("span", { class: "t" }, `#${i + 1} · ${fmt.tp(c.time_sec)}`),
      h("span", { class: "hint" }, c.arrival ? "Arrival check" : "Regular check"),
      h("span", { class: "c" }, h("span", { class: "tag tag-muted" }, `${c.workers.length} on screen`),
        ok ? h("span", { class: "tag tag-ok" }, `${ok} OK`) : null, bad ? h("span", { class: "tag tag-bad" }, `${bad} missing`) : null)));
  });
  return stage(4, "Check schedule", `${plural(t.checks.length, "check")}: solid boxes are regular checks (everyone on screen), dashed ones arrival checks (only newcomers). Pick one to open it below.`,
    t.checks.length ? strip : h("p", { class: "hint" }, "Waiting for the first check…"));
}

// ---------------------------------------------------------------- 5. one check in full

function stageCheck(t, view, rerender) {
  const c = t.checks[view.check];
  if (!c) return stage(5, "Inside a check", "Nothing to show yet.");
  const size = t.frame_size ?? [t.video.width, t.video.height];
  const tun = t.settings.tuning;
  const minH = Math.max(tun.min_check_height_frac * size[1], tun.min_check_height_px);
  const pick = (tid) => { view.worker = tid; view.all = false; rerender(); };
  if (view.worker == null || !c.workers.some((w) => w.track_id === view.worker)) {
    view.worker = (c.workers.find((w) => w.state === "violation") ?? c.workers.find((w) => w.checked) ?? c.workers[0])?.track_id ?? null;
  }
  const frameBoxes = c.workers.map((w) => ({
    rect: [w.box[0] / size[0], w.box[1] / size[1], w.box[2] / size[0], w.box[3] / size[1]],
    cls: `${COLOR_CLASS[w.drawn?.color] ?? (w.state === "violation" ? "red" : w.state === "ok" ? "" : "grey")}${w.track_id === view.worker ? " sel" : ""}`,
    label: `#${w.track_id}`, id: w.track_id, title: `#${w.track_id}: ${STATE[w.state]?.[0]}`,
  }));
  const gates = c.workers.map((w) => {
    const ht = w.box[3] - w.box[1];
    const why = w.checked ? `Checked: ${Math.round(ht)} px tall (min ${Math.round(minH)})`
      : w.too_small ? `Too far: ${Math.round(ht)} px < ${Math.round(minH)} px · no calls`
      : w.check_failed ? "Every Moondream call failed"
      : "Not checked at this frame (too new or still entering)";
    return h("button", { type: "button", class: "gate", "aria-pressed": String(!view.all && w.track_id === view.worker), onclick: () => pick(w.track_id) },
      h("span", { class: "id" }, `#${w.track_id}`),
      h("span", {}, h("span", { class: `tag tag-${STATE[w.state]?.[1] === "bad" ? "bad" : STATE[w.state]?.[1] === "ok" ? "ok" : STATE[w.state]?.[1] === "warn" ? "warn" : "muted"}` }, STATE[w.state]?.[0] ?? w.state),
        h("br"), h("span", { class: "why" }, why)));
  });
  const shown = view.all ? c.workers : c.workers.filter((w) => w.track_id === view.worker);
  return stage(5, `Inside check #${view.check + 1} · ${fmt.tp(c.time_sec)}`,
    `${c.arrival ? "Arrival check: only workers who just appeared." : "Regular check: every worker on screen."} Detected at ${fmt.clock(c.detected_at)}. Each worker goes through: size gate → crop → one Moondream question per item → decisions → result → vote → what's drawn.`,
    h("div", { class: "check-top" },
      h("div", { class: "card frame-card" },
        h("div", { class: "toolbar" }, h("span", { class: "eyebrow" }, "Frame (no boxes drawn) with every tracked worker")),
        h("button", { type: "button", class: "node node-image", style: { maxWidth: "100%", width: "100%" },
          onclick: () => openDrawer({ title: `Frame at ${fmt.tp(c.time_sec)}`, explain: EXPLAIN.frame,
            images: [boxedImage(pic(t, c.image), frameBoxes), h("img", { src: pic(t, c.annotated), alt: "As drawn in the annotated video" })],
            fields: [["Frame", c.frame], ["Workers", c.workers.length]] }) },
          boxedImage(pic(t, c.image), frameBoxes, { onBox: pick }))),
      h("div", {}, h("div", { class: "toolbar" }, h("span", { class: "eyebrow" }, "Who gets checked"),
        h("a", { class: "btn btn-sm", href: `#/t/${encodeURIComponent(t.id)}/walk?check=${view.check}` }, "Step through this check ▶"),
        h("span", { class: "seg" },
          h("button", { type: "button", "aria-pressed": String(!view.all), onclick: () => { view.all = false; rerender(); } }, "One worker"),
          h("button", { type: "button", "aria-pressed": String(view.all), onclick: () => { view.all = true; rerender(); } }, "All workers"))),
        h("div", { class: "gate-list" }, gates))),
    shown.map((w) => workerLane(t, c, w, minH)));
}

function workerLane(t, c, w, minH) {
  const tun = t.settings.tuning;
  const rec = w.trace;
  const head = h("h3", {}, h("span", { class: "id" }, `#${w.track_id}`), STATE[w.state]?.[0] ?? w.state);
  const drawn = w.drawn ? node("result", { cls: `is-${{ red: "bad", green: "ok", amber: "warn", grey: "grey" }[w.drawn.color]}`, k: "Drawn in the video", v: w.drawn.tag,
    details: { title: "Drawn in the video", explain: EXPLAIN.drawn, images: [h("img", { src: pic(t, c.annotated), alt: "" })], fields: [["Tag", w.drawn.tag], ["Colour", w.drawn.color]] } }) : null;

  if (!rec) {
    const ht = w.box[3] - w.box[1];
    const gate = w.too_small
      ? node("decision", { cls: "is-no", k: "Size gate", v: `${Math.round(ht)} px < ${Math.round(minH)} px`, s: "Too far: no Moondream call", details: { title: "Too far to check", explain: EXPLAIN.gate_too_far, fields: [["Box", w.box.join(", ")]] } })
      : node("decision", { cls: "is-unclear", k: "Age gate", v: "Not due yet", s: "Tracked too briefly, or still entering the frame", details: { title: "Not checked yet", explain: EXPLAIN.gate_new, fields: [["Box", w.box.join(", ")]] } });
    return h("div", { class: `card lane-card is-${w.state === "violation" ? "violation" : "other"}` }, head, chain(gate, drawn));
  }

  const [cx1, cy1, cx2, cy2] = rec.crop_box;
  const cw = cx2 - cx1, ch = cy2 - cy1;
  const inCrop = (b) => [(b[0] - cx1) / cw, (b[1] - cy1) / ch, (b[2] - cx1) / cw, (b[3] - cy1) / ch];
  const cropBoxes = [...rec.others.map((o) => ({ rect: inCrop(o), cls: "grey", label: "other" })), { rect: inCrop(w.box), cls: "", label: `#${w.track_id}` }];
  const cropNode = node("io", { cls: "node-image", k: "Crop sent to Moondream",
    body: h("span", { style: { width: "130px" } }, boxedImage(pic(t, rec.crop), cropBoxes)),
    s: `${rec.crop_size.join("×")} px · box + ${fmt.pct(tun.crop_padding)} padding`,
    details: { title: `Crop of worker #${w.track_id}`, explain: EXPLAIN.crop, images: [boxedImage(pic(t, rec.crop), cropBoxes)],
      fields: [["Crop box (frame px)", rec.crop_box.join(", ")], ["Worker box", w.box.join(", ")], ["Neighbours in the frame", rec.others.length]] } });
  const gate = node("decision", { cls: "is-yes", k: "Size gate", v: `${Math.round(w.box[3] - w.box[1])} px ≥ ${Math.round(minH)} px`, s: "Big enough and due: check",
    details: { title: "Checked", explain: EXPLAIN.gate_checked } });
  const together = rec.together?.length ? rec.together.map((st) => st.step === "call"
    ? modelNode(t, st, "Combined question")
    : node("decision", { k: "Read each line", v: Object.entries(st.parsed).map(([k, v]) => `${k}: ${v}`).join(" · ") || "nothing readable",
      details: { title: "Combined answer", explain: EXPLAIN.combined, fields: [["Answer", st.answer]], json: st } })) : [];

  const items = Object.entries(rec.items).map(([item, r]) => h("div", { class: "item-row" },
    h("span", { class: "item-name" }, t.settings.items[item]?.replace(/^No /, "") ?? item),
    itemChain(t, w, rec, item, r)));

  return h("div", { class: `card lane-card is-${w.state === "violation" ? "violation" : w.state === "ok" ? "ok" : w.state === "unverified" ? "unverified" : "other"}` },
    head, chain(gate, cropNode, together),
    items,
    h("div", { class: "merge" }, chain(
      node("result", { cls: w.violations.length ? "is-bad" : w.unverified.length ? "is-warn" : "is-ok", k: "This check found",
        v: w.violations.length ? w.violations.join(", ") : w.unverified.length ? `${w.unverified.join(", ")} not visible` : "All items present",
        details: { title: "This check's result", explain: EXPLAIN.check_result, json: { violations: w.violations, unverified: w.unverified, failed: w.failed } } }),
      voteNode(t, w), drawn)));
}

function modelNode(t, st, title) {
  const isDetect = st.kind === "detect";
  const objects = st.response?.objects ?? [];
  const answer = st.error ? `Failed: ${st.error}` : isDetect ? `${plural(objects.length, "box")} found` : st.response?.answer;
  const img = st.image ? boxedImage(pic(t, st.image), objects.map((o, i) => ({ rect: [o.x_min, o.y_min, o.x_max, o.y_max], cls: st._judged?.[i] ?? "off", label: `${i + 1}` }))) : null;
  return node("model", { k: `${title ?? (isDetect ? "Moondream detect" : "Moondream query")} · ${fmt.ms(st.ms ?? 0)}`,
    body: isDetect && img ? h("span", { style: { width: "110px" } }, img) : null,
    v: isDetect ? h("span", {}, "Find ", h("q", {}, st.prompt)) : h("q", {}, st.prompt.length > 90 ? `${st.prompt.slice(0, 88)}…` : st.prompt),
    s: h("span", {}, "→ ", h("span", { class: "answer" }, answer ?? "")),
    details: { title: title ?? (isDetect ? `Detect “${st.prompt}”` : "Moondream query"), explain: isDetect ? EXPLAIN.detect : EXPLAIN.query,
      images: img ? [boxedImage(pic(t, st.image), objects.map((o, i) => ({ rect: [o.x_min, o.y_min, o.x_max, o.y_max], cls: st._judged?.[i] ?? "off", label: `${i + 1}` })))] : [],
      fields: [[isDetect ? "Term" : "Prompt", st.prompt], ["Answer", answer],
        ["Round trip (sent → answer received)", fmt.ms(st.ms ?? 0)],
        ["Moondream's own processing", modelTime(st.response?.metrics)],
        ["Image sent", st.image_size ? `${st.image_size.join("×")} px` : null],
        ["Raw response", JSON.stringify(st.response ?? st.error, null, 2)]] } });
}

// One item's path through the decision tree, from the recorded steps.
function itemChain(t, w, rec, item, r) {
  const tun = t.settings.tuning;
  const spec = tun.items[item];
  const steps = r.steps;
  // Colour each detect box by its judgment (judgments follow the detect call, in order).
  steps.forEach((st, i) => {
    if (st.step !== "call" || st.kind !== "detect") return;
    st._judged = [];
    for (let j = i + 1; j < steps.length && steps[j].step !== "detect"; j++) {
      if (steps[j].step === "detect_object") st._judged.push(steps[j].on_worker ? "" : steps[j].reason === "not needed" ? "grey" : "off");
    }
  });
  const out = [];
  let readAs = null;
  for (const st of steps) {
    if (st.step === "call") {
      out.push(modelNode(t, st, st.kind === "query" && readAs != null ? "Re-check the box" : null));
    } else if (st.step === "query") {
      readAs = st.read_as;
      if (st.question == null) out.push(node("io", { k: "Answer from", v: "the combined question", s: `“${st.answer}”`, details: { title: "Answer from the combined question", explain: EXPLAIN.combined } }));
      out.push(node("decision", { cls: `is-${readAs}`, k: "Read the answer", v: readAs.toUpperCase(), s: `“${st.answer}”`,
        details: { title: "Reading the answer", explain: EXPLAIN.read, fields: [["Answer", st.answer], ["Read as", readAs]] } }));
      if (readAs === "unclear") {
        out.push(node("decision", { cls: "is-unclear", k: "Unclear answer", v: `counts as ${tun.unclear_answer}`, details: { title: "Unclear answer", explain: EXPLAIN.read } }));
      } else if (readAs === "yes") {
        const share = overlapShare(w.box, rec.others);
        if (!tun.confirm_yes_on_overlap) out.push(node("skip", { k: "Overlap check", v: "Off", s: "“yes” stands", details: { title: "Overlap check off", explain: EXPLAIN.overlap } }));
        else if (share < tun.overlap_threshold) out.push(node("decision", { cls: "is-yes", k: "Overlap with neighbours", v: `${fmt.pct(share)} < ${fmt.pct(tun.overlap_threshold)}`, s: "“yes” is about this worker",
          details: { title: "Overlap with neighbours", explain: EXPLAIN.overlap, fields: [["Share of the box covered by others", fmt.pct(share)]] } }));
      } else if (readAs === "no") {
        if (!tun.confirm_no_with_detect) out.push(node("skip", { k: "Confirm “no” with detect", v: "Off", s: "“no” stands", details: { title: "Confirmation off", explain: EXPLAIN.confirm_no } }));
        else out.push(arrow("confirm"));
      }
    } else if (st.step === "overlap") {
      out.push(node("decision", { cls: "is-unclear", k: "Overlap with neighbours", v: `${fmt.pct(st.share)} ≥ ${fmt.pct(tun.overlap_threshold)}`, s: "confirm the “yes” with detect",
        details: { title: "Overlapping a neighbour", explain: EXPLAIN.overlap } }));
    } else if (st.step === "detect_object") {
      out.push(node("decision", { cls: st.on_worker ? "is-yes" : "is-no", k: `“${st.term}” box`, v: REASON[st.reason] ?? st.reason,
        details: { title: `Judging a “${st.term}” box`, explain: EXPLAIN.box, fields: [["Box (fraction of the crop)", JSON.stringify(st.box)], ["Verdict", REASON[st.reason] ?? st.reason]] } }));
    } else if (st.step === "verify") {
      out.push(node("decision", { cls: st.confirmed ? "is-yes" : "is-no", k: "Re-check answer", v: st.confirmed ? "Confirmed" : "Not confirmed", s: `“${st.answer}”`,
        details: { title: "Re-check of the detected box", explain: EXPLAIN.verify, fields: [["Question", st.question], ["Answer", st.answer]] } }));
    } else if (st.step === "detect") {
      const bodyPart = st.term === spec.body_part && st.term !== spec.detect_term;
      out.push(node("decision", { cls: st.on_worker ? "is-yes" : "is-no", k: bodyPart ? `Is the ${st.term} visible?` : `${st.term} on this worker?`,
        v: st.on_worker ? "Yes" : "No", s: `${plural(st.found, "box")} found`,
        details: { title: bodyPart ? "Body part check" : "Item found on the worker?", explain: bodyPart ? EXPLAIN.body : EXPLAIN.detect, json: st } }));
    }
  }
  const status = r.status;
  out.push(node("result", { cls: `is-${status}`, k: "Item result", v: ITEM_RESULT[status] ?? status, s: whyItem(status, steps, tun, spec),
    details: { title: `${t.settings.items[item]?.replace(/^No /, "")}: ${ITEM_RESULT[status] ?? status}`, explain: whyItem(status, steps, tun, spec), json: steps } }));
  return chain(out);
}

function voteNode(t, w) {
  const tun = t.settings.tuning;
  const labels = Object.values(t.settings.items);
  const win = w.window;
  const unvName = (l) => l.replace(/^No /, "");
  const cell = (c, l) => (c.violations.includes(l) ? h("td", { class: "y" }, "✗") : c.unverified.map((u) => u.toLowerCase()).includes(unvName(l).toLowerCase()) ? h("td", { class: "u" }, "?") : h("td", { class: "n" }, "✓"));
  const table = h("table", { class: "vote" },
    h("tr", {}, h("th", {}, ""), win.map((c) => h("th", {}, `#${c.check + 1}`)), h("th", {}, "shown")),
    labels.map((l) => h("tr", {}, h("th", {}, unvName(l)), win.map((c) => cell(c, l)),
      w.shown.includes(l) ? h("td", { class: "y" }, "✗") : h("td", { class: "n" }, w.confirmed ? "✓" : "…"))));
  const verdict = !w.confirmed ? `Only ${plural(win.length, "check")} so far: “Checking…” until ${tun.confirm_checks}`
    : w.shown.length ? `Shown missing: ${w.shown.map(unvName).join(", ")}` : "Shown: PPE OK";
  return node("decision", { cls: !w.confirmed ? "is-unclear" : w.shown.length ? "is-no" : "is-yes", k: `Vote over last ${win.length} of ${tun.vote_window} checks`,
    body: table, v: verdict,
    details: { title: "Vote", explain: EXPLAIN.vote, fields: [["Rule", `An item is shown missing when it was missing in more than half of the last ${tun.vote_window} checks and at least ${tun.confirm_checks} times; on a tie the previous state holds.`], ["Window", table.cloneNode(true)]], json: win } });
}

// ---------------------------------------------------------------- 6. violations and figures

function stageViolations(t, select) {
  const s = t.summary;
  if (!s) return stage(6, "From checks to violations", "Filled in when the run completes.");
  const rows = s.events.map((e, i) => {
    const checks = t.checks.filter((c) => c.time_sec >= e.time_sec && c.time_sec <= (e.end_sec ?? e.time_sec)
      && c.workers.some((w) => w.track_id === e.track_id && w.state === "violation"));
    return h("tr", {},
      h("td", {}, i + 1),
      h("td", {}, e.track_id != null ? `#${e.track_id}` : "–"),
      h("td", {}, e.violations.map((v) => h("span", { class: "tag tag-bad", style: { marginRight: "4px" } }, v))),
      h("td", { class: "num" }, `${fmt.tp(e.time_sec)}–${fmt.tp(e.end_sec ?? e.time_sec)}`),
      h("td", { class: "num" }, fmt.clock(e.detected_at)),
      h("td", {}, checks.map((c) => h("button", { type: "button", class: "link-btn", style: { marginRight: "4px" }, onclick: () => select(c.index, e.track_id) }, `#${c.index + 1}`))));
  });
  const judged = s.ok_checks + s.violation_checks;
  const formula = (title, value, expr) => h("div", { class: "card formula" }, h("span", { class: "eyebrow" }, title), h("b", { class: "num" }, value), h("code", {}, expr));
  return stage(6, "From checks to violations", EXPLAIN.incident,
    chain(
      node("io", { k: "Worker checks", v: plural(s.worker_checks, "check"), s: `${s.ok_checks} OK · ${s.violation_checks} missing · ${s.unverified_checks} not visible · ${s.pending_checks} first`, details: { title: "Worker checks", explain: "Every checked worker at every check, by the state their voted status was in after it." } }),
      node("decision", { k: "Group", v: "Consecutive missing checks of one worker", details: { title: "Grouping into violations", explain: EXPLAIN.incident } }),
      node("result", { cls: s.events.length ? "is-bad" : "is-ok", k: "Violations", v: plural(s.events.length, "violation"), details: { title: "Violations", explain: EXPLAIN.incident, json: s.events } })),
    s.events.length ? h("table", { class: "inc-table", style: { marginTop: "14px" } },
      h("tr", {}, ["No.", "Worker", "Missing", "Video time", "Detected at", "Checks (click to open)"].map((x) => h("th", {}, x))), rows) : null,
    h("div", { class: "formulas" },
      formula("Compliance", s.compliance_rate == null ? "–" : fmt.pct(s.compliance_rate), `OK checks ÷ (OK + missing) = ${s.ok_checks} ÷ ${judged}`),
      formula("Time with a violation", s.frames_analysed ? fmt.pct(s.frames_with_violation / s.frames_analysed) : "–", `regular checks with a violation ÷ regular checks = ${s.frames_with_violation} ÷ ${s.frames_analysed}`),
      formula("Violations by type", s.by_type.map((x) => `${x.label} ${x.count}`).join(" · ") || "none", "violations that include each item (one violation can include several)"),
      formula("People", `${s.people.length} checked`, `${s.people.filter((p) => p.state === "flagged").length} with a violation · peak ${s.peak_workers} on screen`)));
}

// ---------------------------------------------------------------- 7. outputs

function stageOutputs(t) {
  if (t.status !== "completed") return stage(7, "Outputs", "Ready when the run completes.");
  return stage(7, "Outputs", "The annotated video (every frame, with each worker's voted status drawn), the report with every check, and this trace.",
    h("div", { class: "outputs" },
      h("video", { src: file(t, t.outputs.video), controls: true, playsinline: true, preload: "metadata" }),
      h("div", { class: "chain", style: { flexDirection: "column", alignItems: "stretch", gap: "8px" } },
        node("result", { cls: "is-ok", k: "Annotated video", v: `${t.frames_written} frames`, s: "H.264, original audio", details: { title: "Annotated video", explain: "Every frame of the input with the boxes and tags; the same video the main app shows." } }),
        node("result", { cls: "is-ok", k: "Report", v: "report.json", s: "settings and every check with its vote", details: { title: "Report", explain: "The pipeline's own report: the same data the main app's dashboard is built from." } }),
        node("result", { cls: "is-ok", k: "Trace", v: "This flowchart", s: `${t.calls.query + t.calls.detect} Moondream calls recorded`, details: { title: "Trace", explain: "Everything on this page, as JSON: download it with the Trace JSON button." } }))));
}
