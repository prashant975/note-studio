"""Add Gujarati and per-run instructions around the unchanged Short Notes engine.

The original engine's layout, schemas, English/Hindi prompts and quality rules
remain authoritative. These hooks add a new language and its missing font.
"""
from __future__ import annotations

import contextlib
import contextvars
from pathlib import Path
import re
import threading

from .gujarati_editorial import EDITORIAL_RULES

GUJARATI_RULES = """GUJARATI OUTPUT: Write the concise notes in natural Gujarati
(ગુજરાતી), using Gujarati script. Translate the source's eligible theory into
Gujarati even when the class is in Hindi or English. Use Gujarati for the title,
subject, headings, explanations, definitions, key terms, tables, flow steps,
where-lines and revision-map labels. Do not write Hindi prose or Romanized
Gujarati. Preserve formulas, variables, SI units, chemical symbols, source
proper names and necessary first-mention exam terminology. Preserve every
source condition, equation and eligible theory point in the original order;
do not invent facts or add question/answer material. Follow the category's
existing Short Notes format. Set output_language to gujarati.
For Gujarati notes, translate factual title, subject and audience metadata
into Gujarati too; this overrides the generic metadata instruction to use
the source's wording. Keep the source meaning. Do not leave an English title
or audience. Keep quote empty unless the source itself contains that exact
quote in Gujarati; do not translate or invent a quote. Use Gujarati for
callout words such as Important, Note and Remember.
""" + EDITORIAL_RULES
GUJARATI_LABELS = {
    "KEY TERMS": "મહત્વપૂર્ણ શબ્દો",
    "Where,": "જ્યાં,",
    "Mind Map — Quick Revision": "મન નકશો — ઝડપી પુનરાવર્તન",
    "DIAGRAM — draw here": "આકૃતિ — અહીં દોરો",
    "Lecture": "વ્યાખ્યાન",
}
_GUJARATI = re.compile(r"[\u0a80-\u0aff]")
_EXTRA = contextvars.ContextVar("notecraft_shortnotes_instructions", default="")
_RENDER_OPTIONS = contextvars.ContextVar("notecraft_shortnotes_render_options", default=None)
_LOCK = threading.Lock()
_INSTALLED = False
FONT_DIR = Path(__file__).resolve().parent / "static/fonts/gujarati"


@contextlib.contextmanager
def instructions(text: str, *, watermark: bool | None = None, logo_watermark: bool | None = None):
    token = _EXTRA.set(str(text or "").strip())
    options = {key: value for key, value in (("watermark", watermark), ("logo_watermark", logo_watermark))
               if value is not None}
    render_token = _RENDER_OPTIONS.set(options or None)
    try:
        yield
    finally:
        _EXTRA.reset(token)
        _RENDER_OPTIONS.reset(render_token)


def install() -> None:
    """Install additive hooks once; contexts follow the engine's worker threads."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        from notesbot import final_quality, gemini_client, notes_pdf, prompts, source_coverage
        from sheets_bridge import pdf_fonts

        regular = FONT_DIR / "BalooBhai2-Regular.ttf"
        bold = FONT_DIR / "BalooBhai2-Bold.ttf"
        if not regular.is_file() or not bold.is_file() or not pdf_fonts.install():
            raise RuntimeError("The Gujarati notes fonts are missing. Reinstall NoteCraft.")
        if not pdf_fonts.shaping_available():
            raise RuntimeError("Gujarati text shaping is missing. Reinstall NoteCraft.")
        prompts._LANGUAGE_RULES["gujarati"] = GUJARATI_RULES
        original_prompt = prompts.build_prompt

        def build_prompt(language, signature, edit_instruction="", *args, **kwargs):
            extra = _EXTRA.get()
            if extra:
                edit_instruction = "\n\n".join(part for part in (edit_instruction, extra) if part)
            result = original_prompt(language, signature, edit_instruction, *args, **kwargs)
            # Keep this last so generic source-metadata wording cannot reverse
            # an explicit choice to write the notes in Gujarati.
            return result + "\n\n" + GUJARATI_RULES if language == "gujarati" else result

        prompts.build_prompt = gemini_client.build_prompt = source_coverage.build_prompt = build_prompt
        original_font = pdf_fonts._font_for
        original_width = pdf_fonts._shaped_width_mm
        original_label = pdf_fonts.localise_label
        original_init = notes_pdf.NotesPDF.__init__
        original_output = notes_pdf.NotesPDF.output
        original_word_width = notes_pdf.NotesPDF._word_width
        original_schema = gemini_client._generate_pdf_schema
        original_findings = final_quality.local_findings
        original_consolidate = source_coverage.consolidate_lecture
        original_generate = gemini_client._generate_short_notes

        def font_for(text):
            return "Gujarati" if _GUJARATI.search(text or "") else original_font(text)

        def shaped_width(font_path, text, size_mm):
            if _GUJARATI.search(text or ""):
                font_path = str(regular)
            return original_width(font_path, text, size_mm)

        def localise_label(text, language=None):
            language = pdf_fonts.render_language() if language is None else language
            if language != "gujarati" or not isinstance(text, str):
                return original_label(text, language)
            page = re.fullmatch(r"Page (\d+)", text)
            return f"પૃષ્ઠ {page.group(1)}" if page else GUJARATI_LABELS.get(text, text)

        def init(pdf, *args, **kwargs):
            kwargs.update(_RENDER_OPTIONS.get() or {})
            original_init(pdf, *args, **kwargs)
            if pdf.language != "gujarati" and not _GUJARATI.search(pdf.notes.model_dump_json()):
                return
            pdf.add_font("Gujarati", "", str(regular))
            pdf.add_font("Gujarati", "B", str(bold))
            if pdf.language == "gujarati":
                pdf.set_fallback_fonts(["Gujarati", "Kalam"])
            else:
                pdf.set_fallback_fonts(["Kalam", "Gujarati"])

        def output(pdf, *args, **kwargs):
            data = original_output(pdf, *args, **kwargs)
            if pdf.language == "gujarati" and isinstance(data, (bytes, bytearray)):
                # Gujarati shaping can create glyphs without a Unicode value.
                # Reuse the engine's existing Sanskrit CMap repair so extractors
                # do not invent ASCII/control characters for those glyphs.
                from notesbot.sanskrit import map_shaping_only_glyphs
                return map_shaping_only_glyphs(pdf, bytes(data))
            return data

        def word_width(pdf, pieces, body_pt):
            if not any(_GUJARATI.search(piece) for piece, _mode in pieces):
                return original_word_width(pdf, pieces, body_pt)
            width = 0.0
            for piece, mode in pieces:
                size = body_pt if mode == "n" else body_pt * notes_pdf._SCRIPT_SCALE
                family = font_for(piece)
                pdf.set_font(family, "", size)
                measured = shaped_width(str(regular), piece, pdf.font_size) if family == "Gujarati" else None
                width += measured if measured is not None else pdf.get_string_width(piece)
            pdf.set_font("Patrick", "", body_pt)
            return width

        def generate_schema(files, prompt, schema, *args, **kwargs):
            if schema.__name__ == "QualityReview" and "Requested language: gujarati." in prompt:
                prompt += "\n\n" + GUJARATI_RULES
            return original_schema(files, prompt, schema, *args, **kwargs)

        def consolidate(pdf_bytes, notes, *, language, **kwargs):
            if language != "gujarati":
                return original_consolidate(pdf_bytes, notes, language=language, **kwargs)
            from .gujarati_editorial import consolidate as review
            notes._gujarati_editorial_issue = "Gujarati source organisation review was unavailable; generated content was retained."
            return review(pdf_bytes, notes, language=language, instructions=_EXTRA.get(), **kwargs)

        def generate(pdf_bytes, edit_instruction, language, subject_hint):
            notes = original_generate(pdf_bytes, edit_instruction, language, subject_hint)
            from notesbot import generation_profile
            # Automation owns its whole-lecture review after page groups have
            # merged. Legacy uploads need this same review once, not per chunk.
            if notes.output_language == "gujarati" and generation_profile.inline_source_check_enabled():
                from notesbot.errors import ModelBudgetError
                try:
                    notes = consolidate(pdf_bytes, notes, language="gujarati", subject_hint=subject_hint)
                except (KeyboardInterrupt, ModelBudgetError):
                    raise
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Gujarati source organisation review unavailable; keeping generated content", exc_info=True)
            return notes

        def local_findings(notes, data, language):
            count, issues = original_findings(notes, data, language)
            if language == "gujarati":
                if getattr(notes, "_gujarati_editorial_issue", ""):
                    issues.append(final_quality.QualityIssue(page=0, kind="clarity",
                        detail=notes._gujarati_editorial_issue))
                for field in ("title", "subject", "audience"):
                    value = getattr(notes, field, "") or ""
                    if re.search(r"[A-Za-z]{3}", value) and not _GUJARATI.search(value):
                        issues.append(final_quality.QualityIssue(page=0, kind="language",
                            detail=f"Translate the {field} metadata into Gujarati, preserving its source meaning."))
                for index, section in enumerate(notes.sections):
                    for value in section.notes:
                        if re.search(r"\b(?:Important|Remember|Warning|Note)\b", value, re.I):
                            issues.append(final_quality.QualityIssue(page=0, kind="language",
                                detail=f"Use Gujarati callout wording in section {index+1}, preserving its meaning."))
            return count, issues

        pdf_fonts._font_for = font_for
        pdf_fonts._shaped_width_mm = shaped_width
        pdf_fonts.localise_label = localise_label
        notes_pdf.NotesPDF.__init__ = init
        notes_pdf.NotesPDF.output = output
        notes_pdf.NotesPDF._word_width = word_width
        gemini_client._generate_pdf_schema = generate_schema
        gemini_client._generate_short_notes = generate
        source_coverage.consolidate_lecture = consolidate
        final_quality.local_findings = local_findings
        from . import gujarati_layout, gujarati_math
        gujarati_layout.install(notes_pdf)
        gujarati_math.install(regular)
        _INSTALLED = True
