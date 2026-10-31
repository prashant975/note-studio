"""Note Studio's entry to this engine: one notes check per process (Note Studio doc 07).

    python engine_main.py --request <run>/request.json --out <run>      (cwd = this folder)

- It reads only request.json, plus the sign-in token from NOTE_STUDIO_PW_TOKEN.
- It sets this engine's configuration BEFORE importing `src`, so the original
  project's .env is never read and runs and reports go into Note Studio's run folder.
- It checks the files exactly the way app.py does (its run_pdf_qc_batch call,
  app.py:224-230, and its result words, app.py:264-301).
- The user's decisions (4 Oct 2026): every AI call goes through the PW proxy, so
  the direct GEMINI_API_KEY lookup in src/grounded_lookup.py is switched off here,
  at run time (no engine file is edited); web search through the proxy stays on.
- "full": false runs only the checks that need no AI (no sign-in, no cost): for
  notes that are not UPSC notes, which the AI check's prompts are written for.
- It prints one JSON object per line: step / cost / warning / output / done.

This file is Note Studio's; nothing else in this folder is changed for it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN_ENV = "NOTE_STUDIO_PW_TOKEN"
PROXY_APP = "upsc_file_QC"


def emit(event: str, **data) -> None:
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def configure(request: dict, out: Path) -> None:
    """The engine's environment, from request.json only. Must run before `src` is imported."""
    env = {
        "UPSC_PDF_QC_ROOT": str(HERE),
        # A path that never exists: the original project's .env is never read.
        "UPSC_PDF_QC_ENV": str(out / "no-env" / ".env"),
        "RUNS_DIR": str(out / "engine_runs"),
        "OUTPUT_DIR": str(out / "engine_outputs"),
    }
    notes = [m for m in (request.get("models") or {}).get("notes") or [] if m]
    if notes:
        env["MODEL_NAME"] = notes[0]
        env["FALLBACK_MODEL_NAME"] = notes[1] if len(notes) > 1 else ""
    os.environ.update(env)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):          # PW proxy only
        os.environ.pop(name, None)


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

    for name in ("gemini_generate", "claude_generate"):
        if hasattr(pw_access, name):
            setattr(pw_access, name, counted(getattr(pw_access, name), name))


def words_for(result, full: bool) -> tuple[str, str, str]:
    """(status, plain words, code) for one checked file: app.py:270-301, plus the
    two cases app.py cannot meet (a basic check, and an account PW refused)."""
    name, errors = result.file_name, int((result.error_report_summary or {}).get("total", 0))
    report_status = result.error_report_status or ""
    if result.status == "failed":
        return "failed", f"{name}: QC failed: {result.error}", ""
    denied = [w for w in result.warnings or [] if "proxy denied access" in w or "proxy gate errored" in w]
    if full and denied:
        return ("needs_attention", f"{name}: PW did not allow this account for Check notes (the '{PROXY_APP}' "
                "column of the whitelist), so the notes were NOT checked for errors; only the basic checks ran.",
                "sign_in")
    if not getattr(result, "error_report_complete", True):
        return ("needs_attention", f"{name}: PARTIAL SCAN: {errors} error(s) found so far. Part of this file was "
                "not checked; the count is a floor, not a total.", "")
    if report_status in ("missing", "parse_failed", "failed"):
        return ("needs_attention", f"{name}: the AI's check did not produce a readable report "
                f"({report_status.replace('_', ' ')}). This is NOT a clean result; see the workbook.", "")
    if report_status in ("unavailable", "disabled"):
        if not full:
            findings = len(getattr(result, "error_rows", None) or [])
            return ("complete", f"{name}: basic checks done (spelling, names, leftover markers, repeats): "
                    f"{findings} finding(s). The AI error check is for UPSC notes and was not run.", "")
        return ("needs_attention", f"{name}: check not run ({report_status}). The notes were NOT checked for errors.", "")
    if report_status == "filtered_out":
        return ("complete", f"{name}: findings were reported but all were out of scope at this strictness level, "
                "so none are listed.", "")
    if report_status == "all_refuted":
        return "complete", f"{name}: no confirmed errors (every claimed error was rejected on review).", ""
    return "complete", f"{name}: {errors} error(s).", ""


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
    from src import grounded_lookup
    from src.config import OUTPUTS_DIR
    from src.pipeline import run_pdf_qc_batch

    grounded_lookup._api_key = lambda: ""           # PW proxy only: never the direct Gemini call
    report_costs()
    full = bool(request.get("full"))
    token = os.environ.get(TOKEN_ENV, "") if full else ""
    if full and not token:
        emit("done", status="failed", code="sign_in", reason="Sign in with Google in Settings, then press Try again.")
        return 0
    files = [Path(f) for f in request.get("files") or []]
    if not files or not all(f.is_file() for f in files):
        emit("done", status="failed", reason="The file to check is missing.")
        return 0
    current = {"name": ""}

    def on_file(index: int, of: int, name: str, finished: bool = False) -> None:   # app.py:199-210
        current["name"] = name
        emit("step", text=f"Checked {index} / {of}" if finished else f"Processing {index} / {of}: {name}",
             pct=int(100 * (index if finished else index - 1) / max(of, 1)) or 2)

    def on_stage(label: str) -> None:                                               # app.py:212-214
        emit("step", text=f"{current['name']}: {label.lower()}", pct=None)

    try:
        run = run_pdf_qc_batch(files, google_token=token, output_dir=OUTPUTS_DIR,     # app.py:224-230
                               progress_callback=on_file, stage_callback=on_stage)
    except Exception as exc:  # noqa: BLE001
        emit("done", status="failed", reason=f"QC could not be completed: {exc}"[:600])
        return 0

    statuses, messages, codes = [], [], []
    for result in run.results:
        status, words, code = words_for(result, full)
        statuses.append(status)
        messages.append(words)
        codes.append(code)
        for warning in result.warnings or []:
            emit("warning", text=f"{result.file_name}: {warning}"[:500])
    if run.workbook_path and Path(run.workbook_path).is_file():
        emit("output", path=str(run.workbook_path), kind="xlsx", label="Error report")
    else:
        statuses.append("failed")
        messages.append("The report could not be written. Please run the check again.")
    for path in run.individual_paths or []:
        if Path(path).is_file():
            emit("output", path=str(path), kind="xlsx", label="Error report for one file")
    status = "failed" if "failed" in statuses else "needs_attention" if "needs_attention" in statuses else "complete"
    emit("done", status=status, reason=" ".join(messages)[:1500], code="sign_in" if "sign_in" in codes else "",
         run_dir=str(run.run_dir), errors=sum(int((r.error_report_summary or {}).get("total", 0)) for r in run.results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
