"""Export the existing DOCX builders' output using the bundled PDF renderer."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def export_docx_to_pdf(docx_path: Path, output_dir: Path) -> tuple[Path | None, str | None]:
    """Return only a fresh PDF; no Word, LibreOffice or external process is used."""
    docx_path, output_dir = Path(docx_path), Path(output_dir)
    try:
        from .docx_pdf import render_docx

        output_dir.mkdir(parents=True, exist_ok=True)
        # Build beside the destination so replacement is atomic. A failed render
        # leaves any earlier export intact and never reports that file as success.
        with tempfile.TemporaryDirectory(prefix=".pdf-", dir=output_dir) as work:
            rendered = Path(work) / f"{docx_path.stem}.pdf"
            render_docx(docx_path, rendered)
            if not rendered.is_file() or rendered.stat().st_size == 0:
                return None, "The bundled PDF renderer did not create a PDF."
            destination = output_dir / rendered.name
            os.replace(rendered, destination)
            return destination, None
    except Exception as exc:
        return None, f"PDF export failed: {exc}"
