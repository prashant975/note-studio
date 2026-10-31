from __future__ import annotations

import re

from pathlib import Path

from .dtp_parser import find_dtp_notes
from .models import SlideData
from .style_config import (
    BODY_COLOR,
    BODY_SIZE_PT,
    BOLD_COLOR,
    FONT_MATH,
    FONT_PRIMARY,
    FONT_SYMBOL,
    FOOTER_TEXT,
    HEADER_DISTANCE_IN,
    IMAGE_MAX_WIDTH_IN,
    LECTURE_TITLE_SIZE_PT,
    MARGIN_BOTTOM_IN,
    MARGIN_LEFT_IN,
    MARGIN_RIGHT_IN,
    MARGIN_TOP_IN,
    PAGE_HEIGHT_IN,
    PAGE_WIDTH_IN,
    SECTION_HEADING_SIZE_PT,
    SUBJECT_SIZE_PT,
    TITLE_COLOR,
)

# How far a measured value may drift before QC complains (inches / points).
_TOL_IN = 0.05
_TOL_PT = 0.6

# Sizes the template is allowed to produce. Anything else means the renderer
# started inventing font sizes (13/14/15/17/19 pt), which breaks the series look.
_ALLOWED_SIZES_PT = {BODY_SIZE_PT, SECTION_HEADING_SIZE_PT,
                     LECTURE_TITLE_SIZE_PT, SUBJECT_SIZE_PT}


def _near(value: float | None, target: float, tol: float) -> bool:
    return value is not None and abs(value - target) <= tol


def check_docx_formatting(docx_path: Path) -> list[str]:
    """Verify the rendered DOCX still matches the concise-notes template.

    Formatting QC only — it reads the finished file, so it catches a renderer
    regression (wrong paper size, a stray font size, an image past the margin)
    that unit tests on the writer's inputs cannot see. Returns warnings; an
    unreadable document is reported rather than raised.
    """
    warnings: list[str] = []
    try:
        from docx import Document
        from docx.shared import Inches
    except Exception as e:                       # pragma: no cover - env issue
        return [f"Formatting QC skipped: python-docx unavailable ({e})."]
    try:
        doc = Document(str(docx_path))
    except Exception as e:
        return [f"Formatting QC could not open the DOCX: {e}"]

    section = doc.sections[0]
    if not (_near(section.page_width.inches, PAGE_WIDTH_IN, _TOL_IN)
            and _near(section.page_height.inches, PAGE_HEIGHT_IN, _TOL_IN)):
        warnings.append(
            f"Page is not A4 portrait ({section.page_width.inches:.2f} x "
            f"{section.page_height.inches:.2f} in)."
        )
    for name, value, expected in (
            ("top", section.top_margin, MARGIN_TOP_IN),
            ("bottom", section.bottom_margin, MARGIN_BOTTOM_IN),
            ("left", section.left_margin, MARGIN_LEFT_IN),
            ("right", section.right_margin, MARGIN_RIGHT_IN)):
        if not _near(value.inches, expected, _TOL_IN):
            warnings.append(
                f"{name.capitalize()} margin is {value.inches:.2f} in, expected {expected:.2f} in.")
    if section.header_distance is not None and section.header_distance.inches > HEADER_DISTANCE_IN + 0.35:
        warnings.append(
            f"Header distance is {section.header_distance.inches:.2f} in — the PW logo "
            "may sit too far down the page."
        )

    # ---- furniture ----
    header_xml = section.header._element.xml if section.header is not None else ""
    footer_xml = section.footer._element.xml if section.footer is not None else ""
    if "blip" not in header_xml:
        warnings.append("PW logo/watermark image is missing from the page header.")
    elif "wp:anchor" not in header_xml:
        warnings.append("PW watermark is not anchored behind the text (it may push content around).")
    if FOOTER_TEXT not in footer_xml:
        warnings.append(f"Footer branding {FOOTER_TEXT!r} is missing.")
    if "PAGE" not in footer_xml:
        warnings.append("Automatic page-number field is missing from the footer.")

    # ---- typography ----
    sizes: set[float] = set()
    fonts: set[str] = set()
    title_seen = False
    body_runs = 0
    body_wrong_color = 0
    for para in doc.paragraphs:
        # A kept-visible DTP note is deliberately red on yellow — template
        # output, not a body-colour defect.
        is_dtp = "note to dtp" in (para.text or "").lower()
        for run in para.runs:
            if not (run.text or "").strip():
                continue
            font = run.font
            if font.size is not None:
                pt = font.size.pt
                sizes.add(round(pt, 1))
                if _near(pt, SUBJECT_SIZE_PT, _TOL_PT) or _near(pt, LECTURE_TITLE_SIZE_PT, _TOL_PT):
                    if font.color is not None and font.color.rgb == TITLE_COLOR:
                        title_seen = True
                elif _near(pt, BODY_SIZE_PT, _TOL_PT) and not is_dtp:
                    body_runs += 1
                    # Equations legitimately render in the maths font; their
                    # colour still has to match the body text. The **bold**
                    # accent colour is part of the template, but only on runs
                    # that really are bold.
                    rgb = font.color.rgb if font.color is not None else None
                    if (rgb is not None and rgb != BODY_COLOR
                            and not (rgb == BOLD_COLOR and run.font.bold)):
                        body_wrong_color += 1
            if font.name:
                fonts.add(font.name)
    stray = {s for s in sizes if not any(_near(s, t, _TOL_PT) for t in _ALLOWED_SIZES_PT)}
    if stray:
        warnings.append(
            "Unexpected font size(s) in the notes: "
            + ", ".join(f"{s:g} pt" for s in sorted(stray))
            + f" (the template uses only {sorted(_ALLOWED_SIZES_PT)} pt)."
        )
    if fonts and FONT_PRIMARY not in fonts:
        warnings.append(f"Body text is not set in {FONT_PRIMARY}; found {sorted(fonts)}.")
    unexpected_fonts = {f for f in fonts if f not in {FONT_PRIMARY, FONT_MATH, FONT_SYMBOL}}
    if unexpected_fonts:
        warnings.append(f"Unexpected font(s) used: {sorted(unexpected_fonts)}.")
    if not title_seen:
        warnings.append("Red centered subject/chapter title block was not found.")
    if body_runs and body_wrong_color > body_runs * 0.2:
        warnings.append(
            f"{body_wrong_color} body run(s) are not in the dark-blue body colour (#000099)."
        )

    # ---- images stay inside the margins ----
    usable_emu = int(Inches(IMAGE_MAX_WIDTH_IN))
    oversized = 0
    for shape in doc.inline_shapes:
        try:
            if int(shape.width) > usable_emu + 9144:      # 0.01 in slack
                oversized += 1
        except Exception:
            continue
    if oversized:
        warnings.append(
            f"{oversized} image(s) are wider than the {IMAGE_MAX_WIDTH_IN:.2f} in text column "
            "and would cross the page margin."
        )
    return warnings


def _concepts_covered_items(notes_text: str) -> int:
    """How many bullets the 'Concepts Covered' index actually lists."""
    count = 0
    in_section = False
    for line in notes_text.splitlines():
        s = line.strip()
        if not s:
            continue
        if not in_section:
            if "concepts covered" in s.lower():
                in_section = True
            continue
        if s.startswith(("•", "◦", "▪", "-", "–", "*", "·")) or re.match(r"^\d+[.)]\s", s):
            count += 1
        else:
            break                        # next heading/paragraph ends the index
    return count


def quality_check(notes_text: str, slides: list[SlideData], docx_path: Path | None, pdf_path: Path | None, *, send_images_to_ai: bool = True, image_report: dict | None = None) -> list[str]:
    warnings: list[str] = []
    if "Concepts Covered" not in notes_text:
        warnings.append("Missing 'Concepts Covered in the Class' section.")
    else:
        # The prompt asks for at most 8 items, but nothing enforces it — and the
        # flash-tier fallback models are exactly the ones that ignore soft caps.
        concept_items = _concepts_covered_items(notes_text)
        if concept_items > 8:
            warnings.append(
                f"'Concepts Covered' lists {concept_items} items; the template caps it at 8."
            )
    if len(notes_text.strip()) < 100:
        warnings.append("Generated notes look too short. Check API output or extraction.")
    notes = find_dtp_notes(notes_text)
    slide_nums = {s.slide_no for s in slides}
    for note in notes:
        if not note.slide_no:
            warnings.append(f"DTP note missing slide number: {note.raw[:120]}")
        elif note.slide_no not in slide_nums:
            warnings.append(f"DTP note references slide {note.slide_no}, but that slide was not found.")
    if not docx_path or not docx_path.exists():
        warnings.append("DOCX was not created.")
    else:
        warnings.extend(check_docx_formatting(docx_path))
    if not pdf_path or not pdf_path.exists():
        warnings.append("PDF was not created. Install LibreOffice or Microsoft Word/pywin32 for PDF export.")
    if not any((s.prompt_text or "").strip() for s in slides):
        if send_images_to_ai and any(s.image_path for s in slides):
            warnings.append("No text layer found in the source; notes were generated from slide images (vision).")
        else:
            warnings.append("No extractable text found. Enable 'Send slide images to AI' for scanned/image-only lecture files.")
    if image_report:
        by_reason = image_report.get("rejected_by_reason") or {}
        if by_reason:
            top = ", ".join(f"{k} x{v}" for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])[:4])
            warnings.append(
                f"Image relevance filter kept {image_report.get('accepted', 0)} diagram(s) and "
                f"rejected {image_report.get('rejected', 0)} ({top})."
            )
    return warnings
