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
    try {
      const body = await res.json();
      if (body.detail) message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* not JSON */ }
    throw new Error(message);
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  config: () => request("/api/config"),
  listRuns: () => request("/api/runs"),
  getRun: (id) => request(`/api/runs/${encodeURIComponent(id)}`),
  deleteRun: (id) => request(`/api/runs/${encodeURIComponent(id)}`, { method: "DELETE" }),

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

  urls: {
    video: (id, download = false) => `/api/runs/${encodeURIComponent(id)}/video${download ? "?download=true" : ""}`,
    report: (id) => `/api/runs/${encodeURIComponent(id)}/report`,
    preview: (id) => `/api/runs/${encodeURIComponent(id)}/preview?t=${Date.now()}`,
    snapshot: (id, name) => `/api/runs/${encodeURIComponent(id)}/snapshots/${encodeURIComponent(name)}`,
    frame: (id, name) => `/api/runs/${encodeURIComponent(id)}/frames/${encodeURIComponent(name)}`,
    person: (id, name) => `/api/runs/${encodeURIComponent(id)}/people/${encodeURIComponent(name)}`,
  },
};
