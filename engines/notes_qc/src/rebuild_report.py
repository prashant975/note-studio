"""Rebuild a report from a run folder, without calling the AI again.

Every AI reply is written to disk as it arrives — ``error_report_response_NN.txt``
per part, and the merged array as ``error_report_raw.json``. So when a run is
interrupted, crashes, loses its network, or finishes with parts marked
incomplete, the answers you already paid for are still there.

This reads them back and rebuilds the workbook. Nothing is re-asked, so it
costs nothing and takes a second, and it recovers rows that a failed run never
got as far as writing into a report.

    python run_cli.py --rebuild runs/qc_20260826_112400_7a8a0e12

What it cannot do is invent the parts that never answered. If a window's reply
is missing from the folder, its pages contributed nothing, and the rebuilt
report says so in the same words a live run would.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .error_report_writer import build_report_rows, extract_error_report_block, summarise
from .models import PdfQcResult

__all__ = ["rebuild_result", "rebuild_run"]

_RESPONSE_RE = re.compile(r"error_report_response_(\d+)(_retry)?\.txt$", re.I)


def _blocks_from_responses(folder: Path) -> tuple[list[str], list[int], int]:
    """(blocks, parts that carried no report, files read).

    A part that was retried has both files on disk; the numbered one holds the
    reply that was USED, so it is preferred and the ``_rejected`` copy ignored.
    """
    blocks: list[str] = []
    unreadable: list[int] = []
    seen = 0
    for path in sorted(folder.glob("error_report_response_*.txt")):
        if path.name.endswith("_rejected.txt"):
            continue
        match = _RESPONSE_RE.search(path.name)
        if not match:
            continue
        seen += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _body, block, found = extract_error_report_block(text)
        if not found:
            # The same marker-less fallback a live run uses.
            from .qc.engine import _block_without_markers

            block, found = _block_without_markers(text)
        if found and block.strip():
            blocks.append(block)
        else:
            unreadable.append(int(match.group(1)))
    return blocks, unreadable, seen


def rebuild_result(folder: Path) -> PdfQcResult | None:
    """One file's result, rebuilt from its run sub-folder. None if nothing is there."""
    folder = Path(folder)
    summary_path = folder / "qc_result.json"
    result = PdfQcResult(file_name=folder.name, run_dir=folder)
    if summary_path.exists():
        try:
            saved = json.loads(summary_path.read_text(encoding="utf-8"))
            result.file_name = saved.get("file_name") or folder.name
            result.page_count = saved.get("page_count") or 0
            result.word_count = saved.get("word_count") or 0
            result.lecture_title = saved.get("lecture_title") or ""
            result.stages = dict(saved.get("stages") or {})
        except Exception:
            pass

    from .qc.engine import _merge_error_blocks, dedupe_rows

    blocks, unreadable, seen = _blocks_from_responses(folder)
    if not blocks:
        # Fall back to the merged array, which a run writes even when the
        # per-part files are gone.
        raw = folder / "error_report_raw.json"
        if raw.exists():
            try:
                rows = json.loads(raw.read_text(encoding="utf-8"))
                if isinstance(rows, list) and rows:
                    blocks = [json.dumps(rows, ensure_ascii=False)]
            except Exception:
                blocks = []
    if not blocks:
        return None

    combined, salvaged, unusable = _merge_error_blocks(blocks)
    rows, status = build_report_rows(combined, marker_found=True, strictness="strict")
    from .error_report_writer import _PLACEHOLDER_TYPES

    real = [r for r in rows if r.get("error_type") not in _PLACEHOLDER_TYPES]
    if real:
        deduped, _removed = dedupe_rows(real)
        rows = deduped
    result.error_rows = rows
    result.error_report_status = status
    result.error_report_summary = summarise(rows)
    result.status = "completed_with_warnings"
    result.error_report_complete = not (unreadable or salvaged or unusable)
    result.warnings.append(
        f"This report was REBUILT from {seen} saved AI reply file(s) in the run "
        f"folder, without calling the model again. It contains every row those "
        f"replies carried."
    )
    if unreadable:
        result.warnings.append(
            f"INCOMPLETE COVERAGE: part(s) "
            f"{', '.join(str(p) for p in unreadable)} carried no error-report "
            f"block, so those pages contributed nothing. Re-run the file to "
            f"check them."
        )
    if salvaged:
        result.warnings.append(
            f"INCOMPLETE COVERAGE: {salvaged} row(s) were recovered from a reply "
            f"that was cut off. Any error the model had not yet written is "
            f"missing from this rebuild."
        )
    return result


def rebuild_run(run_dir: Path) -> list[PdfQcResult]:
    """Every file's result in ``run_dir``, rebuilt from what is on disk."""
    run_dir = Path(run_dir)
    out: list[PdfQcResult] = []
    for folder in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        if folder.name in {"input", "output"}:
            continue
        result = rebuild_result(folder)
        if result is not None:
            out.append(result)
    return out
