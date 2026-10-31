"""Note Studio's entry to this engine: one lecture deck per process (Note Studio doc 07).

    python engine_main.py --request <run>/request.json --out <run>      (cwd = this folder)

- It reads only request.json, plus the sign-in token from NOTE_STUDIO_PW_TOKEN.
- It sets this engine's configuration BEFORE importing `src`, so the original
  project's .env is never read and runs go into Note Studio's run folder.
- It runs a lecture exactly the way app.py does: the fixed settings
  (app.py:1052-1062), the backend processing mode (app.py:1248-1261), the model
  routing (app.py:1321-1341) and the run_pipeline call (app.py:1383-1406).
  run_cli.py takes another path. src/pw_auth.py is not used: the pipeline takes
  the token as a plain argument.
- It prints one JSON object per line: step / cost / warning / output / done.

This file is Note Studio's; nothing else in this folder is changed for it
except the extra-instructions hook in src/prompt_builder.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN_ENV = "NOTE_STUDIO_PW_TOKEN"
LANGUAGES = {"english": "English", "hindi": "Hindi"}
PLACEHOLDER = "failed after retries and were left as a placeholder"


def emit(event: str, **data) -> None:
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def configure(request: dict, out: Path) -> None:
    """The engine's environment, from request.json only. Must run before `src` is imported."""
    env = {
        "HANDWRITTEN_NOTES_ROOT": str(HERE),
        # A path that never exists: the original project's .env is never read.
        "HANDWRITTEN_NOTES_ENV": str(out / "no-env" / ".env"),
        "RUNS_DIR": str(out / "engine_runs"),
        "OUTPUT_DIR": str(out / "engine_outputs"),
    }
    models = request.get("models") or {}
    # The router reads <TASK>_MODEL_PRIMARY / _FALLBACK_1 / _FALLBACK_2 (src/model_router.py).
    for task in ("notes", "vision", "qc"):
        order = [m for m in models.get(task) or [] if m]
        if order:
            for name, value in zip(("PRIMARY", "FALLBACK_1", "FALLBACK_2"), order + ["", ""]):
                env[f"{task.upper()}_MODEL_{name}"] = value
    if models.get("images"):
        env["IMAGE_MODEL_NAME"] = models["images"][0]
    os.environ.update(env)


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

    for name in ("gemini_generate", "mathpix_ocr", "gemini_image"):
        if hasattr(pw_access, name):
            setattr(pw_access, name, counted(getattr(pw_access, name), name))


def show_progress() -> None:
    """run_pipeline has no progress callback: say which stage it is in by passing
    through the pipeline's own module-level steps (called by name at run time)."""
    import src.pipeline as pipeline

    steps = (("_extract_input", "Reading the lecture", 5), ("build_generation_prompt", "Writing the notes", 20),
             ("build_merge_prompt", "Joining the parts", 60), ("write_notes_docx", "Writing the Word file", 75),
             ("export_docx_to_pdf", "Making the PDF", 85), ("quality_check", "Checking the notes", 92),
             ("zip_dir", "Packing the run", 97))
    for name, words, pct in steps:
        real = getattr(pipeline, name, None)
        if real is None:
            continue
        said = {"done": False}

        def step(*args, _real=real, _words=words, _pct=pct, _said=said, **kwargs):
            if not _said["done"]:                     # once per stage (chunks call it many times)
                _said["done"] = True
                emit("step", text=_words, pct=_pct)
            return _real(*args, **kwargs)

        setattr(pipeline, name, step)


def outcome(result) -> tuple[str, str]:
    if not result.docx_path or not Path(result.docx_path).is_file():
        return "failed", "The notes document was not written."
    problems = []
    if not result.pdf_path:
        problems.append("The PDF could not be made (it needs LibreOffice or Microsoft Word).")
    missing = [w for w in result.warnings or [] if PLACEHOLDER in str(w)]
    if missing:
        problems.append("Some pages could not be written after retries: " + " ".join(str(w) for w in missing)[:400])
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
    # Imported only now, after configure(): src.config reads the environment once.
    from src import model_router as mr
    from src import prompt_builder
    from src.ai_client import GeminiError
    from src.config import (DEFAULT_IMAGE_MODEL, DEFAULT_MATH_RENDER_MODE, DEFAULT_PROCESSING_MODE, EXAMS,
                            MATH_RENDER_MODES, SUBJECTS)
    from src.pipeline import run_pipeline

    report_costs()
    show_progress()
    prompt_builder.EXTRA_INSTRUCTIONS = str(request.get("extra_instructions") or "")
    if request.get("test_stub_pdf"):                 # tests only (the PDF converter needs Word/LibreOffice)
        import src.pipeline

        src.pipeline.export_docx_to_pdf = lambda *a, **k: (None, "PDF export is stubbed in tests.")

    mock = bool(request.get("mock"))                 # tests only: no sign-in, the engine's mock notes
    google_token = "" if mock else os.environ.get(TOKEN_ENV, "")
    source = Path(str(request.get("file") or ""))
    subject = str(request.get("subject") or "")
    language = LANGUAGES.get(str(request.get("language") or "").lower(), "")
    if not source.is_file():
        emit("done", status="failed", reason="The lecture file is missing.")
        return 0
    if subject not in SUBJECTS:
        emit("done", status="failed", reason="Select a subject to enable generation.")
        return 0
    if not language:
        emit("done", status="failed", reason="Choose English or Hindi.")
        return 0
    exam = str(request.get("exam") or "")
    exam = exam if exam in EXAMS.values() else ""
    mode = str(request.get("mode") or "summary")
    mode = mode if mode in ("summary", "complete") else "summary"
    math_render_mode = str(request.get("math_render_mode") or DEFAULT_MATH_RENDER_MODE)
    math_render_mode = math_render_mode if math_render_mode in MATH_RENDER_MODES.values() else DEFAULT_MATH_RENDER_MODE
    strict_math = bool(request.get("strict_math", True))

    def redraw_enabled(profile_default: bool) -> bool:          # app.py:1065-1075
        raw = (os.getenv("REDRAW_DIAGRAMS") or "").strip().lower()
        return profile_default if not raw else raw in {"1", "true", "yes", "on"}

    # The backend processing mode (app.py:1248-1261).
    processing_mode = DEFAULT_PROCESSING_MODE
    if processing_mode == "auto":
        preview_cfg = mr.load_routing_config()
        preview_health = mr.peek_health(preview_cfg.all_models()) or mr.assume_available(preview_cfg.all_models())
        effective_mode = mr.recommend_mode(preview_cfg, preview_health)[0] if preview_health else mr.DEFAULT_MODE
    else:
        effective_mode = processing_mode if processing_mode in mr.MODE_PROFILES else mr.DEFAULT_MODE
    profile = mr.MODE_PROFILES[effective_mode]

    # Model routing (app.py:1321-1345).
    emit("step", text="Preparing", pct=2)
    cfg = mr.load_routing_config()
    run_mode, run_profile = processing_mode, profile
    try:
        health = mr.check_models(cfg.all_models(), mr.make_probe(google_token), cfg)
        if processing_mode == "auto":
            run_mode, _reason = mr.recommend_mode(cfg, health)
            run_profile = mr.MODE_PROFILES[run_mode]
        decision = mr.resolve(run_mode, cfg, health, manual={})
        notes_fallbacks = mr.available_chain("notes", run_mode, cfg, health)
        vision_fallbacks = mr.available_chain("vision", run_mode, cfg, health)
    except mr.RouterError as exc:
        emit("done", status="failed", reason=str(exc))
        return 0
    except Exception as exc:  # noqa: BLE001
        emit("done", status="failed", reason=f"Model routing failed: {exc}")
        return 0

    def retry(notice: str) -> None:                  # app.py:1381 + 1416-1423 (shown as notices)
        emit("step", text=f"Notice: {notice}", pct=None)

    try:
        # Exactly app.py:1383-1406.
        result = run_pipeline(
            source, subject=subject, language=language, mode=mode,
            google_token=google_token, model=decision.notes.model,
            send_images_to_ai=run_profile.vision_default_on,
            strict_filter=True,
            allow_mock=mock, image_insert_mode="smart_crop", dtp_note_policy="hide_note_insert_image",
            ai_redraw_diagrams=redraw_enabled(run_profile.redraw_diagrams),
            image_model=DEFAULT_IMAGE_MODEL,
            retry_callback=retry,
            exam=exam, strict_math=strict_math,
            math_render_mode=math_render_mode,
            processing_mode=run_mode,
            notes_model=decision.notes.model,
            vision_model=decision.vision.model or decision.notes.model,
            notes_fallbacks=notes_fallbacks,
            vision_fallbacks=vision_fallbacks,
            qc_model=(decision.qc.model if decision.qc else None),
            qc_level=run_profile.qc_level,
            routing_summary=decision.summary(),
        )
    except PermissionError:
        emit("done", status="failed", code="sign_in",
             reason="Your Google sign-in is missing or not allowed for this app. Sign in again in Settings.")
        return 0
    except GeminiError as exc:                       # app.py:1410-1411
        print(traceback.format_exc(), flush=True)
        code = "sign_in" if "sign" in str(exc).lower() and "google" in str(exc).lower() else ""
        emit("done", status="failed", reason=f"Gemini API failed: {exc}"[:600], code=code)
        return 0
    except Exception as exc:  # noqa: BLE001 — app.py:1413-1414
        print(traceback.format_exc(), flush=True)
        emit("done", status="failed", reason=f"Processing failed: {exc}"[:600])
        return 0

    for warning in result.warnings or []:
        emit("warning", text=str(warning)[:500])
    run_dir = Path(result.run_dir)
    for path, kind, label in ((result.docx_path, "docx", "Notes (Word)"), (result.pdf_path, "pdf", "Notes (PDF)"),
                              (run_dir / "equation_quality_report.md", "md", "Equation quality report"),
                              (result.zip_path, "zip", "Everything from this run (ZIP)")):
        if path and Path(path).is_file():
            emit("output", path=str(path), kind=kind, label=label)
    status, reason = outcome(result)
    emit("done", status=status, reason=reason, run_dir=str(run_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
