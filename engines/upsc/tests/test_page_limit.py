"""The 15-page limit, enforced against the RENDERED document.

The word budget elsewhere is a prediction. This is the part that uses the real
number: render the DOCX, export the PDF, count the pages actually produced,
and hand the notes back to the model if there are too many.

These tests drive the whole loop through ``run_pipeline`` with a fake client, so
what is being checked is the behaviour a user gets — a document at or under the
limit, intermediate drafts kept, and nothing silently deleted — rather than the
shape of the code that produces it.

They need the bundled DOCX->PDF renderer and skip without
one, because with no renderer there is no page count to act on.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import pipeline
from src.config import DCN_MAX_COMPRESSION_PASSES, DCN_TARGET_PAGES
from src.note_budget import build_page_compress_prompt, count_words, page_limit

TRANSCRIPT = (
    "Aaj hum Indian Society ke andar tribal communities, caste system, "
    "communalism aur urbanisation par baat karenge. Scheduled Tribes ko "
    "Article 342 ke through notify kiya jata hai. "
) * 40


def _notes(sections: int, bullets_per_section: int = 12) -> str:
    """Marker-format notes of a controllable length."""
    out = ["#LECTURE_TITLE: L04_DCN04_Indian Society", ""]
    for s in range(sections):
        out += [f"#H1: Major Theme {s + 1}", "", f"#H2: Sub-theme {s + 1}", ""]
        for b in range(bullets_per_section):
            out.append(
                f"- **Point {s + 1}.{b + 1}:** The Environment (Protection) Act, 1986 "
                f"and Article 342 are examined together here, with the committee "
                f"report and the scheme data that the faculty walked through in "
                f"detail during this stretch of the class, restated at length.")
        out.append("")
    return "\n".join(out)


class _Resp:
    def __init__(self, text):
        self.text = text
        self.model = "fake-model"
        self.provider = "fake"
        self.usage = {}


class _Client:
    """Writes the notes, then returns progressively shorter versions of them.

    One fake serves both calls the loop makes: the first generation, and each
    compression. They are told apart by the prompt, because that is the only
    thing that distinguishes them at this seam.
    """

    def __init__(self, first_draft, compressions=()):
        self.first_draft = first_draft
        self.drafts = list(compressions)
        self.compress_prompts = []
        self.tighten_prompts = []
        #: The real client collects these when a model falls back; the pipeline
        #: reads them at the end of every run.
        self.fallback_notices = []

    def generate(self, prompt, image_paths=None, **kw):
        if "NOTES TO COMPRESS" in prompt:
            self.compress_prompts.append(prompt)
            return _Resp(self.drafts.pop(0) if self.drafts else "")
        if "NOTES TO TIGHTEN" in prompt:
            # The word-budget pass, which runs before anything is rendered.
            # Declining it leaves the page loop the thing under test.
            self.tighten_prompts.append(prompt)
            return _Resp("")
        return _Resp(self.first_draft)


def _renderer_available() -> bool:
    from src.pdf_exporter import export_docx_to_pdf
    from src.upsc_dcn_docx_writer import write_upsc_dcn_docx
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        docx, _ = write_upsc_dcn_docx("#LECTURE_TITLE: T\n\n#H1: X\n\n- a point.\n",
                                      tmp / "probe.docx", language_code="en")
        pdf, _ = export_docx_to_pdf(docx, tmp)
        return bool(pdf)


#: These tests MEASURE a real render — the page loop counts pages in the PDF
#: the bundled renderer produced, so a stubbed converter would leave every one of
#: them passing without exercising the loop at all. `real_pdf` opts them out of
#: the suite-wide COM stub in conftest; the skipif keeps them off machines with
#: no converter installed.
needs_renderer = pytest.mark.skipif(
    not _renderer_available(), reason="no DOCX->PDF converter on this machine")


def _with_real_pdf(mark):
    def decorate(fn):
        return mark(pytest.mark.real_pdf(fn))
    return decorate


needs_renderer = _with_real_pdf(needs_renderer)


def _run(monkeypatch, *, client, **kw):
    """A DCN run driven by `client`, going through the real page loop.

    allow_mock=False and a token, because mock mode leaves ``client`` as None
    and the compression loop has nothing to call — which is correct behaviour
    and would make every test here silently pass without exercising anything.
    """
    monkeypatch.setattr(pipeline, "GeminiClient", lambda *a, **k: client)
    monkeypatch.setattr(pipeline.pw_access, "check_allowed", lambda *a, **k: True)
    defaults = dict(
        subject="society", language="en", mode="complete", allow_mock=False,
        google_token="test-token",
        workflow="upsc", note_type="dcn", upsc_output_format="dcn",
        quality_level="off", generate_error_report=False,
        transcript_text_override=TRANSCRIPT,
    )
    defaults.update(kw)
    return pipeline.run_pipeline(None, **defaults)


# --------------------------------------------------------------- the prompt

def test_the_compression_prompt_quotes_the_real_page_count():
    """"Make it shorter" trims a token amount off a document four pages over.
    The measured number and an explicit proportion are what make it converge."""
    prompt = build_page_compress_prompt("- a point", language_code="en",
                                        actual_pages=21, attempt=1)
    assert "21 pages" in prompt
    assert f"limit is {page_limit()} pages" in prompt
    assert "% shorter" in prompt


def test_the_compression_prompt_protects_everything_valuable():
    prompt = build_page_compress_prompt("- a point", language_code="en",
                                        actual_pages=18, attempt=1)
    for protected in ("definition", "Article", "Act", "scheme", "committee",
                      "case stud", "comparison", "table", "#FIGURE", "heading"):
        assert protected.lower() in prompt.lower(), protected
    # Deleting a whole section is the cheap way to lose pages and the one way
    # the reader cannot tell anything is missing.
    assert "Do NOT drop a section" in prompt


def test_the_final_attempt_says_so():
    """The model behaves differently when it knows there is no next round."""
    last = build_page_compress_prompt("- x", actual_pages=20,
                                      attempt=DCN_MAX_COMPRESSION_PASSES)
    assert "FINAL attempt" in last
    assert "FINAL attempt" not in build_page_compress_prompt(
        "- x", actual_pages=20, attempt=1)


# ------------------------------------------------------------- the real loop

@needs_renderer
def test_a_long_note_is_compressed_until_it_fits(monkeypatch, tmp_path):
    """The headline behaviour: over the limit in, at or under the limit out."""
    # The compressed draft must stay above MIN_RETAINED_FRACTION of the
    # original or the safety guard rejects it as a summary — which is the
    # behaviour the summary test below covers.
    client = _Client(_notes(16), [_notes(10)])
    result = _run(monkeypatch, client=client)

    import fitz

    with fitz.open(result.pdf_path) as doc:
        assert doc.page_count <= page_limit(), f"{doc.page_count} pages"
    assert client.compress_prompts, "the loop never asked for a compression"

    meta = result.metadata["note_length"]
    assert meta["page_attempts"][0]["pages"] > page_limit(), "test note was not long"
    assert meta["final_pages"] <= page_limit()
    assert meta["page_limit"] == page_limit()


@needs_renderer
def test_the_uncompressed_draft_is_kept(monkeypatch, tmp_path):
    """Compression is a model call and models are wrong sometimes. The reviewer
    has to be able to see what was cut."""
    client = _Client(_notes(16), [_notes(10)])
    result = _run(monkeypatch, client=client)
    assert (result.run_dir / "notes_before_page_compress_1.md").exists()
    assert (result.run_dir / "notes_page_compressed_1.md").exists()


@needs_renderer
def test_the_loop_is_bounded(monkeypatch, tmp_path):
    """A model that will not shorten the notes must not spin the run forever."""
    stubborn = _notes(16)
    client = _Client(stubborn, [stubborn[:-200], stubborn[:-400], stubborn[:-600],
                                stubborn[:-800], stubborn[:-1000]])
    result = _run(monkeypatch, client=client)
    assert len(client.compress_prompts) <= DCN_MAX_COMPRESSION_PASSES
    # And it says so rather than pretending it succeeded.
    assert any("above the" in w and "page limit" in w for w in result.warnings), \
        result.warnings


@needs_renderer
def test_a_summary_is_refused_and_the_full_document_ships(monkeypatch, tmp_path):
    """The dangerous failure. A model that reads "compress" as "summarise"
    returns a fraction of the notes; swapping that in loses the facts the
    document exists for, and the run still looks successful."""
    client = _Client(_notes(16),
                     ["#LECTURE_TITLE: T\n\n#H1: X\n\n- Everything, briefly.\n"])
    result = _run(monkeypatch, client=client)

    import fitz

    with fitz.open(result.pdf_path) as doc:
        assert doc.page_count > page_limit(), "the summary was accepted"
    assert any("not a safe shortening" in w for w in result.warnings), result.warnings


@needs_renderer
def test_a_short_note_is_left_completely_alone(monkeypatch, tmp_path):
    """10 pages is a target, not a floor. Nothing is added to reach it, and a
    short class costs no compression call at all."""
    client = _Client(_notes(2, bullets_per_section=4))
    result = _run(monkeypatch, client=client)

    import fitz

    with fitz.open(result.pdf_path) as doc:
        pages = doc.page_count
    assert pages < DCN_TARGET_PAGES[0]
    assert not client.compress_prompts, "a short note was sent for compression"
    assert result.metadata["note_length"]["final_pages"] == pages


@needs_renderer
def test_compression_keeps_the_document_renderable_and_titled(monkeypatch, tmp_path):
    """A compressed document is still a DCN: title block, headings, no marker
    text leaking onto the page."""
    client = _Client(_notes(16), [_notes(10)])
    result = _run(monkeypatch, client=client)

    import fitz

    with fitz.open(result.pdf_path) as doc:
        text = "\n".join(page.get_text() for page in doc)
    assert "Indian Society" in text
    assert "#H1:" not in text and "#LECTURE_TITLE" not in text
    assert "Major Theme 1" in text
