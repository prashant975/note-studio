"""What an edit is allowed to say, and what fails before a browser starts.

Nothing here touches the QBG portal. The one test that starts Playwright
points it at a closed local port to prove a missing debug Chrome turns into
operator instructions rather than a stack trace.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qbg_bridge import QbgBridgeError, QbgEdit
from src.qbg_bridge.client import _require_valid_ref


class TestEditValidation:
    def test_a_full_edit_normalises_every_field(self):
        edit = QbgEdit(
            language="hi",
            question="  Which Act?  ",
            options={"b": " New option text "},
            correct_letters=("c", "a", "c"),
            solution="Solution: Because the Act says so.",
        ).normalized()
        assert edit.language == "Hindi"
        assert edit.question == "Which Act?"
        assert edit.options == {"B": "New option text"}
        assert edit.correct_letters == ("C", "A")
        assert edit.solution == "Because the Act says so."
        assert edit.summary() == (
            "question, option B, correct answer C, A, solution")

    def test_an_empty_edit_is_refused(self):
        with pytest.raises(QbgBridgeError, match="Nothing to push"):
            QbgEdit().normalized()

    def test_unknown_languages_are_refused(self):
        with pytest.raises(QbgBridgeError, match="English or Hindi"):
            QbgEdit(language="Both", solution="x").normalized()

    def test_option_letters_outside_a_to_d_are_refused(self):
        with pytest.raises(QbgBridgeError, match="A-D"):
            QbgEdit(options={"E": "text"}).normalized()

    def test_blanking_a_field_is_refused(self):
        with pytest.raises(QbgBridgeError, match="question statement"):
            QbgEdit(question="   ").normalized()
        with pytest.raises(QbgBridgeError, match="Option B"):
            QbgEdit(options={"B": "  "}).normalized()
        with pytest.raises(QbgBridgeError, match="solution"):
            QbgEdit(solution=" ``` ").normalized()

    def test_letters_alone_are_a_valid_edit(self):
        edit = QbgEdit(correct_letters=("b",)).normalized()
        assert edit.correct_letters == ("B",)
        assert edit.summary() == "correct answer B"


class TestReferences:
    def test_an_id_and_an_admin_link_pass(self):
        assert _require_valid_ref("a8hri1z75sqihd0v9vphoisa5")
        assert _require_valid_ref("https://qbg-admin.penpencil.co/whatever")

    def test_anything_else_fails_before_any_browser_starts(self):
        with pytest.raises(QbgBridgeError, match="neither"):
            _require_valid_ref("row 17 of the sheet")
        with pytest.raises(QbgBridgeError, match="neither"):
            _require_valid_ref("")


class TestConnectFailure:
    def test_no_debug_chrome_yields_instructions_not_a_traceback(self, monkeypatch):
        playwright_sync = pytest.importorskip("playwright.sync_api")

        from src.qbg_bridge import browser as bridge_browser

        # An unused local port: connection refused, immediately.
        monkeypatch.setattr(bridge_browser, "QBG_HEADLESS", False)
        monkeypatch.setattr(bridge_browser, "CHROME_CDP_URL", "http://127.0.0.1:59999")

        with playwright_sync.sync_playwright() as playwright:
            with pytest.raises(QbgBridgeError) as caught:
                bridge_browser.connect_browser(playwright)
        message = str(caught.value)
        assert "debug Chrome" in message or "Could not connect" in message
        assert "--remote-debugging-port=9222" in message
