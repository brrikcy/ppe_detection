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
4. Boxes and tags are drawn on every frame. Between sampled frames, the most recent results are held.
5. The output is re-encoded to H.264 with the original audio (if `ffmpeg` is installed), and a JSON report is written next to it.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then put your key in MOONDREAM_API_KEY
```

## Usage

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
- Each checked frame is analysed independently, so tags can flicker between seconds. Requiring a violation to persist across consecutive checks would fix this.
- Moondream's free tier rate-limits (HTTP 429). Requests are retried with backoff, and lowering `MAX_WORKERS` reduces how often this happens.

## Tests

```bash
python -m pytest -q   # offline, uses a fake Moondream model
```
