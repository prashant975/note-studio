"""Turn an accepted input file into a PDF the QC pipeline can read.

The pipeline is built around paginated PDFs: seven `fitz.open()` sites and a
long tail of page-geometry checks all assume one. Rather than teach every one
of them about Word documents and PNGs, anything that is not already a PDF is
converted to one here, at the door, and the rest of the app carries on
unchanged.

Two conversion routes, no new third-party packages:

* **PyMuPDF** already opens images, plain text and SVG. `convert_to_pdf()`
  re-wraps them as a PDF.
* **Office** files go through Word/PowerPoint/Excel over COM, driven by a
  PowerShell one-shot. Office is what produced these files, so its own export
  is the most faithful renderer available, and it needs nothing installed
  beyond the Office the user already has.

What comes back records where it came from, because the caller has to know:
the PW notes template checks (5.5in content column, header logo) only mean
something for a PDF that was laid out from that template, and firing them at a
converted Word file would report the format rather than a defect.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "UnsupportedInputError",
    "NormalizedInput",
    "normalize_to_pdf",
    "kind_for",
    "SUPPORTED_EXTS",
    "UPLOADER_TYPES",
    "describe_support",
]


class UnsupportedInputError(Exception):
    """The file is not something this app can quality-check."""


# --------------------------------------------------------------- what we take
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif"}
TEXT_EXTS = {".txt"}
VECTOR_EXTS = {".svg"}

WORD_EXTS = {".docx", ".doc", ".rtf", ".odt"}
PPT_EXTS = {".pptx", ".ppt", ".odp"}
EXCEL_EXTS = {".xlsx", ".xls", ".ods"}

NATIVE_EXTS = PDF_EXTS | IMAGE_EXTS | TEXT_EXTS | VECTOR_EXTS
OFFICE_EXTS = WORD_EXTS | PPT_EXTS | EXCEL_EXTS
OFFICE_KINDS = {"word", "powerpoint", "excel"}
SUPPORTED_EXTS = NATIVE_EXTS | OFFICE_EXTS

#: For `st.file_uploader(type=...)`, which wants bare extensions.
UPLOADER_TYPES = sorted(e.lstrip(".") for e in SUPPORTED_EXTS)

#: Only these keep the PW notes template checks. Everything else was laid out
#: by something other than the template, so that geometry says nothing.
TEMPLATE_KINDS = {"pdf"}

_KINDS = (
    (PDF_EXTS, "pdf"),
    (IMAGE_EXTS, "image"),
    (TEXT_EXTS, "text"),
    (VECTOR_EXTS, "vector"),
    (WORD_EXTS, "word"),
    (PPT_EXTS, "powerpoint"),
    (EXCEL_EXTS, "excel"),
)

_KIND_LABEL = {
    "pdf": "PDF",
    "image": "image",
    "text": "text file",
    "vector": "SVG drawing",
    "word": "Word document",
    "powerpoint": "PowerPoint deck",
    "excel": "Excel workbook",
}

# ProgID, and the SaveAs format number each Office app uses for PDF.
_OFFICE_APP = {
    "word": ("Word.Application", 17),         # wdFormatPDF
    "powerpoint": ("PowerPoint.Application", 32),  # ppSaveAsPDF
    "excel": ("Excel.Application", 0),        # xlTypePDF
}


def kind_for(ext: str) -> str:
    """"pdf" / "image" / "word" / ... for a file extension, or "" if unknown."""
    ext = ("." + ext.lstrip(".")).lower()
    for exts, kind in _KINDS:
        if ext in exts:
            return kind
    return ""


def describe_support() -> str:
    """One line naming what may be uploaded, for the UI caption."""
    return ("PDF, Word (.docx/.doc/.rtf/.odt), PowerPoint (.pptx/.ppt/.odp), "
            "Excel (.xlsx/.xls/.ods), images (.png/.jpg/.tif/.bmp/.gif), "
            "text (.txt) and .svg")


@dataclass
class NormalizedInput:
    """A PDF the pipeline can read, plus where it came from."""

    pdf_path: Path
    origin_ext: str
    origin_kind: str
    converted: bool
    note: str = ""

    @property
    def origin_label(self) -> str:
        return _KIND_LABEL.get(self.origin_kind, self.origin_kind or "file")

    @property
    def template_checks_apply(self) -> bool:
        """Whether the PW notes template geometry means anything for this file."""
        return self.origin_kind in TEMPLATE_KINDS


# ------------------------------------------------------------------- PyMuPDF
def _convert_native(source: Path, target: Path) -> None:
    """Images, text and SVG — PyMuPDF opens these and re-wraps them as a PDF."""
    import pymupdf

    with pymupdf.open(str(source)) as doc:
        if doc.page_count == 0:
            raise UnsupportedInputError("the file contains no pages")
        pdf_bytes = doc.convert_to_pdf()
    target.write_bytes(pdf_bytes)


# --------------------------------------------------------------------- Office
_PS_CONVERT = r"""
param([string]$In, [string]$Out, [string]$ProgId, [int]$Fmt)
$ErrorActionPreference = 'Stop'
$app = $null
$doc = $null
try {
    $app = New-Object -ComObject $ProgId
    try { $app.Visible = $false } catch {}
    try { $app.DisplayAlerts = 0 } catch {}
    try { $app.AutomationSecurity = 3 } catch {}   # msoAutomationSecurityForceDisable

    if ($ProgId -eq 'Word.Application') {
        $doc = $app.Documents.Open($In, [ref]$false, [ref]$true)   # ReadOnly
        $doc.SaveAs([ref]$Out, [ref]$Fmt)
    } elseif ($ProgId -eq 'PowerPoint.Application') {
        # PowerPoint refuses WithWindow=$false on some builds; open read-only.
        $doc = $app.Presentations.Open($In, $true, $false, $false)
        $doc.SaveAs($Out, $Fmt)
    } else {
        $doc = $app.Workbooks.Open($In, 0, $true)                  # ReadOnly
        $doc.ExportAsFixedFormat($Fmt, $Out)
    }
    Write-Output 'CONVERTED'
} finally {
    if ($doc -ne $null) { try { $doc.Close(0) } catch {} }
    if ($app -ne $null) { try { $app.Quit() } catch {} }
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($doc) 2>$null | Out-Null
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) 2>$null | Out-Null
}
"""


def _convert_office(source: Path, target: Path, kind: str, timeout: int = 180) -> None:
    """Word / PowerPoint / Excel -> PDF, using the installed Office over COM.

    The original is opened read-only and never written back to; the export
    lands on ``target``.
    """
    prog_id, fmt = _OFFICE_APP[kind]
    app_name = prog_id.split(".")[0]

    script = Path(tempfile.mkdtemp(prefix="upscqc_ps_")) / "convert.ps1"
    script.write_text(_PS_CONVERT, encoding="utf-8")
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-In", str(source.resolve()), "-Out", str(target.resolve()),
             "-ProgId", prog_id, "-Fmt", str(fmt)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise UnsupportedInputError(
            f"{app_name} did not finish converting the file within "
            f"{timeout}s. It may be waiting on a dialog — try opening the "
            f"file in {app_name} once, then run QC again."
        ) from None
    finally:
        shutil.rmtree(script.parent, ignore_errors=True)

    if not target.exists() or target.stat().st_size == 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason = detail[-1][:200] if detail else "no output was produced"
        raise UnsupportedInputError(
            f"{app_name} could not convert this file to PDF ({reason})"
        )


# ------------------------------------------------------------------ front door
def normalize_to_pdf(source: Path, dest_dir: Path, stem: str) -> NormalizedInput:
    """Return ``source`` as a PDF inside ``dest_dir``, converting if needed.

    ``source`` is only ever read. Raises :class:`UnsupportedInputError` when the
    file is not a format this app can check, so the caller can report that file
    and carry on with the rest of the batch.
    """
    source = Path(source)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    ext = source.suffix.lower()
    kind = kind_for(ext)
    target = dest_dir / f"{stem}.pdf"

    if not kind:
        raise UnsupportedInputError(
            f"{ext or 'this file'} is not a format this app can check. "
            f"Accepted: {describe_support()}."
        )

    if kind == "pdf":
        shutil.copy2(source, target)
        return NormalizedInput(target, ext, kind, converted=False)

    if kind in OFFICE_KINDS:
        _convert_office(source, target, kind)
    else:
        _convert_native(source, target)

    label = _KIND_LABEL.get(kind, kind)
    return NormalizedInput(
        target, ext, kind, converted=True,
        note=(f"Converted from {label} ({ext}) to PDF for checking. "
              f"Page-layout findings describe the converted copy."),
    )
