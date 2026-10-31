"""Fixtures for the DPP question-set builder tests.

Ported with the notes_to_dpp package. The sys.path bootstrap lives here rather
than in each file because a conftest is imported before collection, so it fixes
the import path for every ported test at once.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from io import BytesIO

import pytest
from openpyxl import Workbook

from src.notes_to_dpp.models import Difficulty, QuestionRecord, QuestionTag, TaxonomyRow


@pytest.fixture
def xlsx_bytes():
    def build(
        headers: list[str],
        rows: list[list[object]],
        *,
        sheet_name: str = "Sheet1",
    ) -> bytes:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = sheet_name
        sheet.append(headers)
        for row in rows:
            sheet.append(row)

        output = BytesIO()
        workbook.save(output)
        workbook.close()
        return output.getvalue()

    return build


@pytest.fixture
def taxonomy_row_factory():
    def build(
        *,
        row_id: str = "ROW-1",
        subject_id: str = "SUBJECT-1",
        chapter_id: str = "CHAPTER-1",
        topic_id: str = "TOPIC-1",
        subtopic_id: str | None = None,
    ) -> TaxonomyRow:
        return TaxonomyRow(
            row_id=row_id,
            subject=f"Subject {subject_id}",
            subject_id=subject_id,
            chapter=f"Chapter {chapter_id}",
            chapter_id=chapter_id,
            topic=f"Topic {topic_id}",
            topic_id=topic_id,
            subtopic=f"Subtopic {subtopic_id}" if subtopic_id else None,
            subtopic_id=subtopic_id,
        )

    return build


@pytest.fixture
def question_factory():
    def build(
        question_id: str,
        difficulty: Difficulty,
        tag: QuestionTag,
        *,
        fingerprint: str | None = None,
    ) -> QuestionRecord:
        return QuestionRecord(
            question_id=question_id,
            prompt=f"Prompt for {question_id}",
            options=("Option A", "Option B"),
            difficulty=difficulty,
            tags=(tag,),
            source_tags=("DPP",),
            content_fingerprint=fingerprint or f"fingerprint-{question_id}",
            verified=True,
        )

    return build


# ---------------------------------------------------------------------------
# Most pipeline tests exercise generation rather than PDF layout, so they
# substitute the exporter. Mark real_pdf to exercise the bundled renderer.
# ---------------------------------------------------------------------------


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_pdf: run the bundled DOCX->PDF renderer",
    )


@pytest.fixture(autouse=True)
def _stub_pdf_rendering(request, monkeypatch):
    """Neutralise the DOCX->PDF converter unless the test asks for it.

    Patched at the two module attributes the callers actually resolve, not at
    the definition: `pipeline` and `dmp.export` both imported the name directly,
    so patching only `pdf_exporter` would leave their bindings pointing at the
    real function.
    """
    if request.node.get_closest_marker("real_pdf"):
        return

    def _stub(docx_path, output_dir):
        return None, ("PDF export is stubbed in tests; mark the test with "
                      "@pytest.mark.real_pdf to run the real converter.")

    targets = []
    for module_name, attr in (
        ("src.pdf_exporter", "export_docx_to_pdf"),
        ("src.pipeline", "export_docx_to_pdf"),
        ("src.dmp.export", "export_docx_to_pdf"),
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 — a module that will not import cannot leak COM
            continue
        if hasattr(module, attr):
            targets.append((module, attr))
    # Import every caller before patching the definition. Otherwise a freshly
    # imported caller binds the stub as its original, and undo leaves that stub
    # behind even for the next real_pdf test.
    for module, attr in targets:
        monkeypatch.setattr(module, attr, _stub)


# ---------------------------------------------------------------------------
# The authorization gate is restored after every test
# ---------------------------------------------------------------------------
# tests/app_harness.py boots app.py for Streamlit's AppTest, and to reach the
# page at all it has to stand in for a signed-in, whitelisted user: it replaces
# pw_access.check_allowed_status and the proxy-session helpers. AppTest runs
# that script in THIS process, so those replacements are permanent — the first
# run of test_app_runtime.py disabled the gate for every test that came after
# it, and three security tests in test_auth_session.py went green while
# asserting nothing.
#
# A test that neuters the fail-closed gate and leaves it neutered is worse than
# no test. This fixture makes that impossible to do by accident: whatever a
# test does to these attributes, they are the originals again before the next
# one starts.

_SECURITY_CRITICAL = (
    ("pw_access", ("check_allowed", "check_allowed_status")),
    ("src.proxy_session", ("attach", "persist")),
)


@pytest.fixture(scope="session")
def _pristine_gate():
    """The real functions, captured before any test has run.

    Session-scoped on purpose. Snapshotting inside the per-test fixture looked
    right and was not: pytest builds higher-scoped fixtures first, so a
    module-scoped fixture that boots the app harness had already replaced these
    by the time a function-scoped snapshot ran — and the "restore" faithfully
    put the patched versions back.
    """
    saved = []
    for module_name, attrs in _SECURITY_CRITICAL:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 — a module that will not import cannot be patched
            continue
        for attr in attrs:
            if hasattr(module, attr):
                saved.append((module, attr, getattr(module, attr)))
    return saved


@pytest.fixture(autouse=True)
def _restore_the_authorization_gate(_pristine_gate):
    try:
        yield
    finally:
        for module, attr, original in _pristine_gate:
            setattr(module, attr, original)
