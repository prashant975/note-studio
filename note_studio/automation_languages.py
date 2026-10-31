"""Automatic Gujarati sheet language around the unchanged copied engine."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import replace
import re
import threading

_GUJARATI_HEADER = ContextVar("notecraft_gujarati_sheet_header", default=False)
_GUJARATI_BATCH = re.compile(r"(?:^|[\s_/-])gujarat(?:i)?(?:$|[\s_/-])|ગુજરાત", re.I)
_GUJARATI_TEXT = re.compile(r"[\u0a80-\u0aff]")
_SUBJECT_NAMES = {
    "physics": "ભૌતિકશાસ્ત્ર", "chemistry": "રસાયણશાસ્ત્ર",
    "physical chemistry": "ભૌતિક રસાયણશાસ્ત્ર", "organic chemistry": "કાર્બનિક રસાયણશાસ્ત્ર",
    "inorganic chemistry": "અકાર્બનિક રસાયણશાસ્ત્ર", "biology": "જીવવિજ્ઞાન",
    "botany": "વનસ્પતિશાસ્ત્ર", "zoology": "પ્રાણીશાસ્ત્ર", "maths": "ગણિત",
    "science": "વિજ્ઞાન", "social science": "સામાજિક વિજ્ઞાન",
    "accountancy": "હિસાબશાસ્ત્ર", "accounting": "હિસાબશાસ્ત્ર", "economics": "અર્થશાસ્ત્ર",
    "business studies": "વ્યવસાય અભ્યાસ", "statistics": "આંકડાશાસ્ત્ર",
    "english": "અંગ્રેજી", "hindi": "હિન્દી", "sanskrit": "સંસ્કૃત",
}
_LOCK = threading.Lock()
_INSTALLED = False


def _pdf_is_gujarati(pdf_bytes: bytes, pages: int = 6) -> bool:
    from io import BytesIO
    from pypdf import PdfReader

    try:
        sample = " ".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf_bytes)).pages[:pages])
    except Exception:
        return False
    letters = [character for character in sample if character.isalpha()]
    gujarati = sum("\u0a80" <= character <= "\u0aff" for character in letters)
    return gujarati >= 20 and gujarati / max(1, len(letters)) >= 0.5


def install() -> None:
    """Resolve Gujarati before cache identity, generation and PDF review."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        from sheets_bridge import catalog, catalog_flow, coverage, generator, mapping, runner
        from sheets_bridge.catalog_flow import CatalogFlow

        original_map = mapping.map_language
        original_catalog = catalog.language_for_batch
        original_plan = CatalogFlow.plan
        original_header = mapping.sheet_header
        original_chapter = catalog.header_chapter
        original_detection = coverage.detect_source_language
        original_generate = generator.generate_notes_pdf

        def wants_gujarati(batch):
            return bool(_GUJARATI_BATCH.search(str(batch or "")))

        def map_language(batch):
            return "gujarati" if wants_gujarati(batch) else original_map(batch)

        def language_for_batch(batch):
            return "gujarati" if wants_gujarati(batch) else original_catalog(batch)

        def gujarati_text(value):
            for line in str(value or "").splitlines():
                if _GUJARATI_TEXT.search(line):
                    return " ".join(line.split())
            return ""  # Let the generator's Gujarati metadata supply an untranslated field.

        def sheet_header(*, subject, chapter, lecture, language=""):
            if language != "gujarati":
                return original_header(subject=subject, chapter=chapter, lecture=lecture, language=language)
            subject_text = _SUBJECT_NAMES.get(mapping.subject_name(subject).lower(), gujarati_text(subject))
            return mapping.SheetHeader(subject=subject_text, chapter=gujarati_text(chapter),
                                       lecture=mapping.display_lecture(lecture).replace("Lecture", "વ્યાખ્યાન"))

        def header_chapter(chapter):
            if _GUJARATI_HEADER.get():
                return gujarati_text(catalog._CH_PREFIX.sub("", str(chapter or "")))
            return original_chapter(chapter)

        def plan(self, row):
            token = _GUJARATI_HEADER.set(wants_gujarati(row.batch))
            try:
                result = original_plan(self, row)
            finally:
                _GUJARATI_HEADER.reset(token)
            if result.language == "gujarati" and result.note:
                result = replace(result, note="Notes are written in Gujarati")
            return result

        def detect_source_language(pdf_bytes, pages=6):
            detected = original_detection(pdf_bytes, pages)
            return "gujarati" if not detected and _pdf_is_gujarati(pdf_bytes, pages) else detected

        def generate_notes_pdf(pdf_bytes, **kwargs):
            if str(kwargs.get("language") or "auto").strip().lower() == "auto":
                if detect_source_language(pdf_bytes) == "gujarati":
                    kwargs["language"] = "gujarati"
                    header = kwargs.get("header")
                    if header is not None:
                        kwargs["header"] = sheet_header(subject=header.subject, chapter=header.chapter,
                                                       lecture=header.lecture, language="gujarati")
                    if kwargs.get("log") is not None:
                        kwargs["log"]("source looks like a Gujarati lecture - generating in Gujarati")
            return original_generate(pdf_bytes, **kwargs)

        mapping.map_language = runner.map_language = map_language
        catalog.language_for_batch = language_for_batch
        mapping.sheet_header = runner.sheet_header = catalog_flow.sheet_header = sheet_header
        catalog.header_chapter = header_chapter
        CatalogFlow.plan = plan
        coverage.detect_source_language = detect_source_language
        generator.generate_notes_pdf = generate_notes_pdf
        _INSTALLED = True
