"""Command-line runner — the same QC, without the browser.

Its real job is regression testing: it takes the PDFs the note-generation app
already produced, runs this app's QC over them, and writes the workbook where
you can diff it against that app's own error report.

    python run_cli.py notes1.pdf notes2.pdf
    python run_cli.py "C:\\path\\to\\folder"          # every PDF in the folder
    python run_cli.py notes.pdf --no-ai              # free checks only

Sign-in: the browser app gets its Google token from Streamlit. There is no
browser here, so pass one with --token or set UPSC_QC_GOOGLE_TOKEN. Without a
token the AI stages report "unavailable" and everything else still runs.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src.config import OUTPUTS_DIR
from src.input_normalize import SUPPORTED_EXTS
from src.pipeline import run_pdf_qc_batch


def _stored_session() -> tuple[str, str]:
    """(email, status) after trying to restore the stored 7-day proxy pass.

    Returns an empty email when there is nothing usable. The pass is the one the
    browser app minted; this runner only borrows it, and never writes one.
    """
    import json
    import os

    from src import proxy_session

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    store = (Path(base) / "UPSC_Notes" if base else Path.home() / ".upsc_notes")
    store = store / "proxy_session.json"
    if not store.exists():
        return "", ("No stored session found. Sign in once with `streamlit run "
                    "app.py`, then --session works for seven days.")
    try:
        email = str(json.loads(store.read_text(encoding="utf-8")).get("email") or "")
    except Exception as e:
        return "", f"The stored session could not be read ({e})."
    if not email:
        return "", "The stored session carries no email."
    if not proxy_session.attach(email):
        return "", (f"The stored session for {email} has expired or is too close "
                    "to expiring. Sign in again in the browser app.")
    return email, ""


def _collect(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in inputs:
        p = Path(raw).expanduser()
        if p.is_dir():
            paths.extend(sorted(q for q in p.iterdir()
                                if q.is_file() and q.suffix.lower() in SUPPORTED_EXTS))
        elif p.is_file():
            paths.append(p)
        else:
            print(f"  skipped (not found): {p}")
    return paths


def _rebuild(run_dir: Path, output_dir: Path) -> int:
    """Rebuild a workbook from a run folder's saved replies. No AI calls."""
    from src.rebuild_report import rebuild_run
    from src.config import now_ist
    from src.report.workbook import write_qc_workbook

    if not run_dir.is_dir():
        print(f"  Not a run folder: {run_dir}")
        return 2
    results = rebuild_run(run_dir)
    if not results:
        print(f"  No saved AI replies found under {run_dir}. Nothing to rebuild.")
        return 2

    for r in results:
        counts = r.error_report_summary or {}
        print(f"  {r.file_name}: {counts.get('total', 0)} error(s) recovered "
              f"(H{counts.get('high', 0)}/M{counts.get('medium', 0)}/"
              f"L{counts.get('low', 0)})")
        for warning in r.warnings:
            print(f"      ! {warning}")

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_ist().strftime("%Y-%m-%d_%H-%M")
    path = write_qc_workbook(results, output_dir / f"qc_report_rebuilt_{stamp}.xlsx")
    print(f"\nRebuilt workbook: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run UPSC notes QC over one or more already-generated PDFs.")
    parser.add_argument("inputs", nargs="*", help="PDF files, or folders of PDFs")
    parser.add_argument("--output", default=str(OUTPUTS_DIR),
                        help="where the workbook is written (default: output/)")
    parser.add_argument("--token", default=os.getenv("UPSC_QC_GOOGLE_TOKEN", ""),
                        help="Google id token for the PW proxy")
    parser.add_argument("--session", action="store_true",
                        help="reuse the 7-day proxy pass the browser app stored")
    parser.add_argument("--no-ai", action="store_true",
                        help="skip every AI stage; run the free checks only")
    parser.add_argument("--no-individual", action="store_true",
                        help="do not write the per-file XLSX reports")
    parser.add_argument("--rebuild", metavar="RUN_DIR", default="",
                        help="rebuild the workbook from a run folder's saved AI "
                             "replies, without calling the model again")
    args = parser.parse_args(argv)

    if args.rebuild:
        return _rebuild(Path(args.rebuild), Path(args.output))

    paths = _collect(args.inputs)
    if not paths:
        print("No PDFs to check.")
        return 2

    token = "" if args.no_ai else args.token
    if args.session and not args.no_ai:
        email, problem = _stored_session()
        if not email:
            print(f"  {problem}")
            return 2
        print(f"Signed in as {email} (stored 7-day proxy pass).")
        # pw_access rides on the restored pass. This sentinel tells the engine
        # that authorisation already happened, so its fail-closed gate does not
        # try to re-verify a value that is not a Google token.
        from src.proxy_session import RESTORED_PASS_TOKEN

        token = token or RESTORED_PASS_TOKEN

    if not token and not args.no_ai:
        print("  No credential: the AI stages will report \"unavailable\". "
              "Use --session, or --token, or pass --no-ai to say so on purpose.")

    print(f"Checking {len(paths)} PDF(s)...")

    def on_file(index: int, total: int, name: str, finished: bool = False) -> None:
        # In a parallel run several files are in flight at once, so the count is
        # of files finished and the name is the one that has just finished.
        print(f"\n[{index}/{total}] {name}{' — done' if finished else ''}")

    def on_stage(label: str) -> None:
        print(f"    {label}")

    run = run_pdf_qc_batch(
        paths,
        google_token=token,
        output_dir=Path(args.output),
        write_individual_reports=not args.no_individual,
        progress_callback=on_file,
        stage_callback=on_stage,
    )

    print("\n--- results ---")
    for r in run.results:
        counts = r.error_report_summary or {}
        if r.status == "failed":
            print(f"  {r.file_name}: QC FAILED — {r.error}")
            continue
        flag = "" if getattr(r, "error_report_complete", True) else "  [PARTIAL SCAN]"
        print(f"  {r.file_name}: {r.page_count} pages, "
              f"{counts.get('total', 0)} error(s) "
              f"(H{counts.get('high', 0)}/M{counts.get('medium', 0)}/L{counts.get('low', 0)}){flag}")
        # Per-file warnings live on the result, not on the batch, so without
        # this loop an incomplete scan never reaches the console at all — the
        # terminal would show a count that looks like a total.
        for warning in r.warnings:
            if warning.startswith("INCOMPLETE COVERAGE") or "verification pass" in warning:
                print(f"      ! {warning}")
    for warning in run.warnings:
        print(f"  ! {warning}")
    print(f"\nWorkbook: {run.workbook_path}")
    print(f"Run folder: {run.run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
