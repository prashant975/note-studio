from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .dtp_parser import find_dtp_notes
from .models import SlideData


# The notes open with a topics list, in English or Hindi (see prompts/base_*.txt).
_TOPICS_HEADINGS = ("Topics Covered", "कक्षा में शामिल विषय")


def quality_check(notes_text: str, slides: list[SlideData], docx_path: Path | None, pdf_path: Path | None, *, send_images_to_ai: bool = True) -> list[str]:
    warnings: list[str] = []
    if not any(h in notes_text for h in _TOPICS_HEADINGS):
        warnings.append("Missing the 'Topics Covered in the Class' section.")
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
    if not pdf_path or not pdf_path.exists():
        warnings.append("PDF was not created. Install LibreOffice or Microsoft Word/pywin32 for PDF export.")
    if not any((s.prompt_text or "").strip() for s in slides):
        if send_images_to_ai and any(s.image_path for s in slides):
            warnings.append("No text layer found in the source; notes were generated from slide images (vision).")
        else:
            warnings.append("No extractable text found. Enable 'Send slide images to AI' for scanned/image-only lecture files.")
    return warnings


# --------------------------------------------------------------------------
# UPSC note types: AI output splitting, correction log and quality report.
# The AI wraps its answer in markers so notes and self-audit can be separated;
# everything below is written to degrade gracefully because a model can and
# will drift on spacing, casing and the leading '#'.
# --------------------------------------------------------------------------

# Markers are matched on their own line, with or without '#', in any order.
def _marker_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"^[ \t]*#*[ \t]*{name}[ \t]*#*[ \t]*$", re.I | re.M)


_NOTES_START_RE = _marker_re("NOTES_START")
_NOTES_END_RE = _marker_re("NOTES_END")
_QUALITY_START_RE = _marker_re("QUALITY_REPORT_START")
_QUALITY_END_RE = _marker_re("QUALITY_REPORT_END")

_ANY_MARKER_RE = re.compile(r"^[ \t]*#*[ \t]*(?:NOTES_START|NOTES_END|QUALITY_REPORT_START|QUALITY_REPORT_END)[ \t]*#*[ \t]*$", re.I | re.M)


def _slice_between(text: str, start: re.Match[str] | None, end: re.Match[str] | None) -> str:
    """Take the span between two markers, tolerating a missing one on either side."""
    if start is not None and end is not None and end.start() > start.end():
        return text[start.end():end.start()]
    if start is not None:
        # Run to the next marker of any kind (the AI may drop the closing one).
        nxt = _ANY_MARKER_RE.search(text, start.end())
        return text[start.end():nxt.start()] if nxt else text[start.end():]
    if end is not None:
        prev = None
        for m in _ANY_MARKER_RE.finditer(text[:end.start()]):
            prev = m
        return text[prev.end():end.start()] if prev else text[:end.start()]
    return ""


def split_ai_output(text: str) -> tuple[str, str, bool]:
    """Split a marked AI response into (notes, quality report, used_markers).

    Falls back to treating the whole response as notes so a marker-less answer
    still produces a document instead of an exception.
    """
    if not text:
        return "", "", False
    ns = _NOTES_START_RE.search(text)
    ne = _NOTES_END_RE.search(text)
    if ns is None and ne is None:
        return text, "", False
    notes = _slice_between(text, ns, ne).strip("\n")
    qs = _QUALITY_START_RE.search(text)
    qe = _QUALITY_END_RE.search(text)
    report = _slice_between(text, qs, qe).strip("\n") if (qs or qe) else ""
    return notes, report, True


@dataclass
class CorrectionEntry:
    """One [Faculty Point ...] / [Needs Human Review] pointer found in the notes."""

    kind: str  # 'corrected' | 'removed' | 'review'
    original: str = ""  # original / removed / faculty point text
    correction: str = ""  # corrected version (corrected)
    replacement: str = ""  # suggested replacement (removed)
    issue: str = ""  # issue (review)
    action: str = ""  # suggested action (review)
    reason: str = ""
    confidence: str = ""  # High | Medium | Low | ""
    heading: str = ""  # nearest preceding notes heading
    slide_no: int | None = None
    raw: str = ""


_BLOCK_KINDS = {
    "faculty point corrected": "corrected",
    "faculty point removed": "removed",
    "needs human review": "review",
    "correction note": "corrected",
    "needs verification": "review",
    "सुधार नोट": "corrected",
    "सत्यापन आवश्यक": "review",
}
# Allows bullets, bold and stray spaces around the marker, e.g. "- **[Faculty Point Corrected]**".
_BLOCK_RE = re.compile(
    r"^[\s\-\*•>]*\**\s*\[\s*(Faculty Point Corrected|Faculty Point Removed|"
    r"Needs Human Review|Correction Note|Needs Verification|सुधार नोट|सत्यापन आवश्यक)"
    r"\s*\]\s*\**\s*:?\s*$",
    re.I,
)

# Longest labels first so "Original faculty point" is not eaten by "Faculty point".
_FIELD_LABELS: list[tuple[str, str]] = [
    ("original faculty point", "original"),
    ("removed point", "original"),
    ("faculty point", "original"),
    ("original point", "original"),
    ("point", "original"),
    ("मूल बिंदु", "original"),
    ("बिंदु", "original"),
    ("corrected version", "correction"),
    ("correction", "correction"),
    ("सही रूप", "correction"),
    ("suggested replacement", "replacement"),
    ("replacement", "replacement"),
    ("suggested action", "action"),
    ("action", "action"),
    ("issue", "issue"),
    ("समस्या", "issue"),
    ("reason", "reason"),
    ("कारण", "reason"),
    ("confidence", "confidence"),
    ("विश्वास स्तर", "confidence"),
]

_SLIDE_MENTION_RE = re.compile(r"(?:slide|page)\s*(?:no\.?|#)?\s*[:\-]?\s*(\d+)", re.I)
_QUOTE_CHARS = "\"'“”‘’«»"
_CONFIDENCE_RE = re.compile(r"\b(high|medium|moderate|low)\b", re.I)


def _strip_quotes(value: str) -> str:
    v = value.strip().strip("*").strip()
    while len(v) >= 2 and v[0] in _QUOTE_CHARS and v[-1] in _QUOTE_CHARS:
        v = v[1:-1].strip()
    return v.rstrip(",;").strip()


def _match_field(line: str) -> tuple[str, str] | None:
    """Return (attribute, value) if the line is a 'Label: value' pointer field."""
    s = line.strip().lstrip("-*•> ").strip()
    s = s.replace("**", "")
    if ":" not in s:
        return None
    label, _, value = s.partition(":")
    label = label.strip().lower().rstrip(".")
    for known, attr in _FIELD_LABELS:
        if label == known or label.startswith(known):
            return attr, _strip_quotes(value)
    return None


def _is_notes_heading(line: str) -> bool:
    """Mirror the DOCX writer's heading rule, minus pointer/table/flow lines."""
    s = line.strip()
    if not s or len(s) > 140:
        return False
    if s.startswith(("•", "-", "*", "◦", "▪", "o ")) or re.match(r"^\d+[.)]\s+", s):
        return False
    if "|" in s or _BLOCK_RE.match(s) or _is_arrow_line(s) or _match_field(s):
        return False
    if "note to dtp" in s.lower():
        return False
    if s.startswith("#"):
        return True
    return not s.endswith((".", "।"))


def _clean_heading(line: str) -> str:
    return line.strip().lstrip("#").strip().replace("**", "").strip(": ").strip()


def parse_correction_blocks(notes_text: str) -> list[CorrectionEntry]:
    """Pull every correction pointer out of the notes, in document order."""
    if not notes_text:
        return []
    lines = notes_text.splitlines()
    entries: list[CorrectionEntry] = []
    heading = ""
    i = 0
    while i < len(lines):
        m = _BLOCK_RE.match(lines[i])
        if not m:
            if _is_notes_heading(lines[i]):
                heading = _clean_heading(lines[i])
            i += 1
            continue
        kind = _BLOCK_KINDS.get(m.group(1).strip().lower(), "review")
        start = i
        i += 1
        entry = CorrectionEntry(kind=kind, heading=heading)
        last_attr = ""
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                # A blank line only ends the block when the next real line is not a field.
                j = i
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j >= len(lines) or not _match_field(lines[j]):
                    break
                i = j
                continue
            if _BLOCK_RE.match(line):
                break
            found = _match_field(line)
            if found:
                attr, value = found
                setattr(entry, attr, value)
                last_attr = attr
                i += 1
                continue
            if last_attr and line.startswith((" ", "\t")):
                # Wrapped continuation: re-strip quotes on the joined value, because the
                # opening and closing quote sit on different lines.
                setattr(entry, last_attr, _strip_quotes(getattr(entry, last_attr) + " " + line.strip()))
                i += 1
                continue
            break
        entry.raw = "\n".join(lines[start:i]).strip()
        if entry.confidence:
            cm = _CONFIDENCE_RE.search(entry.confidence)
            entry.confidence = cm.group(1).title() if cm else entry.confidence
        entry.slide_no = _find_slide_no(entry.raw, heading, lines, start)
        entries.append(entry)
    return entries


def _find_slide_no(block: str, heading: str, lines: list[str], start: int) -> int | None:
    """Look for a slide/page number in the block, then its heading, then just above it."""
    for text in (block, heading, "\n".join(lines[max(0, start - 3):start])):
        m = _SLIDE_MENTION_RE.search(text or "")
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-{2,}[\s:|-]*\|?\s*$")
_ARROW_RE = re.compile(r"^[\s\*\-•]*(?:↓|⇓|⬇|⇩|↡|\|\s*[vV]|[vV])[\s↓⇓⬇⇩↡vV]*[\s\*]*$")


def _is_pipe_row(line: str) -> bool:
    return line.count("|") >= 2


def _is_table_separator(line: str) -> bool:
    return "|" in line and "-" in line and bool(_TABLE_SEP_RE.match(line))


def _is_arrow_line(line: str) -> bool:
    s = line.strip()
    if not s or s in {"|", "-"}:
        return False
    return bool(_ARROW_RE.match(s))


def count_structures(notes_text: str) -> dict[str, int]:
    """Count markdown pipe tables and arrow flowcharts, once per block."""
    lines = notes_text.splitlines() if notes_text else []
    tables = 0
    i = 0
    while i < len(lines):
        if _is_pipe_row(lines[i]) and i + 1 < len(lines) and _is_table_separator(lines[i + 1]):
            tables += 1
            i += 2
            while i < len(lines) and _is_pipe_row(lines[i]):
                i += 1
            continue
        i += 1

    flowcharts = 0
    last_arrow = None
    blanks_since_arrow = 0
    for idx, line in enumerate(lines):
        if _is_arrow_line(line):
            # Arrows more than a few lines (or a paragraph break) apart start a new chart.
            if last_arrow is None or idx - last_arrow > 5 or blanks_since_arrow >= 2:
                flowcharts += 1
            last_arrow = idx
            blanks_since_arrow = 0
        elif not line.strip():
            blanks_since_arrow += 1
    return {"tables": tables, "flowcharts": flowcharts}


def build_quality_summary(entries: list[CorrectionEntry], *, total_slides: int, retained_slides: int, removed_slides: int, structures: dict[str, int]) -> dict:
    """Collect the headline numbers shown at the top of the quality report."""
    return {
        "total_slides": total_slides,
        "retained_slides": retained_slides,
        "housekeeping_removed": removed_slides,
        "corrected": sum(1 for e in entries if e.kind == "corrected"),
        "removed": sum(1 for e in entries if e.kind == "removed"),
        "review": sum(1 for e in entries if e.kind == "review"),
        "tables": int(structures.get("tables", 0)),
        "flowcharts": int(structures.get("flowcharts", 0)),
    }


_KIND_LABELS = {
    "corrected": "Faculty Point Corrected",
    "removed": "Faculty Point Removed",
    "review": "Needs Human Review",
}


def _cell(value: object) -> str:
    """Flatten a value so it cannot break out of a markdown table cell."""
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    text = re.sub(r"\s+", " ", text).strip()
    return text or "-"


def _entry_row(entry: CorrectionEntry) -> str:
    if entry.kind == "corrected":
        action, corrected, issue = "Corrected in notes", entry.correction, entry.issue or "Factual / outdated point"
    elif entry.kind == "removed":
        action, corrected, issue = "Removed from notes", entry.replacement, entry.issue or "Incorrect / not needed"
    else:
        action, corrected, issue = (entry.action or "Verify from official source / standard book"), entry.correction, entry.issue
    cells = [
        entry.slide_no if entry.slide_no else "-",
        entry.heading,
        entry.original,
        _KIND_LABELS.get(entry.kind, entry.kind),
        action,
        corrected,
        entry.reason or issue,
        entry.confidence,
    ]
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def render_quality_report_md(summary: dict, entries: list[CorrectionEntry], *, ai_report: str = "", warnings: list[str] | None = None, meta: dict | None = None) -> str:
    """Render the reviewer-facing quality report; AI analysis is passed through, never invented."""
    meta = meta or {}
    out: list[str] = ["# UPSC Notes - Quality Report", ""]
    if meta:
        out += ["| Field | Value |", "| --- | --- |"]
        out += [f"| {_cell(k.replace('_', ' ').title())} | {_cell(v)} |" for k, v in meta.items()]
        out.append("")

    out += [
        "## 1. Summary",
        "",
        "| Metric | Count |",
        "| --- | --- |",
        f"| Total slides in file | {_cell(summary.get('total_slides', 0))} |",
        f"| Slides used in notes | {_cell(summary.get('retained_slides', 0))} |",
        f"| Housekeeping slides removed | {_cell(summary.get('housekeeping_removed', 0))} |",
        f"| Faculty points corrected | {_cell(summary.get('corrected', 0))} |",
        f"| Faculty points removed | {_cell(summary.get('removed', 0))} |",
        f"| Points needing human review | {_cell(summary.get('review', 0))} |",
        f"| Tables created | {_cell(summary.get('tables', 0))} |",
        f"| Flowcharts created | {_cell(summary.get('flowcharts', 0))} |",
        "",
        "## 2. Correction Log",
        "",
    ]
    if entries:
        out += [
            "| Slide/Page No. | Heading | Original Faculty Point | Issue Type | Action | Corrected Version | Reason | Confidence |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        out += [_entry_row(e) for e in entries]
    else:
        out.append("None")
    out.append("")

    flow_text, relevance_text = _split_ai_sections(ai_report)
    out += ["## 3. Concept Flow Check", "", flow_text, ""]
    out += ["## 4. UPSC Relevance Check", "", relevance_text, ""]

    out += ["## 5. Warnings", ""]
    if warnings:
        out += [f"- {w}" for w in warnings]
    else:
        out.append("None")
    out.append("")
    return "\n".join(out)


_FLOW_HEAD_RE = re.compile(r"^[#\s\d.]*\**\s*concept\s+flow[^\n]*$", re.I | re.M)
_RELEVANCE_HEAD_RE = re.compile(r"^[#\s\d.]*\**\s*upsc\s+relevance[^\n]*$", re.I | re.M)


def _split_ai_sections(ai_report: str) -> tuple[str, str]:
    """Pull the AI's own flow / relevance paragraphs out of its quality report block."""
    text = (ai_report or "").strip()
    if not text:
        missing = "_The AI did not provide this section._"
        return missing, missing
    fm = _FLOW_HEAD_RE.search(text)
    rm = _RELEVANCE_HEAD_RE.search(text)
    if fm and rm and rm.start() > fm.start():
        return text[fm.end():rm.start()].strip() or "_Not provided._", text[rm.end():].strip() or "_Not provided._"
    if fm and rm:
        return text[fm.end():].strip() or "_Not provided._", text[rm.end():fm.start()].strip() or "_Not provided._"
    # No recognisable headings: show the whole AI report under flow and say so under relevance.
    return text, "_Included in the AI report above._"
