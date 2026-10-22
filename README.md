# PPE Detection (MVP)

Takes a construction-site video and produces an annotated copy where every worker gets a tag:

- **`PPE OK`** (green box) when they're wearing all the required PPE
- **`PPE Violation (N) - No Helmet, No Vest`** (red box) listing each missing item

All vision runs on the [Moondream](https://moondream.ai) Cloud API.

## How it works

1. A frame is sampled every `ANALYSIS_INTERVAL_SEC` seconds (default 1s).
2. Moondream `detect("person")` finds every worker in the frame.
3. Each worker is cropped, and Moondream `query` is asked a yes/no question for each required PPE item. These calls run in parallel.
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

**Cost:** each analysed frame makes `1 + workers × required_items` API calls. A 60s clip with 5 workers, checking helmet and vest at 1s intervals, makes about 660 calls.

## Tests

```bash
python -m pytest -q   # offline, uses a fake Moondream model
```
