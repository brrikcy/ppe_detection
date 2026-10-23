// Developer options: tune Moondream prompts, decision rules, requests and tracking,
// and try a prompt on one worker image before saving.

import { api } from "../api.js";
import { fmt, h } from "../ui.js";

const ITEM_LABELS = { helmet: "Helmet", vest: "Vest", gloves: "Gloves", boots: "Boots", goggles: "Goggles", mask: "Mask" };

// Settings that only matter when another one is on: return why a field is unused, or null.
const NO_CHECK = "Double-check “no” answers with detect";
const YES_CHECK = "Confirm “yes” when workers overlap";
const offUnless = (key, label) => (d) => (d[key] ? null : `Not used while “${label}” is off.`);
// Detected boxes are judged by both double-checks.
const usesDetect = (d) => (d.confirm_no_with_detect || d.confirm_yes_on_overlap ? null
  : `Not used while “${NO_CHECK}” and “${YES_CHECK}” are both off.`);
const usesBodyPart = (d) => (!d.confirm_no_with_detect ? `Not used while “${NO_CHECK}” is off.`
  : !d.check_body_part ? "Not used while “Unverified when the body part can't be seen” is off." : null);
const usesVerify = (d) => usesDetect(d) ?? (d.verify_detected_box ? null : "Not used while “Re-ask about the detected box” is off.");

const f = (key, type, label, hint, extra = {}) => ({ key, type, label, hint, ...extra });

// Each card has groups of rows; `child` rows are indented under the setting they depend on.
const CARDS = [
  {
    id: "rules", nav: "Decision rules", title: "Decision rules",
    intro: "How Moondream's answers become PPE OK, a violation, or unverified.",
    groups: [
      { title: "Unclear answers", fields: [
        f("unclear_answer", "select", "An unclear answer counts as",
          "When the reply is neither yes nor no (for example “Not sure”). Present avoids false alarms; missing avoids misses.",
          { options: [["present", "Present (no violation)"], ["unverified", "Unverified"], ["missing", "Missing (violation)"]] }),
      ] },
      { title: "When Moondream says “no”", fields: [
        f("confirm_no_with_detect", "toggle", NO_CHECK,
          "A “no” only counts if detect can't find the item on this worker. Off: fewer calls per “no”, more false alarms."),
        f("check_body_part", "toggle", "Unverified when the body part can't be seen",
          "If the head (for a helmet) isn't visible, the item is unverified instead of missing.",
          { child: true, dependsOn: offUnless("confirm_no_with_detect", NO_CHECK) }),
      ] },
      { title: "When Moondream says “yes”", fields: [
        f("confirm_yes_on_overlap", "toggle", YES_CHECK,
          "A “yes” for a worker partly covered by another may be about the other person. It then has to be found on this worker with detect, or the item is unverified."),
        f("overlap_threshold", "number", "Overlap that needs confirming", "Share of the worker's box covered by other workers.",
          { child: true, min: 0.01, max: 1, step: 0.05, unit: "× box", dependsOn: offUnless("confirm_yes_on_overlap", YES_CHECK) }),
      ] },
      { title: "Detected boxes", intro: "Used by both double-checks above to decide whether a box detect found counts for this worker.", fields: [
        f("min_region_overlap", "number", "Inside the item's region",
          "Share of a detected box that must lie in the item's region of the worker (set per item). 0 = only its centre counts.",
          { min: 0, max: 1, step: 0.05, unit: "× box", dependsOn: usesDetect }),
        f("verify_detected_box", "toggle", "Re-ask about the detected box",
          "Moondream is asked about the box alone before it counts (a cap is not a hard hat). One more call when it applies.",
          { dependsOn: usesDetect }),
      ] },
      { title: "Which workers are checked", fields: [
        f("crop_padding", "number", "Crop padding",
          "Extra space around each worker in the image sent to Moondream. More context, but neighbours creep in.",
          { min: 0, max: 0.5, step: 0.01, unit: "× box" }),
        f("min_check_height_frac", "number", "Minimum worker height",
          "Shorter workers are shown as “Too far” and not checked (no API calls).",
          { min: 0, max: 0.5, step: 0.01, unit: "× frame" }),
        f("min_check_height_px", "number", "…and at least",
          "The larger of the two limits applies, so low-resolution video doesn't get checks on workers a few pixels tall.",
          { min: 0, max: 2000, step: 10, unit: "px" }),
      ] },
    ],
  },
  {
    id: "voting", nav: "Status and voting", title: "Status and voting",
    intro: "How checks become the status in the video, the violation list and the People section. All three use the same rule.",
    groups: [
      { title: "Status", fields: [
        f("vote_window", "number", "Vote over the last",
          "An item is shown missing when it was missing in most of this many recent checks. 1 = no smoothing.",
          { min: 1, max: 9, step: 1, unit: "checks" }),
        f("confirm_checks", "number", "Missing in at least",
          "Before a box turns red. A new worker shows “Checking…” until they have this many checks. Can't exceed the vote window.",
          { min: 1, max: 9, step: 1, unit: "checks",
            dependsOn: (d) => (d.confirm_checks > d.vote_window ? "Larger than the vote window: this can't be saved." : null), warnOnly: true }),
      ] },
      { title: "When checks happen", fields: [
        f("min_check_age_sec", "number", "Check a new person after",
          "Tracks younger than this aren't checked, so short-lived false detections don't cost calls or get flagged.",
          { min: 0, max: 10, step: 0.1, unit: "s" }),
        f("skip_edge_arrivals", "toggle", "Wait until a newcomer is fully in frame",
          "A worker whose box touches the frame edge gets their first check once they're fully visible (or at the next regular check)."),
        f("max_failed_checks", "number", "Stop the run after",
          "When Moondream doesn't answer, the run stops instead of retrying for a long time.",
          { min: 1, max: 100, step: 1, unit: "failed checks in a row" }),
      ] },
    ],
  },
  {
    id: "requests", nav: "Requests", title: "Moondream requests",
    intro: "Which model answers, and speed versus rate limits.", endpointNote: true,
    groups: [
      { fields: [
        f("moondream_model", "text", "Moondream model",
          "Sent with every request so results don't change when the server's default does. Moondream Cloud accepts moondream3.1-9B-A2B and moondream3-preview.",
          { list: "md-models" }),
        f("parallel_requests", "number", "Parallel requests", "Higher is faster but hits Moondream's rate limit (HTTP 429) sooner.",
          { min: 1, max: 16, step: 1 }),
        f("retry_attempts", "number", "Attempts per call", "Failed calls are retried after 1, 2, 4… seconds. Lower gives up faster during an outage.",
          { min: 1, max: 8, step: 1 }),
      ] },
    ],
  },
  {
    id: "tracking", nav: "Tracking", title: "Person tracking",
    intro: "YOLO finds people on every frame; BoT-SORT keeps their IDs.",
    groups: [
      { title: "Finding people", fields: [
        f("person_model", "model", "Detection model", "Bigger models find more partly hidden workers but are slower. Put .pt files in models/."),
        f("detector_imgsz", "select", "Detector input size", "Frames are scaled to this. Larger finds smaller people but is slower.",
          { numeric: true, options: [320, 480, 640, 800, 960, 1280, 1600, 1920].map((v) => [v, `${v} px`]) }),
        f("detector_conf", "number", "Detection confidence", "Weakest detection the tracker considers. Low values help keep partly hidden workers.",
          { min: 0.01, max: 0.9, step: 0.01 }),
      ] },
      { title: "Keeping IDs", fields: [
        f("new_track_thresh", "number", "Confidence to start a new person",
          "Lower tracks more hard-to-see workers but creates more short, spurious IDs.", { min: 0.05, max: 0.95, step: 0.05 }),
        f("min_track_age_sec", "number", "Ignore tracks younger than",
          "Filters flicker: a track must last this long before its box is drawn. Checks wait longer (see “Check a new person after”).",
          { min: 0, max: 3, step: 0.1, unit: "s" }),
        f("hidden_sec", "number", "Keep a hidden worker's ID for",
          "Someone out of view for less than this keeps their ID when they reappear near where they were lost.",
          { min: 0, max: 60, step: 1, unit: "s" }),
        f("coast_sec", "number", "Keep drawing a missed box for",
          "When the detector misses someone for a few frames, their last box stays on screen instead of blinking off. Not checked meanwhile.",
          { min: 0, max: 3, step: 0.1, unit: "s" }),
      ] },
      { title: "Camera and appearance", fields: [
        f("fixed_camera", "toggle", "Fixed camera", "Skips camera-motion compensation: faster, and steadier boxes when the camera doesn't move."),
        f("with_reid", "toggle", "Compare appearance (ReID)",
          "Also matches people by appearance (models/yolo11n-cls.pt, downloaded on first use). About 40% slower per frame; in our tests it didn't prevent ID swaps when workers cross."),
      ] },
    ],
  },
];

// Per-item settings, shown one item at a time.
const ITEM_ROWS = [
  { key: "question", type: "textarea", label: "Wording", hint: "Replaces {item} in the question template." },
  { key: "detect_term", type: "text", label: "Detect term",
    hint: "What detect looks for to overrule a “no”, or to confirm a “yes” when workers overlap.", dependsOn: usesDetect },
  { key: "body_part", type: "text", label: "Body part", hint: "If detect can't find it on the worker, the item is unverified. Blank = no check.",
    dependsOn: usesBodyPart, blankIsNull: true },
  { pair: true, label: "Region", hint: "Where on the worker the item may be, as a share of the box height from the top (negative = above the box).",
    dependsOn: usesDetect, parts: [["region_top", "top", -0.5, 1.5, 0.05], ["region_bottom", "bottom", -0.5, 1.5, 0.05]] },
  { pair: true, label: "Detected width", hint: "Plausible width of a detected box, as a share of the worker's box width.",
    dependsOn: usesDetect, parts: [["min_width", "min", 0, 2, 0.01], ["max_width", "max", 0, 2, 0.05]] },
  { pair: true, label: "Detected height", hint: "Plausible height of a detected box, as a share of the worker's box height.",
    dependsOn: usesDetect, parts: [["min_height", "min", 0, 2, 0.01], ["max_height", "max", 0, 2, 0.05]] },
];

const clone = (o) => JSON.parse(JSON.stringify(o));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export async function renderDev(root) {
  let state = await api.dev.get();
  let saved = clone(state.active);
  let draft = clone(state.active);
  const defaults = state.defaults;
  const controls = []; // {refresh()} for every field, to redraw markers after load/reset

  // ---- header and save bar --------------------------------------------------
  const dirtyPill = h("span", { class: "pill queued", hidden: true }, "Unsaved changes");
  const changesNote = h("p", { class: "hint" });
  const errorBox = h("div", { class: "alert", role: "alert", hidden: true });
  const saveBtn = h("button", { class: "btn btn-primary", type: "button", onclick: save }, "Save");
  const discardBtn = h("button", { class: "btn", type: "button", onclick: () => load(saved) }, "Discard changes");
  const resetBtn = h("button", { class: "btn btn-ghost", type: "button", onclick: resetAll }, "Reset all to defaults");

  function refreshBar() {
    const dirty = !same(draft, saved);
    dirtyPill.hidden = !dirty;
    saveBtn.disabled = discardBtn.disabled = !dirty;
    const n = state.changes.length;
    changesNote.textContent = n
      ? `Saved settings: ${n} change${n === 1 ? "" : "s"} from the defaults. New runs use them; each run keeps a copy of the settings it used.`
      : "Saved settings are the defaults. New runs use them; each run keeps a copy of the settings it used.";
    controls.forEach((c) => c.refresh());
  }

  async function save() {
    errorBox.hidden = true;
    saveBtn.disabled = true;
    try {
      state = await api.dev.save(draft);
      saved = clone(state.active);
      draft = clone(state.active);
    } catch (e) {
      errorBox.replaceChildren(h("h3", {}, "Not saved"), h("p", { style: "white-space:pre-line" }, e.message));
      errorBox.hidden = false;
      errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    refreshBar();
  }

  async function resetAll() {
    if (resetBtn.dataset.confirm !== "1") {
      resetBtn.dataset.confirm = "1";
      resetBtn.textContent = "Click again to reset everything";
      setTimeout(() => { resetBtn.dataset.confirm = ""; resetBtn.textContent = "Reset all to defaults"; }, 3000);
      return;
    }
    state = await api.dev.reset();
    load(state.active, true);
    resetBtn.dataset.confirm = "";
    resetBtn.textContent = "Reset all to defaults";
  }

  function load(values, alsoSaved = false) {
    draft = clone(values);
    if (alsoSaved) saved = clone(values);
    refreshBar();
  }

  // ---- one settings row: label and description on the left, control on the right ----
  function makeInput({ id, type, options, min, max, step, numeric, list }, set) {
    let input;
    if (type === "toggle") {
      input = h("input", { id, type: "checkbox", class: "switch", role: "switch", onchange: () => { set(input.checked); refreshBar(); } });
    } else if (type === "select" || type === "model") {
      input = h("select", { id, class: "input", onchange: () => { set(numeric ? Number(input.value) : input.value); refreshBar(); } });
      const opts = type === "model"
        ? [["", `Server default (${state.default_model})`], ...state.models.map((m) => [m, m])]
        : options;
      input.replaceChildren(...opts.map(([v, t]) => h("option", { value: v }, t)));
    } else if (type === "textarea") {
      input = h("textarea", { id, class: "input textarea", rows: 3, spellcheck: "false", oninput: () => { set(input.value); refreshBar(); } });
    } else if (type === "text") {
      input = h("input", { id, class: "input", type: "text", list, autocomplete: "off", spellcheck: "false",
        oninput: () => { set(input.value); refreshBar(); } });
    } else {
      input = h("input", { id, class: "input num-input", type: "number", min, max, step,
        oninput: () => { if (input.value !== "" && !Number.isNaN(input.valueAsNumber)) { set(input.valueAsNumber); refreshBar(); } } });
    }
    return input;
  }

  function showValue(input, type, v) {
    if (type === "toggle") input.checked = !!v;
    else if (document.activeElement !== input) input.value = v ?? "";
  }

  // parts: [{input, type, get, def, reset}] — one row may hold several inputs (e.g. min and max).
  function row({ id, label, hint, type, wide, child, dependsOn, warnOnly }, control, parts) {
    const dot = h("span", { class: "changed-dot", title: "Changed from the default" });
    const reset = h("button", { type: "button", class: "link-btn", onclick: () => { parts.forEach((p) => p.reset()); refreshBar(); } }, "Reset");
    const note = h("p", { class: warnOnly ? "row-note is-error" : "row-note", hidden: true });
    const el = h("div", { class: `set-row${type === "toggle" ? " is-toggle" : ""}${wide ? " is-wide" : ""}${child ? " is-child" : ""}` },
      h("div", { class: "set-text" },
        h("div", { class: "set-label" }, h("label", { for: id }, label), dot, reset),
        hint ? h("p", { class: "hint" }, hint) : null,
        note),
      h("div", { class: "set-control" }, control));
    controls.push({
      refresh() {
        let changed = false;
        for (const p of parts) {
          showValue(p.input, p.type, p.get());
          changed ||= !same(p.get() ?? null, p.def() ?? null);
        }
        dot.hidden = reset.hidden = !changed;
        const why = dependsOn ? dependsOn(draft) : null;
        el.classList.toggle("is-off", !!why && !warnOnly);
        note.hidden = !why;
        note.textContent = why ?? "";
      },
    });
    return el;
  }

  // A setting stored at draft[key] (or obj()[key] for per-item settings).
  function setting(spec, obj = () => draft, defObj = () => defaults, idPrefix = "f-") {
    const id = `${idPrefix}${spec.key}`;
    const set = (v) => { obj()[spec.key] = spec.blankIsNull ? (v.trim() || null) : v; };
    const input = makeInput({ ...spec, id }, set);
    const unit = spec.unit ? h("span", { class: "unit" }, spec.unit) : null;
    const part = { input, type: spec.type, get: () => obj()[spec.key], def: () => defObj()[spec.key],
      reset: () => { obj()[spec.key] = clone(defObj()[spec.key]); } };
    return row({ ...spec, id, wide: spec.type === "textarea" }, [input, unit], [part]);
  }

  // Two numbers in one row, e.g. region top and bottom.
  function pairSetting(spec, obj, defObj, idPrefix) {
    const parts = spec.parts.map(([key, label, min, max, step]) => {
      const id = `${idPrefix}${key}`;
      const input = makeInput({ id, type: "number", min, max, step }, (v) => { obj()[key] = v; });
      return { key, label, id, input, type: "number", get: () => obj()[key], def: () => defObj()[key],
        reset: () => { obj()[key] = defObj()[key]; } };
    });
    const control = h("div", { class: "pair" }, parts.flatMap((p, i) => [
      i ? h("span", { class: "pair-sep" }, "–") : null,
      h("label", { class: "pair-part" }, h("span", {}, p.label), p.input)]));
    return row({ ...spec, id: parts[0].id }, control, parts);
  }

  function group({ title, intro, fields }, render) {
    return h("div", { class: "set-group" },
      title ? h("h3", { class: "set-group-title" }, title) : null,
      intro ? h("p", { class: "hint set-group-intro" }, intro) : null,
      h("div", { class: "set-rows" }, fields.map(render)));
  }

  function card(id, title, intro, ...body) {
    return h("section", { class: "card card-pad dev-card", id: `dev-${id}` },
      h("div", { class: "card-head" }, h("div", {}, h("h2", {}, title), intro ? h("p", {}, intro) : null)),
      ...body);
  }

  // ---- prompts ----------------------------------------------------------------
  const preview = h("p", { class: "prompt-preview" });
  const previewItem = h("select", { class: "input", style: "max-width:140px", "aria-label": "Preview for item",
    onchange: () => refreshPreview() },
    Object.entries(ITEM_LABELS).map(([k, v]) => h("option", { value: k }, v)));
  function refreshPreview() {
    const it = draft.items[previewItem.value];
    preview.textContent = draft.question_template.includes("{item}")
      ? draft.question_template.replaceAll("{item}", it.question)
      : "The template must contain {item}.";
  }
  controls.push({ refresh: refreshPreview });

  const promptCard = card("prompts", "Moondream prompts", "What Moondream is asked. Unsaved changes are used by the playground above.",
    group({ title: "Question for each item", fields: [
      { key: "question_template", type: "textarea", label: "Question template",
        hint: "{item} is replaced by the item's wording (see PPE items). Keep “Answer only yes or no”: other replies count as unclear." },
    ] }, (s) => setting(s)),
    h("div", { class: "prompt-preview-row" }, h("span", { class: "eyebrow" }, "Sent for"), previewItem, preview),
    group({ title: "One question for all items", fields: [
      { key: "one_query_per_worker", type: "toggle", label: "Ask about all items at once",
        hint: "One request per worker instead of one per item: fewer calls, but answers may be less reliable. Items missing from the reply are asked about on their own." },
      { key: "multi_template", type: "textarea", label: "Question for all items", child: true,
        hint: "{items} is replaced by one “name: wording” line per item. The reply must name each item followed by yes or no.",
        dependsOn: offUnless("one_query_per_worker", "Ask about all items at once") },
    ] }, (s) => setting(s)),
    group({ title: "Re-check of a detected box", fields: [
      { key: "verify_template", type: "textarea", label: "Re-check question",
        hint: "{term} is replaced by the item's detect term. Asked about a detected box alone before it counts.",
        dependsOn: usesVerify },
    ] }, (s) => setting(s)));

  // ---- PPE items: one tab per item ------------------------------------------------
  let currentItem = "helmet";
  const tabs = h("div", { class: "item-tabs", role: "tablist", "aria-label": "PPE item" });
  const panels = {};
  const tabButtons = {};
  for (const key of Object.keys(ITEM_LABELS)) {
    const obj = () => draft.items[key];
    const defObj = () => defaults.items[key];
    panels[key] = h("div", { class: "set-rows", role: "tabpanel", id: `item-panel-${key}`, hidden: key !== currentItem },
      ITEM_ROWS.map((r) => (r.pair ? pairSetting(r, obj, defObj, `i-${key}-`) : setting(r, obj, defObj, `i-${key}-`))));
    const dot = h("span", { class: "changed-dot", title: "Changed from the default" });
    tabButtons[key] = h("button", { type: "button", role: "tab", class: "item-tab", "aria-controls": `item-panel-${key}`,
      onclick: () => selectItem(key) }, ITEM_LABELS[key], dot);
    controls.push({ refresh() { dot.hidden = same(draft.items[key], defaults.items[key]); } });
  }
  function selectItem(key) {
    currentItem = key;
    for (const k of Object.keys(panels)) {
      panels[k].hidden = k !== key;
      tabButtons[k].setAttribute("aria-selected", String(k === key));
    }
  }
  tabs.append(...Object.values(tabButtons));
  selectItem(currentItem);
  const itemsCard = card("items", "PPE items", "How each item is asked about and checked. A dot marks items changed from the defaults.",
    tabs, h("div", { class: "set-group" }, ...Object.values(panels)));

  // ---- other settings ----------------------------------------------------------------
  const settingCards = CARDS.map((c) => card(c.id, c.title, c.intro,
    c.endpointNote ? h("p", { class: "endpoint-note" },
      "Endpoint: ", h("code", {}, state.moondream_endpoint),
      h("span", { class: "hint" }, " · set MOONDREAM_ENDPOINT in the server's .env to use another provider or a self-hosted server")) : null,
    c.endpointNote ? h("datalist", { id: "md-models" },
      ["moondream3.1-9B-A2B", "moondream3-preview"].map((m) => h("option", { value: m }))) : null,
    ...c.groups.map((g) => group(g, (s) => setting(s)))));
  const cards = { rules: settingCards[0], voting: settingCards[1], requests: settingCards[2], tracking: settingCards[3] };

  // ---- playground ----------------------------------------------------------------
  const playground = await buildPlayground(() => draft);
  playground.id = "dev-playground";

  const nav = h("nav", { class: "dev-nav", "aria-label": "Sections" },
    [["playground", "Playground"], ["prompts", "Prompts"], ["items", "PPE items"], ["rules", "Decision rules"],
      ["voting", "Status and voting"], ["requests", "Requests"], ["tracking", "Tracking"]]
      .map(([id, label]) => h("button", { type: "button", onclick: () => document.getElementById(`dev-${id}`)
        ?.scrollIntoView({ behavior: "smooth", block: "start" }) }, label)));

  root.replaceChildren(h("div", { class: "page dev-page" },
    h("div", { class: "run-head" },
      h("div", { class: "title" },
        h("span", { class: "eyebrow" }, "Developer options"),
        h("h1", {}, "Tune detection"),
        changesNote),
      h("div", { class: "actions" }, resetBtn)),
    h("div", { class: "dev-bar" }, nav, h("div", { class: "actions" }, dirtyPill, discardBtn, saveBtn)),
    errorBox,
    playground,
    promptCard,
    itemsCard,
    cards.rules, cards.voting, cards.requests, cards.tracking));
  refreshBar();
  return () => {};
}

// ---- prompt playground ------------------------------------------------------------
async function buildPlayground(getDraft) {
  const runs = (await api.listRuns()).filter((r) => r.status === "completed");
  let pick = null; // {run_id, folder, name, url} or {file, url}

  const runSelect = h("select", { class: "input", onchange: () => loadThumbs() },
    runs.length ? runs.map((r) => h("option", { value: r.id }, `${r.name} · ${fmt.dateTime(r.created_at)}`))
      : [h("option", { value: "" }, "No completed runs yet")]);
  const thumbs = h("div", { class: "pg-thumbs" });
  const upload = h("input", { type: "file", accept: "image/*", id: "pg-upload", class: "visually-hidden", onchange: () => {
    const f = upload.files[0];
    if (!f) return;
    select({ file: f, url: URL.createObjectURL(f) }, null);
  } });
  const itemSelect = h("select", { class: "input", id: "pg-item" },
    Object.entries(ITEM_LABELS).map(([k, v]) => h("option", { value: k }, v)));
  const keyInput = h("input", { class: "input", id: "pg-key", type: "password", autocomplete: "off",
    placeholder: "Moondream key (empty = server key)" });
  const runBtn = h("button", { class: "btn btn-primary", type: "button", disabled: true, onclick: runTest }, "Run test");
  const result = h("div", { class: "pg-result" }, h("p", { class: "hint" }, "Pick a worker image, choose an item and run the test. Unsaved changes are used."));

  async function loadThumbs() {
    thumbs.replaceChildren(h("p", { class: "hint" }, "Loading…"));
    if (!runSelect.value) return thumbs.replaceChildren(h("p", { class: "hint" }, "Run an inspection first, or upload an image."));
    const run = await api.getRun(runSelect.value);
    const s = run.summary;
    const imgs = [
      ...(s?.people ?? []).filter((p) => p.photo).map((p) => ({ folder: "people", name: p.photo, label: `#${p.id}` })),
      ...(s?.events ?? []).filter((e) => e.snapshot).slice(0, 24).map((e) => ({
        folder: "snapshots", name: e.snapshot, label: `${e.track_id != null ? `#${e.track_id} · ` : ""}${fmt.timePrecise(e.time_sec)}`,
      })),
    ];
    if (!imgs.length) return thumbs.replaceChildren(h("p", { class: "hint" }, "This run has no worker images."));
    thumbs.replaceChildren(...imgs.map((im) => {
      const url = im.folder === "people" ? api.urls.person(run.meta.id, im.name) : api.urls.snapshot(run.meta.id, im.name);
      const b = h("button", { type: "button", class: "pg-thumb", title: im.label,
        onclick: () => select({ run_id: run.meta.id, folder: im.folder, name: im.name, url }, b) },
        h("img", { src: url, alt: im.label, loading: "lazy" }), h("span", {}, im.label));
      return b;
    }));
  }

  function select(p, btn) {
    pick = p;
    thumbs.querySelectorAll(".pg-thumb").forEach((t) => t.setAttribute("aria-pressed", String(t === btn)));
    runBtn.disabled = false;
    result.replaceChildren(h("div", { class: "pg-stage" }, h("img", { src: p.url, alt: "Selected worker" })));
  }

  async function runTest() {
    const form = new FormData();
    form.append("item", itemSelect.value);
    form.append("tuning", JSON.stringify(getDraft()));
    if (keyInput.value.trim()) form.append("api_key", keyInput.value.trim());
    if (pick.file) form.append("image", pick.file);
    else { form.append("run_id", pick.run_id); form.append("folder", pick.folder); form.append("name", pick.name); }
    runBtn.disabled = true;
    runBtn.textContent = "Asking Moondream…";
    try {
      showResult(await api.dev.tryPrompt(form));
    } catch (e) {
      result.replaceChildren(h("div", { class: "alert" }, h("p", { style: "white-space:pre-line" }, e.message)));
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = "Run test";
    }
  }

  function showResult(r) {
    const tone = { present: "completed", missing: "failed", unverified: "queued" }[r.status];
    const verdict = { present: "Present", missing: "Missing (violation)", unverified: "Unverified" }[r.status];
    // The image Moondream was shown: in "run" mode the worker's crop from the original frame.
    const stage = h("div", { class: "pg-stage" }, h("img", { src: r.image ?? pick.url, alt: "Image sent to Moondream" }));
    const steps = [];
    const reasons = { ok: "counts", "other worker": "on another worker", "outside region": "outside the item's region",
      "wrong size": "wrong size for the item", "re-check said no": "re-check said it isn't one", "not needed": "not needed" };
    for (const s of r.steps) {
      if (s.step === "query") {
        if (s.question) steps.push(h("li", {}, h("b", {}, "Asked: "), h("q", {}, s.question)));
        steps.push(h("li", {}, h("b", {}, "Moondream answered: "), h("code", {}, JSON.stringify(s.answer)), ` → read as ${s.read_as}`));
      } else if (s.step === "overlap") {
        steps.push(h("li", {}, h("b", {}, "Overlap: "),
          `${Math.round(s.share * 100)}% of this worker is covered by others, so the “yes” must be confirmed`));
      } else if (s.step === "verify") {
        steps.push(h("li", {}, h("b", {}, "Re-check: "), h("q", {}, s.question), " → ",
          h("code", {}, JSON.stringify(s.answer)), s.confirmed ? " (confirmed)" : " (not confirmed)"));
      } else if (s.step === "detect") {
        steps.push(h("li", {}, h("b", {}, `Detect “${s.term}”: `),
          `${s.found} found${s.found ? `, ${s.on_worker ? "one counts for this worker" : "none counts"}` : ""}`));
      } else if (s.step === "detect_object") {
        const b = s.box;
        const why = reasons[s.reason] ?? s.reason ?? (s.on_worker ? "counts" : "outside region");
        steps.push(h("li", { class: "hint" }, `“${s.term}” box: ${why}`));
        // Keep labels inside the image for boxes at its right or top edge.
        const edge = `${b.x_min > 0.5 ? " at-right" : ""}${b.y_min < 0.06 ? " at-top" : ""}`;
        stage.append(h("div", { class: `pg-box ${s.on_worker ? "on" : "off"}${edge}`, title: `${s.term}: ${why}`,
          style: `left:${b.x_min * 100}%;top:${b.y_min * 100}%;width:${(b.x_max - b.x_min) * 100}%;height:${(b.y_max - b.y_min) * 100}%` },
          h("span", {}, s.term)));
      }
    }
    result.replaceChildren(
      stage,
      h("div", { class: "pg-steps" },
        h("div", {}, h("span", { class: `pill ${tone}` }, verdict), " ",
          h("span", { class: "hint" }, `${ITEM_LABELS[r.item]} · ${r.image_size[0]}×${r.image_size[1]} px`)),
        r.note ? h("p", { class: "pg-note" }, r.note) : null,
        h("ol", {}, steps)));
  }

  const card = h("section", { class: "card card-pad" },
    h("div", { class: "card-head" }, h("div", {},
      h("h2", {}, "Prompt playground"),
      h("p", {}, "Check one PPE item on one worker with the settings on this page, saved or not. Violation photos are re-checked exactly as in their run (original frame, same crop and neighbours). Uses 1–4 Moondream calls."))),
    h("div", { class: "pg-grid" },
      h("div", { class: "pg-pick" },
        h("label", { class: "label", for: "pg-run" }, "Worker image from a run"),
        runSelect,
        thumbs,
        h("label", { class: "btn", for: "pg-upload", style: "justify-self:start" }, "Or upload an image"), upload,
        h("div", { class: "pg-controls" },
          h("label", { class: "label", for: "pg-item" }, "Item"), itemSelect,
          h("label", { class: "label", for: "pg-key" }, "API key"), keyInput),
        runBtn),
      result));
  runSelect.id = "pg-run";
  if (runs.length) loadThumbs(); else thumbs.replaceChildren(h("p", { class: "hint" }, "Run an inspection first, or upload an image."));
  return card;
}
