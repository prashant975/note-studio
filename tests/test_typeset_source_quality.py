"""Every-page final review and an exact-file cache, using the NEET/JEE schema."""
import copy

import fitz

from note_studio.typeset import render, source, source_quality


def document(tmp_path):
    with fitz.open() as pdf:
        p = pdf.new_page()
        p.insert_text((40,60), "Original source wording and 12 kg must remain.")
        data = pdf.tobytes()
    doc, manifest = source.extract(data, tmp_path)
    rendered, _ = render.render_pdf(doc, render.Branding(), tmp_path)
    return doc, manifest, rendered


class Session:
    pages = 1
    def __init__(self, response):
        self.response, self.calls = response, []
    def json(self, kind, models, system, parts, schema, **kwargs):
        self.calls.append(kind)
        assert "SOURCE ITEMS" in parts[1]["text"]
        return copy.deepcopy(self.response)


def test_review_covers_every_page_and_only_reuses_the_exact_pdf(tmp_path):
    doc, manifest, pdf = document(tmp_path)
    session = Session({"reviewed_pages": [1], "unreadable_pages": [], "issues": [], "edits": []})
    report = source_quality.review(pdf, doc, manifest, tmp_path, lambda: session, [], lambda *args: None)
    assert report["verdict"] == "passed" and report["reviewed_pages"] == [1]
    assert "chemistry" in report["checks"] and "figures_and_labels" in report["checks"]
    source_quality.review(pdf, doc, manifest, tmp_path, lambda: session, [], lambda *args: None)
    assert session.calls == ["final quality"]
    source_quality.review(pdf+b"\n% changed metadata", doc, manifest, tmp_path, lambda: session, [], lambda *args: None)
    assert session.calls == ["final quality", "final quality"]
    # An unchanged rerender has no clock field to invalidate its paid review.
    again, _ = render.render_pdf(doc, render.Branding(), tmp_path)
    assert again == pdf


def test_incomplete_review_never_passes_and_can_be_retried_without_new_content(tmp_path):
    doc, manifest, pdf = document(tmp_path)
    session = Session({"reviewed_pages": [], "unreadable_pages": [], "issues": [], "edits": []})
    result = source_quality.review(pdf, doc, manifest, tmp_path, lambda: session, [], lambda *args: None)
    assert result["verdict"] == "unreviewed" and result["reviewed_pages"] == []
    session.response["reviewed_pages"] = [1]
    result = source_quality.review(pdf, doc, manifest, tmp_path, lambda: session, [], lambda *args: None)
    assert result["verdict"] == "passed" and len(session.calls) == 2


def test_scientific_or_language_suggestions_are_reported_without_rewriting_source(tmp_path):
    doc, manifest, pdf = document(tmp_path)
    original = copy.deepcopy(doc)
    session = Session({"reviewed_pages": [1], "unreadable_pages": [], "issues": [
        {"page": 1, "kind": "clarity", "detail": "An original-source claim needs a human review."}],
        "edits": [{"path": "/blocks/0/text", "before": "Original", "after": "Different"}]})
    result = source_quality.review(pdf, doc, manifest, tmp_path, lambda: session, [], lambda *args: None)
    assert result["verdict"] == "needs_attention" and result["advisory_issues"] and result["unapplied_suggestions"]
    assert doc == original
