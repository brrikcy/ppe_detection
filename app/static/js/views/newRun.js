// "New inspection" view: video, PPE items, interval, run. The API key lives in Developer options.

import { api, apiKey } from "../api.js";
import { fmt, h, icons } from "../ui.js";

const INTERVALS = [0.5, 1, 2, 5];

export async function renderNewRun(root, { onCreated }) {
  const [config, dev] = await Promise.all([api.config(), api.dev.get()]);
  let file = null;
  let duration = null;
  let previewFailed = false; // browser can't decode the file; the server still can

  // ---- API key ----
  // Set in Developer options; without one the server's own key (or model) is used.
  const enteredKey = apiKey.get();
  const keyNote = enteredKey ? h("div", { class: "dev-note" }, "Using the API key entered in ",
      h("a", { href: "#dev" }, "Developer options"), " for this browser tab.")
    : config.server_key_configured ? null
    : h("div", { class: "dev-note" }, "This server has no API key. Add one in ",
      h("a", { href: "#dev" }, "Developer options"), " before running an inspection.");

  // ---- video ----
  const fileInput = h("input", { id: "video", type: "file", accept: "video/*", "aria-label": "Choose a video" });
  const drop = h("label", { class: "drop", for: "video" });
  const fileLine = h("div", { class: "file-line", hidden: true });
  const setEmptyDrop = () => {
    drop.className = "drop";
    drop.replaceChildren(fileInput,
      h("div", { html: icons.upload }),
      h("div", { class: "big" }, "Drop a site video here"),
      h("div", { class: "hint" }, `or click to browse · MP4, MOV, AVI, MKV, WebM · up to ${config.max_upload_mb} MB`));
  };
  setEmptyDrop();

  function setFile(f) {
    if (!f) return;
    file = f;
    const url = URL.createObjectURL(f);
    const vid = h("video", { src: url, muted: true, controls: true, playsinline: true, preload: "metadata" });
    vid.addEventListener("loadedmetadata", () => { duration = vid.duration; updateEstimate(); });
    vid.addEventListener("error", () => {
      previewFailed = true;
      updateEstimate();
    });
    drop.className = "drop has-file";
    drop.replaceChildren(vid);
    fileLine.hidden = false;
    fileLine.replaceChildren(
      h("span", { class: "mono", title: f.name }, f.name),
      h("span", {}, fmt.bytes(f.size), " · ",
        h("button", { type: "button", class: "btn btn-ghost", onclick: (e) => {
          e.preventDefault(); file = null; duration = null; previewFailed = false; URL.revokeObjectURL(url);
          setEmptyDrop(); fileLine.hidden = true; updateEstimate();
        } }, "Change")));
    updateEstimate();
  }
  fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
  for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); });
  for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); setFile(e.dataTransfer.files[0]); });

  // ---- options ----
  const checks = h("div", { class: "checks" }, config.ppe_items.map((it) =>
    h("label", { class: "check" },
      h("input", { type: "checkbox", name: "ppe", value: it.key,
        checked: config.defaults.required_ppe.includes(it.key), onchange: updateEstimate }),
      it.label)));
  const seg = h("div", { class: "seg", role: "radiogroup", "aria-label": "Check interval" }, INTERVALS.map((v) =>
    h("label", {},
      h("input", { type: "radio", name: "interval", value: v,
        checked: v === config.defaults.analysis_interval_sec, onchange: updateEstimate }),
      `${v}s`)));
  const maxSec = h("input", { id: "max-seconds", class: "input", type: "number", min: "1", step: "1",
    placeholder: "Whole video", style: "max-width:160px", oninput: updateEstimate });
  const estimate = h("div", { class: "estimate" });
  const liveBox = h("input", { type: "checkbox", name: "live", checked: true });

  // ---- alpha: the tracker's confidence to start a new person ----
  // A saved developer setting (new_track_thresh): changing it here changes it for later runs too.
  const devNote = h("div", { class: "field" });
  function showDevChanges(changes) {
    devNote.hidden = !changes.length;
    devNote.replaceChildren(h("div", { class: "dev-note" },
      `Developer settings: ${changes.length} change${changes.length === 1 ? "" : "s"} from the defaults will be used. `,
      h("a", { href: "#dev" }, "Review")));
  }
  showDevChanges(dev.changes);
  const alpha = h("input", { id: "alpha", class: "input", type: "number", min: "0.05", max: "0.95", step: "0.05",
    value: dev.active.new_track_thresh, style: "max-width:120px" });
  const alphaStatus = h("span", { class: "hint", role: "status", hidden: true });
  const sayAlpha = (text) => { alphaStatus.textContent = text; alphaStatus.hidden = false; };
  let alphaSaved = Promise.resolve(true); // the latest save; a run waits for it
  async function saveAlpha() {
    const v = Number(alpha.value);
    if (!(alpha.value !== "" && v >= 0.05 && v <= 0.95)) {
      sayAlpha("Alpha must be between 0.05 and 0.95.");
      return false;
    }
    try {
      const current = (await api.dev.get()).active; // fresh, so other settings saved elsewhere are kept
      if (current.new_track_thresh !== v) {
        showDevChanges((await api.dev.save({ ...current, new_track_thresh: v })).changes);
      }
      sayAlpha(`Saved: new runs use alpha ${v}.`);
      return true;
    } catch (err) {
      sayAlpha(`Couldn't save alpha: ${err.message}`);
      return false;
    }
  }
  alpha.addEventListener("change", () => { alphaSaved = saveAlpha(); });

  function selectedItems() {
    return [...checks.querySelectorAll("input:checked")].map((i) => i.value);
  }
  function interval() {
    return Number(seg.querySelector("input:checked").value);
  }
  function updateEstimate() {
    const items = selectedItems().length;
    const span = Math.min(duration ?? Infinity, Number(maxSec.value) || Infinity);
    if (!Number.isFinite(span)) {
      estimate.replaceChildren(h("span", {}, file && previewFailed
        ? "This browser can't preview the file, so there's no estimate. It will still be processed."
        : file ? "Reading video length…" : "Choose a video to see an estimate."));
      return;
    }
    const frames = Math.ceil(span / interval());
    // Rough guide: assume ~4 workers per frame.
    const calls = frames * (1 + 4 * items);
    estimate.replaceChildren(
      h("span", {}, h("b", { class: "num" }, frames), ` frames will be checked over ${fmt.time(span)}.`),
      h("span", {}, `About ${calls.toLocaleString()} model calls if ~4 workers are on screen.`));
  }
  updateEstimate();

  // ---- submit ----
  const runBtn = h("button", { type: "submit", class: "btn btn-primary btn-lg" }, "Run inference");
  const uploadBar = h("div", { class: "upload-bar", hidden: true }, h("div", { class: "bar" }, h("i", { style: "width:0%" })));
  const formError = h("div", { class: "form-error", role: "alert" });

  const form = h("form", { class: "page", novalidate: true },
    h("div", { class: "run-head" },
      h("div", { class: "title" },
        h("span", { class: "eyebrow" }, "New inspection"),
        h("h1", {}, "Check a site video for PPE"),
        h("p", { class: "hint" }, "Every worker is boxed and tagged PPE OK or PPE Violation with the missing items."))),
    h("div", { class: "form-grid" },
      h("section", { class: "card card-pad" },
        h("div", { class: "card-head" }, h("h2", {}, "Video")),
        drop, fileLine),
      h("section", { class: "card card-pad" },
        h("div", { class: "field" },
          h("span", { class: "label" }, "Required PPE"),
          checks),
        h("div", { class: "field" },
          h("span", { class: "label" }, "Check one frame every"),
          seg,
          h("span", { class: "hint" }, "Shorter intervals catch more but take longer.")),
        h("div", { class: "field" },
          h("label", { class: "label", for: "alpha" }, "Alpha"),
          alpha,
          alphaStatus),
        h("div", { class: "field" },
          h("label", { class: "label", for: "max-seconds" }, "Only the first … seconds"),
          maxSec),
        h("div", { class: "field" },
          h("label", { class: "check" }, liveBox, "Live mode"),
          h("span", { class: "hint" }, "Plays the video at normal speed while it's inspected. A worker's PPE tag can follow about a second behind.")),
        devNote,
        keyNote ? h("div", { class: "field" }, keyNote) : null,
        h("div", { class: "field" }, estimate),
        h("div", { class: "submit-row" }, runBtn, uploadBar),
        formError)));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    formError.textContent = "";
    const items = selectedItems();
    if (!file) return (formError.textContent = "Choose a video first.");
    if (!items.length) return (formError.textContent = "Select at least one PPE item.");
    if (!enteredKey && !config.server_key_configured)
      return (formError.textContent = "Add an API key in Developer options first.");
    // The run takes its tracker settings from the saved developer settings, so alpha must be saved first.
    // Saved again here too: Enter in the alpha box can submit before its change event.
    alphaSaved = alphaSaved.then(saveAlpha);
    if (!(await alphaSaved)) return (formError.textContent = "Fix alpha first.");

    const body = new FormData();
    body.append("video", file);
    if (enteredKey) body.append("api_key", enteredKey);
    body.append("required_ppe", items.join(","));
    body.append("analysis_interval_sec", String(interval()));
    if (Number(maxSec.value) > 0) body.append("max_seconds", String(Number(maxSec.value)));
    body.append("live", String(liveBox.checked));

    runBtn.disabled = true;
    runBtn.textContent = "Uploading…";
    uploadBar.hidden = false;
    const fill = uploadBar.querySelector("i");
    try {
      const run = await api.createRun(body, (f) => (fill.style.width = `${Math.round(f * 100)}%`));
      onCreated(run.meta.id);
    } catch (err) {
      formError.textContent = err.message;
      runBtn.disabled = false;
      runBtn.textContent = "Run inference";
      uploadBar.hidden = true;
    }
  });

  root.replaceChildren(form);
  return () => {};
}
