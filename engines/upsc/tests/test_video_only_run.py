"""A run with no lecture file: notes from the class recording alone.

Many classes simply have no deck. The lecture file used to be mandatory, so
those classes could not be processed at all. What these tests defend is that
making it optional did not quietly weaken the run:

* the recording becomes the ONLY source, so a missing transcript must still stop
  the run rather than produce a document that looks complete;
* everything the file name used to supply — the subject, the output name, the
  lecture title — has to come from somewhere else, and from something true;
* a run that DOES have a lecture file must behave exactly as it did before.
"""
from pathlib import Path
import json
import re
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline import _title_from_notes, run_pipeline
from src.quality_checker import quality_check
from src.upsc_dcn_docx_writer import write_upsc_dcn_docx
from src.video_sources.errors import EmptyTranscript

TRANSCRIPT = (
    "Namaskar students, aaj hum Indian Society ke andar tribal communities, "
    "caste system, communalism aur urbanisation par baat karenge. "
    "Scheduled Tribes ko Article 342 ke through notify kiya jata hai. "
    "Social justice, vulnerable sections, regionalism aur diversity — "
    "ye sab Indian Society ke core themes hain jo mains mein poochhe jate hain. "
) * 40


def _plain(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        doc = z.read("word/document.xml").decode("utf-8", "replace")
    return re.sub(r"<[^>]+>", "", doc).replace("​", "")


def _run(**kw):
    defaults = dict(
        subject="", language="en", mode="complete", allow_mock=True,
        workflow="upsc", note_type="dcn", upsc_output_format="dcn",
        quality_level="off", generate_error_report=False,
        transcript_text_override=TRANSCRIPT,
    )
    defaults.update(kw)
    return run_pipeline(None, **defaults)


# ------------------------------------------------------------- the happy path

def test_a_run_with_no_lecture_file_produces_notes():
    result = _run()
    assert result.docx_path and Path(result.docx_path).exists()
    assert any("built from the class recording alone" in w for w in result.warnings)


def test_supplied_text_skips_video_lookup_and_audio_and_records_its_real_source(monkeypatch):
    def unexpected_acquisition(*args, **kwargs):
        pytest.fail("The supplied transcript must bypass remote lookup and audio transcription")

    monkeypatch.setattr("src.pipeline.acquire_transcript", unexpected_acquisition)
    stages = []
    result = _run(
        video_url="https://drive.google.com/file/d/1T2o_M95n15XjThlI8R_uL_4njKVQ4cfj/view",
        stage_callback=stages.append,
    )
    assert result.docx_path.exists()
    assert result.metadata["transcription_engine"] == "supplied"
    assert result.metadata["transcript_source"] == "supplied by the user"
    assert result.metadata["stt_used"] is False
    assert result.metadata["native_transcript_found"] is False
    record = json.loads((result.run_dir / "transcript_segments.json").read_text(encoding="utf-8"))
    assert record["full_text"] == TRANSCRIPT.strip()
    assert "Using supplied transcript" in stages
    assert "Extracting audio" not in stages


def test_incomplete_supplied_text_is_rejected_without_starting_audio(monkeypatch):
    monkeypatch.setattr("src.pipeline.acquire_transcript", lambda *a, **k: pytest.fail("audio started"))
    with pytest.raises(EmptyTranscript):
        _run(video_url="https://drive.google.com/file/d/test-lecture-file/view",
             transcript_text_override="only a few words")


def test_the_subject_is_detected_from_the_recording():
    """With no slides and no file name there is nothing to detect from until the
    transcript arrives, so detection waits for it rather than falling back to
    the default subject — which would print the wrong one on the cover."""
    result = _run()
    assert any("auto-detected from the recording" in w for w in result.warnings), result.warnings
    assert result.metadata.get("subject_line") == "Indian Society"


def test_the_outputs_are_named_from_the_title_not_from_a_placeholder():
    """There is no file name to build an output stem from, and
    "lecture_notes.docx" in a downloads folder is indistinguishable from every
    other one."""
    result = _run()
    stem = Path(result.docx_path).stem
    assert stem != "file_upsc_notes"
    assert stem != "lecture_notes_upsc_notes", "fell through to the last-resort name"


def test_a_typed_lecture_number_survives_a_run_with_no_file():
    """The number came from the user, the title from the model. Neither source
    knows about the other, so the writer has to be told the number separately —
    which is the bug this covers."""
    result = _run(lecture_no="7", lecture_title="Tribal Communities of India")
    assert "Lecture 7" in _plain(Path(result.docx_path))


# ------------------------------------------------------------- what must fail

def test_a_video_that_yields_no_transcript_is_refused(monkeypatch):
    """The recording is the only source. A run that cannot transcribe it must
    stop rather than ship a document that looks complete and says nothing the
    faculty taught — and `require_transcript=False` must not be able to weaken
    that, because with no slides there is no second source to fall back to.
    """
    from src.models import NormalizedTranscript

    monkeypatch.setattr("src.pipeline.acquire_transcript",
                        lambda *a, **k: NormalizedTranscript())
    with pytest.raises(EmptyTranscript):
        run_pipeline(None, subject="", language="en", mode="complete", allow_mock=True,
                     workflow="upsc", note_type="dcn", upsc_output_format="dcn",
                     quality_level="off", generate_error_report=False,
                     video_url="https://www.youtube.com/watch?v=abcdefghijk",
                     require_transcript=False)


def test_nothing_at_all_is_refused_before_any_work_is_done():
    """No file, no link, no transcript. Caught at the top of the run rather than
    after transcription has been attempted against nothing."""
    with pytest.raises(ValueError, match="Nothing to build notes from"):
        run_pipeline(None, subject="", language="en", mode="complete", allow_mock=True,
                     workflow="upsc", note_type="dcn", upsc_output_format="dcn",
                     quality_level="off", generate_error_report=False)


# ------------------------------------------------- the pieces, without a run

def test_the_slide_text_warning_does_not_fire_without_slides():
    """"Enable 'Send slide images to AI'" sends someone who uploaded no slides
    looking for a setting that would change nothing."""
    warnings = quality_check("#H1: X\n- a point.", [], None, None, send_images_to_ai=False)
    assert not any("No extractable text" in w for w in warnings), warnings


def test_the_title_is_read_back_out_of_the_notes():
    notes = "#LECTURE_TITLE: L03_DCN03_Tribal Communities\n\n#H1: X\n- a point.\n"
    assert _title_from_notes(notes) == "Tribal Communities"
    assert _title_from_notes("#H1: no title marker here") == ""
    assert _title_from_notes("") == ""


def test_the_writer_supplies_a_lecture_number_the_title_lacks(tmp_path):
    """Only the NUMBER is taken from the caller, and only when the title has
    none of its own — an absent number is still omitted, never invented."""
    bare = "#LECTURE_TITLE: Capital Market Basics\n\n#H1: X\n\n- a point.\n"
    numbered = "#LECTURE_TITLE: L03_DCN03_Capital Market Basics\n\n#H1: X\n\n- a point.\n"

    out, _ = write_upsc_dcn_docx(bare, tmp_path / "a.docx", language_code="en")
    assert not re.search(r"Lecture\s+\d", _plain(out)), "a number was invented"

    out, _ = write_upsc_dcn_docx(bare, tmp_path / "b.docx", language_code="en", lecture_no="7")
    assert "Lecture 7" in _plain(out)

    out, _ = write_upsc_dcn_docx(numbered, tmp_path / "c.docx", language_code="en", lecture_no="7")
    assert "Lecture 3" in _plain(out), "the title's own number must win"

    out, _ = write_upsc_dcn_docx("#LECTURE_TITLE: पूंजी बाज़ार\n\n#H1: क\n\n- बिंदु।\n",
                                 tmp_path / "d.docx", language_code="hi", lecture_no="7")
    assert "लेक्चर-7:" in _plain(out)
