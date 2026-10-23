// Developer options: tune Moondream prompts, decision rules, requests and tracking,
// and try a prompt on one worker image before saving.

import { api } from "../api.js";
import { fmt, h } from "../ui.js";

const ITEM_LABELS = { helmet: "Helmet", vest: "Vest", gloves: "Gloves", boots: "Boots", goggles: "Goggles", mask: "Mask" };

const SECTIONS = [
  {
    title: "Decision rules",
    intro: "How Moondream's answers become PPE OK, a violation, or unverified.",
    fields: [
      { key: "unclear_answer", type: "select", label: "An unclear answer counts as",
        options: [["present", "Present (no violation)"], ["unverified", "Unverified"], ["missing", "Missing (violation)"]],
        hint: "When the reply is neither yes nor no. Present avoids false alarms; missing avoids misses." },
      { key: "confirm_no_with_detect", type: "toggle", label: "Double-check “no” answers with detect",
        hint: "A “no” only counts if detect can't find the item on this worker. Off: 1–2 fewer calls per “no”, more false alarms." },
      { key: "check_body_part", type: "toggle", label: "Unverified when the body part can't be seen",
        hint: "If the head (for a helmet) isn't visible, the item is unverified instead of missing." },
      { key: "crop_padding", type: "number", label: "Crop padding", min: 0, max: 0.5, step: 0.01, unit: "× box",
        hint: "Extra space around each worker in the image sent to Moondream. More context, but neighbours creep in." },
      { key: "min_check_height_frac", type: "number", label: "Minimum worker height", min: 0, max: 0.5, step: 0.01, unit: "× frame",
        hint: "Workers shorter than this are shown as “Too far” and not checked (no API calls)." },
      { key: "vote_window", type: "number", label: "Video vote window", min: 1, max: 9, step: 1, unit: "checks",
        hint: "A box turns red when an item was missing in most of this many recent checks. 1 = no smoothing." },
      { key: "person_flag_ratio", type: "number", label: "Flag a person when missing in more than", min: 0.05, max: 0.95, step: 0.05, unit: "of checks",
        hint: "Used for the People section's overall status. Lower = stricter." },
    ],
  },
  {
    title: "Moondream requests",
    intro: "Which model answers, and speed versus rate limits.",
    endpointNote: true,
    fields: [
      { key: "moondream_model", type: "text", label: "Moondream model", list: "md-models",
        hint: "Sent with every request so results don't change when the server's default does. Moondream Cloud accepts moondream3.1-9B-A2B and moondream3-preview." },
      { key: "parallel_requests", type: "number", label: "Parallel requests", min: 1, max: 16, step: 1,
        hint: "Higher is faster but hits Moondream's rate limit (HTTP 429) sooner." },
      { key: "retry_attempts", type: "number", label: "Attempts per call", min: 1, max: 8, step: 1,
        hint: "Failed calls are retried after 1, 2, 4… seconds. Lower gives up faster during an outage." },
    ],
  },
  {
    title: "Person tracking",
    intro: "YOLO finds people on every frame; BoT-SORT keeps their IDs.",
    fields: [
      { key: "person_model", type: "model", label: "Detection model",
        hint: "Bigger models find more partly hidden workers but are slower. Put .pt files in models/." },
      { key: "detector_imgsz", type: "select", label: "Detector input size", numeric: true,
        options: [320, 480, 640, 800, 960, 1280, 1600, 1920].map((v) => [v, `${v} px`]),
        hint: "Frames are scaled to this. Larger finds smaller people but is slower." },
      { key: "detector_conf", type: "number", label: "Detection confidence", min: 0.01, max: 0.9, step: 0.01,
        hint: "Weakest detection the tracker considers. Low values help keep partly hidden workers." },
      { key: "new_track_thresh", type: "number", label: "Confidence to start a new person", min: 0.05, max: 0.95, step: 0.05,
        hint: "Lower tracks more hard-to-see workers but creates more short, spurious IDs." },
      { key: "min_track_age_sec", type: "number", label: "Ignore tracks younger than", min: 0, max: 3, step: 0.1, unit: "s",
        hint: "Filters flicker. A track must last this long before it is shown or checked." },
      { key: "hidden_sec", type: "number", label: "Keep a hidden worker's ID for", min: 0, max: 60, step: 1, unit: "s",
        hint: "Someone out of view for less than this keeps their ID when they reappear." },
    ],
  },
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

  // ---- generic field --------------------------------------------------------
  // get/set read and write the draft; def() returns the default value.
  function field({ id, label, hint, unit, type, options, min, max, step, numeric, list }, get, set, def) {
    const dot = h("span", { class: "changed-dot", title: "Changed from the default" });
    const reset = h("button", { type: "button", class: "link-btn", onclick: () => { set(clone(def())); refreshBar(); } }, "Reset");
    let input;
    if (type === "toggle") {
      input = h("input", { id, type: "checkbox", class: "switch", onchange: () => { set(input.checked); refreshBar(); } });
    } else if (type === "select" || type === "model") {
      input = h("select", { id, class: "input", onchange: () => { set(numeric ? Number(input.value) : input.value); refreshBar(); } });
      const opts = type === "model"
        ? [["", `Server default (${state.default_model})`], ...state.models.map((m) => [m, m])]
        : options;
      input.replaceChildren(...opts.map(([v, t]) => h("option", { value: v }, t)));
    } else if (type === "textarea") {
      input = h("textarea", { id, class: "input textarea", rows: 3, oninput: () => { set(input.value); refreshBar(); } });
    } else if (type === "text") {
      input = h("input", { id, class: "input", type: "text", list, autocomplete: "off", spellcheck: "false",
        oninput: () => { set(input.value); refreshBar(); } });
    } else {
      input = h("input", { id, class: "input num-input", type: "number", min, max, step,
        oninput: () => { if (input.value !== "") { set(Number(input.value)); refreshBar(); } } });
    }
    const row = h("div", { class: `dev-field ${type === "toggle" ? "is-toggle" : ""}` },
      h("div", { class: "dev-label" }, dot, h("label", { for: id }, label), reset),
      h("div", { class: "dev-input" }, input, unit ? h("span", { class: "unit" }, unit) : null),
      hint ? h("p", { class: "hint" }, hint) : null);
    controls.push({
      refresh() {
        const v = get();
        if (type === "toggle") input.checked = !!v;
        else if (document.activeElement !== input) input.value = v ?? "";
        const changed = !same(v ?? null, def() ?? null);
        dot.hidden = reset.hidden = !changed;
      },
    });
    return row;
  }

  // ---- prompts ----------------------------------------------------------------
  const preview = h("p", { class: "prompt-preview" });
  const previewItem = h("select", { class: "input", style: "max-width:140px", onchange: () => refreshPreview() },
    Object.entries(ITEM_LABELS).map(([k, v]) => h("option", { value: k }, v)));
  function refreshPreview() {
    const it = draft.items[previewItem.value];
    preview.textContent = draft.question_template.includes("{item}")
      ? draft.question_template.replaceAll("{item}", it.question)
      : "The template must contain {item}.";
  }
  controls.push({ refresh: refreshPreview });

  const promptCard = h("section", { class: "card card-pad" },
    h("div", { class: "card-head" }, h("div", {},
      h("h2", {}, "Moondream prompts"),
      h("p", {}, "The question asked for every worker and PPE item. {item} is replaced by the item's wording below."))),
    field({ id: "t-template", type: "textarea", label: "Question template",
      hint: "Keep “Answer only yes or no”: other replies are handled by the unclear-answer rule." },
      () => draft.question_template, (v) => { draft.question_template = v; }, () => defaults.question_template),
    h("div", { class: "prompt-preview-row" }, h("span", { class: "eyebrow" }, "Sent for"), previewItem, preview),
    h("div", { class: "divider" }),
    h("h3", {}, "Per item"),
    h("p", { class: "hint", style: "margin:4px 0 14px" },
      "Wording: fills {item}. Detect term: what detect looks for to overrule a “no”. Body part: if it can't be found, the item is unverified. Region: where on the worker the item may be, as a share of the box height from the top (negative = above the box)."),
    h("div", { class: "item-grid" }, Object.keys(ITEM_LABELS).map((key) => {
      const g = (k) => () => draft.items[key][k];
      const s = (k) => (v) => { draft.items[key][k] = v; };
      const d = (k) => () => defaults.items[key][k];
      return h("div", { class: "item-card" },
        h("h3", {}, ITEM_LABELS[key]),
        field({ id: `i-${key}-q`, type: "textarea", label: "Wording" }, g("question"), s("question"), d("question")),
        field({ id: `i-${key}-d`, type: "text", label: "Detect term" }, g("detect_term"), s("detect_term"), d("detect_term")),
        field({ id: `i-${key}-b`, type: "text", label: "Body part (blank = none)" },
          g("body_part"), (v) => { draft.items[key].body_part = v.trim() || null; }, d("body_part")),
        h("div", { class: "region-row" },
          field({ id: `i-${key}-rt`, type: "number", label: "Region top", min: -0.5, max: 1.5, step: 0.05 },
            g("region_top"), s("region_top"), d("region_top")),
          field({ id: `i-${key}-rb`, type: "number", label: "Region bottom", min: -0.5, max: 1.5, step: 0.05 },
            g("region_bottom"), s("region_bottom"), d("region_bottom"))));
    })));

  const sectionCards = SECTIONS.map((sec) => h("section", { class: "card card-pad" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, sec.title), h("p", {}, sec.intro))),
    sec.endpointNote ? h("p", { class: "endpoint-note" },
      "Endpoint: ", h("code", {}, state.moondream_endpoint),
      h("span", { class: "hint" }, " · set MOONDREAM_ENDPOINT in the server's .env to use another provider or a self-hosted server")) : null,
    sec.endpointNote ? h("datalist", { id: "md-models" },
      ["moondream3.1-9B-A2B", "moondream3-preview"].map((m) => h("option", { value: m }))) : null,
    h("div", { class: "dev-fields" }, sec.fields.map((f) =>
      field({ ...f, id: `f-${f.key}` }, () => draft[f.key], (v) => { draft[f.key] = v; }, () => defaults[f.key])))));

  // ---- playground ----------------------------------------------------------------
  const playground = await buildPlayground(() => draft);

  root.replaceChildren(h("div", { class: "page" },
    h("div", { class: "run-head" },
      h("div", { class: "title" },
        h("span", { class: "eyebrow" }, "Developer options"),
        h("h1", {}, "Tune detection"),
        changesNote),
      h("div", { class: "actions" }, dirtyPill, discardBtn, resetBtn, saveBtn)),
    errorBox,
    playground,
    promptCard,
    ...sectionCards));
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
    const stage = h("div", { class: "pg-stage" }, h("img", { src: pick.url, alt: "Tested worker" }));
    const steps = [];
    for (const s of r.steps) {
      if (s.step === "query") {
        steps.push(h("li", {}, h("b", {}, "Asked: "), h("q", {}, s.question)));
        steps.push(h("li", {}, h("b", {}, "Moondream answered: "), h("code", {}, JSON.stringify(s.answer)), ` → read as ${s.read_as}`));
      } else if (s.step === "detect") {
        steps.push(h("li", {}, h("b", {}, `Detect “${s.term}”: `),
          `${s.found} found${s.found ? `, ${s.on_worker ? "on this worker" : "none in the item's region"}` : ""}`));
      } else if (s.step === "detect_object") {
        const b = s.box;
        stage.append(h("div", { class: `pg-box ${s.on_worker ? "on" : "off"}`, title: `${s.term}${s.on_worker ? "" : " (outside region)"}`,
          style: `left:${b.x_min * 100}%;top:${b.y_min * 100}%;width:${(b.x_max - b.x_min) * 100}%;height:${(b.y_max - b.y_min) * 100}%` },
          h("span", {}, s.term)));
      }
    }
    result.replaceChildren(
      stage,
      h("div", { class: "pg-steps" },
        h("div", {}, h("span", { class: `pill ${tone}` }, verdict), " ",
          h("span", { class: "hint" }, `${ITEM_LABELS[r.item]} · ${r.image_size[0]}×${r.image_size[1]} px`)),
        h("ol", {}, steps)));
  }

  const card = h("section", { class: "card card-pad" },
    h("div", { class: "card-head" }, h("div", {},
      h("h2", {}, "Prompt playground"),
      h("p", {}, "Check one PPE item on one worker with the settings on this page, saved or not. Uses 1–3 Moondream calls."))),
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
