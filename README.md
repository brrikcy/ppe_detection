# PPE Detection (MVP)

Takes a construction-site video and produces an annotated copy where every worker gets a tag:

- **`#3 PPE OK`** (green box) when they're wearing all the required PPE
- **`#3 Missing: Helmet, Vest`** (red box) listing each missing item
- **`#3 OK? Helmet unseen`** (amber box) when an item couldn't be seen in most recent checks
- **`#3 Checking...`** (grey box) until a worker has enough checks for a verdict
- **`#3 Too far`** / **`#3 Too far (was PPE OK)`** (grey box) when a worker is too small to judge, with their last status if they had one

People are found and tracked locally on every frame with [Ultralytics](https://docs.ultralytics.com) YOLO11s and BoT-SORT. The PPE questions go to the [Moondream](https://moondream.ai) Cloud API.

It comes as a web app with a results dashboard (`run.py`) and a command-line tool (`annotate_video.py`). Both use the same pipeline in `ppe/`.

## How it works

1. **Every frame: find and track people** (`ppe/people.py`).
   - YOLO11s finds people (the frame is scaled to 640px for this), and BoT-SORT links them over time, so each worker keeps one ID.
   - BoT-SORT predicts motion with a Kalman filter and corrects for camera motion (turn this off with **Fixed camera**). It also uses weak detections to keep a partly hidden worker.
   - A lost worker keeps their ID for 8 seconds. Tracks younger than 0.3s are treated as flicker and not shown. When the detector misses someone for a few frames, their last box stays on screen for up to 0.5s instead of blinking off.
2. **Every check interval (default 1s): check PPE** (`ppe/analyzer.py`). Every tracked worker who has been on screen for at least 1 second is cropped, and Moondream `query` is asked a yes/no question for each required PPE item (or, optionally, one question for all items). These calls run in parallel.
   - Answers are read by their first word: "yes" or "no". Anything else, including "Not sure" and "Not visible", counts as unclear and follows the **unclear answer** setting (default: present).
   - A "no" answer is double-checked with `detect` on the crop. A detected box overrules the "no" only if it is on this worker and not on a neighbour, at least 60% of it lies in the item's part of the worker (e.g. the top third for a helmet), it has a plausible size for the item (a helmet-sized box is not a vest), and Moondream answers "yes" when asked about that box alone ("Is this a hard hat?"; a baseball cap fails this).
   - If the body part the item belongs on (e.g. the `head`) can't be found on this worker, because they are cut off or blocked from view, the item is recorded as `unverified` and isn't counted as a violation.
   - A "yes" for a worker whose box is more than 20% covered by other workers may be about the neighbour. It counts only if detect finds the item on this worker; otherwise the item is `unverified`.
   - A worker who appears between checks is checked as soon as they have been on screen for 1 second and are fully in frame, unless the next regular check is less than half a second (or half an interval) away. This arrival check is recorded in the report too, so someone who leaves before the next regular check still has a result.
   - Workers shorter than 8% of the frame height, or shorter than 100 pixels, are drawn grey ("Too far") and left out of the figures, with no API calls spent on them.
   - Calls are retried with back-off. If an item's calls still fail, only that item is recorded as failed and the worker's other items count. A worker whose check failed completely waits for the next regular check. After 5 failed checks in a row the run stops with the error. A rejected API key stops the run at once.
3. **Status** (`ppe/status.py`): an item is shown as missing when it was missing in most of the worker's last 3 checks, and in at least 2 of them, so one misread never turns a box red. A new worker shows "Checking..." until their second check. On a tie the previous state holds, so the box doesn't flip back and forth. The status after every check is stored in the report, and the dashboard uses the same status, so the video, the violation list and the People section agree.
4. Boxes and tags are drawn on every frame; tags are moved so they don't cover each other or the counts bar.
5. Frames are piped straight into ffmpeg (H.264, with the original audio), so the video is compressed once. It uses the system `ffmpeg` or the copy bundled with `imageio-ffmpeg`. A JSON report is written next to it (see [Report](#report)).

## Setup

Needs Python 3.10 or newer.

```bash
pip install -r requirements.txt
cp .env.example .env   # then put your key in MOONDREAM_API_KEY
curl -L --create-dirs -o models/yolo11s.pt https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s.pt
```

On Windows PowerShell, type `curl.exe` instead of `curl`; plain `curl` there is a different command. Use `copy` instead of `cp`.

The person model (about 19 MB) is loaded from `models/yolo11s.pt`; if it's missing, it is downloaded on first use, and if that fails the run stops with the download command. Set `PERSON_MODEL` in `.env`, or pick a model in Developer options, to use other YOLO weights. YOLO11n (`yolo11n.pt`, 5 MB) is faster but found 30% fewer people in our test video. Ultralytics is AGPL-3.0. It runs on the CPU unless PyTorch finds a GPU; on our test machine, tracking took about 170 ms per 1080p frame with YOLO11s (100 ms with YOLO11n).

If `opencv-python-headless` is already installed, Ultralytics' `opencv-python` dependency overwrites the same `cv2` files. Either keep one of the two, or install Ultralytics with `pip install --no-deps ultralytics` and add its other dependencies by hand.

### Settings (`.env`)

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `MOONDREAM_API_KEY` | — | both | Moondream key. In the web app it's the fallback when the form field is empty. |
| `MOONDREAM_ENDPOINT` | `https://api.moondream.ai/v1` | both | Base URL of the Moondream API: Moondream Cloud, another provider with the same API, or a self-hosted server. The API key is sent here. |
| `PERSON_MODEL` | `models/yolo11s.pt` | both | YOLO weights for person tracking (run from the project folder). |
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
3. While the run is processing, the page fills in live: the annotated video as it's being written (refreshed about twice a second) with a **Live** badge and the video time, progress with an estimate of the time left, what it's doing right now (e.g. "Asking Moondream about 3 workers at 0:04…", so a slow answer doesn't look like a freeze), running counts (violations, people, worker checks, compliance), the compliance chart growing column by column, violations sliding in as they're found (newest first), and a strip of the people found so far, coloured by status (click one to open their profile). The page is updated in place rather than redrawn, and images swap in once loaded, so nothing flickers. You can leave the page; the run keeps going.
4. When it finishes, the dashboard shows:
   - **Summary figures**: compliance (OK checks out of OK + violation checks; checks where an item couldn't be seen and first checks are left out and listed), violation incidents, share of time with a violation, and people tracked.
   - **A warning** when worker checks failed, and a stronger one when more than 10% failed, because the results are then incomplete.
   - **The annotated video**, with Video and Report download buttons. Below it: how many regular checks and arrival checks were made.
   - **Violations by type**, counted per incident, and items it couldn't verify. A low-coverage warning appears when an item could only be seen in a minority of checks, with a count of workers too far away to check.
   - **Compliance over time**: a chart of workers on screen at each regular check, split into PPE OK, item not visible, PPE violation, and not checked (too far, too new, or failed). Click a column to jump the video there.
   - **People**: one card per tracked worker with their ID, photo, status, time on screen and how often each item was missing in single checks. Click a card to open the worker's **profile**: their status, how many of their checks found each class (PPE OK, each missing item, each item not visible, failed checks), and a gallery of every photo taken of them at each check. Click a class to show only those photos, and a photo to enlarge it (with "Show in video").
     - **Flagged**: their status showed an item missing at some point (the same rule as the red box in the video), with the number of incidents and the time in violation. A violation of any length counts, even if the person was compliant most of the time.
     - **Not verified**: an item couldn't be seen in most of their checks.
     - **Seen briefly**: not checked often enough for a verdict.
     - **PPE OK**: otherwise. Single-check misreads the vote ignored are listed on the card.
   - **Violations**: one card per incident, i.e. a person whose status showed missing PPE, from the first to the last check in a row. Filterable by type. Click one to open the image viewer (← → to browse, full frame or worker crop, "Show in video").
5. Every run is listed in the sidebar, so you can switch between runs or delete them. **Run again** inspects the same video as a new run, with the same PPE items, interval and time limit, and either the current developer settings (to try out a change) or exactly the settings that run used. The new run links back to the original, which stays as it was. Runs made with non-default developer settings show a **Custom settings** chip; hover it to see what was changed.

### Developer options

**Developer options** in the sidebar tunes detection without touching code. Saved settings apply to new runs, and each run keeps a copy of the settings it used, so results stay reproducible. Settings are grouped by what they do, and a bar that stays at the top jumps between sections and holds **Save** and **Discard changes**. The **Layout** switch next to **Reset all to defaults** chooses between **Stacked** (the default: label above each control, settings in columns, all PPE items side by side) and **List** (one setting per row with the control on the right, PPE items as tabs); the choice is remembered in the browser. Click a section's title to fold or unfold it (or use **Expand all** / **Collapse all** in the top bar); a folded section shows how many of its settings differ from the defaults, the section links unfold the section they jump to, and folded sections are remembered too. Each setting shows a dot and a **Reset** link when it differs from the default.

- **Moondream prompts**: the question template (`{item}` is replaced by the item's wording), an option to ask about all items in one question per worker (fewer calls, possibly less reliable), and the re-check question for a detected box (`{term}`).
- **PPE items**: one card (or, in the List layout, one tab) per item with its wording, detect term, the body part that must be visible, the region of the worker's box where the item may be, and the plausible width and height of a detected box. A dot marks an item changed from the defaults.
- **Decision rules**:
  - what an unclear answer counts as (present, unverified or missing)
  - whether "no" answers are double-checked with detect, and whether a hidden body part then makes an item unverified
  - whether a "yes" is confirmed when workers overlap, and how much overlap triggers it
  - for both double-checks: how much of a detected box must be in the item's region, and whether Moondream is re-asked about the box
  - crop padding, and the minimum worker height to check (share of the frame and pixels)
- **Status and voting**: the vote window, how many missing checks turn a box red, how long a new person is tracked before their first check, whether newcomers wait until they're fully in frame, and how many failed checks in a row stop a run.

Settings that have no effect because another one is off are greyed out, with a note saying which.
- **Moondream requests**: the Moondream model (default `moondream3.1-9B-A2B`, sent with every request so results don't change when the server's default does), parallel requests and attempts per call. The endpoint is shown here read-only; change it with `MOONDREAM_ENDPOINT`.
- **Person tracking**: detection model (any `.pt` file in `models/`), input size, detection confidence, the confidence needed to start a new person, the minimum track age, how long a hidden worker keeps their ID, how long a missed box keeps being drawn, **Fixed camera**, and appearance matching (ReID; off by default, see [Known limitations](#known-limitations)).
- **Prompt playground**: pick a worker image from any completed run (or upload one), choose an item and run one check with the settings on the page, saved or not. A violation photo is re-checked exactly as in its run: on the original video frame, with the same crop, padding and neighbouring workers. Other images are checked on their own. It shows the image Moondream was sent, the exact question, Moondream's raw answer, each detect step with why each box did or didn't count, any re-check, the boxes on the image (green: counted, dashed: not counted), and the verdict. It uses 1–4 Moondream calls and saves nothing.

Settings are stored in `runs/_dev/tuning.json`. The command line can use the same file: `python annotate_video.py site.mp4 --tuning runs/_dev/tuning.json`.

Runs execute one at a time; any others wait as *queued*. Restarting the server marks unfinished runs as failed. The annotated video is H.264, which plays in Chrome, Edge, Safari and Firefox. Videos from older runs saved in another format are converted the first time they're opened.

Each run is saved in `runs/<id>/`:

```
meta.json        status, options, progress (never the API key)
input.<ext>      the uploaded video
annotated.mp4    the output video
annotated.json   the full per-check report
preview.jpg      latest checked frame, shown while running
snapshots/       crop of each checked worker, per check (for the people profiles)
frames/          full annotated frame (as drawn in the video) for each check with a violation
people/          best view of each tracked worker, <id>.jpg
```

### Report

`annotated.json` holds the run's settings, `worker_checks` and `failed_checks` counts, and a `timeline` with one entry per check:

```json
{"frame": 30, "time_sec": 1.0, "arrival": false,
 "workers": [{"track_id": 3, "box": [412, 180, 590, 760], "label": "PPE Violation (1) - No Helmet",
              "violations": ["No Helmet"], "unverified": [], "too_small": false, "pending": false,
              "failed": [], "check_failed": false,
              "confirmed": true, "shown": ["No Helmet"], "shown_unverified": []}]}
```

- `violations`, `unverified` and `failed` are what this one check found. `shown` and `shown_unverified` are the worker's voted status after it (what the video shows), and `confirmed` says whether they have had enough checks for a verdict.
- **Regular checks** (`"arrival": false`) list everyone on screen. Workers too far away to check have `"too_small": true`, and workers too new to check have `"pending": true`; neither has a verdict. A check where every Moondream call failed has `"check_failed": true`.
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
| GET | `/api/runs/{id}/persons/{track_id}` | A tracked person's profile: every check with its photo, and counts per class |
| POST | `/api/runs/{id}/rerun` | JSON `{"settings": "current" or "original", "api_key"?}`: inspect the run's video again as a new run |
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
  static/               index.html, styles.css, js/ (api, ui, charts, lightbox, profile, views/: newRun, runView, devView)
ppe/                    detection pipeline (shared with the command line)
  config.py             PPE items and pipeline settings
  tuning.py             developer settings: prompts, decision rules, requests, tracking (validated)
  people.py             YOLO11s + BoT-SORT person tracking, every frame
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
python annotate_video.py site.mp4 --model moondream3-preview      # another Moondream model
```

Supported PPE items: `helmet`, `vest`, `gloves`, `boots`, `goggles`, `mask` (default: `helmet,vest`).

**Cost:** each check makes `workers × required_items` API calls (or one per worker with **One question for all items**), plus 1–3 confirmation calls for each "no" answer, plus 1–2 for each "yes" from a worker who overlaps another, plus one extra check for each worker who appears between checks. A 60s clip with 5 workers, checking helmet and vest at 1s intervals, makes about 600–800 calls.

## Moondream model and endpoint

Every request names its model (`moondream3.1-9B-A2B` by default; change it in Developer options or with `--model`). If the endpoint doesn't accept the model, a run is refused at the start (the key check sends one request with the same model), and a run that hits it mid-way stops with a message pointing to Developer options. Each run records the endpoint and model it used (`moondream_endpoint` and `tuning.moondream_model` in the report; the model is also shown on the run page).

**Moondream Cloud is closing on 13 November 2026** ([Moondream's announcement](https://moondream.ai/blog/moondream-is-joining-prime-intellect)). To keep using Moondream after that, point `MOONDREAM_ENDPOINT` at a service with the same API, such as Prime Intellect's hosted Moondream, or a self-hosted Moondream server, and set the model ID that service uses.

## Known limitations

- Masks, goggles, gloves and boots often can't be seen. The dashboard warns when an item could only be checked in a minority of checks.
- Someone hidden for more than 8 seconds, or who comes out from behind another worker far from where they went in, gets a new ID.
- When two workers cross, their IDs can swap, and each then carries the other's recent checks for a few checks. In our crossing test, BoT-SORT's appearance matching (the **ReID** developer option) didn't prevent this and made tracking about 40% slower, so it is off by default. Comparing clothing colours couldn't tell people apart reliably either: two different workers differed less than one worker did over a second of camera movement.
- With a camera moving through a group, people at the frame edges are detected on and off, which starts new IDs.
- Tracking assumes a continuous shot. A scene cut, or two workers crossing while they look alike, can start a new ID or swap two IDs.
- Moondream's free tier rate-limits (HTTP 429). Requests are retried with backoff, and lowering parallel requests (developer options, or `MAX_WORKERS` on the command line) reduces how often this happens.

## Tests

```bash
python -m pytest -q   # offline, uses a fake Moondream model and a fake person tracker
```
