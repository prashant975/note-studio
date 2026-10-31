"""The hand-drawn notes engine, in process: exactly what the old app's Upload page does.

The old desktop app (engines/shortnotes/sheets_bridge/studio_server.py) runs an
upload as notesbot.web_service.run_job with two swapped-in functions. This
adapter makes the same calls, in the same order, with the engine's own code:

  run_job (web_service.py:657)
    category, subject and Sanskrit style as context variables
      ("strict" only when the language is "sanskrit")
    generate = studio_server._local_generate
      the signed-in account's PW proxy transport;
      set_task(task_id=new_task_id("studio-manual"), filename="studio-upload.pdf",
               input_unit="No. of pages", count=page_count(pdf));
      generate_short_notes(pdf, instructions, language, subject_hint)
      (subject_hint is "" for Auto Detect)
    render = studio_server._local_render
      pdf_fonts.install(); pdf_branding.install();
      build_notes_pdf(notes, signature="by <teacher>", watermark=..., logo_watermark=...)
    validate_pdf_bytes, under cost_meter.metered_job()
  saved as web_service's download name: sanitize_pdf_filename(output_name(name, max_stem_length=90))

It does not build the web job record (JobRecord): that validates the old app's
fixed category and subject lists, and NoteCraft's categories are data. The
parity test (tests/test_parity_upload.py) runs the original project's real
run_job beside this adapter with a stubbed model and compares the requests,
the notes and the PDF.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Plain words for each stage the old job reports when it fails.
FAILURE_WORDS = {
    "source_fetch_failed": "The lecture PDF could not be opened.",
    "generate_failed": "The notes could not be written for this lecture.",
    "render_failed": "The notes were written but the PDF could not be drawn.",
    "output_validation_failed": "The finished PDF did not pass its check.",
}
SIGN_IN_WORDS = {
    "sign_in_required": "Sign in with Google in Settings, then press Try again.",
    "not_authorized": "This Google account is not on the PW list for this app. Ask the owner to add it.",
    "allowlist_undecided": "PW could not confirm your access just now (network or sign-in). Try again in a minute.",
}


@dataclass
class UploadSettings:
    """The old Upload page's fields (web/state.js defaults)."""
    teacher_name: str
    category: str = "neet_jee"
    subject: str = "auto"
    language: str = "auto"
    instructions: str = ""
    watermark: bool = False
    logo_watermark: bool = True
    # The legacy Upload adapter stays available for byte-parity checks.
    # run_settings enables the original automation workflow for New notes.
    automation_quality: bool = False


@dataclass
class Result:
    state: str                  # complete | failed
    output_path: str = ""
    filename: str = ""
    pages: int = 0
    cost_inr: float = 0.0
    calls: int = 0
    failure: str = ""           # the stage that failed (FAILURE_WORDS key)
    message: str = ""           # plain words
    detail: str = ""            # exception class only (redacted, like the old log)
    quality_report: dict = field(default_factory=dict)


def make_notes(pdf_bytes: bytes, source_name: str, out_dir: Path, settings: UploadSettings, *,
               ensure_proxy: Callable[[], None], step: Callable[[str, int | None], None] | None = None,
               capture: dict | None = None) -> Result:
    """Make hand-drawn notes for one lecture PDF into ``out_dir``."""
    if settings.automation_quality:
        return _make_automation_notes(pdf_bytes, source_name, out_dir, settings,
                                      ensure_proxy=ensure_proxy, step=step, capture=capture)
    if settings.language == "gujarati":
        from .. import shortnotes_extensions
        shortnotes_extensions.install()
    from notesbot import cost_meter, generation_profile
    from notesbot.blob_store import validate_pdf_bytes
    from notesbot.gemini_client import generate_short_notes
    from notesbot.notes_pdf import build_notes_pdf
    from notesbot.output_naming import output_name
    from notesbot.web_service import (MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, normalize_signature,
                                      sanitize_pdf_filename)
    from sheets_bridge import pdf_branding, pdf_fonts
    from sheets_bridge.generator import page_count
    from sheets_bridge.pw_gate import new_task_id
    from sheets_bridge.pw_transport import set_call_log, set_task, task_cost_inr

    say = step or (lambda text, pct=None: None)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = Result(state="failed")
    stage = "source_fetch_failed"
    calls = []
    task = {"started": False}

    def generate(pdf, edit_instruction="", language=None, subject_hint=""):
        ensure_proxy()
        set_task(task_id=new_task_id("studio-manual"), filename="studio-upload.pdf",
                 input_unit="No. of pages", count=page_count(pdf) or None)
        task["started"] = True
        # NoteCraft only: one line per model call for the run page (set_task resets it).
        set_call_log(lambda line: (calls.append(line), say(line, None)))
        notes = generate_short_notes(pdf, edit_instruction, language, subject_hint)
        if capture is not None:
            capture["notes"] = notes
        return notes

    def render(*args, **kwargs):
        pdf_fonts.install()
        pdf_branding.install()
        kwargs.setdefault("logo_watermark", True)
        return build_notes_pdf(*args, **kwargs)

    meter_context = cost_meter.metered_job()
    meter_context.__enter__()
    try:
        if not pdf_bytes or len(pdf_bytes) > MAX_INPUT_BYTES:
            raise ValueError("Invalid PDF size")
        validate_pdf_bytes(pdf_bytes)
        stage = "generate_failed"
        subject_hint = "" if settings.subject == "auto" else settings.subject
        sanskrit_style = "strict" if settings.language == "sanskrit" else "hindi"
        say("Writing the notes", 10)
        with generation_profile.exam_category(settings.category), \
                generation_profile.exam_subject(settings.subject), \
                generation_profile.sanskrit_style_as(sanskrit_style):
            notes = generate(pdf_bytes, settings.instructions, language=settings.language,
                             subject_hint=subject_hint)
        stage = "render_failed"
        say("Drawing the notes PDF", 85)
        rendered = render(notes, signature=normalize_signature(settings.teacher_name),
                          watermark=settings.watermark, logo_watermark=settings.logo_watermark)
        stage = "output_validation_failed"
        if not isinstance(rendered, bytes) or not rendered or len(rendered) > MAX_OUTPUT_BYTES:
            raise ValueError("Invalid rendered PDF")
        validate_pdf_bytes(rendered)
        filename = sanitize_pdf_filename(output_name(sanitize_pdf_filename(source_name), max_stem_length=90))
        target = out_dir / filename
        target.write_bytes(rendered)
        result = Result(state="complete", output_path=str(target), filename=filename,
                        pages=page_count(rendered) or 0)
        if capture is not None:
            capture["pdf"] = rendered
    except Exception as failure:
        result.failure = stage
        result.message = FAILURE_WORDS.get(stage, "The notes could not be made.")
        result.detail = f"{type(failure).__module__}.{type(failure).__qualname__}"
        code = getattr(failure, "code", "")
        if type(failure).__name__ == "SignInNeeded":
            result.message, result.failure = str(failure), "sign_in"
        elif code in SIGN_IN_WORDS:
            result.message, result.failure = SIGN_IN_WORDS[code], "sign_in"
        elif code == "model_budget_exceeded":
            result.message, result.failure = str(failure), code
    finally:
        meter_context.__exit__(None, None, None)
    # Before set_task the engine's counter is a shared default, not this run's:
    # a run that stopped before its first call (e.g. at sign-in) cost nothing.
    result.cost_inr = task_cost_inr() if task["started"] else 0.0
    result.calls = len(calls)
    return result


def run_settings(options: dict, category: str, subject: str, language: str, instructions: str) -> UploadSettings:
    """UploadSettings from a NoteCraft run's saved options."""
    return UploadSettings(
        teacher_name=str(options.get("teacher_name") or "").strip(),
        category=category, subject=subject or "auto", language=language or "auto",
        instructions=instructions, watermark=bool(options.get("watermark", False)),
        logo_watermark=bool(options.get("logo_watermark", True)),
        automation_quality=bool(options.get("automation_quality", True)))


def _make_automation_notes(pdf_bytes: bytes, source_name: str, out_dir: Path, settings: UploadSettings, *,
                           ensure_proxy: Callable[[], None], step=None, capture=None) -> Result:
    """Use the original Sheets generation, source audit and final-PDF audit."""
    from .. import shortnotes_extensions
    from notesbot import generation_profile
    from notesbot.blob_store import validate_pdf_bytes
    from notesbot.output_naming import output_name
    from notesbot.web_service import MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, normalize_signature, sanitize_pdf_filename
    from sheets_bridge.generator import generate_notes_pdf, notes_from_json, page_count
    from sheets_bridge.pw_gate import new_task_id
    from sheets_bridge.pw_transport import task_cost_inr

    say = step or (lambda text, pct=None: None)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stage = "source_fetch_failed"
    started = False
    calls = []
    result = Result(state="failed")
    try:
        if not pdf_bytes or len(pdf_bytes) > MAX_INPUT_BYTES:
            raise ValueError("Invalid PDF size")
        validate_pdf_bytes(pdf_bytes)
        shortnotes_extensions.install()
        stage = "generate_failed"
        ensure_proxy()
        started = True
        say("Writing notes with the Short Notes automation checks", 10)

        def log(message):
            message = str(message)
            if " call - INR " in message:
                calls.append(message)
            say(message, None)

        with generation_profile.exam_category(settings.category), \
                generation_profile.exam_subject(settings.subject), \
                generation_profile.sanskrit_style_as("strict" if settings.language == "sanskrit" else "hindi"), \
                generation_profile.fixed_signature(), \
                shortnotes_extensions.instructions(settings.instructions, watermark=settings.watermark,
                                                   logo_watermark=settings.logo_watermark):
            made = generate_notes_pdf(
                pdf_bytes, language=settings.language, subject=settings.subject,
                signature=normalize_signature(settings.teacher_name), filename=sanitize_pdf_filename(source_name),
                task_id=new_task_id("studio-manual"), log=log,
            )
        stage = "output_validation_failed"
        if not isinstance(made.pdf, bytes) or not made.pdf or len(made.pdf) > MAX_OUTPUT_BYTES:
            raise ValueError("Invalid rendered PDF")
        validate_pdf_bytes(made.pdf)
        filename = sanitize_pdf_filename(output_name(sanitize_pdf_filename(source_name), max_stem_length=90))
        target = out_dir / filename
        target.write_bytes(made.pdf)
        (out_dir / "notes.json").write_bytes(made.notes_json)
        result = Result(state="complete", output_path=str(target), filename=filename,
                        pages=page_count(made.pdf), cost_inr=made.cost_inr, calls=len(calls),
                        quality_report=made.quality_report)
        if capture is not None:
            capture.update(notes=notes_from_json(made.notes_json), pdf=made.pdf, quality_report=made.quality_report)
    except Exception as failure:
        result.failure = stage
        result.message = FAILURE_WORDS.get(stage, "The notes could not be made.")
        result.detail = f"{type(failure).__module__}.{type(failure).__qualname__}"
        code = getattr(failure, "code", "")
        if type(failure).__name__ == "SignInNeeded":
            result.message, result.failure = str(failure), "sign_in"
        elif code in SIGN_IN_WORDS:
            result.message, result.failure = SIGN_IN_WORDS[code], "sign_in"
        elif code == "model_budget_exceeded":
            result.message, result.failure = str(failure), code
        elif getattr(failure, "failed_stage", "") == "quality":
            result.message = "The final notes PDF could not finish its quality check. Try again."
        result.cost_inr = float(getattr(failure, "partial_cost_inr", 0) or (task_cost_inr() if started else 0))
        result.calls = len(calls)
    return result
