// The person detection settings, described once for the Developer options "Person tracking"
// card and for the person detection playground (which tries them without saving).

export const f = (key, type, label, hint, extra = {}) => ({ key, type, label, hint, ...extra });

export const TRACKING_GROUPS = [
  { title: "Finding people", fields: [
    f("person_model", "model", "Detection model", "Bigger models find more partly hidden workers but are slower. Put .pt, .engine (TensorRT) or .onnx files in models/."),
    f("detect_fps", "number", "Detect people",
      "How often the person detector runs; in between, boxes move on at their last speed. The detector is the slowest part of a run: 10 a second is about 3× faster than every frame of a 30 fps video. 0 = every frame.",
      { min: 0, max: 60, step: 1, unit: "times a second" }),
    f("detector_imgsz", "select", "Detector input size", "Frames are scaled to this. Larger finds smaller people but is slower.",
      { numeric: true, options: [320, 480, 640, 800, 960, 1280, 1600, 1920].map((v) => [v, `${v} px`]) }),
    f("detector_conf", "number", "Detection confidence", "Weakest detection the tracker considers. Low values help keep partly hidden workers.",
      { min: 0.01, max: 0.9, step: 0.01 }),
  ] },
  { title: "Keeping IDs", fields: [
    f("new_track_thresh", "number", "Alpha",
      "Confidence a detection needs to start a new person. Lower tracks more hard-to-see workers but creates more short, spurious IDs. Also set on the New inspection page.",
      { min: 0.05, max: 0.95, step: 0.05 }),
    f("min_track_age_sec", "number", "Ignore tracks younger than",
      "Filters flicker: a track must last this long before its box is drawn. Checks wait longer (see “Check a new person after”).",
      { min: 0, max: 3, step: 0.1, unit: "s" }),
    f("hidden_sec", "number", "Keep a hidden worker's ID for",
      "Someone out of view for less than this keeps their ID when they reappear near where they were lost.",
      { min: 0, max: 60, step: 1, unit: "s" }),
    f("coast_sec", "number", "Keep drawing a missed box for",
      "When the detector misses someone for a few frames, their last box stays on screen instead of blinking off. Not checked meanwhile.",
      { min: 0, max: 3, step: 0.1, unit: "s" }),
  ] },
  { title: "Camera and appearance", fields: [
    f("fixed_camera", "toggle", "Fixed camera", "Skips camera-motion compensation: faster, and steadier boxes when the camera doesn't move."),
    f("with_reid", "toggle", "Compare appearance (ReID)",
      "Also matches people by appearance (models/yolo11n-cls.pt, downloaded on first use). About 40% slower per frame; in our tests it didn't prevent ID swaps when workers cross."),
  ] },
];

// Not tracking, but they decide which of the people found get a PPE check, so they matter
// when judging a detector: a person who is found but never checked is a missed inspection.
export const CHECK_GROUP = { title: "Who gets a PPE check", fields: [
  f("min_check_height_frac", "number", "Minimum worker height",
    "Shorter workers are “too far” and not checked.", { min: 0, max: 0.5, step: 0.01, unit: "× frame" }),
  f("min_check_height_px", "number", "…and at least",
    "The larger of the two limits applies.", { min: 0, max: 2000, step: 10, unit: "px" }),
  f("min_check_age_sec", "number", "Check a new person after",
    "Tracks younger than this aren't checked.", { min: 0, max: 10, step: 0.1, unit: "s" }),
] };

export const PERSON_KEYS = [...TRACKING_GROUPS, CHECK_GROUP].flatMap((g) => g.fields.map((x) => x.key));
