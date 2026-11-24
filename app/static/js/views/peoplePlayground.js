// Person detection playground (Developer options): try person-detection settings on a video
// and compare the results, to choose the settings to save. The settings on this page are only
// used by the tests started here; they are never saved and don't change new inspections.

import { api } from "../api.js";
import { CHECK_GROUP, PERSON_KEYS, TRACKING_GROUPS } from "../peopleFields.js";
import { fmt, h, plural, statusPill } from "../ui.js";

const DRAFT_KEY = "ppe.peopleDraft";
const clone = (o) => JSON.parse(JSON.stringify(o));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const pick = (obj) => Object.fromEntries(PERSON_KEYS.map((k) => [k, obj[k]]));

function savedDraft() {
  try { return JSON.parse(localStorage.getItem(DRAFT_KEY) || "null"); } catch { return null; }
}
function rememberDraft(draft) {
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify(draft)); } catch { /* not remembered */ }
}

// Sections the viewer folded, remembered in this browser.
const FOLDED_KEY = "ppe.peopleFolded";
function savedFolded() {
  try { return new Set(JSON.parse(localStorage.getItem(FOLDED_KEY) || "[]")); } catch { return new Set(); }
}
function rememberFolded(ids) {
  try { localStorage.setItem(FOLDED_KEY, JSON.stringify([...ids])); } catch { /* not remembered */ }
}

const NAMES = Object.fromEntries([...TRACKING_GROUPS, CHECK_GROUP].flatMap((g) => g.fields.map((x) => [x.key, x.label])));

export async function renderPeoplePlayground(root) {
  const [state, runs] = await Promise.all([api.dev.get(), api.listRuns()]);
  const defaults = pick(state.defaults);
  let globalSettings = pick(state.active); // what new inspections use now
  const draft = { ...defaults, ...pick(savedDraft() ?? {}) };
  // Reading a stored draft from an older version may leave out or invalidate fields: keep only known keys.
  for (const k of PERSON_KEYS) if (draft[k] === undefined) draft[k] = defaults[k];

  let tests = await api.dev.people.list();
  let selectedId = tests.find((t) => t.status === "completed")?.id ?? null;
  let timer = null;
  let stopped = false;
  const controls = [];

  // ---- settings form ---------------------------------------------------------------
  // Playground model list: the re-identification classifier (-cls) can't find people.
  const models = state.models.filter((m) => !/-cls\.(pt|onnx|engine)$/.test(m));

  function makeInput(spec, id) {
    const set = (v) => { draft[spec.key] = v; refresh(); };
    let input;
    if (spec.type === "toggle") {
      input = h("input", { id, type: "checkbox", class: "switch", role: "switch", onchange: () => set(input.checked) });
    } else if (spec.type === "select" || spec.type === "model") {
      input = h("select", { id, class: "input", onchange: () => set(spec.numeric ? Number(input.value) : input.value) });
      const opts = spec.type === "model"
        ? [["", `Server default (${state.default_model})`], ...models.map((m) => [m, m])]
        : spec.options;
      input.replaceChildren(...opts.map(([v, t]) => h("option", { value: v }, t)));
    } else {
      input = h("input", { id, class: "input num-input", type: "number", min: spec.min, max: spec.max, step: spec.step,
        oninput: () => { if (input.value !== "" && !Number.isNaN(input.valueAsNumber)) set(input.valueAsNumber); } });
    }
    return input;
  }

  function fieldRow(spec) {
    const id = `ppg-${spec.key}`;
    const input = makeInput(spec, id);
    const dot = h("span", { class: "changed-dot", title: "Different from the default" });
    const reset = h("button", { type: "button", class: "link-btn", onclick: () => { draft[spec.key] = defaults[spec.key]; refresh(); } }, "Reset");
    const el = h("div", { class: `set-row${spec.type === "toggle" ? " is-toggle" : ""}` },
      h("div", { class: "set-text" },
        h("div", { class: "set-label" }, h("label", { for: id }, spec.label), dot, reset),
        spec.hint ? h("p", { class: "hint" }, spec.hint) : null),
      h("div", { class: "set-control" }, input, spec.unit ? h("span", { class: "unit" }, spec.unit) : null));
    controls.push(() => {
      if (spec.type === "toggle") input.checked = !!draft[spec.key];
      else if (document.activeElement !== input) input.value = draft[spec.key] ?? "";
      dot.hidden = reset.hidden = same(draft[spec.key], defaults[spec.key]);
    });
    return el;
  }

  // Each group of settings folds; a folded group shows how many of its settings differ from the defaults.
  const folded = savedFolded();
  const setOpenFns = [];
  function group({ title, fields }) {
    const rows = h("div", { class: "set-rows" }, fields.map(fieldRow));
    const count = h("span", { class: "fold-count", hidden: true });
    const toggle = h("button", { type: "button", class: "fold-toggle", onclick: () => setOpen(rows.hidden) },
      h("span", { class: "chevron", "aria-hidden": "true" }), title);
    const el = h("div", { class: "set-group" }, h("h3", { class: "set-group-title ppg-fold" }, toggle, count), rows);
    const refreshCount = () => {
      const n = fields.filter((x) => !same(draft[x.key], defaults[x.key])).length;
      count.hidden = !n || !rows.hidden;
      count.textContent = `${n} changed`;
    };
    function setOpen(open) {
      rows.hidden = !open;
      el.classList.toggle("is-folded", !open);
      toggle.setAttribute("aria-expanded", String(open));
      if (open) folded.delete(title); else folded.add(title);
      rememberFolded(folded);
      refreshCount();
    }
    controls.push(refreshCount);
    setOpenFns.push(setOpen);
    setOpen(!folded.has(title));
    return el;
  }

  const changeNote = h("p", { class: "hint" });
  const savedNote = h("p", { class: "hint" });
  const useSavedBtn = h("button", { class: "btn", type: "button", onclick: () => load(globalSettings) }, "Use saved settings");
  const defaultsBtn = h("button", { class: "btn btn-ghost", type: "button", onclick: () => load(defaults) }, "Reset to defaults");
  const expandBtn = h("button", { class: "btn btn-ghost fold-all", type: "button", onclick: () => setOpenFns.forEach((f) => f(true)) }, "Expand all");
  const collapseBtn = h("button", { class: "btn btn-ghost fold-all", type: "button", onclick: () => setOpenFns.forEach((f) => f(false)) }, "Collapse all");

  // Makes the settings in the form the saved ones, used by every new inspection. Other saved
  // settings (prompts, decision rules, ...) are kept as they are now on the server.
  const applyBtn = h("button", { class: "btn btn-primary", type: "button", onclick: applyGlobal }, "Save as global settings");
  const applyMsg = h("p", { class: "hint", role: "status" });
  async function applyGlobal() {
    if (applyBtn.dataset.confirm !== "1") {
      applyBtn.dataset.confirm = "1";
      applyBtn.textContent = "Click again to change the global settings";
      setTimeout(() => { applyBtn.dataset.confirm = ""; applyBtn.textContent = "Save as global settings"; }, 3000);
      return;
    }
    applyBtn.dataset.confirm = "";
    applyBtn.textContent = "Save as global settings";
    applyBtn.disabled = true;
    applyMsg.className = "hint";
    applyMsg.textContent = "Saving…";
    try {
      const current = await api.dev.get(); // latest, so changes made elsewhere aren't overwritten
      const saved = await api.dev.save({ ...current.active, ...draft });
      globalSettings = pick(saved.active);
      applyMsg.textContent = "Saved: new inspections now use these person detection settings.";
    } catch (e) {
      applyMsg.className = "hint key-status bad";
      applyMsg.textContent = `Not saved: ${e.message}`;
    }
    refresh();
  }

  function load(values) {
    Object.assign(draft, clone(values));
    refresh();
  }

  function refresh() {
    controls.forEach((c) => c());
    const n = PERSON_KEYS.filter((k) => !same(draft[k], defaults[k])).length;
    changeNote.textContent = n ? `${plural(n, "setting")} different from the defaults.` : "All settings are the defaults.";
    const diff = PERSON_KEYS.filter((k) => !same(draft[k], globalSettings[k]));
    savedNote.textContent = diff.length
      ? `${plural(diff.length, "setting")} differ from the saved settings that new inspections use.`
      : "Same as the saved settings that new inspections use.";
    useSavedBtn.disabled = applyBtn.disabled = !diff.length;
    rememberDraft(draft);
  }

  const settingsCard = h("section", { class: "card card-pad ppg-form" },
    h("div", { class: "card-head" }, h("div", {},
      h("h2", {}, "Settings to try"),
      h("p", {}, "Used only by the tests on this page. Nothing here is saved to the server or applied to new inspections."))),
    h("div", { class: "ppg-bar" }, useSavedBtn, defaultsBtn, expandBtn, collapseBtn),
    changeNote, savedNote,
    h("div", { class: "ppg-bar" }, applyBtn), applyMsg,
    ...TRACKING_GROUPS.map(group), group(CHECK_GROUP));

  // ---- video and test start ----------------------------------------------------------
  const runSelect = h("select", { class: "input", id: "ppg-run", onchange: () => { upload.value = ""; updateStart(); } },
    h("option", { value: "" }, runs.length ? "Choose a run's video…" : "No runs yet"),
    runs.map((r) => h("option", { value: r.id }, `${r.name} · ${fmt.dateTime(r.created_at)}`)));
  const upload = h("input", { type: "file", id: "ppg-upload", accept: "video/*,.mkv", class: "visually-hidden",
    onchange: () => { runSelect.value = ""; updateStart(); } });
  const uploadName = h("span", { class: "hint" }, "No video chosen");
  const seconds = h("input", { class: "input num-input", id: "ppg-seconds", type: "number", min: 1, step: 1, value: 30,
    style: "max-width:110px" });
  const startBtn = h("button", { class: "btn btn-primary", type: "button", disabled: true, onclick: start }, "Run person detection");
  const errorBox = h("div", { class: "alert", role: "alert", hidden: true });

  function updateStart() {
    const f = upload.files[0];
    uploadName.textContent = f ? `${f.name} · ${fmt.bytes(f.size)}` : "No video chosen";
    startBtn.disabled = !(f || runSelect.value);
  }

  async function start() {
    errorBox.hidden = true;
    const form = new FormData();
    form.append("person_settings", JSON.stringify(draft));
    if (seconds.value) form.append("max_seconds", seconds.value);
    if (upload.files[0]) form.append("video", upload.files[0]);
    else form.append("run_id", runSelect.value);
    startBtn.disabled = true;
    startBtn.textContent = upload.files[0] ? "Uploading…" : "Starting…";
    try {
      const test = await api.dev.people.start(form);
      tests = [test, ...tests.filter((t) => t.id !== test.id)];
      selectedId = test.id;
      renderAll();
      poll();
    } catch (e) {
      errorBox.replaceChildren(h("p", { style: "white-space:pre-line" }, e.message));
      errorBox.hidden = false;
    } finally {
      startBtn.textContent = "Run person detection";
      updateStart();
    }
  }

  const sourceCard = h("section", { class: "card card-pad" },
    h("div", { class: "card-head" }, h("div", {},
      h("h2", {}, "Video"),
      h("p", {}, "People are found and tracked with the settings on the left; no PPE checks are made and no Moondream calls are used."))),
    h("div", { class: "field" },
      h("label", { class: "label", for: "ppg-run" }, "A video from a run"), runSelect,
      h("div", { class: "ppg-or" }, h("span", { class: "hint" }, "or"),
        h("label", { class: "btn", for: "ppg-upload" }, "Upload a video"), upload, uploadName)),
    h("div", { class: "field" },
      h("label", { class: "label", for: "ppg-seconds" }, "Only the first"),
      h("div", { class: "input-row" }, seconds, h("span", { class: "unit" }, "seconds (empty = the whole video)"))),
    h("div", { class: "ppg-bar" }, startBtn),
    errorBox);

  // ---- result of the selected test -----------------------------------------------------
  const resultCard = h("section", { class: "card card-pad" });

  function stat(value, label, note, tone) {
    return h("div", { class: `ppg-stat${tone ? ` is-${tone}` : ""}` },
      h("b", { class: "num" }, value), h("span", {}, label), note ? h("small", {}, note) : null);
  }

  function spark(points, duration) {
    const W = 600, H = 70, max = Math.max(1, ...points.map((p) => p.people));
    const bw = Math.max(2, W / Math.max(1, duration) - 1);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("class", "ppg-spark");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "People on screen per second");
    for (const p of points) {
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      const bh = Math.max(1, (p.people / max) * (H - 4));
      r.setAttribute("x", (p.time_sec / Math.max(1, duration)) * W);
      r.setAttribute("y", H - bh);
      r.setAttribute("width", bw);
      r.setAttribute("height", bh);
      const t = document.createElementNS("http://www.w3.org/2000/svg", "title");
      t.textContent = `${fmt.time(p.time_sec)}: ${p.people} on screen`;
      r.append(t);
      svg.append(r);
    }
    return svg;
  }

  function renderResult() {
    const t = tests.find((x) => x.id === selectedId);
    if (!t) {
      resultCard.replaceChildren(
        h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Result"))),
        h("p", { class: "hint" }, "Run a test to see the video with the people found, and how many of them a run would check."));
      return;
    }
    const head = h("div", { class: "card-head" }, h("div", {},
      h("h2", {}, "Result"),
      h("p", {}, `${t.source}${t.max_seconds ? ` · first ${t.max_seconds} s` : ""}`)), statusPill(t.status));
    if (t.status === "failed") {
      return resultCard.replaceChildren(head, h("div", { class: "alert" }, h("h3", {}, "Test failed"), h("p", {}, t.error)));
    }
    if (t.status !== "completed") {
      return resultCard.replaceChildren(head,
        h("div", { class: "bar" }, h("i", { style: `width:${Math.round(t.progress_fraction * 100)}%` })),
        h("p", { class: "hint" }, t.frames_total ? `Frame ${t.frames_done.toLocaleString()} of ${t.frames_total.toLocaleString()}` : "Starting…"),
        h("button", { class: "btn", type: "button", onclick: () => remove(t.id) }, "Stop"));
    }
    const r = t.report;
    const realtime = r.processing_fps ? r.processing_fps / r.fps : null;
    resultCard.replaceChildren(head,
      h("video", { class: "ppg-video", src: api.dev.people.video(t.id), controls: true, preload: "metadata", playsinline: true }),
      h("p", { class: "hint" }, "Green boxes are people a run would check for PPE; grey ones are too far or too new to be checked; thin amber boxes are kept after a missed detection. The label is the person's ID and detection confidence. With a segmentation model, each person also has a thin outline."),
      h("div", { class: "ppg-stats" },
        stat(r.people, "People found", "different IDs"),
        stat(r.would_be_checked, "Would be checked", "got a PPE check", "good"),
        stat(r.never_checked, "Never checked", "too far or too new", r.never_checked ? "warn" : null),
        stat(r.short_tracks, "Under 1 s", "probably false detections", r.short_tracks ? "warn" : null),
        stat(r.avg_per_frame, "On screen, average", `most: ${r.max_per_frame}`),
        stat(fmt.pct(r.frames_without_people / Math.max(1, r.frames)), "Frames with nobody", `${r.frames_without_people.toLocaleString()} of ${r.frames.toLocaleString()}`),
        stat(r.processing_fps ?? "–", "Frames/s detected", realtime ? `${realtime.toFixed(1)}× real time` : null)),
      h("h3", { class: "set-group-title" }, "People on screen per second"),
      spark(r.timeline, Math.max(1, Math.ceil(r.duration_sec))),
      h("h3", { class: "set-group-title" }, `People (${r.tracks.length})`),
      tracksTable(r));
  }

  function tracksTable(r) {
    if (!r.tracks.length) return h("p", { class: "hint" }, "Nobody was found.");
    const rows = r.tracks.slice(0, 200).map((k) => h("tr", {},
      h("td", { class: "num" }, `#${k.id}`),
      h("td", { class: "num" }, `${fmt.timePrecise(k.first_sec)} – ${fmt.timePrecise(k.last_sec)}`),
      h("td", { class: "num" }, k.frames),
      h("td", { class: "num" }, k.avg_score.toFixed(2)),
      h("td", { class: "num" }, `${k.max_height_px} px`),
      h("td", {}, k.checked ? h("span", { class: "pill completed" }, "Checked") : h("span", { class: "pill queued" }, "Not checked"),
        k.short ? [" ", h("span", { class: "pill failed" }, "Short")] : null)));
    return h("div", { class: "ppg-scroll" },
      h("table", { class: "det-table ppg-table" },
        h("thead", {}, h("tr", {}, ["ID", "On screen", "Frames", "Avg conf.", "Tallest", "Run would…"].map((x) => h("th", {}, x)))),
        h("tbody", {}, rows)),
      r.tracks.length > rows.length ? h("p", { class: "hint" }, `Showing the first ${rows.length} of ${r.tracks.length}.`) : null);
  }

  // ---- history: every test since the server started, to compare settings ------------------
  const historyCard = h("section", { class: "card card-pad" });

  function renderHistory() {
    if (!tests.length) {
      historyCard.hidden = true;
      return;
    }
    historyCard.hidden = false;
    const rows = tests.map((t) => {
      const r = t.report;
      const changed = t.changes.length ? t.changes.map((k) => `${NAMES[k] ?? k}: ${t.settings[k] === "" ? "default" : t.settings[k]}`).join(" · ")
        : "defaults";
      return h("tr", { class: t.id === selectedId ? "is-selected" : "" },
        h("td", {}, h("button", { type: "button", class: "link-btn ppg-link", onclick: () => select(t.id) }, t.source),
          h("div", { class: "hint" }, fmt.relative(t.created_at))),
        h("td", { class: "ppg-changed" }, changed),
        h("td", { class: "num" }, r ? r.people : "–"),
        h("td", { class: "num" }, r ? r.would_be_checked : "–"),
        h("td", { class: "num" }, r ? r.short_tracks : "–"),
        h("td", { class: "num" }, r?.processing_fps ?? "–"),
        h("td", {}, t.status === "completed" ? null : statusPill(t.status)),
        h("td", { class: "ppg-actions" },
          h("button", { type: "button", class: "link-btn", onclick: () => { load(t.settings); root.scrollTo?.({ top: 0 }); } }, "Use settings"),
          h("button", { type: "button", class: "link-btn", onclick: () => remove(t.id) },
            t.status === "running" || t.status === "queued" ? "Stop" : "Delete")));
    });
    historyCard.replaceChildren(
      h("div", { class: "card-head" }, h("div", {},
        h("h2", {}, "Tests so far"),
        h("p", {}, "Kept until the server restarts. Compare how many people each setting found and how many were short-lived false detections; “Use settings” loads a test's settings into the form."))),
      h("div", { class: "ppg-scroll" }, h("table", { class: "det-table ppg-table" },
        h("thead", {}, h("tr", {}, ["Video", "Settings changed from defaults", "People", "Checked", "Short", "Frames/s", "", ""].map((x) => h("th", {}, x)))),
        h("tbody", {}, rows))));
  }

  // ---- wiring -------------------------------------------------------------------------------
  function select(id) {
    selectedId = id;
    renderAll();
  }

  async function remove(id) {
    try { await api.dev.people.remove(id); } catch { /* already gone */ }
    tests = tests.filter((t) => t.id !== id);
    if (selectedId === id) selectedId = tests.find((t) => t.status === "completed")?.id ?? null;
    renderAll();
  }

  function renderAll() {
    renderResult();
    renderHistory();
  }

  // While a test runs, ask for its progress every second.
  async function poll() {
    clearTimeout(timer);
    if (stopped || !tests.some((t) => t.status === "queued" || t.status === "running")) return;
    try {
      const before = tests.map((t) => `${t.id}:${t.status}`).join();
      const next = await api.dev.people.list();
      const playing = resultCard.querySelector("video");
      tests = next;
      if (!tests.some((t) => t.id === selectedId)) selectedId = null;
      // Don't rebuild the page (and restart a playing video) unless something changed.
      const running = tests.some((t) => t.id === selectedId && (t.status === "running" || t.status === "queued"));
      if (running || before !== tests.map((t) => `${t.id}:${t.status}`).join() || !playing) renderAll();
    } catch { /* server busy: try again */ }
    timer = setTimeout(poll, 1000);
  }

  const page = h("div", { class: "page dev-page" },
    h("div", { class: "run-head" },
      h("div", { class: "title" },
        h("span", { class: "eyebrow" }, h("a", { href: "#dev" }, "Developer options"), " / Person detection"),
        h("h1", {}, "Person detection playground"),
        h("p", { class: "hint" }, "Find the best settings for finding people: change them, run a video, and compare. When you're happy, copy them into Developer options (Person tracking), or the New inspection page for Alpha.")),
      h("div", { class: "actions" }, h("a", { class: "btn", href: "#dev" }, "Back to Developer options"))),
    h("div", { class: "ppg-grid" }, settingsCard, h("div", { class: "ppg-side" }, sourceCard, resultCard)),
    historyCard);
  root.replaceChildren(page);
  refresh();
  renderAll();
  poll();
  return () => { stopped = true; clearTimeout(timer); };
}
