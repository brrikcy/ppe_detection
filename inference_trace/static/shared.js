// Helpers and texts shared by the flowchart (flow.js) and the step-through view (walk.js).

// ---------------------------------------------------------------- helpers

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const fmt = {
  time(sec) { const s = Math.max(0, Math.round(sec)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; },
  tp(sec) { const m = Math.floor(sec / 60); return `${m}:${(sec - m * 60).toFixed(1).padStart(4, "0")}`; },
  stamp(iso) { return iso ? new Date(iso).toLocaleString() : "–"; },
  clock(iso) { return iso ? new Date(iso).toLocaleTimeString() : "–"; },
  pct(x) { return `${Math.round(x * 100)}%`; },
  ms(x) { return x >= 1000 ? `${(x / 1000).toFixed(1)} s` : `${x} ms`; },
};
export const plural = (n, w) => `${n} ${w}${n === 1 ? "" : /(s|x|ch|sh)$/.test(w) ? "es" : "s"}`;

export const api = {
  async json(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
      let msg = `${r.status} ${r.statusText}`;
      try { const b = await r.json(); if (typeof b.detail === "string") msg = b.detail; } catch { /* not JSON */ }
      throw new Error(msg);
    }
    return r.status === 204 ? null : r.json();
  },
  config: () => api.json("/api/config"),
  list: () => api.json("/api/traces"),
  get: (id) => api.json(`/api/traces/${encodeURIComponent(id)}`),
  del: (id) => api.json(`/api/traces/${encodeURIComponent(id)}`, { method: "DELETE" }),
  create(form, onProgress) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("POST", "/api/traces");
      x.responseType = "json";
      x.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
      x.onload = () => (x.status < 300 ? resolve(x.response)
        : reject(new Error(typeof x.response?.detail === "string" ? x.response.detail : `Upload failed (${x.status})`)));
      x.onerror = () => reject(new Error("Can't reach the flowchart server."));
      x.send(form);
    });
  },
};
export const file = (t, name) => `/api/traces/${encodeURIComponent(t.id)}/files/${name}`;
export const pic = (t, name) => file(t, `images/${name}`);

// An image with boxes over it; boxes are [x1,y1,x2,y2] fractions of the image.
export function boxedImage(src, boxes = [], { alt = "", onBox } = {}) {
  const wrap = h("span", { class: "pic" }, h("img", { src, alt, loading: "lazy" }));
  for (const b of boxes) {
    const [x1, y1, x2, y2] = b.rect.map((v) => Math.max(0, Math.min(1, v)));
    const style = { left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%` };
    wrap.append(h("span", { class: `bx ${b.cls ?? ""}`, style }, b.label ? h("span", {}, b.label) : null));
    if (onBox && b.id != null) {
      wrap.append(h("button", { type: "button", class: "hit", style, title: b.title ?? b.label ?? "",
        onclick: (e) => { e.stopPropagation(); onBox(b.id); } }));
    }
  }
  return wrap;
}

export const STATE = {
  ok: ["PPE OK", "ok"], violation: ["Missing PPE", "bad"], unverified: ["Item not visible", "warn"],
  pending: ["First check (no verdict yet)", "grey"], new: ["Too new to check", "grey"],
  too_far: ["Too far to check", "grey"], failed: ["Check failed", "grey"],
};
export const ITEM_RESULT = { present: "Present", missing: "Missing", unverified: "Not visible", failed: "Check failed" };
export const COLOR_CLASS = { red: "red", green: "", amber: "amber", grey: "grey" };

export function overlapShare(box, others) {
  const [x1, y1, x2, y2] = box;
  const area = Math.max(1, (x2 - x1) * (y2 - y1));
  let covered = 0;
  for (const o of others) {
    const w = Math.min(x2, o[2]) - Math.max(x1, o[0]);
    const ht = Math.min(y2, o[3]) - Math.max(y1, o[1]);
    if (w > 0 && ht > 0) covered += w * ht;
  }
  return Math.min(1, covered / area);
}

// ---------------------------------------------------------------- explanations

export const EXPLAIN = {
  upload: "The video file you uploaded. It's kept next to the trace but not served back.",
  decode: "ffmpeg decodes the video frame by frame. Every frame goes through person tracking and is written to the annotated video; only some frames are checked for PPE.",
  sample: "A regular PPE check happens every N frames (the check interval). Workers who appear in between get an extra arrival check once they qualify.",
  detector: "YOLO finds people in the frame. To save time it runs a few times a second; in between, each box moves on at its last measured speed.",
  tracker: "BoT-SORT links detections over time so each worker keeps one ID through the video. A worker who disappears keeps their ID for a while in case they come back.",
  tracks: "Each line is one tracked person: from the first to the last frame they were on screen. Dots are the checks they appeared in, coloured by what that check concluded. Click a dot to open that check.",
  frame: "The frame at this check, without boxes. Boxes show every tracked worker and what the check decided for them. Click a box (or a worker on the right) to follow that worker.",
  gate_checked: "Workers are asked about only when they're big enough to judge (taller than the minimum height) and have been tracked long enough. This one qualified.",
  gate_too_far: "Shorter than the minimum height, so no Moondream call is made: a crop this small can't be judged reliably. The video shows a grey “Too far” box (with their last status if they had one).",
  gate_new: "Big enough, but tracked for less than the minimum age (or still entering the frame), so it waits for its first check. The video shows “Checking…”.",
  crop: "The worker's box plus a little padding, cut from the full-resolution frame. This is the image Moondream is shown for every question about this worker.",
  query: "A question sent to Moondream's query endpoint with the crop. The exact prompt and the raw answer are shown.",
  read: "The answer is read by its first word: “yes”, “no” (whole words only), or unclear for anything else, such as “Not sure”. An unclear answer follows the “unclear answer” setting.",
  overlap: "A “yes” for a worker overlapping a neighbour may be about the neighbour's PPE, so it's confirmed with detect when the overlap is above the threshold.",
  confirm_no: "A “no” can be double-checked with detect: if the item is found on this worker, the “no” is overruled.",
  detect: "Moondream's detect endpoint, asked to find a term in the crop. It returns boxes (shown on the crop); each box is then judged.",
  box: "A detected box counts only if it's on this worker (not a neighbour), mostly inside the item's part of the body, of a plausible size, and (if enabled) confirmed when Moondream is asked about it alone.",
  verify: "Moondream is shown the detected box alone (with some context) and asked whether it really is the item.",
  body: "If the item wasn't found, the body part it belongs on is looked for. When that body part can't be seen, the item is “not visible” rather than missing.",
  result: "What this check concluded for the item.",
  combined: "With “ask about all items at once”, one question covers every item; answers it can't read are asked one by one.",
  check_result: "This one check's raw finding for the worker: the items it found missing or couldn't see.",
  vote: "What the video shows is voted over the worker's last few checks, so one misread doesn't turn the box red: an item is shown missing when it was missing in most of the window and at least the minimum number of times. On a tie the previous state holds.",
  drawn: "The box and tag drawn on this worker in the annotated video from this frame on.",
  incident: "Consecutive checks of one worker whose voted status showed missing PPE become one violation (incident), from the first to the last of those checks.",
};

// The model's own timing from the response, e.g. "39 ms inference on H100 80GB HBM3 · 760 tokens in, 2 out".
export function modelTime(m) {
  if (!m) return null;
  const parts = [m.inference_ms != null ? `${Math.round(m.inference_ms)} ms inference` : null,
    m.queue_ms != null ? `${Math.round(m.queue_ms)} ms queued` : null, m.device?.label ? `on ${m.device.label}` : null];
  const tokens = m.input_tokens != null ? `${m.input_tokens} tokens in, ${m.output_tokens} out` : null;
  return [parts.filter(Boolean).join(", "), tokens].filter(Boolean).join(" · ") || null;
}

export const REASON = { ok: "counts", "other worker": "belongs to another worker", "outside region": "outside the item's part of the body",
  "wrong size": "wrong size for the item", "re-check said no": "re-check said it isn't one", "not needed": "not needed (already found)" };

export function whyItem(status, steps, tun, spec) {
  const q = steps.find((s) => s.step === "query");
  const detects = steps.filter((s) => s.step === "detect");
  const call = steps.find((s) => s.step === "call" && s.error);
  if (status === "failed") return `The Moondream call failed${call ? `: ${call.error}` : ""}.`;
  if (!q) return "";
  if (q.read_as === "unclear") return `The answer “${q.answer}” wasn't a clear yes or no, so it counts as ${tun.unclear_answer}.`;
  if (q.read_as === "yes") {
    if (status === "present") return detects.length ? `Moondream said yes, and detect confirmed a ${spec.detect_term} on this worker.` : "Moondream said yes.";
    return "Moondream said yes, but the worker overlaps a neighbour and detect couldn't confirm it was on this worker.";
  }
  if (status === "present") return `Moondream said no, but detect found a ${spec.detect_term} on this worker, which overrules the no.`;
  if (status === "unverified") return `Moondream said no and no ${spec.detect_term} was found, but the ${spec.body_part} couldn't be seen either, so it can't be judged.`;
  if (!tun.confirm_no_with_detect) return "Moondream said no, and confirming with detect is off, so the item is missing.";
  return `Moondream said no and detect found no ${spec.detect_term} on this worker${spec.body_part && tun.check_body_part ? ` while their ${spec.body_part} is visible` : ""}, so the item is missing.`;
}

