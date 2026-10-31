"""The single integration seam with the notes generator (library, not web).

    with cost_meter.metered_job() as meter:
        notes = generate_short_notes(pdf_bytes, "", language, subject_hint)
        out   = build_notes_pdf(notes, signature)
    cost = meter.snapshot()["inr"]

All model traffic inside those calls leaves through the PW proxy (see
``pw_transport``); the app holds no provider keys. One ``task_id`` covers every
model call of one row, so the proxy's raw rows can be grouped per lecture.

The automation's first artifact is the **review document**: the same notes as
an editable Google Doc (with the PW logo and watermark) that a person checks
and corrects. The studygram PDF is rendered from the same notes and can be
uploaded alongside it.

Imports are lazy so unit tests of the pure modules never load the model stack.
"""
from __future__ import annotations

import os

from dataclasses import dataclass, field

from .errors import BridgeError
from .mapping import apply_sheet_header, choose_signature, signature_for
from .mapping import subject_hint as _subject_hint


@dataclass(frozen=True)
class GenerationResult:
    pdf: bytes = b""
    notes_json: bytes = b""
    cost_inr: float = 0.0
    usage: dict = field(default_factory=dict)
    task_id: str = ""
    stage_failed: str = ""
    quality_report: dict = field(default_factory=dict)


def page_count(pdf_bytes: bytes) -> int:
    """Pages in the source PDF - the ledger's input unit. 0 when unreadable."""
    try:
        import io

        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:  # noqa: BLE001
        return 0


def render_artifacts(notes, *, signature: str, source_name: str = "", language: str = "", header=None,
                     show_page_numbers: bool = True) -> bytes:
    """The studygram .pdf, rendered locally with no model calls.

    The editable review .docx was retired: a row's output is the PDF, and the
    sheet's Review dropdown is what a human marks after looking at it.
    ``header`` (a mapping.SheetHeader) puts the sheet's subject, chapter and
    lecture number on page 1 - re-applied on every render, so a quality edit
    of the title cannot bring the model's version back.
    """
    import os

    from . import pdf_branding, pdf_fonts
    from .coverage import dedupe_sections

    from .coverage import notes_hindi_share

    # Leftover question/solution text is reported by the final quality
    # check (as an open issue on the receipt) rather than failing the row:
    # the notes always ship.
    del source_name
    from notesbot.sanskrit import resolve_language
    lang = (language or getattr(notes, "output_language", "") or "auto").strip().lower()
    if lang == "auto" and getattr(notes, "output_language", "") == "sanskrit":
        lang = "sanskrit"
    lang = resolve_language(lang)
    if lang == "sanskrit":
        notes.output_language = lang
        if header is not None:
            from .mapping import sheet_header
            header = sheet_header(subject=header.subject, chapter=header.chapter,
                                  lecture=header.lecture, language="sanskrit")
    apply_sheet_header(notes, header)
    from notesbot.generation_profile import signature_fixed
    signature = signature_for(signature, notes, fixed=signature_fixed())
    pdf_fonts.install()          # Devanagari in the studygram PDF (see pdf_fonts)
    pdf_branding.install()       # PW logo on the page, PW logo as the watermark
    # The page furniture ("KEY TERMS", "Where,", the mind-map title) follows
    # the notes' language; measured from the notes when no language is known.
    if not lang or lang == "auto":
        lang = "hindi" if notes_hindi_share(notes) >= 0.5 else ""
    notes.output_language = lang
    pdf_fonts.set_render_language(lang)
    # Idempotent: also folds duplicates when the notes come from the cache.
    notes.sections = dedupe_sections(notes.sections)
    from notesbot import categories
    if categories.switch("one_word_table"):
        from notesbot.sanskrit import keep_one_word_table
        keep_one_word_table(notes)
    from notesbot.latex_repair import repair_notes
    from notesbot.gemini_client import _ensure_mindmap
    repair_notes(notes)
    _ensure_mindmap(notes, include_revision_map=False)
    # Recover any legacy map-only theory first, then keep one body presentation.
    notes.mindmap = None
    from notesbot.repetition import remove_exact_repetition
    remove_exact_repetition(notes)
    from notesbot.notes_pdf import build_notes_pdf

    # PW branding: the logo watermark replaces the tiled teacher name
    # (SHEETS_BRIDGE_NAME_WATERMARK=1 brings the name tiling back).
    name_watermark = os.environ.get("SHEETS_BRIDGE_NAME_WATERMARK", "").strip().lower() in ("1", "true", "yes")
    pdf = build_notes_pdf(with_revision_map(notes), signature, watermark=name_watermark,
                          logo_watermark=True, show_page_numbers=show_page_numbers)
    if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF"):
        raise BridgeError("render:invalid_pdf")
    return pdf


# A revision mind map is added only when the lecture has enough separate
# theory topics to be worth one. It is built from the finished notes by code,
# so it costs no model call and cannot add anything the notes do not say.
MIN_MINDMAP_BRANCHES = 3


def mindmap_enabled() -> bool:
    """``SHEETS_BRIDGE_MINDMAP=off`` turns the revision map off."""
    raw = os.environ.get("SHEETS_BRIDGE_MINDMAP", "auto").strip().lower()
    return raw not in ("0", "off", "false", "no", "none")


def with_revision_map(notes):
    """The notes to render: a copy carrying a mind map when the lecture needs one.

    The stored notes keep ``mindmap=None`` so the JSON, the cache and the
    quality checks see exactly the theory that was generated. Lectures that
    are mostly solved problems, or have fewer than MIN_MINDMAP_BRANCHES
    topics, get no map.
    """
    if not mindmap_enabled() or len(notes.sections) < MIN_MINDMAP_BRANCHES:
        return notes
    from notesbot import categories
    if categories.switch("mind_map", True) is False:
        return notes  # the format leaves the map out (e.g. Foundation Sanskrit)
    from notesbot.gemini_client import _ensure_mindmap

    rendered = notes.model_copy(deep=True)
    try:
        _ensure_mindmap(rendered, include_revision_map=True)
    except Exception:  # noqa: BLE001 - a map is optional; never cost the PDF
        return notes
    mindmap = rendered.mindmap
    if mindmap is None or len(mindmap.branches) < MIN_MINDMAP_BRANCHES:
        return notes
    # Only the map is taken from the copy; the body stays exactly as stored.
    shown = notes.model_copy(deep=True)
    shown.mindmap = mindmap
    return shown


# List fields the generator sometimes clears to null while repairing a
# section; the schema wants a list, so a cached run would fail to reload.
_LIST_FIELDS = ("points", "tricks", "notes", "key_terms", "flow", "where", "visual_ids", "worked_examples")


def notes_from_json(payload: bytes):
    """Rebuild ``ShortNotes`` from a cached generation (no model cost)."""
    import json

    from notesbot.prompts import ShortNotes

    data = json.loads(payload.decode("utf-8"))
    for section in data.get("sections") or []:
        for field in _LIST_FIELDS:
            if section.get(field) is None:
                section[field] = []
    return ShortNotes.model_validate(data)


def generate_notes_pdf(
    pdf_bytes: bytes,
    *,
    language: str,
    subject: str,
    signature: str,
    filename: str = "",
    task_id: str = "",
    log=None,
    header=None,
    show_page_numbers: bool = True,
) -> GenerationResult:
    """Generate the notes, then render the review Doc (and optionally the PDF).

    On failure the cost accrued so far is attached to the raised exception as
    ``partial_cost_inr`` so the caller can still record spend.
    """
    from notesbot import cost_meter
    from notesbot.generation_profile import lean_requests

    from .coverage import generate_complete_notes
    from .pw_transport import call_breakdown, set_call_log, set_task

    # Every model call of the run shares one task_id; the proxy rolls calls up
    # per (task, app, user, file, model), so each lecture file becomes one row.
    # One stable unit for the whole app - pages - and the SAME count on every
    # call of a lecture (its page count): the proxy recognises that convention
    # and does not sum it. Mixing "rows" and "pages" is what made this app's
    # volume unreadable on the cost sheet (pw-app-kit FOR-EXISTING-APPS.md).
    set_task(
        task_id=task_id,
        filename=filename,
        input_unit="No. of pages",
        count=page_count(pdf_bytes) or None,
    )
    # One row-log line per model call, so the cost of each call type is
    # visible instead of only the lecture total.
    set_call_log(log)

    # A Sanskrit lecture becomes Hindi notes that keep its verses in Sanskrit
    # (unless the run asked for strict Sanskrit-only notes).
    from notesbot.generation_profile import sanskrit_hindi_notes
    from notesbot.sanskrit import generation_language
    _resolved, sanskrit_hindi = generation_language(language, _subject_hint(subject), pdf_bytes)
    if sanskrit_hindi:
        language = "hindi"
        if log is not None:
            log("Sanskrit lecture: Hindi notes, every shloka kept in Sanskrit")
    if log is not None:
        from notesbot import categories
        format_subject = categories.current_subject(_subject_hint(subject), sanskrit_hindi=sanskrit_hindi)
        summary = categories.switch("summary", "", subject=format_subject)
        if summary or categories.commands(subject=format_subject):
            where = categories.LABELS[categories.current()]
            if format_subject:
                where += " / " + categories.subject_label(format_subject)
            log(f"format: {where}" + (f" - {summary}" if summary else ""))

    stage = "generate"
    with cost_meter.metered_job() as meter, lean_requests(), sanskrit_hindi_notes(sanskrit_hindi):
        try:
            notes = generate_complete_notes(
                pdf_bytes,
                language=language,
                subject_hint=_subject_hint(subject),
                log=log,
                verify_source=True,
            )
            if log is not None:
                from notesbot.generation_profile import signature_fixed
                chosen, why = choose_signature(signature, notes, fixed=signature_fixed())
                log(f"signature: {chosen!r} - {why}")
            stage = "quality"
            from notesbot.final_quality import finalize_notes_pdf
            notes, pdf, quality_report = finalize_notes_pdf(
                pdf_bytes, notes, language=language, log=log,
                render=lambda current: render_artifacts(
                    current, signature=signature, source_name=filename, language=language, header=header,
                    show_page_numbers=show_page_numbers),
            )
        except BaseException as exc:
            snapshot = meter.snapshot()
            setattr(exc, "partial_cost_inr", _cost(snapshot))
            setattr(exc, "failed_stage", stage)
            raise
        finally:
            breakdown = call_breakdown()
            set_call_log(None)
            if breakdown and log is not None:
                log(breakdown)
        snapshot = meter.snapshot()
    return GenerationResult(
        pdf=pdf,
        notes_json=notes.model_dump_json().encode("utf-8"),
        cost_inr=_cost(snapshot),
        usage=snapshot,
        task_id=task_id,
        quality_report=quality_report,
    )


def _cost(snapshot: dict) -> float:
    """Proxy-reported cost when available, else the local token estimate."""
    from .pw_transport import task_cost_inr

    reported = task_cost_inr()
    return round(reported if reported > 0 else float(snapshot.get("inr", 0.0)), 2)
