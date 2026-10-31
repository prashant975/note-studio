"""Gateway allowance failures must remain actionable and never trigger retries."""
import io
import time
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from notesbot.errors import ModelBudgetError
from notesbot.generation_policy import GenerationRun
from notesbot.prompts import Section, ShortNotes
from sheets_bridge import pw_transport as transport


@pytest.mark.parametrize("status", [400, 422, 429])
def test_budget_failure_is_classified_without_exposing_gateway_body(status):
    error = transport._translate_error(Exception(
        f'gemini proxy error {status}: {{"type":"budget_exceeded", "key":"secret-key", "lecture":"private source"}}'
    ))
    assert isinstance(error, ModelBudgetError)
    assert "administrator" in str(error)
    assert "secret-key" not in str(error) and "private source" not in str(error)
    unrelated = transport._translate_error(Exception("gemini proxy error 422: invalid request"))
    assert isinstance(unrelated, transport.ProxyRequestError)


def test_proxy_stops_after_one_budget_rejection(monkeypatch):
    calls = []
    def reject(*args, **kwargs):
        calls.append(kwargs)
        raise Exception("gemini proxy error 422: Budget has been exceeded!")
    monkeypatch.setattr(transport.pw_access, "gemini_generate", reject)
    with pytest.raises(ModelBudgetError):
        transport.ProxyClient(lambda: "token").chat.completions.create(
            messages=[], response_format={"type": "json_schema"})
    assert len(calls) == 1


def test_notes_schema_preserves_budget_failure_and_closes_client(monkeypatch):
    from notesbot import gemini_client as client
    state = {"calls": 0, "closed": 0}
    def reject(**kwargs):
        state["calls"] += 1
        raise ModelBudgetError()
    class Gateway:
        chat = SimpleNamespace(completions=SimpleNamespace(create=reject))
        def with_options(self, **kwargs):
            return self
        def close(self):
            state["closed"] += 1
    monkeypatch.setattr(client, "_use_litellm", lambda: True)
    monkeypatch.setattr(client, "_build_visual_lite_client", Gateway)
    with pytest.raises(ModelBudgetError):
        client._generate_pdf_schema([], "prompt", ShortNotes, 100, 0.1,
            GenerationRun(deadline_at=time.monotonic() + 120, max_images=0))
    assert state == {"calls": 1, "closed": 1}


def test_page_group_does_not_retry_or_split_exhausted_budget(monkeypatch):
    from notesbot import gemini_client
    from sheets_bridge import coverage
    calls = []
    def reject(*args, **kwargs):
        calls.append(args)
        raise ModelBudgetError()
    monkeypatch.setattr(gemini_client, "generate_short_notes", reject)
    monkeypatch.setattr(coverage, "split_pages", lambda *args: pytest.fail("budget failure was split"))
    with pytest.raises(ModelBudgetError):
        coverage._generate_group(SimpleNamespace(pdf_bytes=b"pdf", page_count=8, first_page=1, last_page=8),
            instruction="", language="auto", subject_hint="science", emit=lambda line: None)
    assert len(calls) == 1


def test_source_review_preserves_budget_failure():
    from notesbot.source_coverage import ensure_source_coverage
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    source = io.BytesIO()
    writer.write(source)
    def reject(*args):
        raise ModelBudgetError()
    with pytest.raises(ModelBudgetError):
        ensure_source_coverage(source.getvalue(), ShortNotes(lecture_no="1", subject="Science", title="Topic", sections=[Section(heading="Topic", points=["Content"])]),
                               language="english", generate=reject)
