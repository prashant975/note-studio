"""Quality-control package for already-generated UPSC notes PDFs.

Every check in here is the note-generation project's own check, copied
unchanged; only the input layer differs — the notes arrive as a rendered PDF
instead of as marker text plus slides plus a transcript.

Orchestrated exclusively by src/qc/engine.py, which is called from
src/pipeline.py run_pdf_qc() — the single entry point.
"""
