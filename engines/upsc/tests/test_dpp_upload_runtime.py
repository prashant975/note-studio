"""The no-pipeline DPP upload route, driven through the Streamlit page."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

HARNESS = str(Path(__file__).resolve().parent / "app_harness.py")


def _button(at, key: str):
    """By key, not label: DMP and DPP both offer "Read uploaded notes"."""
    matches = [item for item in at.button if item.key == key]
    assert len(matches) == 1, [(item.key, item.label) for item in at.button]
    return matches[0]


def test_uploaded_notes_open_dpp_without_creating_a_notes_run():
    """An upload is a temporary DPP input, not a shortcut into run_pipeline."""
    from src.config import RUNS_DIR

    before = {path.name for path in Path(RUNS_DIR).iterdir()} if Path(RUNS_DIR).exists() else set()
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.run()

    uploader = at.file_uploader(key="dpp_upload_notes")
    uploader.set_value((
        "ecology-revision.txt",
        b"Ecosystem homeostasis relies on producers, consumers and decomposers.",
        "text/plain",
    )).run()
    at.text_input(key="dpp_upload_title").set_value("Ecology revision").run()
    _button(at, "dpp_upload_read").click()
    at.run()
    at.run()

    assert not at.exception, [str(item.value) for item in at.exception]
    source = at.session_state.filtered_state.get("dpp_uploaded_source")
    assert source, "the uploaded notes were not retained for the DPP builder"
    assert source["title"] == "Ecology revision"
    assert source["filename"] == "ecology-revision.txt"
    assert source["source_id"].startswith("dpp_upload_")
    upload_gate = [
        item for item in at.button
        if item.label == "Open the practice builder"
        and item.key == f"dpp_open_{source['source_id']}"
    ]
    assert len(upload_gate) == 1
    after = {path.name for path in Path(RUNS_DIR).iterdir()} if Path(RUNS_DIR).exists() else set()
    assert after == before, "a temporary DPP upload must not create a notes run"
