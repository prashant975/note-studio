"""Two ways a run loses rows it had already paid for, and the fixes.

Reported from a real run: "the AI's reply for part 17 of 21 hit the output
limit and was cut off ... any error the model had not yet written is missing".

1. A cut-off window is now RE-ASKED one page at a time. The output cap is per
   call, so halving what a call must cover doubles the room its answer has —
   the same reason a small window finds more than a large one. Raising the cap
   only moves the ceiling; this removes it for that window.
2. Every reply is on disk as it arrives, so a run that fails half way can be
   rebuilt from the folder with no further calls at all.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qc import engine  # noqa: E402
from src.qc.engine import _Chunk, _split_chunk  # noqa: E402


def rows_json(*statements):
    return json.dumps([
        {"original_statement": s, "error_type": "Factual", "severity": "High",
         "error_concern": f"{s} is wrong.", "correct_information_correction": "fix",
         "source_urls": []} for s in statements])


def marked(body):
    return f"# ERROR_REPORT_START\n{body}\n# ERROR_REPORT_END"


@dataclass
class _P:
    page_no: int
    text: str
    image_path: object = None
    spans: list = field(default_factory=list)


@dataclass
class _Doc:
    pages: list
    language_code: str = "en"

    @property
    def paged_text(self):
        return "\n\n".join(f"[Page {p.page_no}]\n{p.text}" for p in self.pages)


# ------------------------------------------------------------ the splitter

def test_a_two_page_window_splits_into_one_chunk_per_page():
    chunk = _Chunk(text="[Page 4]\nfirst page words\n\n[Page 5]\nsecond page words",
                   page_range="4-5")
    parts = _split_chunk(chunk)
    assert [p.page_range for p in parts] == ["4", "5"]
    assert "first page words" in parts[0].text
    assert "second page words" in parts[1].text
    assert "[Page 5]" not in parts[0].text, "a page must not carry its neighbour"


def test_a_single_page_window_cannot_be_split_further():
    assert _split_chunk(_Chunk(text="[Page 7]\nonly one page", page_range="7")) == []
    assert _split_chunk(_Chunk(text="", page_range="")) == []
    assert _split_chunk(_Chunk(page_range="1-6", images=[Path("a.png")])) == []


# ---------------------------------------------------- the recovery itself

def test_a_cut_off_window_is_re_asked_one_page_at_a_time(tmp_path):
    """The whole-window reply is truncated; the per-page replies are not, and
    they carry rows the cut-off one never reached."""
    doc = _Doc([_P(1, "alpha " * 30), _P(2, "beta " * 30)])
    asked = []

    class _Client:
        def generate(self, prompt, images=None, **kw):
            asked.append(prompt)
            whole = "[Page 1]" in prompt and "[Page 2]" in prompt

            class R:
                # The two-page ask is cut off after one row; each single-page
                # ask answers in full.
                truncated = whole
                text = marked(rows_json("cut off row")) if whole else marked(
                    rows_json("page one row" if "[Page 1]" in prompt else "page two row"))
            return R()

    report = engine._run_error_report_pass(
        _Client(), doc, tmp_path, paged_text=doc.paged_text,
        text_is_readable=True, file_name="x.pdf", lecture_title="",
        subject="", strictness="strict")

    assert report.truncated_parts == [], "a recovered window is no longer truncated"
    assert report.complete, "the run must not report incomplete coverage any more"
    merged = " ".join(report.blocks)
    assert "page one row" in merged and "page two row" in merged
    assert any("[Page 1]" in p and "[Page 2]" not in p for p in asked), (
        "each page must be asked on its own")


def test_a_window_still_cut_off_page_by_page_is_reported_honestly(tmp_path):
    """When even one page overruns, nothing was gained and the user is told."""
    doc = _Doc([_P(1, "alpha " * 30), _P(2, "beta " * 30)])

    class _Client:
        def generate(self, prompt, images=None, **kw):
            class R:
                truncated = True
                text = marked(rows_json("partial row"))
            return R()

    report = engine._run_error_report_pass(
        _Client(), doc, tmp_path, paged_text=doc.paged_text,
        text_is_readable=True, file_name="x.pdf", lecture_title="",
        subject="", strictness="strict")
    assert report.truncated_parts, "an unrecoverable cut-off must still be reported"
    assert not report.complete
    assert any("output limit" in w for w in report.coverage_warnings())


# ------------------------------------------------ rebuilding from the disk

def test_a_report_is_rebuilt_from_the_saved_replies_with_no_ai_calls(tmp_path):
    from src.rebuild_report import rebuild_result, rebuild_run

    folder = tmp_path / "trail"
    folder.mkdir()
    (folder / "error_report_response_01.txt").write_text(
        marked(rows_json("The Taj Mahal stands on the Tons River")), encoding="utf-8")
    (folder / "error_report_response_02.txt").write_text(
        marked(rows_json("Prambanan is in Malaysia")), encoding="utf-8")
    (folder / "qc_result.json").write_text(
        json.dumps({"file_name": "trail.pdf", "page_count": 19}), encoding="utf-8")

    result = rebuild_result(folder)
    assert result is not None
    assert result.file_name == "trail.pdf" and result.page_count == 19
    quoted = " ".join(r["original_statement"] for r in result.error_rows)
    assert "Tons River" in quoted and "Malaysia" in quoted
    assert any("REBUILT" in w for w in result.warnings)

    assert [r.file_name for r in rebuild_run(tmp_path)] == ["trail.pdf"]


def test_a_part_that_answered_with_nothing_is_named_in_the_rebuild(tmp_path):
    from src.rebuild_report import rebuild_result

    folder = tmp_path / "doc"
    folder.mkdir()
    (folder / "error_report_response_01.txt").write_text(
        marked(rows_json("a real row")), encoding="utf-8")
    (folder / "error_report_response_02.txt").write_text(
        "I could not find any errors, sorry.", encoding="utf-8")

    result = rebuild_result(folder)
    assert result is not None and len(result.error_rows) == 1
    assert result.error_report_complete is False
    assert any("part(s) 2" in w for w in result.warnings)


def test_the_rebuild_falls_back_to_the_merged_array(tmp_path):
    """The per-part files can be gone; error_report_raw.json is still there."""
    from src.rebuild_report import rebuild_result

    folder = tmp_path / "doc"
    folder.mkdir()
    (folder / "error_report_raw.json").write_text(
        rows_json("Mount Etna damaged the complex"), encoding="utf-8")
    result = rebuild_result(folder)
    assert result is not None
    assert "Mount Etna" in result.error_rows[0]["original_statement"]


def test_an_empty_folder_rebuilds_nothing_rather_than_pretending(tmp_path):
    from src.rebuild_report import rebuild_result, rebuild_run

    (tmp_path / "empty").mkdir()
    assert rebuild_result(tmp_path / "empty") is None
    assert rebuild_run(tmp_path) == []
