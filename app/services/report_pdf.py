"""The camera wall's PDF report: what was watched, how it went, and every violation with its photo."""

from __future__ import annotations

import io
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.graphics.shapes import Drawing, Line, PolyLine, Rect, String
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

INK = colors.HexColor("#1b1f1a")
MUTED = colors.HexColor("#6f786c")
LINE = colors.HexColor("#d6dbd2")
SOFT = colors.HexColor("#f3f5f1")
RED = colors.HexColor("#c0302c")
GREEN = colors.HexColor("#1f8f2e")
YELLOW = colors.HexColor("#e8c400")

MAX_ROWS = 300  # violations listed
MAX_PHOTOS = 80  # of those, how many get a photo (keeps the file small)
PHOTO_W, PHOTO_H = 22 * mm, 26 * mm


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]
    mk = lambda name, **kw: ParagraphStyle(name, parent=base, **{"textColor": INK, **kw})  # noqa: E731
    return {
        "title": mk("title", fontName="Helvetica-Bold", fontSize=24, leading=28),
        "sub": mk("sub", fontSize=10, leading=14, textColor=MUTED),
        "h2": mk("h2", fontName="Helvetica-Bold", fontSize=13, leading=16, spaceBefore=14, spaceAfter=6),
        "cell": mk("cell", fontSize=9, leading=12, alignment=TA_LEFT),
        "cellb": mk("cellb", fontName="Helvetica-Bold", fontSize=9, leading=12),
        "head": mk("head", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=MUTED),
        "bad": mk("bad", fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=RED),
        "small": mk("small", fontSize=8, leading=11, textColor=MUTED),
        "kpi": mk("kpi", fontName="Helvetica-Bold", fontSize=22, leading=24),
        "kpil": mk("kpil", fontSize=8, leading=10, textColor=MUTED),
    }


def _when(iso: str | None) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y, %H:%M:%S")


def _clock(sec: float | None) -> str:
    sec = int(round(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


def _table(rows, widths, st, header=True, zebra=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
             ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), SOFT), ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK)]
    t.setStyle(TableStyle(style))
    return t


BLUE = colors.HexColor("#2f5f8a")
PANEL_H = 52 * mm


def _clock_label(t: float, bucket: int) -> str:
    fmt = "%H:%M:%S" if bucket < 60 else "%H:%M"
    return datetime.fromtimestamp(t).strftime(fmt)


def _axes(d: Drawing, x0, y0, w, h, max_v, points, bucket):
    """Grid lines with values on the left, time labels along the bottom."""
    for v in (0, max_v / 2, max_v):
        y = y0 + (v / max_v) * h
        d.add(Line(x0, y, x0 + w, y, strokeColor=LINE, strokeWidth=0.5))
        d.add(String(x0 - 4, y - 2.5, f"{v:g}", fontName="Helvetica", fontSize=7, fillColor=MUTED, textAnchor="end"))
    if points:
        idx = sorted({0, (len(points) - 1) // 2, len(points) - 1})
        for i in idx:
            x = x0 + (w / 2 if len(points) == 1 else i / (len(points) - 1) * w)
            anchor = "start" if i == 0 and len(idx) > 1 else "end" if i == len(points) - 1 and len(idx) > 1 else "middle"
            d.add(String(x, y0 - 10, _clock_label(points[i]["t"], bucket), fontName="Helvetica", fontSize=7,
                         fillColor=MUTED, textAnchor=anchor))


def _violations_chart(timeline: dict, width: float) -> Drawing:
    pts, bucket = timeline["points"], timeline["bucket_sec"]
    d = Drawing(width, PANEL_H)
    x0, y0, w, h = 22, 16, width - 30, PANEL_H - 24
    max_v = max(4, max((p["violations"] for p in pts), default=0))
    max_v = max_v if max_v <= 8 else -(-max_v // 5) * 5
    _axes(d, x0, y0, w, h, max_v, pts, bucket)
    slot = w / max(1, len(pts))
    bw = max(1.5, min(14.0, slot * 0.7))
    for i, p in enumerate(pts):
        if p["violations"]:
            d.add(Rect(x0 + i * slot + (slot - bw) / 2, y0, bw, p["violations"] / max_v * h, fillColor=RED, strokeColor=None))
    return d


def _workers_chart(timeline: dict, width: float) -> Drawing:
    pts, bucket = timeline["points"], timeline["bucket_sec"]
    d = Drawing(width, PANEL_H)
    x0, y0, w, h = 22, 16, width - 30, PANEL_H - 34  # room above the plot for the legend
    peak = max((max(p["workers"] or 0, p["missing"] or 0) for p in pts), default=0)
    max_v = max(4, peak)
    max_v = max_v if max_v <= 8 else -(-max_v // 5) * 5
    _axes(d, x0, y0, w, h, max_v, pts, bucket)
    for key, color in (("workers", BLUE), ("missing", RED)):
        xy = []
        for i, p in enumerate(pts):
            if p[key] is not None:
                xy += [x0 + (w / 2 if len(pts) == 1 else i / (len(pts) - 1) * w), y0 + p[key] / max_v * h]
        if len(xy) >= 4:
            d.add(PolyLine(xy, strokeColor=color, strokeWidth=1.6))
        elif len(xy) == 2:
            d.add(Rect(xy[0] - 2, xy[1] - 2, 4, 4, fillColor=color, strokeColor=None))
    d.add(Rect(x0 + w - 150, PANEL_H - 6, 8, 3, fillColor=BLUE, strokeColor=None))
    d.add(String(x0 + w - 139, PANEL_H - 8, "Workers on screen", fontName="Helvetica", fontSize=7, fillColor=MUTED))
    d.add(Rect(x0 + w - 62, PANEL_H - 6, 8, 3, fillColor=RED, strokeColor=None))
    d.add(String(x0 + w - 51, PANEL_H - 8, "Missing PPE", fontName="Helvetica", fontSize=7, fillColor=MUTED))
    return d


def _hbar_chart(rows: list[tuple[str, float, str]], width: float, color=RED) -> Drawing:
    """rows: (label, value, text at the end of the bar)."""
    row_h = 14
    d = Drawing(width, max(1, len(rows)) * row_h + 2)
    top = max(1, len(rows)) * row_h
    peak = max([v for _, v, _ in rows] + [1])
    label_w, text_w = 70 * mm, 28 * mm
    track = width - label_w - text_w
    for i, (label, v, text) in enumerate(rows):
        y = top - (i + 1) * row_h + 3
        d.add(String(0, y + 1.5, label[:34], fontName="Helvetica", fontSize=8, fillColor=INK))
        d.add(Rect(label_w, y, track, 7, fillColor=SOFT, strokeColor=None))
        d.add(Rect(label_w, y, max(1.5, v / peak * track) if v else 0, 7, fillColor=color, strokeColor=None))
        d.add(String(label_w + track + 6, y + 1, text, fontName="Helvetica", fontSize=8, fillColor=MUTED))
    return d


def _analytics_story(a: dict, st: dict, width: float) -> list:
    k = a["kpis"]
    story = [Paragraph("Analytics", st["h2"])]
    if not a["timeline"]["points"]:
        return story + [Paragraph("Not enough data yet: analytics appear once cameras have been running for a moment.", st["cell"])]
    since = datetime.fromtimestamp(a["since"]).strftime("%d %b %Y, %H:%M:%S")
    story.append(Paragraph(f"Since {since}, {_clock(a['duration_sec'])} monitored.", st["small"]))
    story.append(Spacer(1, 4))
    figs = [("Violations", str(k["violations"]) + (f" ({k['per_hour']} an hour)" if k["per_hour"] is not None else "")),
            ("Compliance", "-" if k["compliance"] is None else f"{round(k['compliance'] * 100)}% of {k['checks']} checks"),
            ("Workers tracked", f"{k['workers_tracked']} (up to {k['peak_workers']} on screen at once)"),
            ("Peak missing PPE", f"{k['peak_missing']} workers at the same time"),
            ("Most violations", k["worst_camera"] or "none yet")]
    story.append(_table([[Paragraph(x.upper(), st["head"]), Paragraph(v, st["cell"])] for x, v in figs],
                        [40 * mm, width - 40 * mm], st, header=False))
    story.append(Spacer(1, 8))
    story.append(KeepTogether([Paragraph("Violations over time", st["cellb"]),
                               _violations_chart(a["timeline"], width)]))
    story.append(Spacer(1, 6))
    story.append(KeepTogether([Paragraph("Workers on screen vs missing PPE", st["cellb"]),
                               _workers_chart(a["timeline"], width)]))
    story.append(Spacer(1, 6))
    if a["by_type"]:
        story.append(KeepTogether([Paragraph("Violations by type", st["cellb"]), Spacer(1, 3),
                                   _hbar_chart([(t["label"], t["count"], str(t["count"])) for t in a["by_type"]], width)]))
        story.append(Spacer(1, 6))
    if a["by_camera"]:
        rows = [(c["name"], c["violations"],
                 f"{c['violations']}" + ("" if c["compliance"] is None else f" - {round(c['compliance'] * 100)}% compliant"))
                for c in a["by_camera"]]
        story.append(KeepTogether([Paragraph("Violations by camera", st["cellb"]), Spacer(1, 3), _hbar_chart(rows, width)]))
    return story


def build_camera_report(state: dict, info: dict, photo: Callable[[dict], Path | None],
                        generated: datetime | None = None) -> bytes:
    """The report as PDF bytes.

    `state` is CameraManager.state(); `info` holds what the cameras were started with (required_ppe,
    analysis_interval_sec, settings_state; any may be missing); `photo(event)` gives the path of
    an event's worker photo, or None.
    """
    st = _styles()
    generated = generated or datetime.now().astimezone()
    cameras, totals, events = state["cameras"], state["totals"], state["violations"][:MAX_ROWS]
    all_events = state["violations"]
    shown = [c for c in cameras if c["active"] or any(e["camera_id"] == c["id"] for e in all_events)]

    buf = io.BytesIO()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, "PPE Inspector - camera wall report")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(YELLOW)
        canvas.setLineWidth(2)
        canvas.line(18 * mm, A4[1] - 12 * mm, A4[0] - 18 * mm, A4[1] - 12 * mm)
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=20 * mm,
                            bottomMargin=18 * mm, title="PPE detection report - camera wall",
                            author="PPE Inspector")
    width = A4[0] - 36 * mm
    story = [Paragraph("PPE detection report", st["title"]),
             Paragraph(f"Live camera wall &nbsp;|&nbsp; generated {generated.strftime('%d %b %Y, %H:%M')}", st["sub"]),
             Spacer(1, 10)]

    # ---- summary figures ----
    judged = [c["stats"] for c in shown if c.get("stats") and c["stats"].get("compliance") is not None]
    compliance = (sum(s["compliance"] * s.get("checks", 0) for s in judged) / max(1, sum(s.get("checks", 0) for s in judged))
                  if judged else None)
    kpis = [(str(totals["live"]), "cameras live"), (str(len(shown)), "cameras in this report"),
            (str(len(all_events)), "violations logged"),
            ("-" if compliance is None else f"{round(compliance * 100)}%", "compliance, current loops")]
    kpi_row = [[Table([[Paragraph(v, ParagraphStyle("k", parent=st["kpi"], textColor=RED if label == "violations logged" and v != "0" else INK))],
                       [Paragraph(label.upper(), st["kpil"])]], colWidths=[width / 4 - 6])
                for v, label in kpis]]
    t = Table(kpi_row, colWidths=[width / 4] * 4)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
                           ("BACKGROUND", (0, 0), (-1, -1), SOFT), ("TOPPADDING", (0, 0), (-1, -1), 8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [t, Spacer(1, 6)]

    # ---- how it was run ----
    ppe = ", ".join(i.capitalize() for i in info.get("required_ppe", [])) or "-"
    rows = [[Paragraph("Required PPE", st["head"]), Paragraph(ppe, st["cell"])],
            [Paragraph("Each worker checked", st["head"]),
             Paragraph(f"every {info['analysis_interval_sec']:g} s" if info.get("analysis_interval_sec") else "-", st["cell"])]]
    if info.get("settings_state"):
        rows.append([Paragraph("Settings state", st["head"]),
                     Paragraph({"1": "State 1 (initial prompt)", "2": "State 2 (current prompt)"}.get(
                         info["settings_state"], info["settings_state"]), st["cell"])])
    story += [Paragraph("Setup", st["h2"]), _table(rows, [40 * mm, width - 40 * mm], st, header=False)]

    # ---- by type ----
    by_type = Counter(v for e in all_events for v in e["violations"])
    story.append(Paragraph("Violations by type", st["h2"]))
    if by_type:
        total = sum(by_type.values())
        rows = [[Paragraph("MISSING", st["head"]), Paragraph("VIOLATIONS", st["head"]), Paragraph("SHARE", st["head"])]]
        rows += [[Paragraph(k, st["cellb"]), Paragraph(str(n), st["cell"]), Paragraph(f"{round(100 * n / total)}%", st["cell"])]
                 for k, n in by_type.most_common()]
        story.append(_table(rows, [width * 0.5, width * 0.25, width * 0.25], st))
    else:
        story.append(Paragraph("No violations were detected.", st["cell"]))

    # ---- by camera ----
    story.append(Paragraph("Cameras", st["h2"]))
    if shown:
        head = ["CAMERA", "STATUS", "LOOPS", "WORKERS", "CHECKS", "COMPLIANCE", "VIOLATIONS"]
        rows = [[Paragraph(x, st["head"]) for x in head]]
        for c in shown:
            s = c.get("stats") or {}
            n = sum(1 for e in all_events if e["camera_id"] == c["id"])
            status = "Live" if c["active"] and c.get("state") == "live" else "Starting" if c["active"] else "Removed"
            comp = s.get("compliance")
            rows.append([Paragraph(c["name"], st["cellb"]), Paragraph(status, st["cell"]),
                         Paragraph(str(c.get("loop", "-")) if c["active"] else "-", st["cell"]),
                         Paragraph(str(s.get("people", "-")) if s else "-", st["cell"]),
                         Paragraph(str(s.get("checks", "-")) if s else "-", st["cell"]),
                         Paragraph("-" if comp is None else f"{round(comp * 100)}%", st["cell"]),
                         Paragraph(str(n), st["bad"] if n else st["cell"])])
        story.append(_table(rows, [width * f for f in (0.24, 0.13, 0.1, 0.13, 0.12, 0.15, 0.13)], st))
        story.append(Paragraph("Workers, checks and compliance are for the loop of the video in progress (the video repeats).",
                               st["small"]))
    else:
        story.append(Paragraph("No cameras have been started.", st["cell"]))

    if state.get("analytics"):
        story += _analytics_story(state["analytics"], st, width)

    # ---- the violations ----
    story.append(Paragraph("Violation log", st["h2"]))
    if not events:
        story.append(Paragraph("Nothing to list.", st["cell"]))
    else:
        head = ["PHOTO", "CAMERA", "MISSING", "WORKER", "DETECTED"]
        rows = [[Paragraph(x, st["head"]) for x in head]]
        for i, e in enumerate(events):
            img = None
            path = photo(e) if i < MAX_PHOTOS else None
            if path:
                try:
                    from PIL import Image as PILImage

                    with PILImage.open(path) as im:
                        w, h = im.size
                    scale = min(PHOTO_W / w, PHOTO_H / h)
                    img = Image(str(path), width=w * scale, height=h * scale)
                except Exception:  # an unreadable photo is left out, not fatal
                    img = None
            detail = f"video {_clock(e.get('time_sec'))}, loop {e.get('loop', '-')}"
            rows.append([img or Paragraph("-", st["small"]), Paragraph(e["camera_name"], st["cellb"]),
                         Paragraph(", ".join(e["violations"]), st["bad"]),
                         Paragraph(f"#{e['track_id']}" if e.get("track_id") is not None else "-", st["cell"]),
                         [Paragraph(_when(e.get("detected_at")), st["cell"]), Paragraph(detail, st["small"])]])
        story.append(_table(rows, [width * f for f in (0.14, 0.22, 0.26, 0.1, 0.28)], st))
        if len(all_events) > len(events):
            story.append(Paragraph(f"Showing the newest {len(events)} of {len(all_events)} violations.", st["small"]))
        elif len(events) > MAX_PHOTOS:
            story.append(Paragraph(f"Photos are included for the newest {MAX_PHOTOS} violations.", st["small"]))

    story.append(Spacer(1, 10))
    story.append(KeepTogether(Paragraph(
        "Detections come from an AI vision model and can be wrong; check a photo before acting on it. "
        "A violation seen again when a camera's video repeats is listed again.", st["small"])))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
