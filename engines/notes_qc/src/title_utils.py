"""The lecture title — one place that decides what appears at the top of a note.

The reference format is ``L03_DCN03_Probity in Governance, RTI and Citizens
Charter``: a fixed ``Lnn_DCNnn_`` prefix, then the human title with spaces.

Two failures this module exists to stop, both seen in real output:

* ``Lxx_DCNxx_...`` reaching a student's page. The model emits the placeholder
  from the prompt's own example whenever it is not told the real numbers, so a
  placeholder is treated here as *absent*, never as a title.
* ``Lxx_DCNxx_Probity_in_Governance_RTI_and_Citizens_Charter`` — 56 characters
  with no space in it. Word cannot break an unbreakable run, so it either
  overhangs the 5.5 in column into the notes box or forces a huge left gap.
  Underscores inside the human part are spaces that were lost somewhere
  upstream, and are restored.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

#: The prefix, once it is known: L03_DCN03_
PREFIX_RE = re.compile(r"^\s*L\s*(?P<lecture>\d{1,3})\s*_\s*DCN\s*(?P<dcn>\d{1,3})\s*_\s*(?P<title>.*)$", re.I)

#: The same shape but with the placeholder letters the prompt uses as an example.
PLACEHOLDER_PREFIX_RE = re.compile(r"^\s*L\s*(?:x+|n+|#+|\?+)\s*_\s*DCN\s*(?:x+|n+|#+|\?+)\s*_\s*(?P<title>.*)$", re.I)

#: Lecture/DCN numbers as they appear in a file name: L03, DCN 3, Lecture-03.
_FILE_LECTURE_RE = re.compile(r"(?:^|[^A-Za-z])(?:L|LEC|LECTURE)[\s._-]*(\d{1,3})(?![\d])", re.I)
_FILE_DCN_RE = re.compile(r"(?:^|[^A-Za-z])DCN[\s._-]*(\d{1,3})(?![\d])", re.I)

#: Words a file name carries that are not part of the lecture's title.
_FILE_NOISE = {
    "class", "classes", "notes", "note", "dcn", "lecture", "lec", "final",
    "draft", "copy", "new", "old", "updated", "revised", "ppt", "pptx", "pdf",
    "slides", "slide", "handout", "upsc", "batch", "hindi", "english", "eng",
    "v1", "v2", "v3", "part", "session",
}

#: Anything longer than this with no break opportunity will overhang the column.
MAX_UNBREAKABLE_CHARS = 30

#: Placeholder tokens that must never be rendered.
PLACEHOLDER_TOKENS = ("lxx", "dcnxx", "lnn", "dcnnn", "l##", "dcn##", "<title>",
                      "lecture title", "your title here")


@dataclass(frozen=True)
class LectureTitle:
    """A title split into the parts the writer and the checker each need."""

    lecture_no: str = ""      # "03", or "" when unknown
    dcn_no: str = ""          # "03", or "" when unknown
    title: str = ""           # "Probity in Governance, RTI and Citizens Charter"
    inferred: bool = False    # numbers came from the file name, not the user

    @property
    def prefix(self) -> str:
        """``L03_DCN03_`` — empty when either number is missing."""
        if self.lecture_no and self.dcn_no:
            return f"L{self.lecture_no}_DCN{self.dcn_no}_"
        return ""

    @property
    def display(self) -> str:
        """What goes on the page.

        With both numbers: ``L03_DCN03_Probity in Governance...``. Without
        them: ``Lecture: Probity in Governance...`` — a real sentence, which is
        the point. Falling back to the placeholder would put ``Lxx`` in front
        of a student.
        """
        if not self.title:
            return f"{self.prefix}Class Notes" if self.prefix else "Class Notes"
        if self.prefix:
            return f"{self.prefix}{self.title}"
        return f"Lecture: {self.title}"

    @property
    def is_complete(self) -> bool:
        return bool(self.lecture_no and self.dcn_no and self.title)


def clean_title_text(raw: str) -> str:
    """Turn whatever arrived into a human title.

    Underscores become spaces because they are the failure being repaired: a
    title that reached us as ``Probity_in_Governance`` was flattened somewhere
    upstream, and putting it on the page that way produces the unbreakable run
    the layout check flags.
    """
    text = (raw or "").strip().strip('"“”\'')
    if not text:
        return ""
    text = text.replace("_", " ").replace(" ", " ")
    text = re.sub(r"\s+", " ", text).strip(" -–—:;,")
    # A title in Title_Case_Like_This loses nothing by keeping its own casing;
    # only an all-caps shout is softened, and only when it is clearly one.
    if text.isupper() and len(text) > 12:
        text = text.title()
    return text


def _normalise_number(value: object) -> str:
    """``3`` / ``"3"`` / ``" 03 "`` -> ``"03"``; anything unusable -> ``""``."""
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    number = digits.lstrip("0") or "0"
    if len(number) > 3:
        return ""
    return number.zfill(2)


_HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]+$")


def looks_like_hex_identifier(text: str) -> bool:
    """True when a "title" is really a UUID / hash fragment, not words.

    ``Lecture: b950d402 bc5d 48def60b1910`` is the failure this stops: a file
    named by its upload UUID sailed through ``infer_from_filename`` and reached
    a student's page. A token only counts as hex noise when it is pure
    hex-alphabet, at least 4 characters, and carries a digit — so ``deed`` or
    ``face`` alone stay words; the 12-character floor on the joined noise keeps
    a year like ``2024`` from condemning a real title.
    """
    stripped = re.sub(r"^\s*(?:L\d{1,3}\s*_\s*DCN\d{1,3}\s*_|lecture\s*[:\-–]\s*)",
                      "", (text or "").strip(), flags=re.I)
    tokens = [t for t in re.split(r"[\s_\-]+", stripped) if t]
    if not tokens:
        return False
    hexish = [t for t in tokens
              if _HEX_TOKEN_RE.match(t) and len(t) >= 4 and any(c.isdigit() for c in t)]
    return (len(hexish) >= max(1, (len(tokens) + 1) // 2)
            and len("".join(hexish)) >= 12)


def is_placeholder(text: str) -> bool:
    """True for the example title from the prompt, in any of its forms."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if PLACEHOLDER_PREFIX_RE.match(lowered):
        return True
    compact = re.sub(r"[\s_]+", "", lowered)
    return any(token.replace(" ", "") in compact for token in PLACEHOLDER_TOKENS)


def parse_marker_title(raw: str) -> LectureTitle:
    """Read a ``#LECTURE_TITLE:`` value.

    A placeholder prefix keeps its human half and drops the fake numbers, so
    ``Lxx_DCNxx_Probity in Governance`` still yields a usable title.
    """
    text = (raw or "").strip()
    if not text:
        return LectureTitle()
    match = PREFIX_RE.match(text)
    if match and not is_placeholder(match.group(0)):
        return LectureTitle(
            lecture_no=_normalise_number(match.group("lecture")),
            dcn_no=_normalise_number(match.group("dcn")),
            title=clean_title_text(match.group("title")),
        )
    placeholder = PLACEHOLDER_PREFIX_RE.match(text)
    if placeholder:
        return LectureTitle(title=clean_title_text(placeholder.group("title")))
    # A bare "Lecture: X" or a plain title.
    text = re.sub(r"^\s*(?:lecture|class|topic)\s*[:\-–]\s*", "", text, flags=re.I)
    return LectureTitle(title=clean_title_text(text))


def infer_from_filename(stem: str) -> LectureTitle:
    """Best effort from ``L03_DCN03_Probity_in_Governance.pptx``.

    Only the numbers are trusted from a file name. Its words become the title
    when nothing better exists, with the housekeeping tokens dropped.
    """
    name = (stem or "").strip()
    if not name:
        return LectureTitle()
    lecture = _FILE_LECTURE_RE.search(name)
    dcn = _FILE_DCN_RE.search(name)
    remainder = name
    for pattern in (_FILE_LECTURE_RE, _FILE_DCN_RE):
        remainder = pattern.sub(" ", remainder)
    words = [w for w in re.split(r"[\s._\-]+", remainder) if w]
    kept = [w for w in words if w.lower() not in _FILE_NOISE and not w.isdigit()]
    title = clean_title_text(" ".join(kept))
    if looks_like_hex_identifier(title):
        # An upload UUID is not a title. Returning it empty lets the caller
        # fall through to the section headings, which ARE derived from content.
        title = ""
    return LectureTitle(
        lecture_no=_normalise_number(lecture.group(1)) if lecture else "",
        dcn_no=_normalise_number(dcn.group(1)) if dcn else "",
        title=title,
        inferred=bool(lecture or dcn),
    )


def resolve_lecture_title(
    *,
    lecture_no: object = "",
    dcn_no: object = "",
    title: str = "",
    marker_title: str = "",
    filename_stem: str = "",
    headings: list[str] | None = None,
) -> LectureTitle:
    """Decide the final title from every source, most trusted first.

    User-typed metadata wins, then what the model emitted, then the file name,
    then the first real heading. Each field is resolved independently, so a run
    where the user typed only the lecture number still gets its title from the
    model.
    """
    typed_lecture = _normalise_number(lecture_no)
    typed_dcn = _normalise_number(dcn_no)
    typed_title = "" if is_placeholder(title) else clean_title_text(title)

    from_marker = parse_marker_title(marker_title)
    from_file = infer_from_filename(filename_stem)

    resolved_lecture = typed_lecture or from_marker.lecture_no or from_file.lecture_no
    resolved_dcn = typed_dcn or from_marker.dcn_no or from_file.dcn_no
    resolved_title = typed_title or from_marker.title or from_file.title
    if looks_like_hex_identifier(resolved_title):
        resolved_title = ""
    if not resolved_title:
        for heading in headings or []:
            candidate = clean_title_text(heading)
            if candidate and not is_placeholder(candidate):
                resolved_title = candidate
                break
    return LectureTitle(
        lecture_no=resolved_lecture,
        dcn_no=resolved_dcn,
        title=resolved_title,
        inferred=not (typed_lecture and typed_dcn) and bool(resolved_lecture or resolved_dcn),
    )


def longest_unbreakable_run(text: str) -> int:
    """Characters in the longest stretch Word cannot break.

    Word breaks at spaces and after a hyphen or slash; it will not break inside
    a word joined by underscores, which is exactly how the placeholder title
    overhung the column.
    """
    if not text:
        return 0
    return max((len(part) for part in re.split(r"[\s\-/–—]+", text)), default=0)


def wrap_for_display(text: str, *, max_run: int = MAX_UNBREAKABLE_CHARS) -> str:
    """Insert zero-width spaces so a long title can wrap.

    U+200B is a break opportunity that renders as nothing and copies as
    nothing, so the title still reads and still selects as one string. Applied
    only where a run is genuinely too long — a normal title is untouched.
    """
    if not text or longest_unbreakable_run(text) <= max_run:
        return text
    out: list[str] = []
    for part in re.split(r"(\s+)", text):
        if not part.strip() or len(part) <= max_run:
            out.append(part)
            continue
        # Prefer breaking after punctuation that already ends a sub-token.
        chunk = re.sub(r"(?<=[_,;:.)\]])(?=.)", "​", part)
        if longest_unbreakable_run(chunk.replace("​", " ")) > max_run:
            chunk = "​".join(chunk[i:i + max_run] for i in range(0, len(chunk), max_run))
        out.append(chunk)
    return "".join(out)


def title_marker_line(resolved: LectureTitle) -> str:
    """The ``#LECTURE_TITLE:`` line to write into the notes text."""
    return f"#LECTURE_TITLE: {resolved.display}"
