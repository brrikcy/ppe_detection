// Step-through view of a trace (#/t/<id>/walk): the run as an ordered list of steps,
// one on screen at a time, with a map of the system showing where the step sits.
// Next / Back walk one step; the skip buttons jump to the next item, worker, check or
// part of the flow; "Hide repeats" folds a worker's check into one step when Moondream
// answered exactly as at their previous check.

import { EXPLAIN, ITEM_RESULT, REASON, STATE, api, boxedImage, fmt, h, modelTime, overlapShare, pic, plural, whyItem } from "./shared.js";

const STORE = "ppe.walk.hideRepeats";
const AUTOPLAY_MS = 2600;

// The system, left to right. Steps point at one of these; the loop covers frame..draw.
const MAP = [
  ["input", "Video in"], ["settings", "Prompts"], ["tracking", "Track people"],
  ["frame", "Frame"], ["gate", "Size gate"], ["crop", "Crop"], ["ask", "Ask Moondream"],
  ["read", "Read answer"], ["confirm", "Confirm"], ["result", "Result"], ["vote", "Vote"], ["draw", "Draw"],
  ["violations", "Violations"], ["outputs", "Outputs"],
];
const LOOP = ["frame", "gate", "crop", "ask", "read", "confirm", "result", "vote", "draw"];
const ITEM_LOOP = ["ask", "read", "confirm", "result"];
const PARTS = [["input", "Input"], ["settings", "Settings"], ["tracking", "Tracking"], ["checks", "Checks"],
  ["violations", "Violations"], ["outputs", "Outputs"]];

// ---------------------------------------------------------------- visuals

function frameSize(t) { return t.frame_size ?? [t.video.width, t.video.height]; }

// The check frame with boxes; `focus` highlights one worker and greys the others.
function framePic(t, c, { focus = null, annotated = false, plain = false } = {}) {
  const [fw, fh] = frameSize(t);
  const boxes = annotated || plain ? [] : c.workers.map((w) => {
    const color = { red: "red", green: "", amber: "amber", grey: "grey" }[w.drawn?.color] ?? (w.state === "violation" ? "red" : w.state === "ok" ? "" : "grey");
    const dim = focus != null && w.track_id !== focus;
    return { rect: [w.box[0] / fw, w.box[1] / fh, w.box[2] / fw, w.box[3] / fh], cls: `${dim ? "grey dim" : color}${w.track_id === focus ? " sel" : ""}`, label: dim ? "" : `#${w.track_id}` };
  });
  if (annotated && focus != null) {
    const w = c.workers.find((x) => x.track_id === focus);
    if (w) boxes.push({ rect: [w.box[0] / fw, w.box[1] / fh, w.box[2] / fw, w.box[3] / fh], cls: "sel ring" });
  }
  return boxedImage(pic(t, annotated ? c.annotated : c.image), boxes);
}

// The crop sent to Moondream, with the worker's box and anything else to show on it.
function cropPic(t, w, extra = []) {
  const rec = w.trace;
  const [cx1, cy1, cx2, cy2] = rec.crop_box;
  const cw = cx2 - cx1, ch = cy2 - cy1;
  const inCrop = (b) => [(b[0] - cx1) / cw, (b[1] - cy1) / ch, (b[2] - cx1) / cw, (b[3] - cy1) / ch];
  return boxedImage(pic(t, rec.crop), [
    ...rec.others.map((o) => ({ rect: inCrop(o), cls: "grey", label: "" })),
    { rect: inCrop(w.box), cls: "", label: `#${w.track_id}` }, ...extra]);
}

// Where on the worker an item sits, as a band on the crop.
function regionBand(w, spec) {
  const [cx1, cy1, cx2, cy2] = w.trace.crop_box;
  const [x1, y1, x2, y2] = w.box;
  const bh = y2 - y1, ch = cy2 - cy1, cw = cx2 - cx1;
  const top = (y1 + spec.region_top * bh - cy1) / ch, bottom = (y1 + spec.region_bottom * bh - cy1) / ch;
  return { rect: [(x1 - cx1) / cw - 0.02, top, (x2 - cx1) / cw + 0.02, bottom], cls: "region", label: "item's region" };
}

function chat(t, step, { title } = {}) {
  const answer = step.error ? `(failed: ${step.error})` : step.kind === "detect"
    ? `${plural(step.response?.objects?.length ?? 0, "box")} found` : step.response?.answer;
  return h("div", { class: "chat" },
    h("div", { class: "msg sys" },
      h("span", { class: "who" }, title ?? "PPE Inspector → Moondream", h("span", { class: "hint" }, step.kind === "detect" ? " · detect" : " · query")),
      step.image ? h("img", { src: pic(t, step.image), alt: "Image sent" }) : null,
      h("p", {}, step.kind === "detect" ? h("span", {}, "Find: ", h("q", {}, step.prompt)) : step.prompt)),
    h("div", { class: "msg md" },
      h("span", { class: "who" }, "Moondream", h("span", { class: "hint" }, ` · ${fmt.ms(step.ms ?? 0)}`)),
      h("p", { class: "ans" }, answer ?? "")));
}

const big = (text, tone) => h("div", { class: `bigword is-${tone}` }, text);
const facts = (rows) => h("dl", { class: "facts" }, rows.filter(([, v]) => v != null && v !== "").map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));

// ---------------------------------------------------------------- the steps

function itemLabel(t, item) { return t.settings.items[item]?.replace(/^No /, "") ?? item; }

function signature(w) {
  if (!w.trace) return null;
  const items = Object.fromEntries(Object.entries(w.trace.items).map(([k, r]) =>
    [k, [r.status, ...r.steps.filter((s) => s.step === "query" || s.step === "detect").map((s) => s.read_as ?? s.on_worker)]]));
  return JSON.stringify([items, w.shown, w.drawn?.tag, w.state]);
}

function buildSteps(t, { hideRepeats }) {
  const steps = [];
  const tun = t.settings.tuning;
  const add = (s) => steps.push(s);
  const first = t.checks[0];
  const [fw, fh] = frameSize(t);
  const minH = Math.max(tun.min_check_height_frac * fh, tun.min_check_height_px);

  // ---- input
  add({ part: "input", map: "input", title: "The video comes in",
    visual: first ? framePic(t, first, { plain: true }) : null,
    text: `“${t.name}” is decoded frame by frame with ffmpeg. Every frame is tracked and written to the annotated video; only some are checked for PPE.`,
    facts: [["Resolution", `${t.video.width}×${t.video.height}`], ["Frame rate", `${t.video.fps} fps`], ["Length", `${fmt.tp(t.video.duration_sec)} · ${t.video.frames} frames`],
      ["Processed at", frameSize(t).join("×")], ["Limit", t.settings.max_seconds ? `first ${t.settings.max_seconds} s` : null]] });
  add({ part: "input", map: "input", title: "Which frames get checked",
    visual: h("div", { class: "mini-schedule" }, t.checks.map((c) => h("span", { class: c.arrival ? "is-arrival" : "", title: fmt.tp(c.time_sec) },
      h("img", { src: pic(t, c.image), alt: "" }), fmt.tp(c.time_sec)))),
    text: `A regular check every ${t.settings.check_every_frames} frames (every ${t.settings.interval_sec} s) looks at everyone on screen. A worker who appears in between gets an arrival check once they qualify. This run made ${t.checks.filter((c) => !c.arrival).length} regular and ${t.checks.filter((c) => c.arrival).length} arrival checks.`,
    facts: [["Check interval", `${t.settings.interval_sec} s`], ["Checks", t.checks.length]] });

  // ---- settings
  add({ part: "settings", map: "settings", title: "The questions Moondream will be asked",
    visual: h("div", { class: "prompt-cards" }, t.settings.required_ppe.map((i) => h("div", { class: "prompt-card" },
      h("span", { class: "eyebrow" }, itemLabel(t, i)), h("q", {}, t.settings.questions[i]),
      h("span", { class: "hint" }, `detect “${tun.items[i].detect_term}”${tun.items[i].body_part ? ` · body part “${tun.items[i].body_part}”` : ""}`)))),
    text: `Every worker crop gets one yes/no question per required item${tun.one_query_per_worker ? " (asked together in one question)" : ""}, sent to ${t.settings.model} at ${t.settings.endpoint_host}. ${t.settings.choice === "saved" ? "These are the Developer options saved in the main app." : "These are the default settings."}`,
    facts: [["Model", t.settings.model], ["Endpoint", t.settings.endpoint_host]] });
  const sw = (on, label) => h("li", { class: on ? "on" : "off" }, h("b", {}, on ? "On" : "Off"), label);
  add({ part: "settings", map: "settings", title: "How answers become decisions",
    visual: h("ul", { class: "switches" },
      sw(tun.confirm_no_with_detect, "Confirm a “no” with detect"), sw(tun.verify_detected_box, "Re-check a detected box"),
      sw(tun.check_body_part, "“Not visible” when the body part can't be seen"), sw(tun.confirm_yes_on_overlap, `Confirm a “yes” when overlapping a neighbour (≥ ${fmt.pct(tun.overlap_threshold)})`),
      h("li", {}, h("b", {}, "→"), `An unclear answer counts as ${tun.unclear_answer}`),
      h("li", {}, h("b", {}, "→"), `Shown missing after ${tun.confirm_checks} of the last ${tun.vote_window} checks`)),
    text: "Steps that are off are skipped for every worker; you'll see them as “skipped” later in the walk.",
    facts: [["Changed from defaults", t.settings.changes.join(", ") || "nothing"]] });

  // ---- tracking
  const total = Math.max(1, t.frames_written ?? t.video.frames);
  add({ part: "tracking", map: "tracking", title: "Finding and following people",
    visual: h("div", { class: "lifelines" }, t.tracks.map((tr) => h("div", { class: "lifeline" }, h("span", {}, `#${tr.id}`),
      h("span", { class: "track" }, h("i", { style: { left: `${(tr.first_frame / total) * 100}%`, width: `${((tr.last_frame - tr.first_frame + 1) / total) * 100}%` } }),
        t.checks.flatMap((c) => c.workers.filter((w) => w.track_id === tr.id).map((w) =>
          h("b", { class: `dot-${w.state}`, style: { left: `${(c.frame / total) * 100}%` }, title: `${fmt.tp(c.time_sec)}: ${STATE[w.state]?.[0]}` }))))))),
    text: `YOLO finds people ${tun.detect_fps ? `${tun.detect_fps} times a second` : "on every frame"} and BoT-SORT links them, so each worker keeps one ID. ${plural(t.tracks.length, "person")} were tracked; each line runs from when they appeared to when they left, with a dot at every check.`,
    facts: [["Detector", tun.person_model || "YOLO11s"], ["People", t.tracks.length]] });

  // ---- checks
  const last = {}; // track id -> {sig, check}
  for (const c of t.checks) {
    const where = `Check ${c.index + 1} of ${t.checks.length} · ${fmt.tp(c.time_sec)}`;
    const checked = c.workers.filter((w) => w.trace);
    add({ part: "checks", map: "frame", check: c.index, crumb: [where], title: `${c.arrival ? "Arrival check" : "Check"} at ${fmt.tp(c.time_sec)}`,
      visual: framePic(t, c),
      text: `${c.arrival ? "Only the worker(s) who just appeared are checked." : "Everyone on screen is considered."} ${plural(c.workers.length, "worker")} here; ${checked.length} big enough and due to be asked about. Detected at ${fmt.clock(c.detected_at)}.`,
      facts: [["Frame", c.frame], ["Workers", c.workers.map((w) => `#${w.track_id} ${STATE[w.state]?.[0] ?? ""}`).join(" · ")]] });
    for (const w of c.workers) {
      const base = { part: "checks", check: c.index, worker: w.track_id, crumb: [where, `Worker #${w.track_id}`] };
      const ht = Math.round(w.box[3] - w.box[1]);
      if (!w.trace) {
        add({ ...base, map: "gate", title: `#${w.track_id} isn't asked about`, visual: framePic(t, c, { focus: w.track_id }),
          text: w.too_small ? `${ht} px tall, under the ${Math.round(minH)} px minimum: too far to judge, so no Moondream call. The video shows a grey “Too far” box.`
            : "Tracked too briefly, or still entering the frame, so this worker waits for a later check. The video shows “Checking…”.",
          tone: "grey", facts: [["Drawn", w.drawn?.tag]] });
        continue;
      }
      const sig = signature(w);
      const prev = last[w.track_id];
      last[w.track_id] = { sig, check: c.index };
      if (hideRepeats && prev && prev.sig === sig) {
        add({ ...base, map: "result", collapsed: true, title: `#${w.track_id}: same as check ${prev.check + 1}`,
          visual: h("div", { class: "pair" }, framePic(t, c, { focus: w.track_id }), cropPic(t, w)),
          text: `Moondream answered every question exactly as at this worker's previous check, so the decisions and the result are the same. Turn off “Hide repeats” to walk through it anyway.`,
          tone: w.state === "violation" ? "bad" : w.state === "ok" ? "ok" : "grey",
          facts: Object.entries(w.trace.items).map(([i, r]) => [itemLabel(t, i), `${r.steps.find((s) => s.step === "query")?.answer ?? ""} → ${ITEM_RESULT[r.status]}`]).concat([["Drawn", w.drawn?.tag]]) });
        continue;
      }
      add({ ...base, map: "gate", title: `#${w.track_id} is big enough to check`, visual: framePic(t, c, { focus: w.track_id }),
        text: `${ht} px tall, over the ${Math.round(minH)} px minimum, and tracked long enough: this worker is asked about.`, tone: "ok" });
      add({ ...base, map: "crop", title: `Cut out worker #${w.track_id}`, visual: h("div", { class: "pair" }, framePic(t, c, { focus: w.track_id }), cropPic(t, w)),
        text: `${EXPLAIN.crop} Neighbours overlapping the crop are shown in grey; their PPE mustn't count for this worker.`,
        facts: [["Crop", `${w.trace.crop_size.join("×")} px`], ["Padding", fmt.pct(tun.crop_padding)], ["Overlap with neighbours", fmt.pct(overlapShare(w.box, w.trace.others))]] });
      for (const st of w.trace.together ?? []) {
        if (st.step === "call") add({ ...base, map: "ask", title: "One question for all items", visual: chat(t, st), text: EXPLAIN.combined, facts: [["Model", modelTime(st.response?.metrics)]] });
      }
      for (const [item, r] of Object.entries(w.trace.items)) itemSteps(t, w, item, r, { ...base, item, crumb: [...base.crumb, itemLabel(t, item)] }, add);
      add({ ...base, map: "vote", title: "Vote over recent checks", visual: voteTable(t, w), tone: !w.confirmed ? "grey" : w.shown.length ? "bad" : "ok",
        text: `${EXPLAIN.vote} ${!w.confirmed ? `Only ${plural(w.window.length, "check")} so far, so the video says “Checking…”.` : w.shown.length ? `Shown missing: ${w.shown.map((l) => l.replace(/^No /, "")).join(", ")}.` : "Nothing is shown missing."}`,
        facts: [["This check found", w.violations.length ? w.violations.join(", ") : w.unverified.length ? `${w.unverified.join(", ")} not visible` : "all items present"]] });
      add({ ...base, map: "draw", title: `Drawn: ${w.drawn?.tag ?? "–"}`, visual: framePic(t, c, { focus: w.track_id, annotated: true }),
        tone: { red: "bad", green: "ok", amber: "warn", grey: "grey" }[w.drawn?.color] ?? "grey",
        text: `${EXPLAIN.drawn} The box is ${w.drawn?.color ?? "grey"}.`, facts: [["Tag", w.drawn?.tag]] });
    }
  }

  // ---- violations
  const s = t.summary;
  if (s) {
    add({ part: "violations", map: "violations", title: `${plural(s.events.length, "violation")} found`,
      visual: s.events.length ? h("table", { class: "mini-table" }, h("tr", {}, ["Worker", "Missing", "Video time", "Checks"].map((x) => h("th", {}, x))),
        s.events.map((e) => h("tr", {}, h("td", {}, `#${e.track_id}`), h("td", {}, e.violations.map((v) => h("span", { class: "tag tag-bad" }, v))),
          h("td", { class: "num" }, `${fmt.tp(e.time_sec)}–${fmt.tp(e.end_sec ?? e.time_sec)}`), h("td", { class: "num" }, e.checks)))) : big("None", "ok"),
      text: EXPLAIN.incident });
    const judged = s.ok_checks + s.violation_checks;
    add({ part: "violations", map: "violations", title: "The figures",
      visual: h("div", { class: "formula-grid" },
        h("div", {}, h("span", { class: "eyebrow" }, "Compliance"), h("b", {}, s.compliance_rate == null ? "–" : fmt.pct(s.compliance_rate)), h("code", {}, `${s.ok_checks} OK ÷ ${judged} judged`)),
        h("div", {}, h("span", { class: "eyebrow" }, "Time with a violation"), h("b", {}, s.frames_analysed ? fmt.pct(s.frames_with_violation / s.frames_analysed) : "–"), h("code", {}, `${s.frames_with_violation} ÷ ${s.frames_analysed} regular checks`)),
        h("div", {}, h("span", { class: "eyebrow" }, "By type"), h("b", {}, s.by_type.map((x) => `${x.label.replace(/^No /, "")} ${x.count}`).join(" · ") || "–"), h("code", {}, "violations including each item")),
        h("div", {}, h("span", { class: "eyebrow" }, "People"), h("b", {}, `${s.people.length}`), h("code", {}, `${s.people.filter((p) => p.state === "flagged").length} with a violation`))),
      text: "The dashboard's figures, worked out from every worker check of the run." });
  }

  // ---- outputs
  if (t.status === "completed") {
    add({ part: "outputs", map: "outputs", title: "What comes out",
      visual: h("video", { src: `/api/traces/${encodeURIComponent(t.id)}/files/${t.outputs.video}`, controls: true, playsinline: true, preload: "metadata" }),
      text: "The annotated video (every frame with each worker's voted status), the report with every check, and this trace.",
      facts: [["Frames written", t.frames_written], ["Moondream calls", `${t.calls.query + t.calls.detect} (${fmt.ms(Math.round(t.calls.ms / Math.max(1, t.calls.query + t.calls.detect)))} each on average)`]] });
  }
  return steps;
}

// One item's steps: ask, read, each confirmation, result.
function itemSteps(t, w, item, r, base, add) {
  const tun = t.settings.tuning;
  const spec = tun.items[item];
  const label = itemLabel(t, item);
  const steps = r.steps;
  let readAs = null;
  for (let i = 0; i < steps.length; i++) {
    const st = steps[i];
    if (st.step === "call" && st.kind === "query" && readAs == null) {
      add({ ...base, map: "ask", title: `Ask about the ${label.toLowerCase()}`, visual: chat(t, st), text: EXPLAIN.query,
        facts: [["Round trip", fmt.ms(st.ms ?? 0)], ["Moondream's own time", modelTime(st.response?.metrics)]] });
    } else if (st.step === "call" && st.kind === "query") {
      const v = steps[i + 1]?.step === "verify" ? steps[++i] : null; // the re-check and its reading, as one step
      add({ ...base, map: "confirm", title: "Re-check the detected box", visual: chat(t, st), tone: v?.confirmed ? "ok" : "bad",
        text: `${EXPLAIN.verify} ${v ? (v.confirmed ? "Confirmed." : "Not confirmed, so the box doesn't count.") : ""}` });
    } else if (st.step === "query") {
      readAs = st.read_as;
      add({ ...base, map: "read", title: "Read the answer", visual: big(readAs.toUpperCase(), { yes: "ok", no: "bad", unclear: "warn" }[readAs]),
        text: `${EXPLAIN.read}${st.question == null ? " This answer came from the combined question." : ""}`, facts: [["Raw answer", `“${st.answer}”`]] });
      if (readAs === "unclear") {
        add({ ...base, map: "confirm", title: `Unclear counts as ${tun.unclear_answer}`, visual: big(tun.unclear_answer.toUpperCase(), "warn"), text: "Set by the “unclear answer” rule." });
      } else if (readAs === "yes") {
        const share = overlapShare(w.box, w.trace.others);
        if (!tun.confirm_yes_on_overlap) add({ ...base, map: "confirm", skipped: true, title: "Overlap check: off", visual: big("SKIPPED", "grey"), text: `${EXPLAIN.overlap} It's off for this run, so the “yes” stands.` });
        else if (share < tun.overlap_threshold) add({ ...base, map: "confirm", title: "Is the “yes” about this worker?", visual: big(`${fmt.pct(share)} overlap`, "ok"), text: `Under the ${fmt.pct(tun.overlap_threshold)} threshold, so the “yes” is taken to be about this worker.` });
      } else if (!tun.confirm_no_with_detect) {
        add({ ...base, map: "confirm", skipped: true, title: "Confirm the “no”: off", visual: big("SKIPPED", "grey"), text: `${EXPLAIN.confirm_no} It's off for this run, so the “no” stands.` });
      }
    } else if (st.step === "overlap") {
      add({ ...base, map: "confirm", title: "The “yes” might be about a neighbour", visual: big(`${fmt.pct(st.share)} overlap`, "warn"), text: `Over the ${fmt.pct(tun.overlap_threshold)} threshold, so detect must find the ${spec.detect_term} on this worker.` });
    } else if (st.step === "call" && st.kind === "detect") {
      const objs = st.response?.objects ?? [];
      const bodyPart = st.prompt === spec.body_part && st.prompt !== spec.detect_term;
      add({ ...base, map: "confirm", title: bodyPart ? `Is the ${st.prompt} visible?` : `Look for a “${st.prompt}”`,
        visual: h("div", { class: "pair" }, cropPic(t, w, objs.map((o, k) => ({ rect: [o.x_min, o.y_min, o.x_max, o.y_max], cls: "off", label: `${k + 1}` }))), chat(t, st)),
        text: bodyPart ? EXPLAIN.body : EXPLAIN.detect, facts: [["Boxes found", objs.length], ["Round trip", fmt.ms(st.ms ?? 0)]] });
    } else if (st.step === "detect_object") {
      const o = st.box;
      add({ ...base, map: "confirm", title: `Does this “${st.term}” box count?`,
        visual: cropPic(t, w, [regionBand(w, spec), { rect: [o.x_min, o.y_min, o.x_max, o.y_max], cls: st.on_worker ? "sel" : "off", label: st.term }]),
        tone: st.on_worker ? "ok" : "bad", text: `${EXPLAIN.box} Verdict: ${REASON[st.reason] ?? st.reason}.` });
    } else if (st.step === "detect") {
      add({ ...base, map: "confirm", title: st.on_worker ? `Found on this worker` : `Not found on this worker`, visual: big(st.on_worker ? "FOUND" : "NOT FOUND", st.on_worker ? "ok" : "bad"),
        text: `“${st.term}”: ${plural(st.found, "box")} detected, ${st.on_worker ? "one counts for this worker" : "none counts for this worker"}.` });
    }
  }
  add({ ...base, map: "result", title: `${label}: ${ITEM_RESULT[r.status] ?? r.status}`,
    visual: big(ITEM_RESULT[r.status]?.toUpperCase() ?? r.status, { present: "ok", missing: "bad", unverified: "warn", failed: "grey" }[r.status]),
    tone: { present: "ok", missing: "bad", unverified: "warn", failed: "grey" }[r.status], text: whyItem(r.status, steps, tun, spec) });
}

function voteTable(t, w) {
  const labels = Object.values(t.settings.items);
  const name = (l) => l.replace(/^No /, "");
  const cell = (c, l) => (c.violations.includes(l) ? h("td", { class: "y" }, "✗ missing")
    : c.unverified.map((u) => u.toLowerCase()).includes(name(l).toLowerCase()) ? h("td", { class: "u" }, "? unseen") : h("td", { class: "n" }, "✓"));
  return h("table", { class: "vote big" },
    h("tr", {}, h("th", {}, ""), w.window.map((c) => h("th", {}, `check ${c.check + 1}`)), h("th", {}, "shown")),
    labels.map((l) => h("tr", {}, h("th", {}, name(l)), w.window.map((c) => cell(c, l)),
      w.shown.includes(l) ? h("td", { class: "y" }, "✗ missing") : h("td", { class: "n" }, w.confirmed ? "✓ OK" : "… checking"))));
}

// ---------------------------------------------------------------- the player

export async function walkView(app, id) {
  let t = await api.get(id);
  const params = new URLSearchParams(location.hash.split("?")[1] ?? "");
  let hideRepeats = (() => { try { return localStorage.getItem(STORE) !== "0"; } catch { return true; } })();
  let steps = buildSteps(t, { hideRepeats });
  let index = 0;
  if (params.has("s")) index = Math.min(steps.length - 1, Number(params.get("s")) || 0);
  if (params.has("check")) index = Math.max(0, steps.findIndex((s) => s.check === Number(params.get("check"))));
  let timer = null, autoplay = false;

  // ---- static frame of the page
  const mapEl = h("div", { class: "sysmap", role: "navigation", "aria-label": "Where this step is in the system" });
  const outline = h("nav", { class: "outline", "aria-label": "Parts and checks" });
  const card = h("article", { class: "step-card card", "aria-live": "polite" });
  const counter = h("span", { class: "num" });
  const progress = h("div", { class: "seg-progress" });
  const back = h("button", { type: "button", class: "btn", onclick: () => go(index - 1) }, "◀ Back");
  const next = h("button", { type: "button", class: "btn btn-primary btn-next", onclick: () => go(index + 1) }, "Next ▶");
  const skip = (label, level, key) => h("button", { type: "button", class: "btn btn-sm", "data-level": level, title: `Shortcut: ${key}`, onclick: () => skipTo(level) }, label);
  const skips = [skip("Next item", "item", "I"), skip("Next worker", "worker", "W"), skip("Next check", "check", "C"), skip("Next part", "part", "P")];
  const playBtn = h("button", { type: "button", class: "btn btn-sm", onclick: () => setAutoplay(!autoplay) });
  const repeatsBox = h("input", { type: "checkbox", checked: hideRepeats, onchange: () => {
    hideRepeats = repeatsBox.checked;
    try { localStorage.setItem(STORE, hideRepeats ? "1" : "0"); } catch { /* not remembered */ }
    const here = steps[index];
    steps = buildSteps(t, { hideRepeats });
    // Stay on the same check and worker.
    const same = steps.findIndex((s) => s.check === here.check && s.worker === here.worker && s.part === here.part);
    go(same >= 0 ? same : Math.min(index, steps.length - 1), true);
  } });

  app.replaceChildren(
    h("div", { class: "walk-head" },
      h("div", {}, h("span", { class: "eyebrow" }, "Step through"), h("h1", {}, t.name)),
      h("div", { class: "actions" }, h("a", { class: "btn", href: `#/t/${encodeURIComponent(id)}` }, "Flowchart view"), h("a", { class: "btn", href: "#/" }, "All traces"))),
    mapEl,
    h("div", { class: "walk-body" }, outline, card),
    h("div", { class: "walk-controls card" },
      h("div", { class: "walk-nav" }, back, next),
      h("div", { class: "walk-skip" }, h("span", { class: "eyebrow" }, "Skip to"), skips),
      h("div", { class: "walk-opts" },
        h("label", { class: "toggle" }, repeatsBox, "Hide repeats"), playBtn,
        h("span", { class: "hint walk-keys" }, "→ next · ← back · I W C P skip · A autoplay")),
      h("div", { class: "walk-progress" }, progress, counter)));

  // ---- navigation
  function keyFor(level, s) {
    return { part: s.part, check: `${s.part}:${s.check}`, worker: `${s.part}:${s.check}:${s.worker}`, item: `${s.part}:${s.check}:${s.worker}:${s.item}` }[level];
  }
  function nextIndex(level) {
    const here = steps[index];
    const k = keyFor(level, here);
    for (let i = index + 1; i < steps.length; i++) {
      const s = steps[i];
      // Skipping a worker or item only makes sense inside the checks; elsewhere move on a step.
      if (keyFor(level, s) !== k) return i;
    }
    return -1;
  }
  function skipTo(level) { const i = nextIndex(level); if (i >= 0) go(i); }
  function go(i, force = false) {
    if (i < 0 || i >= steps.length || (i === index && !force)) return;
    index = i;
    draw();
    if (autoplay && index === steps.length - 1) setAutoplay(false);
  }
  function setAutoplay(on) {
    autoplay = on;
    clearInterval(timer);
    if (on) timer = setInterval(() => (index < steps.length - 1 ? go(index + 1) : setAutoplay(false)), AUTOPLAY_MS);
    playBtn.textContent = on ? "❚❚ Pause" : "▶ Autoplay";
  }
  const onKey = (e) => {
    if (e.target.closest?.("input, select, textarea, video")) return;
    const k = e.key.toLowerCase();
    if (k === "arrowright" || k === " " || k === "enter") { e.preventDefault(); go(index + 1); }
    else if (k === "arrowleft") { e.preventDefault(); go(index - 1); }
    else if (k === "i") skipTo("item");
    else if (k === "w") skipTo("worker");
    else if (k === "c") skipTo("check");
    else if (k === "p") skipTo("part");
    else if (k === "a") setAutoplay(!autoplay);
    else if (k === "home") go(0);
    else if (k === "end") go(steps.length - 1);
    else return;
    if (autoplay && !["a"].includes(k)) setAutoplay(false);
  };
  document.addEventListener("keydown", onKey);

  // ---- drawing
  function draw() {
    const s = steps[index];
    history.replaceState(null, "", `#/t/${encodeURIComponent(id)}/walk?s=${index}`);
    drawMap(s);
    drawOutline(s);
    drawCard(s);
    drawProgress(s);
    back.disabled = index === 0;
    next.disabled = index === steps.length - 1;
    for (const b of skips) {
      const lvl = b.dataset.level;
      const inChecks = s.part === "checks";
      b.disabled = nextIndex(lvl) < 0 || ((lvl === "item" && !s.item) || (lvl === "worker" && s.worker == null) || (lvl === "check" && !inChecks));
    }
    counter.textContent = `Step ${index + 1} of ${steps.length}`;
  }

  function drawMap(s) {
    const seen = new Set(steps.slice(0, index).map((x) => x.map));
    const nodeFor = ([key, label]) => h("button", { type: "button", class: `map-node${key === s.map ? " is-here" : seen.has(key) ? " is-done" : ""}${s.skipped && key === s.map ? " is-skipped" : ""}`,
      title: `Go to the next “${label}” step`, onclick: () => {
        const i = steps.findIndex((x, j) => j > index && x.map === key);
        go(i >= 0 ? i : steps.findIndex((x) => x.map === key));
      } }, label);
    const before = MAP.filter(([k]) => !LOOP.includes(k) && MAP.findIndex(([x]) => x === k) < MAP.findIndex(([x]) => x === "frame"));
    const after = MAP.filter(([k]) => !LOOP.includes(k) && !before.some(([x]) => x === k));
    const loopNodes = MAP.filter(([k]) => LOOP.includes(k));
    const arrowEl = () => h("span", { class: "map-arrow", "aria-hidden": "true" });
    const join = (nodes) => nodes.flatMap((n, i) => (i ? [arrowEl(), nodeFor(n)] : [nodeFor(n)]));
    const itemNodes = loopNodes.filter(([k]) => ITEM_LOOP.includes(k));
    const outerBefore = loopNodes.filter(([k]) => LOOP.indexOf(k) < LOOP.indexOf("ask"));
    const outerAfter = loopNodes.filter(([k]) => LOOP.indexOf(k) > LOOP.indexOf("result"));
    const ctx = s.part === "checks" ? [s.check != null ? `check ${s.check + 1}/${t.checks.length}` : null, s.worker != null ? `worker #${s.worker}` : null, s.item ? itemLabel(t, s.item) : null].filter(Boolean).join(" · ") : "";
    mapEl.replaceChildren(
      ...join(before), arrowEl(),
      h("div", { class: `map-loop${s.part === "checks" ? " is-active" : ""}` },
        h("span", { class: "loop-label" }, "For every check → every worker", ctx ? h("b", {}, ` · now: ${ctx}`) : null),
        h("div", { class: "loop-row" }, ...join(outerBefore), arrowEl(),
          h("div", { class: "map-loop inner" }, h("span", { class: "loop-label" }, "every item"), h("div", { class: "loop-row" }, ...join(itemNodes))),
          arrowEl(), ...join(outerAfter))),
      arrowEl(), ...join(after));
  }

  function drawOutline(s) {
    const entries = [];
    for (const [key, label] of PARTS) {
      const firstI = steps.findIndex((x) => x.part === key);
      if (firstI < 0) continue;
      entries.push(h("button", { type: "button", class: `ol-part${s.part === key ? " is-here" : ""}`, onclick: () => go(firstI) }, label));
      if (key === "checks") {
        for (const c of t.checks) {
          const ci = steps.findIndex((x) => x.check === c.index);
          if (ci < 0) continue;
          const bad = c.workers.filter((w) => w.state === "violation").length;
          entries.push(h("button", { type: "button", class: `ol-check${s.check === c.index ? " is-here" : ""}`, onclick: () => go(ci) },
            h("span", { class: "num" }, `${c.index + 1}. ${fmt.tp(c.time_sec)}`), c.arrival ? h("span", { class: "hint" }, "arrival") : null,
            bad ? h("span", { class: "tag tag-bad" }, `${bad} ✗`) : h("span", { class: "tag tag-ok" }, "✓")));
          if (s.check === c.index) {
            for (const w of c.workers) {
              const wi = steps.findIndex((x) => x.check === c.index && x.worker === w.track_id);
              if (wi >= 0) entries.push(h("button", { type: "button", class: `ol-worker${s.worker === w.track_id ? " is-here" : ""}`, onclick: () => go(wi) },
                `#${w.track_id}`, h("span", { class: `tag tag-${{ bad: "bad", ok: "ok", warn: "warn" }[STATE[w.state]?.[1]] ?? "muted"}` }, STATE[w.state]?.[0] ?? w.state)));
            }
          }
        }
      }
    }
    outline.replaceChildren(...entries);
    outline.querySelector(".ol-worker.is-here, .ol-check.is-here")?.scrollIntoView({ block: "nearest" });
  }

  function drawCard(s) {
    card.className = `step-card card${s.tone ? ` tone-${s.tone}` : ""}${s.collapsed ? " is-collapsed" : ""}${s.skipped ? " is-skipped" : ""}`;
    card.replaceChildren(
      h("div", { class: "step-visual" }, s.visual?.cloneNode ? s.visual : null),
      h("div", { class: "step-text" },
        s.crumb ? h("div", { class: "crumb" }, s.crumb.map((c, i) => [i ? h("span", { class: "sep" }, "›") : null, h("span", {}, c)])) : h("div", { class: "crumb" }, PARTS.find(([k]) => k === s.part)?.[1]),
        h("h2", {}, s.title),
        s.skipped ? h("span", { class: "tag tag-muted" }, "Skipped: setting off") : null,
        s.collapsed ? h("span", { class: "tag tag-muted" }, "Repeat: folded into one step") : null,
        h("p", {}, s.text),
        s.facts?.length ? facts(s.facts) : null));
  }

  function drawProgress(s) {
    progress.replaceChildren(...PARTS.map(([key, label]) => {
      const idx = steps.map((x, i) => (x.part === key ? i : -1)).filter((i) => i >= 0);
      if (!idx.length) return null;
      const done = idx.filter((i) => i <= index).length;
      return h("button", { type: "button", class: `seg${s.part === key ? " is-here" : ""}`, style: { flexGrow: String(idx.length) },
        title: `${label}: ${idx.length} steps`, onclick: () => go(idx[0]) },
        h("i", { style: { width: `${(done / idx.length) * 100}%` } }), h("span", {}, label));
    }).filter(Boolean));
  }

  setAutoplay(false);
  go(index, true);
  return () => { setAutoplay(false); document.removeEventListener("keydown", onKey); };
}
