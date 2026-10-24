// Full-screen viewer for violation images: ← / → to navigate, Esc to close. "Full frame"
// shows only the selected worker's box, "All boxes" the frame as annotated in the video,
// "Worker" the crop that was checked.

import { fmt, h } from "./ui.js";

const ARROW_L = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg>`;
const ARROW_R = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5l7 7-7 7"/></svg>`;
const CLOSE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>`;

let mode = "frame"; // remembered between openings

/**
 * box and frameSize (optional): the worker's box and the [width, height] of the frame it's measured in.
 * cleanUrl (optional): the frame without boxes; with box, "Full frame" draws only this worker's box
 * on it. Without it (older runs) the annotated frame is dimmed around the box instead.
 * @param {{time_sec:number, violations:string[], notes?:string[], cropUrl:string|null, frameUrl:string|null,
 *          cleanUrl?:string|null, box?:number[], frameSize?:number[]}[]} items
 * @param {number} start
 * @param {{onShowInVideo?: (item) => void}} opts
 */
export function openLightbox(items, start, { onShowInVideo } = {}) {
  let index = start;
  const opener = document.activeElement;

  const img = h("img", { class: "lb-img", alt: "" });
  const empty = h("div", { class: "lb-empty", hidden: true }, "No image saved for this view.");
  const boxLabel = h("b", { class: "lb-box-label" });
  const hole = h("div", { class: "lb-hole" }, boxLabel);
  const dim = h("div", { class: "lb-dim" });
  const spot = h("div", { class: "lb-spot", hidden: true, "aria-hidden": "true" }, dim, hole);
  let shownMode = mode;
  const counter = h("span", { class: "lb-counter num" });
  const caption = h("b", { class: "lb-violation" });
  const sub = h("span", { class: "lb-sub num" });
  const notesEl = h("div", { class: "tags" });
  const modeBtns = {
    frame: h("button", { type: "button", onclick: () => setMode("frame") }, "Full frame"),
    all: h("button", { type: "button", onclick: () => setMode("all") }, "All boxes"),
    crop: h("button", { type: "button", onclick: () => setMode("crop") }, "Worker"),
  };
  const prev = h("button", { type: "button", class: "lb-nav", "aria-label": "Previous violation", html: ARROW_L, onclick: () => go(-1) });
  const next = h("button", { type: "button", class: "lb-nav", "aria-label": "Next violation", html: ARROW_R, onclick: () => go(1) });
  const closeBtn = h("button", { type: "button", class: "lb-close", "aria-label": "Close", html: CLOSE, onclick: close });
  const videoBtn = onShowInVideo
    ? h("button", { type: "button", class: "btn btn-primary", onclick: () => { const it = items[index]; close(); onShowInVideo(it); } }, "Show in video")
    : null;
  const openLink = h("a", { class: "btn lb-open", target: "_blank", rel: "noopener" }, "Open image");

  const root = h("div", { class: "lightbox", role: "dialog", "aria-modal": "true", "aria-label": "Violation images" },
    h("div", { class: "lb-top" }, counter, closeBtn),
    h("div", { class: "lb-stage", onclick: (e) => { if (e.target.classList.contains("lb-stage")) close(); } },
      img, spot, empty),
    // Under the image: ‹ the violation, who and when › — browsing stays next to what it describes.
    h("div", { class: "lb-navbar" }, prev,
      h("div", { class: "lb-caption", "aria-live": "polite" }, caption, sub, notesEl), next),
    h("div", { class: "lb-bottom" },
      h("div", { class: "seg lb-seg", role: "group", "aria-label": "Image" }, modeBtns.frame, modeBtns.all, modeBtns.crop),
      h("div", { class: "lb-actions" }, openLink, videoBtn),
      h("span", { class: "lb-keys" }, "← → to browse · Esc to close")));

  // "frame": the clean frame when there is one (only this box is drawn over it), else the annotated one.
  function urlFor(it, m) {
    if (m === "crop") return it.cropUrl;
    if (m === "all") return it.cleanUrl ? it.frameUrl : null; // without a clean frame, "Full frame" is this
    return it.cleanUrl && it.box ? it.cleanUrl : it.frameUrl;
  }

  function render() {
    const it = items[index];
    // Fall back to whichever image exists (older runs have no full frames).
    const m = urlFor(it, mode) ? mode : ["frame", "crop", "all"].find((k) => urlFor(it, k)) ?? mode;
    const url = urlFor(it, m);
    shownMode = m;
    counter.textContent = `${index + 1} / ${items.length}`;
    caption.textContent = it.violations.length ? it.violations.join(" · ") : (it.notes?.length ? "Not verified" : "PPE OK");
    caption.className = `lb-violation${it.violations.length ? "" : it.notes?.length ? " is-warn" : " is-ok"}`;
    sub.textContent = [it.track_id != null ? `Person #${it.track_id}` : null, fmt.timePrecise(it.time_sec)].filter(Boolean).join(" · ");
    // notes: other findings of the check, e.g. "Helmet not visible" (person profiles).
    notesEl.replaceChildren(...(it.notes ?? []).map((n) => h("span", { class: "tag-warn" }, n)));
    for (const [k, b] of Object.entries(modeBtns)) {
      b.setAttribute("aria-pressed", String(k === m));
      b.disabled = !urlFor(it, k);
    }
    modeBtns.all.hidden = !items.some((x) => x.cleanUrl);
    img.hidden = !url;
    empty.hidden = !!url;
    if (url) {
      img.dataset.clean = String(url === it.cleanUrl);
      img.src = url;
      img.alt = `${m === "crop" ? "Flagged worker" : "Frame"} at ${fmt.timePrecise(it.time_sec)}: ${it.violations.join(", ")}`;
      openLink.href = url;
      openLink.hidden = false;
    } else {
      openLink.hidden = true;
    }
    img.classList.toggle("is-crop", m === "crop");
    placeSpot();
    prev.disabled = index === 0;
    next.disabled = index === items.length - 1;
    // Warm the cache for neighbours so browsing feels instant.
    for (const j of [index - 1, index + 1]) {
      const u = items[j] && urlFor(items[j], m);
      if (u) new Image().src = u;
    }
  }

  // Lays the highlight over the image as drawn (it scales with the window), around the worker's box.
  // On the clean frame: that box alone, in the video's violation red, with its label in bold.
  // On an annotated frame (older runs): the rest is dimmed and the box outlined.
  function placeSpot() {
    const it = items[index];
    const show = shownMode === "frame" && !img.hidden && it.box && it.frameSize && img.complete && img.naturalWidth > 0;
    spot.hidden = !show;
    if (!show) return;
    const solo = !!it.cleanUrl;
    spot.classList.toggle("is-solo", solo);
    Object.assign(spot.style, { left: `${img.offsetLeft}px`, top: `${img.offsetTop}px`,
      width: `${img.offsetWidth}px`, height: `${img.offsetHeight}px` });
    const [fw, fh] = it.frameSize;
    const [x1, y1, x2, y2] = it.box;
    const edges = [Math.max(0, x1 / fw), Math.max(0, y1 / fh), Math.min(1, x2 / fw), Math.min(1, y2 / fh)];
    const [l, t, r, b] = edges.map((v) => `${(v * 100).toFixed(3)}%`);
    Object.assign(hole.style, { left: l, top: t, right: `calc(100% - ${r})`, bottom: `calc(100% - ${b})` });
    // The dim layer covers the frame except the box: the outer rectangle minus the inner one.
    dim.style.clipPath = `polygon(evenodd, 0 0, 100% 0, 100% 100%, 0 100%, 0 0, ${l} ${t}, ${r} ${t}, ${r} ${b}, ${l} ${b}, ${l} ${t})`;
    boxLabel.hidden = !solo;
    boxLabel.textContent = [it.track_id != null ? `#${it.track_id}` : null, it.violations.length ? `Missing: ${it.violations.map((v) => v.replace(/^No /, "")).join(", ")}` : null]
      .filter(Boolean).join(" ");
    // The label goes above the box, below it when the box touches the top, inside when it fills the height.
    hole.dataset.label = edges[1] > 0.06 ? "above" : edges[3] < 0.94 ? "below" : "inside";
  }
  img.addEventListener("load", placeSpot);
  // Older runs' box-free frames are made on request; if that fails, show the annotated frame dimmed.
  img.addEventListener("error", () => {
    if (img.dataset.clean !== "true") return;
    items[index].cleanUrl = null;
    render();
  });
  window.addEventListener("resize", placeSpot);

  function setMode(m) {
    mode = m;
    render();
  }

  function go(step) {
    const j = index + step;
    if (j < 0 || j >= items.length) return;
    index = j;
    render();
  }

  function onKey(e) {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") go(-1);
    else if (e.key === "ArrowRight") go(1);
    else if (e.key === "Tab") {
      // Keep focus inside the dialog.
      const focusables = [...root.querySelectorAll("button:not(:disabled):not([hidden]), a[href]:not([hidden])")];
      const first = focusables[0], last = focusables.at(-1);
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }

  function close() {
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("resize", placeSpot);
    document.body.classList.remove("lb-open-body");
    root.remove();
    opener?.focus?.();
  }

  document.addEventListener("keydown", onKey);
  document.body.classList.add("lb-open-body");
  document.body.append(root);
  render();
  closeBtn.focus();
  return close;
}
