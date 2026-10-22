# PPE Detection (MVP)

Takes a construction-site video and produces an annotated copy where every worker gets a tag:

- **`PPE OK`** (green box) when they're wearing all the required PPE
- **`PPE Violation (N) - No Helmet, No Vest`** (red box) listing each missing item

All vision runs on the [Moondream](https://moondream.ai) Cloud API.

## How it works

1. A frame is sampled every `ANALYSIS_INTERVAL_SEC` seconds (default 1s).
2. Moondream `detect("person")` finds every worker in the frame.
3. Each worker is cropped, and Moondream `query` is asked a yes/no question for each required PPE item. These calls run in parallel.
   - A "no" answer is double-checked with `detect` on the crop. If the item is found (e.g. a `hard hat`), it counts as present.
   - If the body part it belongs on (e.g. the `head`) can't be found, because the worker is cut off or blocked from view, the item is recorded as `unverified` in the JSON report and isn't counted as a violation.
   - Person boxes mostly inside a larger box are dropped as duplicates.
4. **Person tracking** (`ppe/tracking.py`): every worker gets an ID that stays with them.
   - Between checks, each box follows its worker using optical flow.
   - At each check, detections are matched to existing people by box overlap and clothing colour.
   - Someone hidden for up to 3 checks keeps their ID.
5. Boxes and tags (`#3 PPE Violation (1) - No Gloves`) are drawn on every frame. Between checks, a worker keeps their most recent status.
6. The output is re-encoded to H.264 with the original audio, using the system `ffmpeg` or the copy bundled with `imageio-ffmpeg`. A JSON report is written next to it.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then put your key in MOONDREAM_API_KEY
```

## Web app

```bash
python run.py          # opens http://127.0.0.1:8000
```

1. Optionally enter a Moondream API key. If you leave it empty, the server uses `MOONDREAM_API_KEY` from `.env`. **Test key** checks it with one call. Keys are never written to disk.
2. Drop in a video, choose the required PPE and the check interval, then click **Run inference**.
3. While the run is processing, you see live progress and the latest checked frame. When it finishes, the dashboard shows:
   - compliance, violation and worker-count figures
   - the annotated video
   - violations by type, and items it couldn't verify
   - a compliance timeline; click a column to jump the video there
   - **People**: one card per tracked worker with their ID, photo, overall status, time on screen and how often each item was missing.
     - A person is **flagged** for an item when it was missing in more than half of their checks.
     - Fewer than half shows as **occasional**.
   - a snapshot card for every flagged worker; click one to open the image viewer (← → to browse, full frame or worker crop, "Show in video")
4. Every run is saved under `runs/<id>/` and listed in the sidebar, so you can switch between runs or delete them.

Runs execute one at a time; any others wait as *queued*. The annotated video is H.264, which plays in Chrome, Edge, Safari and Firefox.

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/config` | PPE items, defaults, whether the server has a key |
| POST | `/api/key/check` | Verify a key (form field `api_key`, falls back to the server key) |
| POST | `/api/runs` | Multipart: `video`, `api_key?`, `required_ppe`, `analysis_interval_sec`, `max_seconds?` |
| GET | `/api/runs` | All runs, newest first |
| GET | `/api/runs/{id}` | Status, progress and dashboard summary (partial while running) |
| GET | `/api/runs/{id}/video` · `/report` · `/preview` · `/snapshots/{name}` · `/frames/{name}` · `/people/{name}` | Media |
| DELETE | `/api/runs/{id}` | Delete a finished run |

Interactive docs: http://127.0.0.1:8000/docs

### Code layout

```
app/
  main.py               create_app(): wires settings, services, routers, static files
  core/settings.py      server settings from env/.env
  schemas/runs.py       Pydantic request/response models
  services/
    run_store.py        filesystem persistence of runs (meta.json never holds the key)
    runner.py           background execution, progress, preview and snapshots
    summary.py          dashboard metrics from the per-frame timeline
    keys.py             form key -> server key fallback, key check
  api/deps.py           dependency providers
  api/routes/           config.py, runs.py
  static/               index.html, styles.css, js/ (api, ui, charts, views/)
ppe/                    detection pipeline (shared with the CLI)
```

## Command line

```bash
python annotate_video.py site.mp4                       # -> outputs/site_annotated.mp4 + .json
python annotate_video.py site.mp4 --interval 0.5        # analyse 2 frames/sec
python annotate_video.py site.mp4 --required helmet,vest,gloves
python annotate_video.py site.mp4 --max-seconds 10      # quick trial on the first 10s
```

Supported PPE items: `helmet`, `vest`, `gloves`, `boots`, `goggles`, `mask` (default: `helmet,vest`).

**Cost:** each analysed frame makes `1 + workers × required_items` API calls, plus 1–2 confirmation calls for each "no" answer. A 60s clip with 5 workers, checking helmet and vest at 1s intervals, makes about 660 calls.

## Known limitations

- A worker whose head is cut off can still be flagged if another worker's head falls inside their box.
- In the video, a worker's tag shows their latest check, so it can flicker between seconds. The People section smooths this with the majority rule.
- Tracking assumes a continuous shot. A scene cut, or two workers crossing while they look alike, can start a new ID or swap two IDs. Someone seen in only one check may be a brief appearance or a misdetection.
- Moondream's free tier rate-limits (HTTP 429). Requests are retried with backoff, and lowering `MAX_WORKERS` reduces how often this happens.

## Tests

```bash
python -m pytest -q   # offline, uses a fake Moondream model
```
