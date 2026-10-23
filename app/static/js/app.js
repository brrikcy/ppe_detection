// Entry point: hash router (#new, #run/<id>) and the runs sidebar.

import { api, ServerUnreachable } from "./api.js";
import { fmt, h, plural, statusPill } from "./ui.js";
import { renderDev } from "./views/devView.js";
import { renderNewRun } from "./views/newRun.js";
import { renderRun } from "./views/runView.js";

const view = document.getElementById("view");
const list = document.getElementById("runs-list");
const count = document.getElementById("runs-count");
const devLink = document.getElementById("dev-link");

let cleanup = () => {};
let sidebarTimer = null;

function currentRoute() {
  const hash = location.hash.slice(1);
  if (hash.startsWith("run/")) return { name: "run", id: decodeURIComponent(hash.slice(4)) };
  if (hash === "dev") return { name: "dev" };
  return { name: "new" };
}

async function refreshSidebar() {
  clearTimeout(sidebarTimer);
  let runs = [];
  try {
    runs = await api.listRuns();
  } catch {
    sidebarTimer = setTimeout(refreshSidebar, 5000);
    return;
  }
  const route = currentRoute();
  devLink.setAttribute("aria-current", route.name === "dev" ? "page" : "false");
  count.textContent = runs.length ? String(runs.length) : "";
  list.replaceChildren(...(runs.length ? runs.map((r) => {
    const active = r.status === "running" || r.status === "queued";
    let meta;
    if (active) meta = h("div", { class: "mini-bar" }, h("i", { style: `width:${Math.round(r.progress_fraction * 100)}%` }));
    else if (r.status === "completed") meta = h("span", { class: "meta num" },
      `${r.incidents ? plural(r.incidents, "violation") : "No violations"} · ${plural(r.worker_checks, "check")}`);
    else meta = h("span", { class: "meta" }, "Did not finish");
    return h("a", {
      class: "run-item", href: `#run/${encodeURIComponent(r.id)}`,
      "aria-current": route.name === "run" && route.id === r.id ? "page" : null,
    },
      h("div", { class: "row" }, h("span", { class: "name", title: r.name }, r.name), statusPill(r.status)),
      h("div", { class: "row" }, meta, h("span", { class: "meta" }, fmt.relative(r.created_at))));
  }) : [h("p", { class: "runs-empty" }, "No runs yet. Start one with New inspection.")]));

  // Keep polling while something is in progress.
  if (runs.some((r) => r.status === "running" || r.status === "queued")) sidebarTimer = setTimeout(refreshSidebar, 2000);
}

async function route() {
  cleanup();
  cleanup = () => {};
  const r = currentRoute();
  view.scrollTop = 0;
  try {
    if (r.name === "dev") {
      cleanup = await renderDev(view);
    } else if (r.name === "run") {
      cleanup = await renderRun(view, r.id, {
        onChanged: refreshSidebar,
        onDeleted: () => { location.hash = "#new"; refreshSidebar(); },
      });
    } else {
      cleanup = await renderNewRun(view, {
        onCreated: (id) => { location.hash = `#run/${encodeURIComponent(id)}`; refreshSidebar(); },
      });
    }
  } catch (e) {
    const offline = e instanceof ServerUnreachable;
    view.replaceChildren(h("div", { class: "page" },
      h("div", { class: "alert", role: "alert" },
        h("h3", {}, offline ? "Server not reachable" : "Something went wrong"),
        h("p", {}, e.message)),
      h("div", {}, h("button", { class: "btn btn-primary", type: "button", onclick: route }, "Retry"))));
  }
  refreshSidebar();
}

window.addEventListener("hashchange", route);
route();
