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


def build_camera_report(state: dict, info: dict, photo: Callable[[dict], Path | None],
                        generated: datetime | None = None) -> bytes:
    """The report as PDF bytes.

    `state` is CameraManager.state(); `info` holds what the cameras were started with (required_ppe,
    analysis_interval_sec, model, settings_state; any may be missing); `photo(event)` gives the path of
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
    if info.get("model"):
        rows.append([Paragraph("Vision model", st["head"]), Paragraph(info["model"], st["cell"])])
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
