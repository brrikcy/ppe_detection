// Thin client for the FastAPI backend.

export class ServerUnreachable extends Error {
  constructor() {
    super(`Can't reach the PPE Inspector server at ${location.host}. Check that "python run.py" is still running in its terminal (and look there for errors), then retry.`);
    this.name = "ServerUnreachable";
  }
}

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    // fetch only rejects when no HTTP response arrived at all.
    throw new ServerUnreachable();
  }
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    let detail = null;
    try {
      const body = await res.json();
      detail = body.detail ?? null;
      if (typeof detail === "string") message = detail;
      else if (Array.isArray(detail)) {
        // FastAPI validation errors: [{loc: ["body", "crop_padding"], msg: "..."}]
        message = detail.map((d) => {
          const where = d.loc.filter((p) => p !== "body").join(".");
          const msg = String(d.msg).replace(/^Value error, /, "");
          return where ? `${where}: ${msg}` : msg;
        }).join("\n");
      }
    } catch { /* not JSON */ }
    const err = new Error(message);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

// The API key entered in Developer options. Kept for this browser tab only (sessionStorage),
// never on the server; empty means the server's own key or model.
const KEY_STORE = "ppe.apiKey";
export const apiKey = {
  get() {
    try { return sessionStorage.getItem(KEY_STORE) ?? ""; } catch { return memoryKey; }
  },
  set(value) {
    memoryKey = value.trim();
    try { memoryKey ? sessionStorage.setItem(KEY_STORE, memoryKey) : sessionStorage.removeItem(KEY_STORE); } catch { /* memory only */ }
  },
};
let memoryKey = "";

export const api = {
  config: () => request("/api/config"),
  listRuns: () => request("/api/runs"),
  getRun: (id) => request(`/api/runs/${encodeURIComponent(id)}`),
  deleteRun: (id) => request(`/api/runs/${encodeURIComponent(id)}`, { method: "DELETE" }),
  getPerson: (id, personId) => request(`/api/runs/${encodeURIComponent(id)}/persons/${encodeURIComponent(personId)}`),
  rerun: (id, body) => request(`/api/runs/${encodeURIComponent(id)}/rerun`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }),

  checkKey(apiKey) {
    const form = new FormData();
    if (apiKey) form.append("api_key", apiKey);
    return request("/api/key/check", { method: "POST", body: form });
  },

  // Uses XHR rather than fetch so we can report upload progress.
  createRun(form, onUploadProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/runs");
      xhr.responseType = "json";
      xhr.upload.onprogress = (e) => e.lengthComputable && onUploadProgress?.(e.loaded / e.total);
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) return resolve(xhr.response);
        const detail = xhr.response?.detail;
        reject(new Error(typeof detail === "string" ? detail : `Upload failed (${xhr.status})`));
      };
      xhr.onerror = () => reject(new ServerUnreachable());
      xhr.send(form);
    });
  },

  dev: {
    get: () => request("/api/dev/tuning"),
    save: (tuning) => request("/api/dev/tuning", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(tuning),
    }),
    reset: () => request("/api/dev/tuning", { method: "DELETE" }),
    tryPrompt: (form) => request("/api/dev/try", { method: "POST", body: form }),
  },

  urls: {
    video: (id, download = false) => `/api/runs/${encodeURIComponent(id)}/video${download ? "?download=true" : ""}`,
    report: (id) => `/api/runs/${encodeURIComponent(id)}/report`,
    preview: (id) => `/api/runs/${encodeURIComponent(id)}/preview?t=${Date.now()}`,
    live: (id) => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/runs/${encodeURIComponent(id)}/live`,
    snapshot: (id, name) => `/api/runs/${encodeURIComponent(id)}/snapshots/${encodeURIComponent(name)}`,
    frame: (id, name) => `/api/runs/${encodeURIComponent(id)}/frames/${encodeURIComponent(name)}`,
    person: (id, name) => `/api/runs/${encodeURIComponent(id)}/people/${encodeURIComponent(name)}`,
  },
};
