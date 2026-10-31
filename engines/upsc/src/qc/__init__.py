"""Quality-control and source-verification package.

Implements the PW OnlyIAS Notes QC Framework (see
reference_documents/notes_qc_framework.md): an independent review of generated
notes, verification of high-risk factual claims against approved subject
sources, safe application of supported corrections, and an Error Fixed Report.

Orchestrated exclusively by src/qc/engine.py, which is called from
src/pipeline.py run_pipeline() — the single pipeline entry point.
"""
