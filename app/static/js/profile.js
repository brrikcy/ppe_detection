// A tracked person's profile: every crop of them, with counts per class
// (PPE OK, each missing item, items not visible, failed checks) that filter the gallery.

import { api } from "./api.js";
import { openLightbox } from "./lightbox.js";
import { fmt, h, plural } from "./ui.js";

const CLOSE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>`;

// Colour of a class chip / tag: ok, bad (missing item), warn (not visible), muted (failed).
export function labelTone(label) {
  if (label === "PPE OK") return "ok";
  if (label === "Check failed") return "muted";
  if (label.endsWith("not visible")) return "warn";
  return "bad";
}

const STATE_CLASS = { flagged: "bad", unverified: "unverified", brief: "brief", ok: "ok" };

/** Opens person `personId` of `run` in a panel over the page. Returns a function that closes it. */
export async function openProfile(run, personId, { onShowInVideo } = {}) {
  const opener = document.activeElement;
  const closeBtn = h("button", { type: "button", class: "lb-close pf-close", "aria-label": "Close", html: CLOSE, onclick: close });
  const body = h("div", { class: "pf-body" }, h("p", { class: "hint" }, "Loading…"));
  const panel = h("div", { class: "pf-panel", role: "dialog", "aria-modal": "true", "aria-label": `Person #${personId}` },
    closeBtn, body);
  const root = h("div", { class: "profile", onclick: (e) => { if (e.target === root) close(); } }, panel);

  function onKey(e) {
    if (document.querySelector(".lightbox")) return; // the image viewer on top handles its own keys
    if (e.key === "Escape") close();
  }
  function close() {
    document.removeEventListener("keydown", onKey);
    document.body.classList.remove("lb-open-body");
    root.remove();
    opener?.focus?.();
  }
  document.addEventListener("keydown", onKey);
  document.body.classList.add("lb-open-body");
  document.body.append(root);
  closeBtn.focus();

  let d;
  try {
    d = await api.getPerson(run.meta.id, personId);
  } catch (e) {
    body.replaceChildren(h("div", { class: "alert" }, h("p", {}, e.message)));
    return close;
  }
  const p = d.person;
  const runId = run.meta.id;
  let filter = null;
  const chips = h("div", { class: "filters pf-classes", role: "group", "aria-label": "Filter by class" });
  const grid = h("div", { class: "pf-grid" });
  const countNote = h("p", { class: "hint" });

  const items = (checks) => checks.map((c) => ({
    track_id: p.id, time_sec: c.time_sec, violations: c.violations,
    notes: c.labels.filter((l) => !c.violations.includes(l) && l !== "PPE OK"),
    cropUrl: c.snapshot ? api.urls.snapshot(runId, c.snapshot) : null,
    frameUrl: c.frame_image ? api.urls.frame(runId, c.frame_image) : null,
  }));

  function draw() {
    chips.replaceChildren(
      h("button", { type: "button", "aria-pressed": String(filter === null), onclick: () => { filter = null; draw(); } },
        `All (${d.checks.length})`),
      ...d.classes.map((c) => h("button", { type: "button", class: `chip-${labelTone(c.label)}`,
        "aria-pressed": String(filter === c.label), onclick: () => { filter = c.label; draw(); } },
        h("i", { class: "dot" }), `${c.label} `, h("b", { class: "num" }, c.count))));
    const list = d.checks.filter((c) => !filter || c.labels.includes(filter));
    countNote.textContent = `${plural(list.length, "check")}${filter ? ` with “${filter}”` : ""}, oldest first. Click a photo to enlarge it.`;
    const its = items(list);
    grid.replaceChildren(...list.map((c, i) => h("button", { type: "button", class: "pf-tile", title: "Open image",
      onclick: () => openLightbox(its, i, { onShowInVideo: onShowInVideo && ((it) => { close(); onShowInVideo(it); }) }) },
      h("div", { class: "thumb" }, c.snapshot
        ? h("img", { src: api.urls.snapshot(runId, c.snapshot), alt: `Person ${p.id} at ${fmt.timePrecise(c.time_sec)}`, loading: "lazy" })
        : h("span", { class: "hint" }, "No photo")),
      h("div", { class: "pf-tile-body" },
        h("span", { class: "time num" }, fmt.timePrecise(c.time_sec) + (c.arrival ? " · arrival" : "")),
        h("div", { class: "tags" }, c.labels.map((l) => h("span", { class: `tag-${labelTone(l)}` }, l)))))));
  }

  body.replaceChildren(
    h("div", { class: "pf-head" },
      h("div", { class: "pf-photo" }, p.photo ? h("img", { src: api.urls.person(runId, p.photo), alt: `Person ${p.id}` }) : null),
      h("div", { class: "pf-title" },
        h("span", { class: "eyebrow" }, "Worker profile"),
        h("h2", {}, `Person #${p.id}`),
        h("span", { class: `pf-status ${STATE_CLASS[p.state] ?? "ok"}` }, p.status_label),
        h("span", { class: "meta num" },
          `On screen ${fmt.time(p.first_seen_sec)}–${fmt.time(p.last_seen_sec)} · ${plural(p.checks, "check")}` +
          (p.incidents ? ` · ${plural(p.incidents, "violation incident")}, ${fmt.time(p.violation_sec)} in violation` : "") +
          (p.failed_checks ? ` · ${p.failed_checks} failed` : "")))),
    h("div", { class: "pf-section" },
      h("h3", {}, "Detections by class"),
      h("p", { class: "hint" }, "How many of this person's checks found each class. A check can find more than one (e.g. no helmet and no vest)."),
      chips),
    h("div", { class: "pf-section" },
      h("h3", {}, "All detections"),
      countNote,
      d.crops_for_all_checks ? null : h("p", { class: "hint warn-note" },
        "This run was made before every check's photo was saved, so only checks with a missing item have one. Run the video again to see them all."),
      grid));
  draw();
  return close;
}
