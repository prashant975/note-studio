"""Printable question and solution PDFs for a finalized practice selection.

The exporter deliberately consumes the selected slots in their displayed order and
never queries or re-samples the question bank.  It accepts the project's dataclass
records as well as mapping-shaped records, which keeps PDF generation independent
from workbook parsing and Streamlit session state.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html import escape as xml_escape
from html.parser import HTMLParser
from io import BytesIO
import os
from pathlib import Path
import re
import sys
from threading import Lock
from typing import Any, Callable, Iterable, Mapping, Sequence

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    FrameBreak,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


# Geometry follows the house DPP sample the content team supplied: "UPSC" in
# the corner, ONE header line holding the subject (centred) and the boxed
# "DPP: 1" (right), a thin rule, then two columns split by a rose rule.
PAGE_WIDTH, PAGE_HEIGHT = A4
OUTER_MARGIN = 29.15
COLUMN_GUTTER = 26.0
COLUMN_WIDTH = (PAGE_WIDTH - (2 * OUTER_MARGIN) - COLUMN_GUTTER) / 2
RIGHT_COLUMN_X = OUTER_MARGIN + COLUMN_WIDTH + COLUMN_GUTTER
BODY_BOTTOM = 78.0
HEADER_BASELINE = PAGE_HEIGHT - 34.0
HEADING_BASELINE = PAGE_HEIGHT - 70.5
BADGE_BOTTOM = PAGE_HEIGHT - 78.0
BADGE_HEIGHT = 19.0
HEADER_RULE_Y = PAGE_HEIGHT - 82.0
SECTION_BASELINE = PAGE_HEIGHT - 101.0
FIRST_BODY_TOP = PAGE_HEIGHT - 94.5
#: First page of a document whose section is named under the rule
#: ("Answer Key", "Hints & Solutions").
TITLED_BODY_TOP = PAGE_HEIGHT - 113.0
CONTINUATION_BODY_TOP = PAGE_HEIGHT - 52.0

BODY_SIZE = 10.0
BODY_LEADING = 16.0
HEADING_SIZE = 15.5
HEADING_MIN_SIZE = 11.0
BADGE_SIZE = 11.0
#: "Q1" hangs in this gutter; the question, its options and its solution all
#: start after it. It widens for "Q10" and up (see _styles).
QUESTION_GUTTER = 23.5
#: Where a numbered statement's number sits, measured from the stem's indent.
STATEMENT_NUMBER_INDENT = 4.5
SOLUTION_BULLET_TEXT_INDENT = 11.5
#: Below this, a table's text is too small to read comfortably on paper.
TABLE_SMALL_SIZE = 8.5
#: Left plus right cell padding, and a little air, in a prompt table.
TABLE_CELL_PADDING = 9.0
QUESTION_SPACING = 11.0
#: The title used when no subject was given (an older caller, or a teacher who
#: cleared the field). The layout is the same; only the words are generic.
FALLBACK_HEADING = "Practice Questions"

INK = HexColor("#202124")
MUTED_INK = HexColor("#5F6368")
HEADER_RULE = HexColor("#D9DCE1")
TABLE_RULE = HexColor("#3C4043")
LINK_BLUE = HexColor("#2A547D")
DIVIDER = HexColor("#C5B2B2")
WATERMARK = HexColor("#F3F3F3")
SUPPORT_YELLOW = HexColor("#F3C843")

_FONT_REGULAR = "PracticeNotoSansDevanagari"
_FONT_BOLD = "PracticeNotoSansDevanagari-Bold"
#: The Latin face, once a humanist sans has been found (see _body_fonts).
_LATIN_REGULAR = "PracticeSans"
_LATIN_BOLD = "PracticeSans-Bold"
#: A second humanist face kept only for the symbols the first one lacks.
_SPARE_REGULAR = "PracticeSansSpare"
_SPARE_BOLD = "PracticeSansSpare-Bold"
_FONT_LOCK = Lock()
_DEVANAGARI_RUN = re.compile(r"[\u0900-\u097f\ua8e0-\ua8ff\u200c\u200d]+")
_BREAK_TAG = re.compile(r"(?i)<br\s*/?>|</p\s*>|</li\s*>|</div\s*>")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")


@dataclass(frozen=True, slots=True)
class PracticePdfExport:
    """The printable documents generated from one finalized selection."""

    question_pdf: bytes
    solutions_pdf: bytes
    # Questions with their answer and solution inline — the take-home study
    # copy, as against the question paper that is sat and the key it is
    # marked with. Defaulted so older constructor call sites keep working.
    combined_pdf: bytes = b""

    @property
    def questions_pdf(self) -> bytes:
        """Plural compatibility alias used by UI/download call sites."""

        return self.question_pdf

    @property
    def solution_pdf(self) -> bytes:
        """Singular compatibility alias matching :attr:`question_pdf`."""

        return self.solutions_pdf


@dataclass(frozen=True, slots=True)
class _RenderableQuestion:
    question_id: str
    prompt: str
    prompt_markup: str | None
    options: tuple[str, ...]
    correct_option_indexes: tuple[int, ...]
    answer_text: str
    solution: str


def export_practice_pdfs(
    slots: Iterable[Any],
    questions: Iterable[Any] | Mapping[str, Any],
    language: Any,
    *,
    dpp_no: str = "",
    subject: str = "",
) -> PracticePdfExport:
    """Return question-only and answer/solution PDFs for ``slots``.

    The selected-slot sequence is the source of truth for order.  Missing
    questions, duplicate selected IDs, or an empty selection are rejected rather
    than silently producing a misleading document.  ``language`` may be a string
    or an enum with a ``value`` attribute (for example ``Language.HINDI``).
    """

    selected_slots = tuple(slots)
    if not selected_slots:
        raise ValueError("No selected questions are available for PDF export.")

    if isinstance(questions, Mapping):
        question_records = tuple(questions.values())
    else:
        question_records = tuple(questions)

    question_by_id: dict[str, Any] = {}
    for question in question_records:
        question_id = _question_id(question)
        if question_id and question_id not in question_by_id:
            question_by_id[question_id] = question

    target_language = _language_key(language)
    ordered: list[_RenderableQuestion] = []
    selected_ids: set[str] = set()
    for slot in selected_slots:
        question_id = _slot_question_id(slot)
        if not question_id:
            raise ValueError("A selected slot has no question ID.")
        if question_id in selected_ids:
            raise ValueError(f"Selected question ID is duplicated: {question_id}")
        try:
            question = question_by_id[question_id]
        except KeyError as exc:
            raise ValueError(
                f"Selected question ID was not found in the question set: {question_id}"
            ) from exc
        selected_ids.add(question_id)
        ordered.append(_renderable_question(question, target_language))

    _ensure_devanagari_fonts()
    _body_fonts()
    rendered = tuple(ordered)
    dpp_label = _dpp_label(dpp_no)
    subject = " ".join(str(subject or "").split())
    return PracticePdfExport(
        question_pdf=_build_question_pdf(rendered, target_language, dpp_label, subject),
        solutions_pdf=_build_solution_pdf(rendered, target_language, dpp_label, subject),
        combined_pdf=_build_combined_pdf(rendered, target_language, dpp_label, subject),
    )


def _field(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            candidate = value[name]
        else:
            candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return default


def _question_id(question: Any) -> str:
    return str(
        _field(question, "question_id", "qbgid", "unique_id", "id", default="")
    ).strip()


def _slot_question_id(slot: Any) -> str:
    if isinstance(slot, str):
        return slot.strip()
    return str(
        _field(slot, "question_id", "qbgid", "unique_id", "id", default="")
    ).strip()


def _language_key(language: Any) -> str:
    raw = getattr(language, "value", language)
    value = str(raw or "").strip().casefold().replace("_", "-")
    if "hindi" in value or "हिंदी" in value or "हिन्दी" in value:
        return "hindi"
    if "english" in value or value in {"en", "eng"}:
        return "english"
    return "unknown"


def _dpp_label(dpp_no: object) -> str:
    """The badge text, "DPP: 7", the way the house sample numbers a set.

    The app keeps its padded "07" for file names; the badge drops the padding
    because the printed sets never carry it.
    """
    digits = re.sub(r"\D", "", str(dpp_no or ""))
    return f"DPP: {int(digits)}" if digits else "DPP"


def _localized_item(question: Any, target_language: str) -> Any | None:
    localized = _field(
        question,
        "localized_content",
        "localized",
        "translations",
        default=(),
    )
    if isinstance(localized, Mapping):
        entries: Sequence[Any] = tuple(localized.values())
    elif isinstance(localized, Sequence) and not isinstance(localized, (str, bytes)):
        entries = localized
    else:
        entries = ()

    fallback = None
    for item in entries:
        fallback = fallback or item
        item_language = _language_key(_field(item, "language", "lang", default=""))
        if target_language != "unknown" and item_language == target_language:
            return item
    return fallback if target_language == "unknown" else None


#: Bidi and joiner controls the bank's exported text carries. They change no
#: word, but they defeat a plain comparison against the line they sit in.
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

#: A line that only restates the answer: "Ans: C", "Answer - (d)", "An: C",
#: "उत्तर: C", "सही उत्तर (C) है।".
_RESTATED_ANSWER = re.compile(
    r"^(?:ans|answer|an|सही\s*उत्तर|सही\s*विकल्प|उत्तर|उतर)"
    r"\s*[:.\u0964\-\u2013\u2014]?\s*"
    r"\(?\s*[A-Da-d1-4]\s*\)?"
    r"\s*(?:है|hai)?\s*[\u0964.]?$",
    re.IGNORECASE,
)

#: A bare label with nothing after it: "Exp:", "Explanation:", "व्याख्या:".
_RESTATED_LABEL = re.compile(
    r"^(?:exp|explanation|व्याख्या|स्पष्टीकरण)\s*[:.\u0964\-\u2013\u2014]?$",
    re.IGNORECASE,
)


#: The option a restated answer line names, e.g. "Ans: (d)" or "उत्तर B है।".
_STATED_OPTION = re.compile(
    r"[:.\u0964\-\u2013\u2014\s(]\s*([A-Da-d1-4])\s*\)?\s*(?:है|hai)?\s*[\u0964.]?$"
)


def _stated_option_index(line: str) -> int | None:
    match = _STATED_OPTION.search(line)
    if not match:
        return None
    token = match.group(1).upper()
    return int(token) - 1 if token.isdigit() else "ABCD".index(token)


def _strip_restated_answer(solution: str, correct_indexes: tuple[int, ...]) -> str:
    """Drop the "Ans: C / Exp:" preamble the bank keeps inside the body.

    The solutions PDF prints those two labels itself, from the option flags,
    so a body that opens by restating them renders the heading twice over.
    Nearly every question is written that way — 99% of the bank's English
    solutions and 98% of the Hindi ones — so this is what the page looks like
    rather than an edge case.

    A restatement is only removed when it names the same option as the answer
    key. About 200 questions in the bank name a different one — usually a
    Hindi row whose options were reordered while the prose kept the English
    letter — and there the duplicate is the only sign on the page that the two
    disagree. Which of them is right is not something this can decide, so the
    contradiction is left visible rather than tidied away into a single answer
    that may be the wrong one.

    Only a line that is *nothing but* the restatement is ever dropped; a line
    carrying explanation along with it is left exactly as it is.
    """
    lines = solution.split("\n")
    start = 0
    for index, line in enumerate(lines):
        probe = _INVISIBLE.sub("", line).strip()
        if not probe:
            start = index + 1
            continue
        if _RESTATED_LABEL.match(probe):
            start = index + 1
            continue
        if _RESTATED_ANSWER.match(probe):
            stated = _stated_option_index(probe)
            if stated is None or stated not in correct_indexes:
                break
            start = index + 1
            continue
        break
    return "\n".join(lines[start:]).strip()


def _renderable_question(question: Any, target_language: str) -> _RenderableQuestion:
    localized = _localized_item(question, target_language)
    source = localized if localized is not None else question

    prompt = _coerce_text(
        _field(source, "prompt", "question", "content", "text", default="")
    )
    if not prompt and source is not question:
        prompt = _coerce_text(_field(question, "prompt", "question", default=""))
    if not prompt:
        prompt = "Question text unavailable."

    prompt_markup = _coerce_prompt_markup(
        _field(source, "prompt_markup", default=None)
    )
    if prompt_markup is None and source is not question:
        prompt_markup = _coerce_prompt_markup(
            _field(question, "prompt_markup", default=None)
        )

    raw_options = _field(source, "options", "choices", default=())
    if not raw_options and source is not question:
        raw_options = _field(question, "options", "choices", default=())
    options, option_flags = _coerce_options(raw_options)

    indexes = _coerce_indexes(
        _field(
            source,
            "correct_option_indexes",
            "correct_indexes",
            "answer_indexes",
            default=(),
        ),
        option_count=len(options),
    )
    if not indexes and option_flags:
        indexes = option_flags

    raw_answer = _field(
        source,
        "answer_text",
        "correct_answer",
        "answer",
        "answer_key",
        "correct_option",
        default="",
    )
    answer_text = _answer_label(indexes, raw_answer, len(options))

    solution = _coerce_text(
        _field(
            source,
            "solution",
            "detailed_solution",
            "explanation",
            "solution_text",
            "rationale",
            default="",
        )
    )
    if not solution and source is not question:
        solution = _coerce_text(
            _field(
                question,
                "solution",
                "detailed_solution",
                "explanation",
                default="",
            )
        )

    return _RenderableQuestion(
        question_id=_question_id(question),
        prompt=prompt,
        prompt_markup=prompt_markup,
        options=options,
        correct_option_indexes=indexes,
        answer_text=answer_text,
        solution=_strip_restated_answer(solution, indexes),
    )


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        value = _field(value, "text", "content", "value", default="")
    text = str(value)
    if "<" in text and ">" in text:
        text = _BREAK_TAG.sub("\n", text)
        text = _HTML_TAG.sub("", text)
    text = unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def _coerce_prompt_markup(value: Any) -> str | None:
    """Return table-bearing prompt markup without treating it as PDF markup.

    All content eventually enters ReportLab through :func:`_markup`, which
    escapes it.  This raw value is used only by the small HTML table parser
    below; regular HTML-bearing values continue through ``_coerce_text``.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        value = _field(value, "text", "content", "value", default="")
    markup = str(value).strip()
    return markup if re.search(r"<table\b", markup, re.IGNORECASE) else None


def _coerce_options(value: Any) -> tuple[tuple[str, ...], tuple[int, ...]]:
    if not value:
        return (), ()
    if isinstance(value, Mapping):
        values = tuple(value.values())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = tuple(value)
    else:
        values = (value,)

    options: list[str] = []
    correct: list[int] = []
    for item in values:
        if isinstance(item, Mapping):
            text = _coerce_text(_field(item, "text", "option", "content", default=""))
            is_correct = bool(
                _field(item, "isCorrect", "is_correct", "correct", default=False)
            )
        else:
            text = _coerce_text(item)
            is_correct = False
        if text:
            if is_correct:
                correct.append(len(options))
            options.append(text)
    return tuple(options), tuple(correct)


def _coerce_indexes(value: Any, *, option_count: int) -> tuple[int, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = value
    else:
        values = (value,)
    indexes: list[int] = []
    for item in values:
        try:
            index = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= index < option_count and index not in indexes:
            indexes.append(index)
    return tuple(indexes)


def _answer_label(indexes: tuple[int, ...], raw_answer: Any, option_count: int) -> str:
    if indexes:
        return ", ".join(_option_letter(index) for index in indexes)

    answer = _coerce_text(raw_answer)
    compact = answer.strip().upper()
    letter_match = re.fullmatch(r"\(?\s*([A-Z])\s*\)?", compact)
    if letter_match:
        return letter_match.group(1)
    if compact.isdigit() and option_count:
        number = int(compact)
        if 1 <= number <= option_count:
            return _option_letter(number - 1)
    return answer


def _option_letter(index: int) -> str:
    if 0 <= index < 26:
        return chr(ord("A") + index)
    return str(index + 1)


def _font_candidates(filename: str) -> tuple[Path, ...]:
    # parents[2], not parents[1]: this package now lives at src/notes_to_dpp/
    # inside the notes app rather than at the root of its own project, so one
    # more level is needed to reach assets/. ROOT_DIR is preferred over the
    # relative walk because it already honours HANDWRITTEN_NOTES_ROOT and the
    # frozen-bundle layout.
    try:
        from ..config import ROOT_DIR

        project_root = Path(ROOT_DIR)
    except Exception:  # pragma: no cover - standalone use, outside the app
        project_root = Path(__file__).resolve().parents[2]
    candidates = [project_root / "assets" / "fonts" / filename]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.insert(0, Path(bundle_root) / "assets" / "fonts" / filename)
    candidates.append(Path(sys.executable).resolve().parent / "assets" / "fonts" / filename)
    return tuple(dict.fromkeys(candidates))


def _find_font(filename: str) -> Path:
    candidates = _font_candidates(filename)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    locations = ", ".join(str(path) for path in candidates)
    raise RuntimeError(f"Bundled Devanagari font is missing; checked: {locations}")


def _ensure_devanagari_fonts() -> None:
    if _FONT_REGULAR in pdfmetrics.getRegisteredFontNames():
        return
    with _FONT_LOCK:
        if _FONT_REGULAR in pdfmetrics.getRegisteredFontNames():
            return
        regular = _find_font("NotoSansDevanagari-Regular.ttf")
        bold = _find_font("NotoSansDevanagari-Bold.ttf")
        pdfmetrics.registerFont(TTFont(_FONT_REGULAR, str(regular), shapable=True))
        pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(bold), shapable=True))
        pdfmetrics.registerFontFamily(
            _FONT_REGULAR,
            normal=_FONT_REGULAR,
            bold=_FONT_BOLD,
            italic=_FONT_REGULAR,
            boldItalic=_FONT_BOLD,
        )


#: Humanist sans faces for the Latin text, closest to the house sample first.
#: The sample is set in Noto Sans, used wherever the machine has it installed;
#: Segoe UI, on every Windows, is the near-identical face otherwise. Neither is
#: bundled or downloaded: the app's fonts are installed on the machine, as for
#: the notes (see assets/README.md). A file of the same name dropped into
#: assets/fonts is used first. With neither present the PDFs fall back to
#: Helvetica, the face they always used, so an export never fails over a font.
_LATIN_FACES = (
    ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf"),
    ("segoeui.ttf", "segoeuib.ttf"),
)

#: Body faces once resolved: (regular, bold, spare-or-None).
_RESOLVED_FACES: tuple[str, str, str | None] | None = None
#: Per-character font routes, built once the faces are known.
_ROUTES: tuple[Callable[[str], bool], tuple[tuple[str, Callable[[str], bool]], ...]] | None = None
_BOLD_OF = {_FONT_REGULAR: _FONT_BOLD, _SPARE_REGULAR: _SPARE_BOLD}


def _system_font_dirs() -> tuple[Path, ...]:
    dirs: list[Path] = []
    windows = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windows:
        dirs.append(Path(windows) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    dirs.extend((
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/noto"),
        Path("/usr/share/fonts/google-noto"),
        Path("/Library/Fonts"),
        Path.home() / "Library" / "Fonts",
    ))
    return tuple(dirs)


def _installed_font(filename: str) -> Path | None:
    candidates = (*_font_candidates(filename),
                  *(folder / filename for folder in _system_font_dirs()))
    return next((path for path in candidates if path.is_file()), None)


def _register_face(regular_name: str, bold_name: str, files: tuple[str, str]) -> bool:
    paths = [_installed_font(name) for name in files]
    if not all(paths):
        return False
    try:
        # Not shaped: Latin needs no shaping, and harfbuzz's "fi"/"fl"
        # ligatures would make the printed text copy out as one glyph.
        regular = TTFont(regular_name, str(paths[0]), shapable=False)
        bold = TTFont(bold_name, str(paths[1]), shapable=False)
    except Exception:  # noqa: BLE001 — a damaged or embed-restricted file: try the next
        return False
    pdfmetrics.registerFont(regular)
    pdfmetrics.registerFont(bold)
    pdfmetrics.registerFontFamily(
        regular_name, normal=regular_name, bold=bold_name,
        italic=regular_name, boldItalic=bold_name)
    return True


def _body_fonts() -> tuple[str, str]:
    """(regular, bold) for everything that is not Devanagari."""
    global _RESOLVED_FACES
    if _RESOLVED_FACES is None:
        with _FONT_LOCK:
            if _RESOLVED_FACES is None:
                chosen = next(
                    (index for index, files in enumerate(_LATIN_FACES)
                     if _register_face(_LATIN_REGULAR, _LATIN_BOLD, files)),
                    None)
                spare = None
                if chosen is not None:
                    # A later face covers symbols the chosen one lacks (Noto
                    # Sans has no arrows or geometric bullets; Segoe UI has).
                    for files in _LATIN_FACES[chosen + 1:]:
                        if _register_face(_SPARE_REGULAR, _SPARE_BOLD, files):
                            spare = _SPARE_REGULAR
                            break
                    _RESOLVED_FACES = (_LATIN_REGULAR, _LATIN_BOLD, spare)
                else:
                    _RESOLVED_FACES = ("Helvetica", "Helvetica-Bold", None)
    return _RESOLVED_FACES[0], _RESOLVED_FACES[1]


def _encodes(encoding: str) -> Callable[[str], bool]:
    def covers(character: str) -> bool:
        try:
            character.encode(encoding)
        except (UnicodeError, LookupError):
            return False
        return True

    return covers


def _ttf_covers(font_name: str) -> Callable[[str], bool]:
    try:
        face = getattr(pdfmetrics.getFont(font_name), "face", None)
    except Exception:  # noqa: BLE001 — unregistered: covers nothing
        face = None
    codepoints = frozenset(getattr(face, "charToGlyph", None) or ())
    return lambda character: ord(character) in codepoints


def _glyph_routes():
    """(does the body face cover it?, ordered fallbacks for what it lacks)."""
    global _ROUTES
    if _ROUTES is None:
        _ensure_devanagari_fonts()
        regular, _ = _body_fonts()
        spare = _RESOLVED_FACES[2] if _RESOLVED_FACES else None
        if regular == "Helvetica":
            # reportlab draws what WinAnsi lacks from Symbol/ZapfDingbats on
            # its own; only the Noto file's extras (the rupee sign) need help.
            base = _encodes("cp1252")
            fallbacks = ((_FONT_REGULAR, _ttf_covers(_FONT_REGULAR)),)
        else:
            base = _ttf_covers(regular)
            fallbacks = (
                *(((spare, _ttf_covers(spare)),) if spare else ()),
                (_FONT_REGULAR, _ttf_covers(_FONT_REGULAR)),
                ("Symbol", _encodes("symbol")),
                ("ZapfDingbats", _encodes("zapfdingbats")),
            )
        _ROUTES = (base, fallbacks)
    return _ROUTES


#: Bidi and zero-width marks the bank's text carries. They print as empty
#: boxes in a face that has no glyph for them, and they change no word. The
#: Devanagari joiners (U+200C/U+200D) are not among them: they shape conjuncts.
_PRINTED_INVISIBLE = re.compile(r"[\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def _runs(text: str) -> list[tuple[str | None, str]]:
    """Split ``text`` into (font, run) pieces; ``None`` is the paragraph's face.

    Devanagari always goes to the bundled Noto file. Anything else the body
    face cannot draw goes to the first fallback that can, so an arrow or a
    rupee sign prints as itself rather than as an empty box.
    """
    base, fallbacks = _glyph_routes()
    runs: list[tuple[str | None, str]] = []
    for character in _PRINTED_INVISIBLE.sub("", text):
        if _DEVANAGARI_RUN.match(character):
            font = _FONT_REGULAR
        elif character == "\n" or base(character):
            font = None
        else:
            font = next((name for name, covers in fallbacks if covers(character)), None)
        if runs and runs[-1][0] == font:
            runs[-1] = (font, runs[-1][1] + character)
        else:
            runs.append((font, character))
    return runs


def _markup(text: str, *, devanagari_bold: bool = False) -> str:
    pieces: list[str] = []
    for font, run in _runs(text):
        escaped = xml_escape(run, quote=False)
        if font is None:
            pieces.append(escaped)
            continue
        if devanagari_bold and font == _FONT_REGULAR:
            font = _FONT_BOLD
        pieces.append(f'<font name="{font}">{escaped}</font>')
    return "".join(pieces).replace("\n", "<br/>")


def _text_width(text: str, size: float, *, bold: bool = False) -> float:
    """Printed width of one line of ``text``, fallback faces included."""
    regular, bold_face = _body_fonts()
    width = 0.0
    for font, run in _runs(text):
        if font is None:
            name = bold_face if bold else regular
        else:
            name = _BOLD_OF.get(font, font) if bold else font
        width += pdfmetrics.stringWidth(run, name, size)
    return width


@dataclass(frozen=True, slots=True)
class _HtmlTableCell:
    text: str
    is_header: bool
    colspan: int = 1
    rowspan: int = 1


@dataclass(frozen=True, slots=True)
class _HtmlTable:
    rows: tuple[tuple[str, ...], ...]
    header_rows: int
    spans: tuple[tuple[int, int, int, int], ...]


@dataclass(frozen=True, slots=True)
class _PromptPart:
    text: str = ""
    table: _HtmlTable | None = None


def _start_tag_text(parser: HTMLParser, tag: str, attrs: Any) -> str:
    """Rebuild a tag only when HTMLParser did not retain its source spelling."""
    source = parser.get_starttag_text()
    if source:
        return source
    rendered = " ".join(
        name if value is None else f'{name}="{xml_escape(str(value), quote=True)}"'
        for name, value in attrs
    )
    return f"<{tag}{(' ' + rendered) if rendered else ''}>"


def _html_fragment_text(markup: str) -> str:
    """Use the workbook's proven list-aware plain-text conversion when present."""
    try:
        # The parser is deliberately shared rather than duplicating its ordered
        # list semantics here.  It does not import this exporter, so there is no
        # cycle.
        from .workbooks import _strip_html

        return _strip_html(markup)
    except Exception:  # pragma: no cover - standalone fallback only
        return _coerce_text(markup)


def _span_value(attrs: Any, name: str) -> int:
    for attr_name, value in attrs:
        if str(attr_name).casefold() != name:
            continue
        try:
            return max(1, int(str(value).strip()))
        except (TypeError, ValueError):
            return 1
    return 1


class _HtmlTableParser(HTMLParser):
    """Extract cells, including basic row/column spans, from one HTML table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._table_depth = 0
        self._rows: list[list[_HtmlTableCell]] = []
        self._row_headers: list[bool] = []
        self._current_row: list[_HtmlTableCell] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_header = False
        self._cell_colspan = 1
        self._cell_rowspan = 1

    def _append_cell_markup(self, markup: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(markup)

    def _finish_cell(self) -> None:
        if self._cell_parts is None:
            return
        if self._current_row is None:
            self._current_row = []
        self._current_row.append(
            _HtmlTableCell(
                text=_html_fragment_text("".join(self._cell_parts)),
                is_header=self._cell_header,
                colspan=self._cell_colspan,
                rowspan=self._cell_rowspan,
            )
        )
        self._cell_parts = None

    def _finish_row(self) -> None:
        self._finish_cell()
        if self._current_row is None:
            return
        if self._current_row:
            self._rows.append(self._current_row)
            self._row_headers.append(all(cell.is_header for cell in self._current_row))
        self._current_row = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        name = tag.casefold()
        source = _start_tag_text(self, tag, attrs)
        if name == "table":
            if self._table_depth:
                self._append_cell_markup(source)
            self._table_depth += 1
            return
        if not self._table_depth:
            return
        if self._table_depth > 1:
            self._append_cell_markup(source)
            return
        if name == "tr":
            self._finish_row()
            self._current_row = []
        elif name in {"td", "th"}:
            self._finish_cell()
            if self._current_row is None:
                self._current_row = []
            self._cell_parts = []
            self._cell_header = name == "th"
            self._cell_colspan = _span_value(attrs, "colspan")
            self._cell_rowspan = _span_value(attrs, "rowspan")
        else:
            self._append_cell_markup(source)

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        self._append_cell_markup(_start_tag_text(self, tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name == "table":
            if self._table_depth > 1:
                self._append_cell_markup(f"</{tag}>")
            elif self._table_depth == 1:
                self._finish_row()
            self._table_depth = max(0, self._table_depth - 1)
            return
        if not self._table_depth:
            return
        if self._table_depth > 1:
            self._append_cell_markup(f"</{tag}>")
            return
        if name in {"td", "th"}:
            self._finish_cell()
        elif name == "tr":
            self._finish_row()
        else:
            self._append_cell_markup(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._table_depth:
            self._append_cell_markup(xml_escape(data, quote=False))

    def table(self) -> _HtmlTable | None:
        self._finish_row()
        if not self._rows:
            return None

        values: dict[tuple[int, int], str] = {}
        occupied: set[tuple[int, int]] = set()
        spans: list[tuple[int, int, int, int]] = []
        widest = 0
        deepest = len(self._rows)
        for row_index, row in enumerate(self._rows):
            column = 0
            for cell in row:
                while (row_index, column) in occupied:
                    column += 1
                values[(row_index, column)] = cell.text
                end_row = row_index + cell.rowspan - 1
                end_column = column + cell.colspan - 1
                deepest = max(deepest, end_row + 1)
                widest = max(widest, end_column + 1)
                if cell.colspan > 1 or cell.rowspan > 1:
                    spans.append((column, row_index, end_column, end_row))
                for covered_row in range(row_index, end_row + 1):
                    for covered_column in range(column, end_column + 1):
                        if (covered_row, covered_column) != (row_index, column):
                            occupied.add((covered_row, covered_column))
                column = end_column + 1

        if not widest:
            return None
        rows = tuple(
            tuple(values.get((row_index, column), "") for column in range(widest))
            for row_index in range(deepest)
        )
        header_rows = 0
        for is_header in self._row_headers:
            if not is_header:
                break
            header_rows += 1
        return _HtmlTable(rows=rows, header_rows=header_rows, spans=tuple(spans))


class _QuestionMarkupParser(HTMLParser):
    """Split a prompt into normal text and separately renderable tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._outside: list[str] = []
        self._table: list[str] | None = None
        self._table_depth = 0
        self._parts: list[_PromptPart] = []

    def _target(self) -> list[str]:
        return self._table if self._table is not None else self._outside

    def _flush_outside(self) -> None:
        text = _html_fragment_text("".join(self._outside))
        self._outside = []
        if text:
            self._parts.append(_PromptPart(text=text))

    def _finish_table(self) -> None:
        markup = "".join(self._table or ())
        self._table = None
        try:
            parser = _HtmlTableParser()
            parser.feed(markup)
            parser.close()
            table = parser.table()
        except Exception:
            table = None
        if table is not None:
            self._parts.append(_PromptPart(table=table))
        else:
            text = _html_fragment_text(markup)
            if text:
                self._parts.append(_PromptPart(text=text))

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        name = tag.casefold()
        source = _start_tag_text(self, tag, attrs)
        if name == "table":
            if not self._table_depth:
                self._flush_outside()
                self._table = []
            self._target().append(source)
            self._table_depth += 1
            return
        self._target().append(source)

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        self._target().append(_start_tag_text(self, tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name == "table" and self._table_depth:
            self._target().append(f"</{tag}>")
            self._table_depth -= 1
            if not self._table_depth:
                self._finish_table()
            return
        self._target().append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._target().append(xml_escape(data, quote=False))

    def parts(self) -> tuple[_PromptPart, ...]:
        if self._table is not None:
            # A malformed, unclosed table is still shown as ordinary text.
            self._finish_table()
            self._table_depth = 0
        self._flush_outside()
        return tuple(self._parts)


def _prompt_parts(markup: str | None) -> tuple[_PromptPart, ...]:
    if not markup:
        return ()
    try:
        parser = _QuestionMarkupParser()
        parser.feed(markup)
        parser.close()
        return parser.parts()
    except Exception:
        return ()


_MARKDOWN_LINE_START = re.compile(r"^(\s*)(\d+)([.)])(\s)|^(\s*)([-*+#>])(\s)")


def _preview_lines(text: str) -> str:
    """Keep every line of a question on its own line in the preview.

    Markdown turns "1. …" into a list and folds the next line into the last
    item, so the stem "Which of the statements…" was shown as part of
    statement 2. Escaping the list markers keeps the numbering as printed, and
    a hard break keeps each line separate — the way the PDF shows it.
    """
    lines = []
    for line in (text or "").splitlines():
        line = _MARKDOWN_LINE_START.sub(
            lambda m: (f"{m[1]}{m[2]}\\{m[3]}{m[4]}" if m[2]
                       else f"{m[5]}\\{m[6]}{m[7]}"), line)
        lines.append(line)
    return "  \n".join(lines)


def prompt_preview_markdown(prompt: str, markup: str | None = None) -> str:
    """Keep the same prose/table order in the preview as in the printed paper."""
    parts = _prompt_parts(markup)
    if not any(part.table is not None for part in parts):
        return _preview_lines(prompt)

    def cell(text: str) -> str:
        text = xml_escape(" ".join(text.split()))
        return re.sub(r"([\\`*_\[\]|])", r"\\\1", text)

    blocks = []
    for part in parts:
        if part.table is None:
            blocks.append(_preview_lines(part.text))
            continue
        table = part.table
        rows = table.rows
        # Empty headers preserve the first data row when HTML has no th cells.
        # Markdown does not have merged cells: retain the rectangular grid,
        # including empty cells covered by a row/column span.
        header = rows[0] if table.header_rows else ("",) * len(rows[0])
        body = rows[1:] if table.header_rows else rows
        lines = ["| " + " | ".join(cell(value) for value in header) + " |",
                 "| " + " | ".join("---" for _ in header) + " |"]
        lines.extend("| " + " | ".join(cell(value) for value in row) + " |"
                     for row in body)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _styles(question_count: int = 1) -> dict[str, ParagraphStyle]:
    """Paragraph styles for one document; ``styles["stem"].leftIndent`` is
    the question gutter every other indent is measured from."""
    regular, bold = _body_fonts()
    base = dict(
        fontName=regular,
        fontSize=BODY_SIZE,
        leading=BODY_LEADING,
        textColor=INK,
        alignment=TA_LEFT,
        allowWidows=0,
        allowOrphans=0,
        splitLongWords=1,
        shaping=True,
        bulletFontName=regular,
        bulletFontSize=BODY_SIZE,
        bulletIndent=0,
    )

    def style(name: str, **overrides: Any) -> ParagraphStyle:
        settings = {**base, **overrides}
        return ParagraphStyle(name, **settings)

    # reportlab keeps 0.6 em clear after a bullet and pushes the first line
    # right when the gutter is narrower than that, so "Q10" and up would
    # start their stem out of line with its own second line.
    widest_label = pdfmetrics.stringWidth(f"Q{max(question_count, 1)}", bold, BODY_SIZE)
    gutter = max(QUESTION_GUTTER, widest_label + 0.6 * BODY_SIZE + 0.5)
    return {
        # "Q1" is the stem's bullet, so it hangs in the gutter and every line
        # of the question, wrapped ones included, starts after it.
        "stem": style("PracticeStem", leftIndent=gutter, bulletFontName=bold),
        # Statements and options carry their own marker inline, with the
        # wrapped lines hanging after it (see _hanging); a reportlab bullet
        # would force a gap twice the width of a space.
        "statement": style("PracticeStatement"),
        "option": style("PracticeOption"),
        "grid_option": style("PracticeGridOption"),
        "gutter_label": style("PracticeGutterLabel", fontName=bold),
        "table_header": style("PracticeTableHeader", fontName=bold, leading=13.5),
        "table_cell": style("PracticeTableCell", leading=13.5),
        "table_header_small": style(
            "PracticeTableHeaderSmall", fontName=bold, fontSize=TABLE_SMALL_SIZE,
            leading=11),
        "table_cell_small": style(
            "PracticeTableCellSmall", fontSize=TABLE_SMALL_SIZE, leading=11),
        "answer": style(
            "PracticeAnswerKey",
            fontSize=11,
            leading=21.75,
            leftIndent=22,
            spaceAfter=1,
        ),
        "solution_heading": style(
            "PracticeSolutionHeading",
            fontName=bold,
            leftIndent=gutter,
            bulletFontName=bold,
            keepWithNext=True,
        ),
        "solution_label": style(
            "PracticeSolutionLabel",
            fontName=bold,
            leftIndent=gutter,
            keepWithNext=True,
        ),
        "answer_line": style(
            "PracticeAnswerLine",
            fontName=bold,
            leftIndent=gutter,
            spaceBefore=4,
        ),
        "solution": style("PracticeSolutionBody", leftIndent=gutter),
        "solution_bullet": style(
            "PracticeSolutionBullet",
            leftIndent=gutter + SOLUTION_BULLET_TEXT_INDENT,
            bulletIndent=gutter,
        ),
        "unavailable": style(
            "PracticeSolutionUnavailable",
            leftIndent=gutter,
            textColor=MUTED_INK,
        ),
    }


def _widest_line(text: str, size: float, *, bold: bool) -> float:
    return max((_text_width(line, size, bold=bold) for line in text.split("\n")),
               default=0.0)


def _auto_widths(table: _HtmlTable, size: float, available: float) -> tuple[float, ...] | None:
    """Column widths the way a browser lays out an auto-width table.

    Each column gets at least its longest word, so no word is cut in two;
    a table whose lines all fit hugs its content; otherwise the room left
    over goes to the columns in proportion to how much more they would like.
    None when even the longest words do not fit side by side at ``size``.
    """
    rows = table.rows
    columns = max(len(row) for row in rows)
    natural: list[float] = []
    minimum: list[float] = []
    for column in range(columns):
        cells = [(row[column], index < table.header_rows) for index, row in enumerate(rows)]
        natural.append(max(_widest_line(text, size, bold=bold) for text, bold in cells)
                       + TABLE_CELL_PADDING)
        minimum.append(max(
            max((_text_width(word, size, bold=bold) for word in text.split()), default=0.0)
            for text, bold in cells) + TABLE_CELL_PADDING)
    if sum(natural) <= available:
        return tuple(natural)
    if sum(minimum) > available:
        return None
    spare = available - sum(minimum)
    wants = [max(0.0, wide - narrow) for wide, narrow in zip(natural, minimum)]
    total = sum(wants) or 1.0
    return tuple(narrow + spare * want / total for narrow, want in zip(minimum, wants))


def _table_layout(table: _HtmlTable, available: float) -> tuple[tuple[float, ...], bool]:
    """(column widths, set at body size?) for one prompt table.

    The body size is preferred, as in the house sample's pairs table; a table
    too wide for it drops to a smaller size before any word has to be cut.
    Spanned tables, and those that fit neither way, share the room by content
    length so that ID columns stay narrow and explanatory ones get the space.
    """
    rows = table.rows
    columns = max((len(row) for row in rows), default=0)
    if not columns:
        return (), True
    if not table.spans:
        for size in (BODY_SIZE, TABLE_SMALL_SIZE):
            widths = _auto_widths(table, size, available)
            if widths is not None:
                return widths, size == BODY_SIZE

    minimum = max(15.0, min(28.0, available / (columns * 2)))
    remaining = max(0.0, available - (minimum * columns))
    weights: list[float] = []
    for column in range(columns):
        longest = max(
            (len(re.sub(r"\s+", " ", row[column]).strip()) for row in rows),
            default=0,
        )
        weights.append(max(1.0, min(12.0, float(max(longest, 1)) ** 0.55)))
    total = sum(weights)
    return tuple(minimum + (remaining * weight / total) for weight in weights), False


def _question_table(
    table: _HtmlTable,
    styles: dict[str, ParagraphStyle],
    *,
    label: str | None = None,
) -> Table:
    """A bordered, split-safe table set at the stem's indent.

    The first column is the question gutter: empty, unless the stem opens
    with the table, in which case "Q1" sits there as it would beside text.
    """
    gutter_width = styles["stem"].leftIndent
    widths, body_size = _table_layout(table, COLUMN_WIDTH - gutter_width - 2)
    header_style = styles["table_header" if body_size else "table_header_small"]
    cell_style = styles["table_cell" if body_size else "table_cell_small"]
    data: list[list[Any]] = []
    for row_index, row in enumerate(table.rows):
        is_header = row_index < table.header_rows
        gutter: Any = ""
        if row_index == 0 and label:
            gutter = Paragraph(label, styles["gutter_label"])
        data.append(
            [gutter]
            + [
                Paragraph(
                    _markup(cell, devanagari_bold=is_header),
                    header_style if is_header else cell_style,
                )
                for cell in row
            ]
        )
    rendered = Table(
        data,
        colWidths=(gutter_width, *widths),
        repeatRows=table.header_rows,
        hAlign="LEFT",
        splitByRow=1,
    )
    commands: list[tuple[Any, ...]] = [
        ("GRID", (1, 0), (-1, -1), 0.5, TABLE_RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, -1), 0),
        ("LEFTPADDING", (1, 0), (-1, -1), 4),
        ("RIGHTPADDING", (1, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    commands.extend(
        ("SPAN", (left + 1, top), (right + 1, bottom))
        for left, top, right, bottom in table.spans
    )
    rendered.setStyle(TableStyle(commands))
    return rendered


def _hanging(
    marker: str,
    body: str,
    style: ParagraphStyle,
    *,
    indent: float,
    **overrides: Any,
) -> Paragraph:
    """``marker`` and ``body`` as one paragraph whose wrapped lines hang
    after the marker: "1. The Chola king..." / "   all authority...".

    The marker is ordinary text followed by an ordinary space, as a browser
    sets it; a reportlab bullet would keep 0.6 em clear instead.
    """
    lead = f"{marker} "
    hang = _text_width(lead, style.fontSize)
    hung = ParagraphStyle(
        f"{style.name}-hung",
        parent=style,
        leftIndent=indent + hang,
        firstLineIndent=-hang,
        **overrides,
    )
    return Paragraph(_markup(lead + body), hung)


#: A numbered statement inside a stem: "1.", "2)", "(3)", "iv.", "(b)".
#: Lower-case letters only, so a name such as "A. P. J. Abdul Kalam" or
#: "C. Rajagopalachari" opening a line is not mistaken for one.
_STATEMENT = re.compile(r"^(\(?(?:\d{1,2}|[ivx]{1,5}|[IVX]{1,5}|[a-h])[.)])\s+(?=\S)")


def _prompt_flowables(
    text: str,
    styles: dict[str, ParagraphStyle],
    *,
    label: str | None,
) -> list[Any]:
    """The stem as the house sample sets it.

    Stem lines start after the gutter; numbered statements are indented as a
    list beneath them, with a little air either side; the line that asks the
    question ("Which of the statements...") returns to the stem's indent.
    """
    entries: list[tuple[str | None, str]] = []
    for line in (raw.strip() for raw in text.split("\n")):
        if not line:
            continue
        match = _STATEMENT.match(line)
        # The opening line carries "Q1" as its bullet, so it stays a stem
        # line even when it happens to be numbered.
        if match and not (label and not entries):
            entries.append((match.group(1), line[match.end():]))
        else:
            entries.append((None, line))

    blocks: list[tuple[bool, list[tuple[str | None, str]]]] = []
    for marker, body in entries:
        is_list = marker is not None
        if blocks and blocks[-1][0] == is_list:
            blocks[-1][1].append((marker, body))
        else:
            blocks.append((is_list, [(marker, body)]))

    gutter = styles["stem"].leftIndent
    flowables: list[Any] = []
    for block_index, (is_list, items) in enumerate(blocks):
        if not is_list:
            for _, body in items:
                bullet = label if (label and not flowables) else None
                flowables.append(Paragraph(_markup(body), styles["stem"], bulletText=bullet))
            continue
        followed = block_index < len(blocks) - 1
        for item_index, (marker, body) in enumerate(items):
            first, last = item_index == 0, item_index == len(items) - 1
            flowables.append(_hanging(
                marker or "", body, styles["statement"],
                indent=gutter + STATEMENT_NUMBER_INDENT,
                spaceBefore=6 if first and flowables else 0,
                spaceAfter=(6 if followed else 3) if last else 0,
            ))
    return flowables


def _option_flowables(
    options: tuple[str, ...],
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    """Options at the stem's indent: a 2x2 grid when all four are short.

    "(A) Dramma   (B) Pana / (C) Dinara   (D) Jittal" is how the house sample
    prints short options, while "(B) Rajendra Chola I" there gets a line of
    its own. The line between the two is an option filling at most half of
    its grid cell, so the two columns still read as separate choices. Any
    longer set goes one per line, wrapped lines hanging after the letter.
    """
    if not options:
        return []
    gutter = styles["stem"].leftIndent
    labels = [f"({_option_letter(index)})" for index in range(len(options))]
    half = (COLUMN_WIDTH - gutter) / 2
    if len(options) == 4 and all(
        "\n" not in option and _text_width(f"{label} {option}", BODY_SIZE) <= half / 2
        for label, option in zip(labels, options)
    ):
        cells = [_hanging(label, option, styles["grid_option"], indent=0)
                 for label, option in zip(labels, options)]
        grid = Table(
            [["", cells[0], cells[1]], ["", cells[2], cells[3]]],
            colWidths=(gutter, half, half),
            hAlign="LEFT",
        )
        grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return [grid]

    return [_hanging(label, option, styles["option"], indent=gutter)
            for label, option in zip(labels, options)]


def _question_flowables(
    question: _RenderableQuestion,
    number: int,
    styles: dict[str, ParagraphStyle],
) -> tuple[list[Any], bool]:
    """One question and its options; True when its stem carries a table.

    Table-bearing stems are rendered as prose/table/prose rather than one
    paragraph, so that the table keeps its rows and columns.
    """
    label = f"Q{number}"
    parts = _prompt_parts(question.prompt_markup)
    has_table = any(part.table is not None for part in parts)
    flowables: list[Any] = []
    if has_table:
        for part in parts:
            pending = None if flowables else label
            if part.table is not None:
                flowables.append(_question_table(part.table, styles, label=pending))
                flowables.append(Spacer(1, 4))
            elif part.text:
                flowables.extend(_prompt_flowables(part.text, styles, label=pending))
    if not flowables:
        has_table = False
        flowables = _prompt_flowables(question.prompt, styles, label=label)
    flowables.extend(_option_flowables(question.options, styles))
    return flowables, has_table


#: A solution line that opens on a verdict: "Statement 1 is incorrect:",
#: "Pair 2 is correctly matched:", "Option (c) is correct:", "कथन 1 सही है:".
#: The house sample prints the verdict bold and underlined, so a student can
#: find each statement's verdict without reading the whole explanation.
_VERDICT_LEAD = re.compile(
    r"^(?:statements?|pairs?|options?|assertion|reason|कथन|युग्म|विकल्प)"
    r"(?=[\s\-\u2013(])[^:\n]{0,48}?"
    r"(?:correct|matched|true|false|सही|गलत|ग़लत|सुमेलित|असत्य|सत्य)[^:\n]{0,12}:",
    re.IGNORECASE,
)

#: A bullet point, whatever mark the bank's editor used for it. The marks
#: that are also ordinary punctuation need a space after them to count.
_BULLET_LINE = re.compile(
    r"^(?:[\u2022\u25cf\u25cb\u25e6\u00b7\u25aa\u25a0\u25a1\u27a2\u27a4\u2794\u2b9a"
    r"\u2756\u2714\u2713]\s*|[\-\u2013*]\s+)(?=\S)"
)


def _solution_line_markup(line: str) -> str:
    lead = _VERDICT_LEAD.match(line)
    if not lead:
        return _markup(line)
    return f"<b><u>{_markup(lead.group(0))}</u></b>{_markup(line[lead.end():])}"


def _solution_flowables(
    question: _RenderableQuestion,
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    """The explanation, one paragraph per line, at the solution's indent.

    Bullet points become round bullets with their text hanging after the
    bullet, whatever mark the bank used, as in the house sample.
    """
    flowables: list[Any] = []
    for raw in (question.solution or "").split("\n"):
        line = _PRINTED_INVISIBLE.sub("", raw).strip()
        if not line:
            continue
        bullet = _BULLET_LINE.match(line)
        if bullet:
            flowables.append(Paragraph(
                _solution_line_markup(line[bullet.end():]),
                styles["solution_bullet"],
                bulletText="•",
            ))
        else:
            flowables.append(Paragraph(_solution_line_markup(line), styles["solution"]))
    if not flowables:
        flowables.append(Paragraph(
            "Detailed solution unavailable for this question.",
            styles["unavailable"],
        ))
    return flowables


def _answer_markup(question: _RenderableQuestion) -> str:
    answer = question.answer_text or "Not available"
    return f"Ans: {_markup(answer, devanagari_bold=True)}"


def _two_column_frames(top: float, *, prefix: str) -> list[Frame]:
    height = top - BODY_BOTTOM
    return [
        Frame(
            OUTER_MARGIN,
            BODY_BOTTOM,
            COLUMN_WIDTH,
            height,
            id=f"{prefix}-left",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        ),
        Frame(
            RIGHT_COLUMN_X,
            BODY_BOTTOM,
            COLUMN_WIDTH,
            height,
            id=f"{prefix}-right",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        ),
    ]


def _page_callback(*, body_top: float, heading: str | None = None,
                   badge: str | None = None, section: str | None = None):
    def draw(canvas: Any, document: Any) -> None:
        del document
        canvas.saveState()
        _draw_watermark(canvas)
        _draw_header(canvas)
        if heading is not None:
            _draw_heading(canvas, heading, badge=badge)
        if section:
            _draw_section(canvas, section)
        canvas.setStrokeColor(DIVIDER)
        canvas.setLineWidth(1.4)
        canvas.line(PAGE_WIDTH / 2, BODY_BOTTOM, PAGE_WIDTH / 2, body_top)
        _draw_footer(canvas)
        canvas.restoreState()

    return draw


def _draw_watermark(canvas: Any) -> None:
    center_x = PAGE_WIDTH / 2
    center_y = PAGE_HEIGHT / 2 - 6
    canvas.setStrokeColor(WATERMARK)
    canvas.setFillColor(WATERMARK)
    canvas.setLineWidth(8)
    canvas.circle(center_x, center_y, 126, stroke=1, fill=0)
    canvas.setFont("Helvetica-Bold", 116)
    canvas.drawCentredString(center_x, center_y - 38, "PW")


def _draw_header(canvas: Any) -> None:
    _, bold = _body_fonts()
    canvas.setFillColor(INK)
    canvas.setFont(bold, 10)
    # Set in a little from the badge's right edge, as the sample has it.
    canvas.drawRightString(PAGE_WIDTH - OUTER_MARGIN - 6, HEADER_BASELINE, "UPSC")


def _title_suffix(subject: str) -> str:
    return f" - {subject}" if subject else ""


def _fitted_heading(text: str, room: float) -> tuple[float, str]:
    """(size, text) that fits ``room`` on one line: smaller first, then cut."""
    size = HEADING_SIZE
    while size > HEADING_MIN_SIZE and _text_width(text, size, bold=True) > room:
        size -= 0.5
    while len(text) > 1 and _text_width(text, size, bold=True) > room:
        text = text[:-2].rstrip() + "…"
    return size, text


def _draw_heading(canvas: Any, heading: str, *, badge: str | None) -> None:
    """The one header line: the subject centred, the DPP box at its right."""
    _, bold = _body_fonts()
    right = PAGE_WIDTH - OUTER_MARGIN
    obstacle = right
    if badge:
        width = max(64.0, pdfmetrics.stringWidth(badge, bold, BADGE_SIZE) + 32)
        left = right - width
        canvas.setStrokeColor(INK)
        canvas.setLineWidth(0.75)
        canvas.rect(left, BADGE_BOTTOM, width, BADGE_HEIGHT, stroke=1, fill=0)
        canvas.setFillColor(INK)
        canvas.setFont(bold, BADGE_SIZE)
        canvas.drawCentredString(left + width / 2, BADGE_BOTTOM + 5.4, badge)
        obstacle = left - 12

    # Centred on the page rather than in the space beside the badge, so the
    # room is twice the distance from the centre to the nearer obstacle. The
    # title is a Paragraph, not drawString, so a Hindi subject is shaped.
    centre = PAGE_WIDTH / 2
    room = 2 * min(centre - OUTER_MARGIN, obstacle - centre)
    size, text = _fitted_heading(heading, room - 4)
    style = ParagraphStyle(
        "PracticeHeading",
        fontName=bold,
        fontSize=size,
        leading=size * 1.2,
        alignment=TA_CENTER,
        textColor=INK,
        shaping=True,
    )
    paragraph = Paragraph(_markup(text, devanagari_bold=True), style)
    _, height = paragraph.wrap(room, size * 4)
    # A Paragraph puts its first baseline one font size below its top.
    paragraph.drawOn(canvas, centre - room / 2, HEADING_BASELINE - height + size)

    canvas.setStrokeColor(HEADER_RULE)
    canvas.setLineWidth(0.6)
    canvas.line(OUTER_MARGIN, HEADER_RULE_Y, PAGE_WIDTH - OUTER_MARGIN, HEADER_RULE_Y)


def _draw_section(canvas: Any, section: str) -> None:
    """Names the part of a solutions document under the header rule."""
    _, bold = _body_fonts()
    canvas.setFillColor(INK)
    canvas.setFont(bold, 12)
    canvas.drawCentredString(PAGE_WIDTH / 2, SECTION_BASELINE, section)


def _draw_footer(canvas: Any) -> None:
    icon_x = (PAGE_WIDTH / 2) - 137
    icon_y = 40
    canvas.setFillColor(SUPPORT_YELLOW)
    canvas.setStrokeColor(INK)
    canvas.setLineWidth(0.8)
    canvas.circle(icon_x, icon_y, 10.5, stroke=1, fill=1)
    canvas.arc(icon_x - 5.5, icon_y - 1, icon_x + 5.5, icon_y + 8, 0, 180)
    canvas.line(icon_x - 5.5, icon_y + 3.5, icon_x - 5.5, icon_y - 2)
    canvas.line(icon_x + 5.5, icon_y + 3.5, icon_x + 5.5, icon_y - 2)

    footer_text = "Android App  |  iOS App  |  PW Website"
    canvas.setFillColor(LINK_BLUE)
    canvas.setFont("Helvetica-Bold", 10.25)
    text_x = (PAGE_WIDTH / 2) + 18
    canvas.drawCentredString(text_x, 36.5, footer_text)
    width = pdfmetrics.stringWidth(footer_text, "Helvetica-Bold", 10.25)
    canvas.setStrokeColor(LINK_BLUE)
    canvas.setLineWidth(0.45)
    canvas.line(text_x - (width / 2), 35, text_x + (width / 2), 35)


def _document(
    buffer: BytesIO,
    *,
    title: str,
    language: str,
) -> BaseDocTemplate:
    return BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=OUTER_MARGIN,
        rightMargin=OUTER_MARGIN,
        topMargin=52,
        bottomMargin=BODY_BOTTOM,
        allowSplitting=1,
        title=title,
        author="UPSC Practice Generator",
        creator="UPSC Practice Generator",
        pageCompression=1,
        lang="hi-IN" if language == "hindi" else "en-IN",
    )


def _continuation_template(prefix: str) -> PageTemplate:
    return PageTemplate(
        id=f"{prefix}-continuation",
        frames=_two_column_frames(CONTINUATION_BODY_TOP, prefix=f"{prefix}-continuation"),
        onPage=_page_callback(body_top=CONTINUATION_BODY_TOP),
        autoNextPageTemplate=f"{prefix}-continuation",
    )


def _build_question_pdf(
    questions: tuple[_RenderableQuestion, ...],
    language: str,
    dpp_label: str = "DPP",
    subject: str = "",
) -> bytes:
    buffer = BytesIO()
    document = _document(
        buffer,
        title=f"UPSC Practice Questions - {dpp_label}{_title_suffix(subject)}",
        language=language,
    )
    document.addPageTemplates(
        [
            PageTemplate(
                id="questions-first",
                frames=_two_column_frames(FIRST_BODY_TOP, prefix="questions-first"),
                onPage=_page_callback(
                    body_top=FIRST_BODY_TOP,
                    heading=subject or FALLBACK_HEADING,
                    badge=dpp_label,
                ),
                autoNextPageTemplate="questions-continuation",
            ),
            _continuation_template("questions"),
        ]
    )

    styles = _styles(len(questions))
    story: list[Any] = []
    for number, question in enumerate(questions, start=1):
        block, has_table = _question_flowables(question, number, styles)
        block.append(Spacer(1, QUESTION_SPACING))
        if has_table:
            # A long table must be allowed to move or split by row.  Keeping it
            # as one object is what previously forced table-shaped prompts into
            # an opaque single paragraph.
            story.extend(block)
        else:
            story.append(KeepTogether(block))

    document.build(story)
    return buffer.getvalue()


def _build_solution_pdf(
    questions: tuple[_RenderableQuestion, ...],
    language: str,
    dpp_label: str = "DPP",
    subject: str = "",
) -> bytes:
    buffer = BytesIO()
    document = _document(
        buffer,
        title=f"UPSC Answer Key and Solutions - {dpp_label}{_title_suffix(subject)}",
        language=language,
    )
    heading = subject or FALLBACK_HEADING
    document.addPageTemplates(
        [
            PageTemplate(
                id="answer-key",
                frames=_two_column_frames(TITLED_BODY_TOP, prefix="answer-key"),
                onPage=_page_callback(
                    body_top=TITLED_BODY_TOP, heading=heading, badge=dpp_label,
                    section="Answer Key"),
                autoNextPageTemplate="answer-key",
            ),
            PageTemplate(
                id="solutions-first",
                frames=_two_column_frames(TITLED_BODY_TOP, prefix="solutions-first"),
                onPage=_page_callback(
                    body_top=TITLED_BODY_TOP, heading=heading, badge=dpp_label,
                    section="Hints & Solutions"),
                autoNextPageTemplate="solutions-continuation",
            ),
            _continuation_template("solutions"),
        ]
    )

    styles = _styles(len(questions))
    story: list[Any] = []
    split_at = (len(questions) + 1) // 2
    for number, question in enumerate(questions, start=1):
        answer = question.answer_text or "Not available"
        story.append(
            Paragraph(
                f"<b>Q{number}.</b>&nbsp;&nbsp;{_markup(answer, devanagari_bold=True)}",
                styles["answer"],
            )
        )
        if number == split_at and number < len(questions):
            story.append(FrameBreak())

    story.extend([NextPageTemplate("solutions-first"), PageBreak()])

    for number, question in enumerate(questions, start=1):
        # "Q2 Text Solution:", then "Ans: B" and "Exp:" under the text, as
        # the house sample sets each solution.
        story.append(Paragraph(
            "Text Solution:", styles["solution_heading"], bulletText=f"Q{number}"))
        story.append(Paragraph(_answer_markup(question), styles["solution_label"]))
        story.append(Paragraph("Exp:", styles["solution_label"]))
        story.extend(_solution_flowables(question, styles))
        story.append(Spacer(1, QUESTION_SPACING))

    document.build(story)
    return buffer.getvalue()


def _build_combined_pdf(
    questions: tuple[_RenderableQuestion, ...],
    language: str,
    dpp_label: str = "DPP",
    subject: str = "",
) -> bytes:
    """One document a student can revise from: each question, then its answer
    and solution, in the same two-column layout as the other exports."""
    buffer = BytesIO()
    document = _document(
        buffer,
        title=f"UPSC Practice Questions with Solutions - {dpp_label}{_title_suffix(subject)}",
        language=language,
    )
    document.addPageTemplates(
        [
            PageTemplate(
                id="combined-first",
                frames=_two_column_frames(TITLED_BODY_TOP, prefix="combined-first"),
                onPage=_page_callback(
                    body_top=TITLED_BODY_TOP,
                    heading=subject or FALLBACK_HEADING,
                    badge=dpp_label,
                    section="Questions with Solutions",
                ),
                autoNextPageTemplate="combined-continuation",
            ),
            _continuation_template("combined"),
        ]
    )

    styles = _styles(len(questions))
    story: list[Any] = []
    for number, question in enumerate(questions, start=1):
        # The question and its options stay together the way the question
        # paper keeps them; the solution follows separately because a long
        # explanation must be allowed to split across frames.
        block, has_table = _question_flowables(question, number, styles)
        block.append(Paragraph(_answer_markup(question), styles["answer_line"]))
        if has_table:
            story.extend(block)
        else:
            story.append(KeepTogether(block))

        story.append(Paragraph("Exp:", styles["solution_label"]))
        story.extend(_solution_flowables(question, styles))
        story.append(Spacer(1, QUESTION_SPACING))

    document.build(story)
    return buffer.getvalue()


__all__ = ["PracticePdfExport", "export_practice_pdfs"]
