"""Rebuild DCN marker text from a rendered notes PDF.

Why this exists
---------------
``src/upsc_quality_checker.check_dcn_notes()`` is the note-generation project's
structural check for Daily Class Notes, and it is one of the checks this app has
to preserve. It reads *marker text* — ``#LECTURE_TITLE:``, ``#H1:``, ``#H2:``,
``#H3:``, ``#TABLE_START``/``#TABLE_END`` — which is what the model emits and the
DOCX writer consumes. Those markers do not survive typesetting: by the time the
notes are a PDF they are a 26 pt dark-green line, a 16 pt dark-red line, and a
drawn table.

So this module reads them back. It is the input adapter, not a new check: it
recovers the structure the markers described, then hands it to the original
check unchanged. For a DCN note nothing here is guessed — the palette and the
type scale come straight from ``src/config.py``. For a PDF built on some other
template those numbers would be wrong, so :func:`measure_type_scale` reads the
scale off the document itself instead: the size that sets most of the characters
is body, and the distinct larger sizes are the heading tiers.

What it cannot recover, and says so
-----------------------------------
* ``#FLOW_START``/``#FLOW_END`` — a flowchart renders as arrows in body text and
  is indistinguishable from prose that contains "↓". Flow zones are therefore
  never emitted, and ``check_dcn_notes``'s flow-marker balance check has nothing
  to fire on. Reported as a not-applicable check rather than a passing one.
  (``src/pdf/text_recovery.py`` does recover them, by reading the page — this
  limitation applies only when there is no model to read it with.)
* ``#TOPICS_COVERED`` — the same is true, but that check asserts the block is
  *absent*, which a PDF with no such block satisfies honestly.
* Table zones depend on PyMuPDF's table finder. When it is unavailable for a
  page, ``tables_detected`` is False and the caller is told the stray-table check
  could not run on that page rather than being given a false pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import (
    DCN_COLOR_H1,
    DCN_COLOR_H2,
    DCN_COLOR_TITLE,
    DCN_SIZE_BODY_PT,
    DCN_SIZE_H1_PT,
    DCN_SIZE_H2_PT,
    DCN_SIZE_TITLE_PT,
)
from ..pdf_layout_check import (
    FOOTER_BAND_IN,
    HEADER_BAND_IN,
    NOTES_BOX_LEFT_IN,
    NOTES_BOX_SLACK_IN,
    POINTS_PER_INCH,
)
from ..quality_checker import _TOPICS_HEADINGS
from .extractor import PdfDocument, PageText

#: PyMuPDF's bold bit in span["flags"].
_BOLD_FLAG = 1 << 4

#: Half-way points of the DCN type scale (26 / 16 / 14 / 11). A span at or above
#: each boundary is that level. Using midpoints rather than exact sizes absorbs
#: the fractional sizes a PDF producer writes (15.96 pt for a 16 pt heading).
#: These are the *defaults*; a document that is not on the DCN scale gets its
#: own, measured by :func:`measure_type_scale`.
_TITLE_MIN_PT = (DCN_SIZE_TITLE_PT + DCN_SIZE_H1_PT) / 2.0     # 21
_H1_MIN_PT = (DCN_SIZE_H1_PT + DCN_SIZE_H2_PT) / 2.0           # 15
_H2_MIN_PT = (DCN_SIZE_H2_PT + DCN_SIZE_BODY_PT) / 2.0         # 12.5

#: A size has to carry at least this share of the document's characters before
#: it counts as a real tier rather than a stray superscript or a caption.
_TIER_MIN_SHARE = 0.004
#: ...and be at least this much larger than body text to be a heading at all.
_TIER_MIN_STEP_PT = 1.0


@dataclass(frozen=True)
class TypeScale:
    """The size boundaries that separate title / H1 / H2 / body in one PDF.

    The DCN scale is the default because these checks were written for DCN
    notes. But a PDF from another template — an older note type, a different
    house style, a colleague's export — has its own scale, and judged against
    DCN's it would come back as one long run of body text with no headings at
    all. So when the DCN palette is absent, the scale is measured from the
    document: whatever size carries most of the characters is body, and the
    distinct larger sizes are the heading tiers, largest first.
    """

    title_min: float = _TITLE_MIN_PT
    h1_min: float = _H1_MIN_PT
    h2_min: float = _H2_MIN_PT
    body_pt: float = float(DCN_SIZE_BODY_PT)
    measured: bool = False              # False = the DCN defaults are in use

    def describe(self) -> str:
        if not self.measured:
            return "DCN type scale (26/16/14/11 pt)"
        return (f"measured from this PDF: body {self.body_pt:g} pt, "
                f"headings from {self.h2_min:g} pt")


def measure_type_scale(document: PdfDocument) -> TypeScale:
    """Work out this document's own type scale.

    Returns the DCN defaults untouched when the DCN palette is present — those
    colours are exact and there is nothing to improve on. Otherwise the sizes
    are counted by how many characters each one sets, and the tiers are read off
    that distribution.
    """
    palette = {DCN_COLOR_TITLE.upper(), DCN_COLOR_H1.upper(), DCN_COLOR_H2.upper()}
    weight: dict[float, int] = {}
    palette_found = False
    for page in document.pages:
        for span in page.spans:
            if str(span.get("color", "")).upper() in palette:
                palette_found = True
            size = round(float(span.get("size") or 0.0) * 2) / 2.0   # to 0.5 pt
            if size > 0:
                weight[size] = weight.get(size, 0) + len(span.get("text", ""))
    if palette_found or not weight:
        return TypeScale()

    total = sum(weight.values())
    body = max(weight, key=lambda s: weight[s])
    tiers = sorted(
        (s for s, n in weight.items()
         if s >= body + _TIER_MIN_STEP_PT and n / total >= _TIER_MIN_SHARE),
        reverse=True,
    )
    if not tiers:
        # Everything is one size. Headings can then only be found by boldness,
        # which _classify already does; the size gates are put out of reach.
        big = body * 100
        return TypeScale(title_min=big, h1_min=big, h2_min=big,
                         body_pt=body, measured=True)

    def midpoint(upper: float, lower: float) -> float:
        return (upper + lower) / 2.0

    title = tiers[0]
    h1 = tiers[1] if len(tiers) > 1 else tiers[0]
    h2 = tiers[2] if len(tiers) > 2 else (tiers[1] if len(tiers) > 1 else tiers[0])
    return TypeScale(
        title_min=midpoint(title, h1) if title > h1 else title - 0.5,
        h1_min=midpoint(h1, h2) if h1 > h2 else h1 - 0.5,
        h2_min=midpoint(h2, body),
        body_pt=body,
        measured=True,
    )

#: Lines that are page furniture, not notes. Matched whole-line, case-folded.
_FURNITURE_RE = re.compile(
    r"^(?:page\s*\d+(?:\s*(?:of|/)\s*\d+)?|\d{1,4}|space\s*for\s*notes|"
    r"pw\s*onlyias|only\s*ias|physics\s*wallah)$", re.I)

#: The glyphs the DCN writer draws for its three bullet levels, plus the ones a
#: template or a converter may substitute. Level order matters: the index into
#: this tuple is the nesting depth the marker text is rebuilt with.
_BULLET_LEVELS = ("●•‣", "○◦", "■▪–—")
_BULLET_CHARS = "".join(_BULLET_LEVELS) + "-*"
_BULLET_RE = re.compile(rf"^\s*([{re.escape(_BULLET_CHARS)}])\s*(.*)$", re.S)
_NUMBERED_RE = re.compile(r"^\s*(\d{1,3}[.)])\s+(.*)$", re.S)

#: A visual line whose right edge stops this far short of the content column was
#: not wrapped — it ended because the paragraph did. Anything closer to the edge
#: was broken by the column and the next line continues it.
_WRAP_SLACK_IN = 0.55

#: Sentence enders in both languages. A line that ends on one of these ends its
#: paragraph even when it happens to reach the column edge.
_TERMINAL = (".", "?", "!", "।", ":", ";")

#: The contents heading, in both languages. Imported from src/quality_checker so
#: the two checks that care about this section can never disagree about what it
#: is called.
_TOPICS_HEADING_RE = re.compile(
    "|".join(re.escape(h) for h in _TOPICS_HEADINGS), re.I)

#: Line-to-line distance, as a multiple of the type size, below which two
#: same-level heading lines are one wrapped heading rather than two sections.
#: Measured on the reference output: a wrapped heading's second line sits at
#: 1.84x the size (29.3 pt under a 16 pt H1; 25.7 pt under a 14 pt H2), while a
#: heading that opens a new section carries its paragraph space and sits at
#: 2.45x. 2.0 separates the two with room on both sides.
_HEADING_WRAP_LEADING = 2.0


@dataclass
class Reconstruction:
    """Marker text rebuilt from the PDF, plus what could not be rebuilt."""

    marker_text: str = ""
    lecture_title: str = ""
    #: Marker line index (0-based, into marker_text.splitlines()) -> page number.
    line_pages: dict[int, int] = field(default_factory=dict)
    headings: list[tuple[int, int, str]] = field(default_factory=list)  # (page, level, text)
    #: Checks that could not run because the PDF cannot carry their input.
    not_applicable: list[str] = field(default_factory=list)
    tables_found: int = 0
    pages_without_table_detection: list[int] = field(default_factory=list)
    #: Which type scale decided what a heading is, for the run log.
    type_scale: str = ""

    def page_of_line(self, line_no: int) -> int | None:
        """Page for a 1-based line number reported by ``check_dcn_notes``."""
        return self.line_pages.get(line_no - 1)


def _classify(span: dict, *, title_seen: bool, scale: TypeScale) -> str:
    """"title" | "h1" | "h2" | "h3" | "body" for one span.

    Colour decides first, and decides absolutely: the DCN palette is
    unambiguous (dark green title, dark red H1, blue H2) and a title typeset
    over three lines is three green lines, not one green line and two red ones.

    Size decides for a PDF whose colours differ from the reference, against
    ``scale`` — which is the DCN scale for a DCN note and the document's own
    measured scale for anything else. There ``title_seen`` does the colour's
    job: the first big block is the title, anything big after it is a heading.

    Bold at body size is H3, which is exactly how the writer renders it.
    """
    size = float(span.get("size") or 0.0)
    color = str(span.get("color") or "").upper()
    bold = bool(int(span.get("flags") or 0) & _BOLD_FLAG)

    if color == DCN_COLOR_TITLE.upper():
        return "title"
    if color == DCN_COLOR_H1.upper():
        return "h1"
    if color == DCN_COLOR_H2.upper():
        return "h2"

    if size >= scale.title_min:
        return "title" if not title_seen else "h1"
    if size >= scale.h1_min:
        return "h1"
    if size >= scale.h2_min:
        return "h2"
    if bold:
        return "h3"
    return "body"


@dataclass
class _Line:
    """One visual line on the page, before wrapped lines are joined."""

    level: str
    text: str
    y: float
    x0: float
    x1: float
    size: float = 0.0


def _visual_lines(page: PageText, *, title_seen: bool,
                  scale: TypeScale) -> tuple[list[_Line], bool]:
    """Every drawn line of body content on one page, furniture excluded.

    Spans are grouped by vertical position, which is how a PDF stores them — a
    heading and its trailing punctuation are separate spans on the same
    baseline. A line's level is the level of its widest span, so one stray
    non-bold character does not demote a heading.
    """
    header_limit = HEADER_BAND_IN * POINTS_PER_INCH
    footer_limit = page.height_pt - FOOTER_BAND_IN * POINTS_PER_INCH
    notes_box_left = (NOTES_BOX_LEFT_IN - NOTES_BOX_SLACK_IN) * POINTS_PER_INCH

    rows: dict[int, list[dict]] = {}
    for span in page.spans:
        x0, y0, x1, y1 = span["bbox"]
        centre_y = (y0 + y1) / 2.0
        if centre_y < header_limit or centre_y > footer_limit:
            continue                      # running header / footer strip
        if x0 >= notes_box_left:
            continue                      # inside the reserved "Space for Notes" column
        rows.setdefault(int(round(centre_y / 3.0)), []).append(span)

    out: list[_Line] = []
    for key in sorted(rows):
        spans = sorted(rows[key], key=lambda s: s["bbox"][0])
        text = " ".join("".join(s["text"] for s in spans).split())
        if not text or _FURNITURE_RE.match(text):
            continue
        widest = max(spans, key=lambda s: s["bbox"][2] - s["bbox"][0])
        level = _classify(widest, title_seen=title_seen, scale=scale)
        if level == "title":
            title_seen = True
        out.append(_Line(level=level, text=text, y=spans[0]["bbox"][1],
                         x0=min(s["bbox"][0] for s in spans),
                         x1=max(s["bbox"][2] for s in spans),
                         size=float(widest.get("size") or 0.0)))
    return out, title_seen


def _bullet_level(char: str) -> int:
    for depth, chars in enumerate(_BULLET_LEVELS):
        if char in chars:
            return depth
    return 0


def _join_wrapped(lines: list[_Line], content_right_pt: float) -> list[_Line]:
    """Put a paragraph broken across the column back together.

    A DCN bullet is one sentence typed on one logical line; the 5.5 in column
    breaks it into four or five drawn lines, and the bullet glyph is frequently
    drawn as a line of its own. Handing those to ``check_dcn_notes`` unjoined
    makes every bullet look like a fragment and every heading look truncated —
    which is exactly the false alarm this join exists to prevent.

    A line continues the one above it when the one above reached the column edge
    without ending on sentence punctuation, and the line itself does not start a
    new bullet, a new number, or a new heading level.
    """
    joined: list[_Line] = []
    for line in lines:
        text = line.text
        bullet = _BULLET_RE.match(text)
        numbered = _NUMBERED_RE.match(text)

        # A bullet glyph alone on its line belongs to whatever comes next.
        if bullet and not bullet.group(2).strip():
            joined.append(_Line(level=line.level, text=bullet.group(1), y=line.y,
                                x0=line.x0, x1=content_right_pt, size=line.size))
            continue

        previous = joined[-1] if joined else None
        # A heading that wrapped is still one heading. It starts a new block only
        # when its level differs from the line above it — two consecutive lines
        # at the same level, the first of which ran to the column edge without
        # ending a sentence, are one heading broken by the column.
        starts_block = bool(bullet or numbered) or (
            line.level != "body" and (previous is None or previous.level != line.level))
        if previous is not None:
            prev_bullet_only = (_BULLET_RE.match(previous.text) is not None
                                and not _BULLET_RE.match(previous.text).group(2).strip())
            prev_wrapped = (previous.x1 >= content_right_pt - _WRAP_SLACK_IN * POINTS_PER_INCH
                            and not previous.text.rstrip().endswith(_TERMINAL))
            same_level = previous.level == line.level
            if prev_bullet_only and line.level == "body":
                previous.text = f"{previous.text} {text}".strip()
                previous.x1 = line.x1
                continue
            if prev_wrapped and same_level and not starts_block:
                previous.text = f"{previous.text} {text}".strip()
                previous.x1 = line.x1
                continue
            # A heading is short, so it often wraps well before the column edge
            # and the width test above cannot see it. Two heading lines at the
            # same level set one line apart are one heading: the writer always
            # puts a paragraph space before a new section, so a real pair of
            # adjacent sections is never this close.
            heading_wrap = (
                same_level and line.level != "body" and previous.size > 0
                and 0 < line.y - previous.y < previous.size * _HEADING_WRAP_LEADING
                and not bullet and not numbered)
            if heading_wrap:
                previous.text = f"{previous.text.rstrip('-')} {text}".strip()
                previous.x1 = line.x1
                continue
        joined.append(_Line(level=line.level, text=text, y=line.y,
                            x0=line.x0, x1=line.x1, size=line.size))
    return joined


def _as_marker_body(text: str) -> str:
    """A body line, rewritten as the bullet ``check_dcn_notes`` expects.

    ``_BULLET_RE`` in src/upsc_quality_checker.py matches ``- ``/``* ``/``• `` and
    numbered items at any indent, so the drawn glyph becomes the equivalent
    marker at the depth it was drawn."""
    bullet = _BULLET_RE.match(text)
    if bullet and bullet.group(2).strip():
        indent = "  " * _bullet_level(bullet.group(1))
        return f"{indent}- {bullet.group(2).strip()}"
    numbered = _NUMBERED_RE.match(text)
    if numbered:
        return f"{numbered.group(1)} {numbered.group(2).strip()}"
    return text


def _in_table(y: float, bboxes: list[tuple[float, float, float, float]]) -> bool:
    return any(y0 - 2 <= y <= y1 + 2 for _x0, y0, _x1, y1 in bboxes)


#: A heading that opens a numbered or named section, as opposed to the second
#: line of a title that simply ran on. Used to decide where the title stops.
_SECTION_START_RE = re.compile(
    r"^(?:\d+[.)]\s|[IVXivx]+[.)]\s|#|Q\d|QUICK REVISION|Key Sources|"
    r"त्वरित पुनरावृत्ति|प्रमुख स्रोत)", re.I)


def _looks_like_section(text: str) -> bool:
    return bool(_SECTION_START_RE.match(text.strip()))


def reconstruct_marker_text(document: PdfDocument) -> Reconstruction:
    """Rebuild ``check_dcn_notes``-shaped marker text from a rendered PDF."""
    result = Reconstruction()
    scale = measure_type_scale(document)
    result.type_scale = scale.describe()
    lines: list[str] = []
    title_emitted = False
    title_seen = False
    pending_title: list[str] = []

    def emit(text: str, page_no: int) -> None:
        result.line_pages[len(lines)] = page_no
        lines.append(text)

    #: The last heading emitted, when it was the final line of its page and did
    #: not finish its sentence. A heading broken by a PAGE break, not a line
    #: break, is still one heading — and the per-page join above cannot see it,
    #: because the two halves are measured against different page origins.
    dangling: tuple[int, str] | None = None    # (index into lines, level)

    for page in document.pages:
        content_right_pt = min(
            page.width_pt,
            (NOTES_BOX_LEFT_IN - NOTES_BOX_SLACK_IN) * POINTS_PER_INCH)
        raw, title_seen = _visual_lines(page, title_seen=title_seen, scale=scale)
        page_lines = _join_wrapped(raw, content_right_pt)

        if dangling is not None and page_lines:
            index, level = dangling
            first = page_lines[0]
            if first.level == level and level != "body":
                prefix = {"title": "#LECTURE_TITLE: ", "h1": "#H1: ",
                          "h2": "#H2: ", "h3": "#H3: "}[level]
                merged = (lines[index][len(prefix):].rstrip("-") + " "
                          + first.text).strip()
                lines[index] = prefix + merged
                if result.headings and result.headings[-1][2] in lines[index]:
                    page_no, hlevel, _ = result.headings[-1]
                    result.headings[-1] = (page_no, hlevel, merged)
                page_lines = page_lines[1:]
        dangling = None

        in_table = False
        in_topics = False
        for line in page_lines:
            table_here = page.tables_detected and _in_table(line.y, page.table_bboxes)
            if table_here and not in_table:
                emit("#TABLE_START", page.page_no)
                in_table = True
                result.tables_found += 1
            elif in_table and not table_here:
                emit("#TABLE_END", page.page_no)
                in_table = False

            if in_table:
                emit(line.text, page.page_no)
                continue

            level, text = line.level, line.text

            # A contents list is its own zone in the marker format, and
            # check_dcn_notes exempts that zone from the full-sentence rule —
            # its items are short phrases by design. Without the markers, every
            # contents item counts as a fragmentary bullet and the fragment
            # ratio for the whole document is wrong.
            if in_topics and level != "body":
                emit("#TOPICS_COVERED_END", page.page_no)
                in_topics = False
            if level != "body" and _TOPICS_HEADING_RE.search(text):
                emit("#TOPICS_COVERED_START", page.page_no)
                in_topics = True
                continue
            if in_topics:
                emit(_as_marker_body(text), page.page_no)
                continue

            if level == "title":
                # The title is typeset over as many lines as it needs, all in
                # the title colour. Every one of them belongs to the same
                # #LECTURE_TITLE; a second title block later in the document
                # (a spliced file) becomes an H1 rather than overwriting it.
                if not title_emitted:
                    pending_title = [text]
                    emit(f"#LECTURE_TITLE: {text}", page.page_no)
                    title_emitted = True
                elif pending_title:
                    pending_title.append(text)
                    lines[-1] = "#LECTURE_TITLE: " + " ".join(pending_title)
                else:
                    result.headings.append((page.page_no, 1, text))
                    emit(f"#H1: {text}", page.page_no)
            elif level == "h1":
                pending_title = []
                result.headings.append((page.page_no, 1, text))
                emit(f"#H1: {text}", page.page_no)
            elif level == "h2":
                pending_title = []
                result.headings.append((page.page_no, 2, text))
                emit(f"#H2: {text}", page.page_no)
            elif level == "h3":
                pending_title = []
                result.headings.append((page.page_no, 3, text))
                emit(f"#H3: {text}", page.page_no)
            else:
                pending_title = []
                emit(_as_marker_body(text), page.page_no)

            # Remember an unfinished heading sitting on the last line of the
            # page, so the next page can finish it.
            if (line is page_lines[-1] and not in_table and level != "body"
                    and not text.rstrip().endswith(_TERMINAL)):
                dangling = (len(lines) - 1, level)
        if in_topics:
            emit("#TOPICS_COVERED_END", page.page_no)
        if in_table:
            emit("#TABLE_END", page.page_no)

        if not page.tables_detected:
            result.pages_without_table_detection.append(page.page_no)

    for line in lines:
        if line.startswith("#LECTURE_TITLE:"):
            result.lecture_title = line[len("#LECTURE_TITLE:"):].strip()
            break

    result.marker_text = "\n".join(lines)
    result.not_applicable.append(
        "Flowchart marker balance (#FLOW_START/#FLOW_END): a flowchart renders as "
        "arrows in ordinary text, so the markers cannot be recovered from a PDF.")
    if result.pages_without_table_detection:
        pages = ", ".join(str(p) for p in result.pages_without_table_detection[:12])
        more = ("…" if len(result.pages_without_table_detection) > 12 else "")
        result.not_applicable.append(
            f"Table detection did not run on page(s) {pages}{more}, so the "
            "stray-table check could not be applied there.")
    return result


def _line_numbers_in(message: str) -> list[int]:
    """Every "Line n" / "Lines a-b" number a check mentioned."""
    out: list[int] = []
    for match in re.finditer(r"\bLines?\s+(\d+)(?:\s*-\s*(\d+))?", message, re.I):
        out.append(int(match.group(1)))
        if match.group(2):
            out.append(int(match.group(2)))
    return out


def locate(message: str, reconstruction: Reconstruction) -> str:
    """Turn a check's "Line 118: ..." into "Page 7" for the report.

    ``check_dcn_notes`` reports positions as line numbers in the marker text,
    which is what it was given. A reviewer holding the PDF needs a page, and the
    reconstruction knows which page every marker line came from.
    """
    pages = sorted({p for n in _line_numbers_in(message)
                    if (p := reconstruction.page_of_line(n)) is not None})
    if not pages:
        return ""
    if len(pages) == 1:
        return f"Page {pages[0]}"
    return f"Pages {pages[0]}-{pages[-1]}"
