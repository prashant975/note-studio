"""Batch orchestration: N PDFs in, one QC workbook out.

Each PDF is an independent job. They are never merged, never compared with each
other, and a file that fails never stops the ones behind it — the workbook says
which file failed and why, beside the results for the files that succeeded.

Layout of a run, mirroring the note-generation project's run folder:

    runs/<run_id>/
        input/            the uploaded PDFs, copied in (never modified)
        <stem>/           one folder per PDF
            rendered/     one PNG per page
            layout_check/ rasterised pages from the layout scan
            format_check_pages/  the pages sent to the visual model
            *.json        every stage's artifact
        output/           the workbook and the per-file reports
"""
from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    DEFAULT_ERROR_REPORT_STRICTNESS,
    DEFAULT_MODEL,
    MAX_PARALLEL_AI_CALLS,
    MAX_PARALLEL_PDFS,
    OUTPUTS_DIR,
    RUNS_DIR,
    now_ist,
    stamp_ist,
)
from .models import PdfQcResult
from .input_normalize import UnsupportedInputError, normalize_to_pdf
from .qc.engine import run_pdf_qc
from .report.workbook import write_individual_report, write_qc_workbook
from .utils import ensure_dir, make_run_id, safe_name, write_json


@dataclass
class QcRunResult:
    """Everything one batch produced."""

    run_id: str
    run_dir: Path
    results: list[PdfQcResult] = field(default_factory=list)
    workbook_path: Path | None = None
    individual_paths: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_errors(self) -> int:
        return sum(int((r.error_report_summary or {}).get("total", 0)) for r in self.results)

    @property
    def total_findings(self) -> int:
        return sum(len(r.qc_findings) for r in self.results)

    @property
    def failed(self) -> list[PdfQcResult]:
        return [r for r in self.results if r.status == "failed"]


class _Throttled:
    """A GeminiClient that waits for a slot before each call.

    Used only when several PDFs are checked at once. Each file's own stages
    already run their chunks concurrently, so without a shared limit the two
    levels multiply and the proxy starts answering 429. Everything else about
    the client — retries, the model fallback, the fallback notices the pipeline
    surfaces — is the real client's and passes straight through.
    """

    def __init__(self, client, slots: threading.Semaphore):
        self._client = client
        self._slots = slots

    def generate(self, *args, **kwargs):
        with self._slots:
            return self._client.generate(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _build_client(google_token: str, task_id: str, file_name: str = "",
                  pages: int | None = None):
    """The note-generation project's GeminiClient, unchanged.

    Returns None when there is no token, which puts every AI stage into the
    "unavailable" state instead of failing the run — the deterministic,
    geometric and structural checks still produce a report.
    """
    if not (google_token or "").strip():
        return None
    from .ai_client import GeminiClient

    return GeminiClient(
        google_token, model=DEFAULT_MODEL, task_id=task_id,
        filename=file_name, input_unit="pages", count=pages,
    )


def run_pdf_qc_batch(
    pdf_paths: list[Path],
    *,
    google_token: str = "",
    output_dir: Path | None = None,
    runs_dir: Path | None = None,
    strictness: str = DEFAULT_ERROR_REPORT_STRICTNESS,
    write_individual_reports: bool = True,
    keep_run_files: bool = True,
    progress_callback=None,
    stage_callback=None,
) -> QcRunResult:
    """QC every PDF in ``pdf_paths`` and write one workbook for the batch.

    ``progress_callback(n, total, file_name, finished)`` is called as work moves
    on. ``finished`` says which it is: False when ``file_name`` is about to be
    checked (serial runs), True when it has just been checked (parallel runs,
    where "about to start" describes several files at once and means nothing).
    A three-argument callback is accepted too.

    ``stage_callback(label)`` reports the stage inside the current file. It is
    only called on a serial run — in parallel the stages of several files
    interleave, and a single line of text cannot honestly describe that.
    """
    import pw_access

    runs_root = ensure_dir(Path(runs_dir or RUNS_DIR))
    out_root = ensure_dir(Path(output_dir or OUTPUTS_DIR))
    run_id = make_run_id("qc")
    run_dir = ensure_dir(runs_root / run_id)
    input_dir = ensure_dir(run_dir / "input")
    run_output_dir = ensure_dir(run_dir / "output")

    run = QcRunResult(run_id=run_id, run_dir=run_dir)
    # One task id for the whole batch, so the proxy's raw usage rows for every
    # file in this run group together — the same convention the note-generation
    # pipeline uses for one lecture.
    task_id = pw_access.new_task_id("upsc-pdf-qc")

    total = len(pdf_paths)
    workers = max(1, min(MAX_PARALLEL_PDFS, total))
    parallel = workers > 1

    stems: set[str] = set()
    # Two files can be checked at once, so the two things they share have to be
    # taken one at a time: the set of folder names already claimed, and the
    # run-level warning list.
    bookkeeping = threading.Lock()

    # Total AI calls in flight across the WHOLE run. Without this the two levels
    # of parallelism multiply — MAX_PARALLEL_PDFS files each running
    # MAX_PARALLEL_AI_CALLS chunks is 12 concurrent requests at the defaults —
    # and the proxy answers 429, which is retried with backoff and costs back
    # exactly the time the parallelism was meant to save. File-level parallelism
    # still pays: rendering pages, scanning layout and finding tables are real
    # work that overlaps with another file's waiting.
    ai_slots = threading.Semaphore(max(1, MAX_PARALLEL_AI_CALLS))

    def claim_folder(source: Path, index: int) -> str:
        stem = safe_name(source.stem, default=f"pdf_{index + 1}")
        with bookkeeping:
            unique = stem
            n = 2
            while unique in stems:
                unique = f"{stem}_{n}"
                n += 1
            stems.add(unique)
        return unique

    def warn(message: str) -> None:
        with bookkeeping:
            run.warnings.append(message)

    def report(n: int, name: str, *, finished: bool) -> None:
        """Call the caller's progress hook, four-argument or three."""
        if not progress_callback:
            return
        try:
            progress_callback(n, total, name, finished)
        except TypeError:
            try:
                progress_callback(n, total, name)
            except Exception:
                pass
        except Exception:
            pass

    def one(index: int, source: Path) -> PdfQcResult:
        source = Path(source)
        file_name = source.name
        # In serial mode the caller is told a file has started, and then hears
        # every stage inside it. In parallel mode several files are inside a
        # stage at once, so "started" is meaningless and progress is reported by
        # the main thread as each file finishes instead.
        if not parallel:
            report(index + 1, file_name, finished=False)

        unique = claim_folder(source, index)
        file_run_dir = ensure_dir(run_dir / unique)
        try:
            # The uploaded file is copied in and read from the copy. The path the
            # caller handed us is never written to, renamed or deleted. Anything
            # that is not already a PDF is converted to one here, so every stage
            # downstream still sees the paginated PDF it was written against.
            origin = normalize_to_pdf(source, input_dir, unique)
            local = origin.pdf_path
        except UnsupportedInputError as e:
            # Not a defect in the file — we simply do not check this format. Say
            # so plainly and let the rest of the batch continue.
            return PdfQcResult(file_name=file_name, run_dir=file_run_dir,
                               status="failed",
                               error=f"This file was not checked: {e}")
        except Exception as e:
            result = PdfQcResult(file_name=file_name, run_dir=file_run_dir,
                                 status="failed",
                                 error=f"The file could not be read: {e}")
            return result

        try:
            client = _build_client(google_token, task_id, file_name=file_name)
            if client is not None and parallel:
                client = _Throttled(client, ai_slots)
        except Exception as e:
            client = None
            warn(f"{file_name}: the AI stages were skipped ({e}).")

        try:
            return run_pdf_qc(
                local, run_dir=file_run_dir, client=client,
                google_token=google_token, file_name=file_name,
                strictness=strictness, origin=origin,
                stage_callback=None if parallel else stage_callback,
            )
        except Exception as e:
            # run_pdf_qc handles its own stage failures; reaching here means
            # something outside every stage broke. One file's collapse must not
            # take the batch down.
            return PdfQcResult(file_name=file_name, run_dir=file_run_dir,
                               status="failed", error=str(e)[:500])

    if parallel:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, i, p): i
                       for i, p in enumerate(pdf_paths)}
            done_count = 0
            by_index: dict[int, PdfQcResult] = {}
            for future in as_completed(futures):
                index = futures[future]
                by_index[index] = future.result()
                done_count += 1
                # Reported from the main thread, not from the worker: the
                # Streamlit widgets a callback writes to belong to the script's
                # own thread and silently do nothing when touched from another.
                report(done_count, by_index[index].file_name, finished=True)
            run.results = [by_index[i] for i in sorted(by_index)]
    else:
        run.results = [one(i, p) for i, p in enumerate(pdf_paths)]

    # ------------------------------------------------------------- reports
    stamp = now_ist().strftime("%Y-%m-%d_%H-%M")
    workbook_name = f"qc_report_{stamp}.xlsx"
    try:
        run.workbook_path = write_qc_workbook(run.results, out_root / workbook_name)
        # A copy inside the run folder, so a run is self-contained even after the
        # output folder is tidied.
        write_qc_workbook(run.results, run_output_dir / workbook_name)
    except Exception as e:
        run.warnings.append(f"The QC workbook could not be written: {e}")

    if write_individual_reports:
        individual_dir = out_root / "individual_reports"
        for result in run.results:
            try:
                path = write_individual_report(result, individual_dir)
                if path:
                    run.individual_paths.append(path)
                    result.error_report_xlsx_path = path
            except Exception as e:
                run.warnings.append(
                    f"{result.file_name}: its individual report could not be "
                    f"written ({e}).")

    write_json(run_dir / "run_log.json", {
        **stamp_ist(),
        "run_id": run_id,
        "task_id": task_id,
        "files": [
            {
                "file_name": r.file_name,
                "status": r.status,
                "error": r.error,
                "pages": r.page_count,
                "words": r.word_count,
                "language": r.language_code,
                "error_report_status": r.error_report_status,
                "error_report_complete": r.error_report_complete,
                "verification_dropped": r.verification_dropped,
                "verification_downgraded": r.verification_downgraded,
                "error_report_summary": r.error_report_summary,
                "qc_findings": len(r.qc_findings),
                "stages": r.stages,
                "warnings": r.warnings,
            }
            for r in run.results
        ],
        "workbook": str(run.workbook_path) if run.workbook_path else None,
        "warnings": run.warnings,
    })

    if not keep_run_files:
        cleanup_run(run)
    return run


def cleanup_run(run: QcRunResult) -> None:
    """Delete the run's working files, keeping the log and the reports.

    The uploaded PDFs and every rendered page live under the run folder; the
    reports were written to the output folder before this is called, so nothing
    a user needs is inside what is removed.
    """
    for child in ("input",):
        target = run.run_dir / child
        try:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
        except Exception:
            pass
    for result in run.results:
        if not result.run_dir:
            continue
        for child in ("rendered", "layout_check", "format_check_pages"):
            target = Path(result.run_dir) / child
            try:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
            except Exception:
                pass
