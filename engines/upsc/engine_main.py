"""Note Studio's entry to this engine: one class per process (Note Studio doc 07).

    python engine_main.py --request <run>/request.json --out <run>      (cwd = this folder)

- It reads only request.json, plus the sign-in token from NOTE_STUDIO_PW_TOKEN.
- It sets this engine's configuration BEFORE importing `src`, so the original
  project's .env is never read and runs go into Note Studio's run folder.
- It calls run_pipeline exactly the way app.py does (the backend defaults at
  app.py:2729-2759 and the call at app.py:3059); run_cli.py takes another path.
- It prints one JSON object per line: step / cost / warning / output / done
  (and "state" for practice steps).
- An "action" other than "notes" is one Mains practice or DPP step, handled by
  engine_practice.py.

This file and engine_practice.py are Note Studio's; nothing else in this folder
is changed for them except the extra-instructions hook in src/prompt_builder.py.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN_ENV = "NOTE_STUDIO_PW_TOKEN"
LANGUAGES = {"english": "English", "hindi": "Hindi", "marathi": "Marathi"}
FORMATS = ("dcn", "simple", "mains", "prelims")

# app.py's own progress words (STEPS, app.py:3003-3027), with a rough percentage.
STEPS = {
    "Extracting lecture": ("Step 1/5: Reading lecture file", 5),
    "Extracting lecture (recording only)": ("Step 1/5: Preparing the lecture", 5),
    "Detecting video source": ("Step 2/5: Detecting video source", 8),
    "Accessing video": ("Step 2/5: Accessing the lecture video", 10),
    "Fetching captions": ("Step 2/5: Fetching video captions", 10),
    "Retrieving existing transcript": ("Step 2/5: Retrieving the existing transcript", 10),
    "Extracting audio": ("Step 2/5: Extracting lecture audio", 12),
    "Preparing transcript": ("Step 2/5: Transcribing the lecture", 15),
    "Using supplied transcript": ("Step 2/5: Using the supplied transcript", 15),
    "Selecting lecture diagrams": ("Step 3/5: Picking the important diagrams", 35),
    "Generating draft notes": ("Step 3/5: Building notes", 40),
    "Tightening notes to the page budget": ("Step 3/5: Fitting the page budget", 60),
    "Running structural checks": ("Step 3/5: Building notes", 62),
    "Creating DOCX/PDF": ("Step 4/5: Creating DOCX/PDF", 75),
    "Verifying layout": ("Step 4/5: Creating DOCX/PDF", 80),
    "Checking final formatting": ("Step 4/5: Creating DOCX/PDF", 82),
    "Building error report": ("Step 5/5: Preparing downloads", 88),
    "Preparing reports": ("Step 5/5: Preparing downloads", 95),
}


def emit(event: str, **data) -> None:
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def step_words(label: str) -> tuple[str, int | None]:
    """A pipeline stage label as app.py shows it (its prefix rules, app.py:3049-3055)."""
    if label in STEPS:
        return STEPS[label]
    if label.startswith("Transcribing the lecture"):
        return f"Step 2/5: {label}", None
    if label.startswith("Building notes"):
        return f"Step 3/5: {label}", None
    return "Step 3/5: Building notes", None


def configure(request: dict, out: Path) -> None:
    """The engine's environment, from request.json only. Must run before `src` is imported."""
    models = request.get("models") or {}
    notes, speech, images = (list(models.get(k) or []) for k in ("notes", "speech", "images"))
    env = {
        "HANDWRITTEN_NOTES_ROOT": str(HERE),
        # A path that never exists: the original project's .env is never read.
        "HANDWRITTEN_NOTES_ENV": str(out / "no-env" / ".env"),
        "RUNS_DIR": str(out / "engine_runs"),
        "OUTPUT_DIR": str(out / "engine_outputs"),
    }
    if notes:
        env["MODEL_NAME"] = notes[0]
        env["FALLBACK_MODEL_NAME"] = notes[1] if len(notes) > 1 else ""
    if speech:
        env["AUDIO_MODEL_NAME"] = speech[0]
        env["AUDIO_FALLBACK_MODEL_NAME"] = speech[1] if len(speech) > 1 else ""
    if images:
        env["IMAGE_MODEL_NAME"] = images[0]
    # DPP workbooks chosen in Note Studio's Settings; otherwise the engine's own data folder.
    for name, key in (("QBG_TAGS_PATH", "taxonomy"), ("QUESTION_BANK_PATH", "questions")):
        chosen = str((request.get("masters") or {}).get(key) or "")
        if chosen:
            env[name] = chosen
    os.environ.update(env)
    # As the packaged app's launcher does (streamlit_launcher_stable.py:147-149):
    # an ffmpeg already on PATH wins; the copy's own only fills the gap.
    bundled = HERE / "third_party" / "ffmpeg"
    if shutil.which("ffmpeg") is None and (bundled / "ffmpeg.exe").exists():
        os.environ["PATH"] = str(bundled) + os.pathsep + os.environ.get("PATH", "")


def keep_state_in(folder: Path) -> None:
    """The engine's writable per-user cache (src/qc/paths.app_data_dir) goes to
    Note Studio's folder, not the original app's %LOCALAPPDATA%\\UPSC_Notes."""
    from src.qc import paths as qc_paths
    from src.qc import reference_index

    def app_data_dir(*parts: str) -> Path:
        path = Path(folder).joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    qc_paths.app_data_dir = app_data_dir
    reference_index.app_data_dir = app_data_dir


def report_costs() -> None:
    """One cost event per proxy call, from the cost the proxy itself reports."""
    import pw_access

    def counted(call, route):
        def wrapper(*args, **kwargs):
            answer = call(*args, **kwargs)
            value = answer.get("cost_inr") if isinstance(answer, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                emit("cost", inr=round(float(value), 4), model=str(kwargs.get("model") or ""), route=route)
            return answer
        return wrapper

    pw_access.gemini_generate = counted(pw_access.gemini_generate, "gemini")
    pw_access.gpt_generate = counted(pw_access.gpt_generate, "gpt")


def outputs(result) -> list[tuple[str, str, str]]:
    """(path, kind, label) for what the old app's normal page offers to download."""
    found = []
    for attr, kind, label in (("docx_path", "docx", "Notes (Word)"), ("pdf_path", "pdf", "Notes (PDF)"),
                              ("error_report_xlsx_path", "xlsx", "Error report"),
                              ("error_report_detailed_path", "md", "Error report with locations")):
        path = getattr(result, attr, None)
        if path and Path(path).is_file():
            found.append((str(path), kind, label))
    return found


def outcome(result, generate_error_report: bool) -> tuple[str, str]:
    """complete, or needs_attention with the reason a part is missing."""
    if not result.docx_path or not Path(result.docx_path).is_file():
        return "failed", "The notes document was not written."
    problems = []
    if not result.pdf_path:
        problems.append("The PDF could not be made" + (f": {result.pdf_error}" if result.pdf_error else "."))
    status = result.error_report_status or ""
    if generate_error_report and status in ("failed", "parse_failed", "missing"):
        problems.append("The error report could not be made" +
                        (f": {result.error_report_error}" if result.error_report_error else "."))
    return ("needs_attention", " ".join(problems)) if problems else ("complete", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--request", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))

    configure(request, out)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    action = str(request.get("action") or "notes")
    if request.get("test_stub_pdf"):                 # tests only, as this engine's own tests/conftest.py does
        import src.pdf_exporter
        import src.pipeline

        def no_pdf(docx_path, output_dir):
            return None, "PDF export is stubbed in tests."

        src.pdf_exporter.export_docx_to_pdf = src.pipeline.export_docx_to_pdf = no_pdf
    if action != "notes":                            # Mains practice and DPP steps
        keep_state_in(Path(request.get("state_dir") or out / "engine_state"))
        report_costs()
        import engine_practice

        return engine_practice.run(request, out, os.environ.get(TOKEN_ENV, ""))
    # Imported only now, after configure(): src.config reads the environment once.
    from src import app_defaults as defaults
    from src import prompt_builder
    from src.config import (DEFAULT_ADVANCED_QC_OPTIONS, DEFAULT_ERROR_REPORT_STRICTNESS, DEFAULT_IMAGE_MODEL,
                            DEFAULT_MODEL, DEFAULT_QUALITY_OPTIONS, DEFAULT_UPSC_FACT_MODE,
                            DEFAULT_UPSC_NOTE_TYPE)
    from src.pipeline import run_pipeline
    from src.upsc_dcn_docx_writer import default_logo_path

    keep_state_in(Path(request.get("state_dir") or out / "engine_state"))
    report_costs()
    prompt_builder.EXTRA_INSTRUCTIONS = str(request.get("extra_instructions") or "")

    mock = bool(request.get("mock"))                 # tests only: no sign-in, the engine's mock notes
    token = "" if mock else os.environ.get(TOKEN_ENV, "")
    language = LANGUAGES.get(str(request.get("language") or "").lower(), "")
    output_format = str(request.get("engine_format") or defaults.DEFAULT_OUTPUT_FORMAT)
    if not language:
        emit("done", status="failed", reason="Choose the notes language.")
        return 0
    if output_format not in FORMATS:
        emit("done", status="failed", reason="That kind of UPSC notes is not known to this engine.")
        return 0
    slides = request.get("slides") or ""
    input_path = Path(slides) if slides else None
    logo = default_logo_path()
    generate_error_report = defaults.GENERATE_ERROR_REPORT

    def stage(label: str) -> None:
        text, pct = step_words(str(label))
        emit("step", text=text, pct=pct)

    emit("step", text="Step 1/5: Preparing the lecture", pct=2)
    try:
        # The original app's run_pipeline call with its backend defaults.
        result = run_pipeline(
            input_path, subject=str(request.get("subject") or "") or defaults.DEFAULT_SUBJECT,
            language=language, mode="complete",
            google_token=token, model=DEFAULT_MODEL,
            send_images_to_ai=True, strict_filter=True,
            allow_mock=mock, image_insert_mode="smart_crop", dtp_note_policy="keep_note_and_insert_image",
            ai_redraw_diagrams=False, image_model=DEFAULT_IMAGE_MODEL,
            video_url=str(request.get("video_url") or "").strip(),
            transcript_text_override=str(request.get("transcript_text_override") or "").strip(),
            workflow="upsc", note_type=DEFAULT_UPSC_NOTE_TYPE, area=defaults.DEFAULT_UPSC_AREA,
            quality_level=defaults.DEFAULT_QUALITY_MODE, quality_options=dict(DEFAULT_QUALITY_OPTIONS),
            generate_quality_report=True,
            show_corrections=True,
            qc_options=dict(DEFAULT_ADVANCED_QC_OPTIONS),
            upsc_output_format=output_format,
            note_depth=defaults.DEFAULT_NOTE_DEPTH,
            fact_mode=DEFAULT_UPSC_FACT_MODE,
            dcn_options=defaults.dcn_layout_options(),
            template_path=None,
            logo_path=str(logo) if logo else None,
            generate_error_report=generate_error_report,
            error_report_strictness=DEFAULT_ERROR_REPORT_STRICTNESS,
            error_report_template_path=None,
            dcn_no=str(request.get("dcn_no") or "").strip(),
            lecture_title=str(request.get("lecture_title") or "").strip(),
            dmp_no=str(request.get("dmp_no") or "").strip(),
            dpp_no=str(request.get("dpp_no") or "").strip(),
            source_options=defaults.source_options(),
            stage_callback=stage,
            # The sign-in token already carries Drive access (the shared
            # sign-in asks for it), so a class recording shared only inside PW
            # opens as the signed-in person — what the app does with its own.
            drive_token=token,
            drive_account=os.environ.get("NOTE_STUDIO_PW_ACCOUNT", ""),
        )
    except PermissionError:
        emit("done", status="failed", code="sign_in",
             reason="Your Google sign-in is missing or not allowed for this app. Sign in again in Settings.")
        return 0
    except Exception as exc:                                  # the old app's own plain words (app.py:1025)
        message = getattr(exc, "user_message", "") or ""
        if not message:
            text = str(exc).lower()
            message = next((words for needle, words in (
                ("not authorized", "Your account is not authorised for this app yet."),
                ("quota", "The service is busy right now. Please try again in a few minutes."),
                ("timed out", "That took too long and was stopped. Please try again."),
                ("timeout", "That took too long and was stopped. Please try again."),
            ) if needle in text), "The notes could not be made for this class.")
        print(f"{type(exc).__name__}: {exc}", flush=True)
        print(traceback.format_exc(), flush=True)
        emit("done", status="failed", reason=message, detail=f"{type(exc).__name__}: {str(exc)[:300]}")
        return 0

    for warning in result.warnings or []:
        emit("warning", text=str(warning)[:500])
    for path, kind, label in outputs(result):
        emit("output", path=path, kind=kind, label=label)
    status, reason = outcome(result, generate_error_report)
    emit("done", status=status, reason=reason, run_dir=str(result.run_dir),
         pages=((result.metadata or {}).get("note_length") or {}).get("final_pages"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
