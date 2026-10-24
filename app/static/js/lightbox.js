// Full-screen viewer for violation images: ← / → to navigate, Esc to close,
// toggle between the whole annotated frame and the worker crop.

import { fmt, h } from "./ui.js";

const ARROW_L = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg>`;
const ARROW_R = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5l7 7-7 7"/></svg>`;
const CLOSE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>`;

let mode = "frame"; // remembered between openings

/**
 * box and frameSize (optional): the worker's box and the [width, height] of the frame it's measured in;
 * when given, the full frame dims everything but that worker, so the one violation stands out.
 * @param {{time_sec:number, violations:string[], notes?:string[], cropUrl:string|null, frameUrl:string|null,
 *          box?:number[], frameSize?:number[]}[]} items
 * @param {number} start
 * @param {{onShowInVideo?: (item) => void}} opts
 */
export function openLightbox(items, start, { onShowInVideo } = {}) {
  let index = start;
  const opener = document.activeElement;

  const img = h("img", { class: "lb-img", alt: "" });
  const empty = h("div", { class: "lb-empty", hidden: true }, "No image saved for this view.");
  const hole = h("div", { class: "lb-hole" });
  const spot = h("div", { class: "lb-spot", hidden: true, "aria-hidden": "true" }, hole);
  let shownMode = mode;
  const counter = h("span", { class: "lb-counter num" });
  const person = h("span", { class: "lb-person" });
  const time = h("span", { class: "lb-time num" });
  const tags = h("div", { class: "tags" });
  const modeBtns = {
    frame: h("button", { type: "button", onclick: () => setMode("frame") }, "Full frame"),
    crop: h("button", { type: "button", onclick: () => setMode("crop") }, "Worker"),
  };
  const prev = h("button", { type: "button", class: "lb-nav lb-prev", "aria-label": "Previous violation", html: ARROW_L, onclick: () => go(-1) });
  const next = h("button", { type: "button", class: "lb-nav lb-next", "aria-label": "Next violation", html: ARROW_R, onclick: () => go(1) });
  const closeBtn = h("button", { type: "button", class: "lb-close", "aria-label": "Close", html: CLOSE, onclick: close });
  const videoBtn = onShowInVideo
    ? h("button", { type: "button", class: "btn btn-primary", onclick: () => { const it = items[index]; close(); onShowInVideo(it); } }, "Show in video")
    : null;
  const openLink = h("a", { class: "btn lb-open", target: "_blank", rel: "noopener" }, "Open image");

  const root = h("div", { class: "lightbox", role: "dialog", "aria-modal": "true", "aria-label": "Violation images" },
    h("div", { class: "lb-top" },
      h("div", { class: "lb-title" }, person, counter, time, tags),
      closeBtn),
    h("div", { class: "lb-stage", onclick: (e) => { if (e.target.classList.contains("lb-stage")) close(); } },
      prev, img, spot, empty, next),
    h("div", { class: "lb-bottom" },
      h("div", { class: "seg lb-seg", role: "group", "aria-label": "Image" }, modeBtns.frame, modeBtns.crop),
      h("div", { class: "lb-actions" }, openLink, videoBtn),
      h("span", { class: "lb-keys" }, "← → to browse · Esc to close")));

  function urlFor(it, m) {
    return m === "frame" ? it.frameUrl : it.cropUrl;
  }

  function render() {
    const it = items[index];
    // Fall back to whichever image exists (older runs have no full frames).
    const m = urlFor(it, mode) ? mode : (mode === "frame" ? "crop" : "frame");
    const url = urlFor(it, m);
    shownMode = m;
    counter.textContent = `${index + 1} / ${items.length}`;
    person.textContent = it.track_id != null ? `Person #${it.track_id}` : "";
    person.hidden = it.track_id == null;
    time.textContent = fmt.timePrecise(it.time_sec);
    // notes: other findings of the check, e.g. "Helmet not visible" (person profiles).
    const notes = it.notes ?? [];
    tags.replaceChildren(...(it.violations.length || notes.length
      ? [...it.violations.map((v) => h("span", { class: "tag-bad" }, v)), ...notes.map((n) => h("span", { class: "tag-warn" }, n))]
      : [h("span", { class: "tag-ok" }, "PPE OK")]));
    for (const [k, b] of Object.entries(modeBtns)) {
      b.setAttribute("aria-pressed", String(k === m));
      b.disabled = !urlFor(it, k);
    }
    img.hidden = !url;
    empty.hidden = !!url;
    if (url) {
      img.src = url;
      img.alt = `${m === "frame" ? "Annotated frame" : "Flagged worker"} at ${fmt.timePrecise(it.time_sec)}: ${it.violations.join(", ")}`;
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

  // Lays the spotlight over the image as drawn (it scales with the window), around the worker's box.
  function placeSpot() {
    const it = items[index];
    const show = shownMode === "frame" && !img.hidden && it.box && it.frameSize && img.complete && img.naturalWidth > 0;
    spot.hidden = !show;
    if (!show) return;
    Object.assign(spot.style, { left: `${img.offsetLeft}px`, top: `${img.offsetTop}px`,
      width: `${img.offsetWidth}px`, height: `${img.offsetHeight}px` });
    const [fw, fh] = it.frameSize;
    const [x1, y1, x2, y2] = it.box;
    // Exactly the box drawn in the frame; the outline sits just outside it.
    const [l, t, r, b] = [Math.max(0, x1 / fw), Math.max(0, y1 / fh), Math.min(1, x2 / fw), Math.min(1, y2 / fh)]
      .map((v) => `${(v * 100).toFixed(3)}%`);
    Object.assign(hole.style, { left: l, top: t, right: `calc(100% - ${r})`, bottom: `calc(100% - ${b})` });
    // The dim layer covers the frame except the box: the outer rectangle minus the inner one.
    spot.style.setProperty("--cutout", `polygon(evenodd, 0 0, 100% 0, 100% 100%, 0 100%, 0 0, ${l} ${t}, ${r} ${t}, ${r} ${b}, ${l} ${b}, ${l} ${t})`);
  }
  img.addEventListener("load", placeSpot);
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
      const focusables = [...root.querySelectorAll("button:not(:disabled), a[href]:not([hidden])")];
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
