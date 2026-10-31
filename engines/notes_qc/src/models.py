"""Data models shared by the QC stages.

``SlideData`` is copied verbatim from the note-generation project's
``src/models.py``: the deterministic checks and the independent AI review both
accept a list of these, and in this app one PDF *page* is one "slide" — that is
the whole of the input adaptation. ``PdfQcResult`` is new and belongs to the
standalone orchestration layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SlideData:
    slide_no: int
    heading: str = ""
    text: str = ""
    cleaned_text: str = ""
    image_path: Path | None = None
    filtered: bool = False
    filter_reason: str | None = None
    source_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_text(self) -> str:
        return self.cleaned_text if self.cleaned_text.strip() else self.text


@dataclass
class PdfQcResult:
    """Everything one PDF's QC run produced. One instance per uploaded file.

    ``status`` is "completed" / "completed_with_warnings" / "failed"; a failed
    file still returns a result object so the report can say what went wrong
    beside the files that succeeded.
    """

    file_name: str
    run_dir: Path | None = None
    status: str = "pending"
    error: str = ""

    # --- document facts -----------------------------------------------------
    page_count: int = 0
    word_count: int = 0
    language_code: str = "en"
    subject: str = ""
    lecture_title: str = ""

    # --- Error Report (the same 7-column rows the original app produces) -----
    error_rows: list[dict] = field(default_factory=list)
    error_report_status: str = "pending"   # ok | no_issues | parse_failed | missing | failed | disabled
    #: False when some part of the document was never checked — a part call
    #: failed, or the model's reply was cut off at the output cap. The row count
    #: below is then a floor, not a total, and the report must say so: "no errors
    #: found" and "the check did not finish" must never look the same.
    error_report_complete: bool = True
    #: How the second-opinion pass changed the findings: claims it refuted and
    #: removed, and claims it could not settle and dropped to Low. Both are
    #: reported, because a reviewer comparing two runs needs to know whether a
    #: row vanished because the notes improved or because the audit caught it.
    verification_dropped: int = 0
    verification_downgraded: int = 0
    #: Findings the strictness filter removed. Counted because an emptied report
    #: and an empty one are different facts about the file.
    strictness_dropped: int = 0
    #: Near-duplicate rows collapsed. A graded trial returned 35 rows of which
    #: 10 were repeats; counting them is how that stays visible.
    duplicate_rows_removed: int = 0
    error_report_summary: dict = field(default_factory=lambda: {"total": 0, "high": 0, "medium": 0, "low": 0})
    error_report_xlsx_path: Path | None = None

    # --- everything the original writes outside the XLSX --------------------
    #: {"stage", "severity", "page", "location", "check", "problem"} rows.
    qc_findings: list[dict] = field(default_factory=list)
    stages: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def findings_by_severity(self) -> dict[str, int]:
        out = {"High": 0, "Medium": 0, "Low": 0}
        for f in self.qc_findings:
            sev = str(f.get("severity") or "Medium")
            if sev in out:
                out[sev] += 1
        return out
