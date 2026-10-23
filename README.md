# PPE Detection (MVP)

Takes a construction-site video and produces an annotated copy where every worker gets a tag:

- **`#3 PPE OK`** (green box) when they're wearing all the required PPE
- **`#3 Missing: Helmet, Vest`** (red box) listing each missing item
- **`#3 OK? Helmet unseen`** (amber box) when an item couldn't be seen at the last check
- **`#3 Checking...`** / **`#3 Too far`** (grey box) before the first check, or when a worker is too small to judge

People are found and tracked locally on every frame with [Ultralytics](https://docs.ultralytics.com) YOLO11n and BoT-SORT. The PPE questions go to the [Moondream](https://moondream.ai) Cloud API.

It comes as a web app with a results dashboard (`run.py`) and a command-line tool (`annotate_video.py`). Both use the same pipeline in `ppe/`.

## How it works

1. **Every frame: find and track people** (`ppe/people.py`).
   - YOLO11n finds people (the frame is scaled to 640px for this), and BoT-SORT links them over time, so each worker keeps one ID.
   - BoT-SORT predicts motion with a Kalman filter and corrects for camera motion. It also uses weak detections to keep a partly hidden worker.
   - A lost worker keeps their ID for 8 seconds. Tracks younger than 0.3s are treated as flicker and not shown or checked.
2. **Every check interval (default 1s): check PPE** (`ppe/analyzer.py`). Every tracked worker is cropped, and Moondream `query` is asked a yes/no question for each required PPE item. These calls run in parallel.
   - A "no" answer is double-checked with `detect` on the crop. If the item is found on this worker (e.g. a `hard hat` near the top of their box, not on a neighbour who overlaps the crop), it counts as present.
   - If the body part it belongs on (e.g. the `head`) can't be found on this worker, because they are cut off or blocked from view, the item is recorded as `unverified` in the JSON report and isn't counted as a violation.
   - A worker who appears between checks is checked straight away, unless the next check is less than half an interval away. This arrival check is recorded in the report too, so someone who leaves before the next regular check still has a status.
   - Workers shorter than 8% of the frame height are drawn grey ("Too far") and left out of the figures, with no API calls spent on them.
   - If a check fails after retries, that frame is skipped rather than recorded with old results. A rejected API key stops the run at once.
3. **Status shown in the video** (`ppe/status.py`): an item is shown as missing when it was missing in most of the worker's last 3 checks. On a tie the previous state holds, so one misread doesn't flip the box.
4. Boxes and tags are drawn on every frame; tags are moved so they don't cover each other or the counts bar.
5. Frames are piped straight into ffmpeg (H.264, with the original audio), so the video is compressed once. It uses the system `ffmpeg` or the copy bundled with `imageio-ffmpeg`. A JSON report is written next to it (see [Report](#report)).

## Setup

Needs Python 3.10 or newer.

```bash
pip install -r requirements.txt
cp .env.example .env   # then put your key in MOONDREAM_API_KEY
curl -L --create-dirs -o models/yolo11n.pt https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt
```

On Windows PowerShell, type `curl.exe` instead of `curl`; plain `curl` there is a different command. Use `copy` instead of `cp`.

The person model (about 5 MB) is loaded from `models/yolo11n.pt`; set `PERSON_MODEL` in `.env` to use other YOLO weights. If the file is missing, runs fail with a message showing the download command. Ultralytics is AGPL-3.0. It runs on the CPU unless PyTorch finds a GPU; on a laptop CPU, tracking takes about 70 ms per frame for 1080p and 150 ms for 4K.

If `opencv-python-headless` is already installed, Ultralytics' `opencv-python` dependency overwrites the same `cv2` files. Either keep one of the two, or install Ultralytics with `pip install --no-deps ultralytics` and add its other dependencies by hand.

### Settings (`.env`)

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `MOONDREAM_API_KEY` | — | both | Moondream key. In the web app it's the fallback when the form field is empty. |
| `PERSON_MODEL` | `models/yolo11n.pt` | both | YOLO weights for person tracking (run from the project folder). |
| `MAX_WORKERS` | `4` | command line | Parallel Moondream requests. In the web app this is a developer option. |
| `REQUIRED_PPE` | `helmet,vest` | command line | PPE items to check. The web app takes them from the form. |
| `ANALYSIS_INTERVAL_SEC` | `1.0` | command line | Seconds between PPE checks. The web app takes it from the form. |
| `PPE_DATA_DIR` | `runs/` | web app | Where runs are saved. |
| `MAX_UPLOAD_MB` | `1024` | web app | Largest video the web app accepts. |

## Web app

```bash
python run.py          # opens http://127.0.0.1:8000
python run.py --port 8080 --no-browser
```

1. Optionally enter a Moondream API key. If you leave it empty, the server uses `MOONDREAM_API_KEY` from `.env`. **Test key** checks it with one call. The key is checked again when you start a run, so a wrong key shows on the form instead of creating a failed run. Keys are never written to disk.
2. Drop in a video, choose the required PPE, the check interval (0.5, 1, 2 or 5s) and optionally a time limit, then click **Run inference**. The form shows an estimate of the Moondream calls.
3. While the run is processing, you see live progress, the latest checked frame and the timeline so far. You can leave the page; the run keeps going.
4. When it finishes, the dashboard shows:
   - **Summary figures**: compliance, violations, share of time with a violation, and people tracked.
   - **The annotated video**, with Video and Report download buttons. Below it: how many regular checks and arrival checks were made.
   - **Violations by type**, and items it couldn't verify. A low-coverage warning appears when an item could only be seen in a minority of checks, with a count of workers too far away to check.
   - **Compliance over time**: a chart of workers on screen at each regular check, split into PPE OK, PPE violation and too far to check. Click a column to jump the video there.
   - **People**: one card per tracked worker with their ID, photo, overall status, time on screen and how often each item was missing. Click a card to see that person's images.
     - **Flagged**: an item was missing in more than half of their checks.
     - **Occasional**: violations in fewer than half of their checks.
     - **Always OK**: no violations in any check.
   - **Violations**: a snapshot card for every flagged worker, filterable by type. Click one to open the image viewer (← → to browse, full frame or worker crop, "Show in video").
5. Every run is listed in the sidebar, so you can switch between runs or delete them. Runs made with non-default developer settings show a **Custom settings** chip; hover it to see what was changed.

### Developer options

**Developer options** in the sidebar tunes detection without touching code. Saved settings apply to new runs, and each run keeps a copy of the settings it used, so results stay reproducible. Each setting shows a dot and a **Reset** link when it differs from the default.

- **Moondream prompts**: the question template (`{item}` is replaced by the item's wording), and per item: the wording, the detect term used to overrule a "no", the body part that must be visible, and the region of the worker's box where the item may be.
- **Decision rules**:
  - what an unclear answer counts as (present, unverified or missing)
  - whether "no" answers are double-checked with detect, and whether a hidden body part makes an item unverified
  - crop padding and the minimum worker height to check
  - the video vote window, and the share of checks that flags a person in the People section
- **Moondream requests**: parallel requests and attempts per call.
- **Person tracking**: detection model (any `.pt` file in `models/`), input size, detection confidence, the confidence needed to start a new person, the minimum track age, and how long a hidden worker keeps their ID.
- **Prompt playground**: pick a worker image from any completed run (or upload one), choose an item and run one check with the settings on the page, saved or not. It shows the exact question, Moondream's raw answer, each detect step, the boxes detect found on the image (green: on this worker, dashed: outside the item's region), and the verdict. It uses 1–3 Moondream calls and saves nothing.

Settings are stored in `runs/_dev/tuning.json`. The command line can use the same file: `python annotate_video.py site.mp4 --tuning runs/_dev/tuning.json`.

Runs execute one at a time; any others wait as *queued*. Restarting the server marks unfinished runs as failed. The annotated video is H.264, which plays in Chrome, Edge, Safari and Firefox. Videos from older runs saved in another format are converted the first time they're opened.

Each run is saved in `runs/<id>/`:

```
meta.json        status, options, progress (never the API key)
input.<ext>      the uploaded video
annotated.mp4    the output video
annotated.json   the full per-check report
preview.jpg      latest checked frame, shown while running
snapshots/       crop of each flagged worker per check
frames/          full annotated frame for each check with a violation
people/          best view of each tracked worker, <id>.jpg
```

### Report

`annotated.json` holds the run's settings and a `timeline` with one entry per check:

```json
{"frame": 30, "time_sec": 1.0, "arrival": false,
 "workers": [{"track_id": 3, "box": [412, 180, 590, 760], "label": "PPE Violation (1) - No Helmet",
              "violations": ["No Helmet"], "unverified": [], "too_small": false}]}
```

- **Regular checks** (`"arrival": false`) list everyone on screen. Workers too far away to check have `"too_small": true` and no verdict.
- **Arrival checks** (`"arrival": true`) list only the workers who appeared since the last regular check.
- `frames_analysed` counts regular checks; `arrival_checks` counts the others. Both kinds count towards the compliance figures, the People section and the violation cards. Only regular checks are used for the timeline chart and the per-frame figures (time with a violation, workers on screen).

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Server is up |
| GET | `/api/config` | PPE items, defaults, whether the server has a key, upload limit |
| POST | `/api/key/check` | Verify a key (form field `api_key`, falls back to the server key) |
| POST | `/api/runs` | Multipart: `video`, `api_key?`, `required_ppe`, `analysis_interval_sec`, `max_seconds?`. Returns 401 for a wrong key. |
| GET | `/api/runs` | All runs, newest first |
| GET | `/api/runs/{id}` | Status, progress and dashboard summary (partial while running) |
| GET | `/api/runs/{id}/video` · `/report` · `/preview` · `/snapshots/{name}` · `/frames/{name}` · `/people/{name}` | Media |
| DELETE | `/api/runs/{id}` | Delete a finished run |
| GET · PUT · DELETE | `/api/dev/tuning` | Read, save (JSON body) or reset the developer settings |
| POST | `/api/dev/try` | Multipart: `item`, `tuning?` (JSON), `api_key?`, and `image` or `run_id` + `folder` + `name`. Returns each step of one check. |

Interactive docs: http://127.0.0.1:8000/docs

### Code layout

```
run.py                  starts the web app
annotate_video.py       command-line tool
app/
  main.py               create_app(): wires settings, services, routers, static files
  core/settings.py      server settings from env/.env
  schemas/runs.py       Pydantic request/response models
  services/
    run_store.py        filesystem persistence of runs (meta.json never holds the key)
    runner.py           background execution, progress, preview, snapshots and person photos
    summary.py          dashboard metrics and per-person status from the report's timeline
    keys.py             form key -> server key fallback, key check
    media.py            converts older runs' videos for browser playback
    dev_settings.py     saved developer settings (runs/_dev/tuning.json)
  api/deps.py           dependency providers
  api/routes/           config.py, runs.py, dev.py
  static/               index.html, styles.css, js/ (api, ui, charts, lightbox, views/: newRun, runView, devView)
ppe/                    detection pipeline (shared with the command line)
  config.py             PPE items and pipeline settings
  tuning.py             developer settings: prompts, decision rules, requests, tracking (validated)
  people.py             YOLO11n + BoT-SORT person tracking, every frame
  analyzer.py           Moondream PPE checks on tracked workers
  moondream_client.py   Moondream Cloud REST client (detect, query)
  status.py             per-worker status voted over recent checks
  video.py              the frame loop: track, check, draw, encode
  annotate.py           boxes and tags
  media.py              ffmpeg output
```

## Command line

```bash
python annotate_video.py site.mp4                       # -> outputs/site_annotated.mp4 + .json
python annotate_video.py site.mp4 --interval 0.5        # check PPE twice a second
python annotate_video.py site.mp4 --required helmet,vest,gloves
python annotate_video.py site.mp4 --max-seconds 10      # quick trial on the first 10s
python annotate_video.py site.mp4 -o out.mp4 --workers 2 -v
python annotate_video.py site.mp4 --tuning runs/_dev/tuning.json   # use the web app's developer settings
```

Supported PPE items: `helmet`, `vest`, `gloves`, `boots`, `goggles`, `mask` (default: `helmet,vest`).

**Cost:** each check makes `workers × required_items` API calls, plus 1–2 confirmation calls for each "no" answer, plus one extra check for each worker who appears between checks. A 60s clip with 5 workers, checking helmet and vest at 1s intervals, makes about 600–700 calls.

## Known limitations

- Masks, goggles, gloves and boots often can't be seen. The dashboard warns when an item could only be checked in a minority of checks.
- Someone hidden for more than 8 seconds, or who comes out from behind another worker far from where they went in, gets a new ID. BoT-SORT's appearance ReID didn't help on our test videos, so it is off.
- With a camera moving through a group, people at the frame edges are detected on and off, which starts new IDs.
- Tracking assumes a continuous shot. A scene cut, or two workers crossing while they look alike, can start a new ID or swap two IDs.
- Moondream's free tier rate-limits (HTTP 429). Requests are retried with backoff, and lowering parallel requests (developer options, or `MAX_WORKERS` on the command line) reduces how often this happens.

## Tests

```bash
python -m pytest -q   # offline, uses a fake Moondream model and a fake person tracker
```
