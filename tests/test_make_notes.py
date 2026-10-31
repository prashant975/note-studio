"""New notes end to end, through the pages and the Smart Queue, with the stubbed
model (no network, no cost): one queue item per PDF, its run, the download,
Runs and its filters, plain-words form errors, Remove, and holding after a
restart or a lost sign-in."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from note_studio import db, job_queue, make_service

FORM ={"category": "neet_jee", "note_type": "concise_handdrawn", "subject": "auto", "language": "auto",
        "teacher_name": "Saleem Ahmad Sir", "logo_watermark": "on"}


def _post(client, pdfs, **changes):
    files = [("files", (name, data, "application/pdf")) for name, data in pdfs]
    return client.post("/new", data={**FORM, **changes}, files=files, follow_redirects=False)


def _runs():
    return db.query("SELECT * FROM runs WHERE source='upload' ORDER BY id")


def test_the_form_offers_the_old_upload_pages_choices(client):
    page = client.get("/new").text
    for words in ("NEET-JEE", "Foundation", "Concise hand-drawn notes", "Auto Detect", "Teacher name",
                  "PW logo watermark", "Make notes"):
        assert words in page, words
    assert "UPSC (OnlyIAS)" in page                     # switched on in Phase 5
    foundation = client.get("/new?category=foundation").text
    assert "Social Science (SST)" in foundation and "mathematics.txt" not in foundation
    rules = client.get("/new/rules?category=foundation&note_type=concise_handdrawn&subject=mathematics").text
    assert "categories/foundation/rules/mathematics.txt" in rules


def test_one_pdf_becomes_finished_notes_to_download(client, fake_model, lecture_pdf):
    response = _post(client, [("Lecture 01.pdf", lecture_pdf)])
    assert response.status_code == 303 and response.headers["location"].startswith("/queue")
    assert job_queue.wait_idle(120)
    run = _runs()[0]
    assert run["state"] == "complete", run["error"]
    assert run["cost_inr"] == pytest.approx(1.25) and run["pct"] == 100
    assert len(fake_model.CALLS) == 1 and fake_model.CALLS[0]["kind"] == "notes"
    assert fake_model.CALLS[0]["model"] == "gemini-3.7-flash"          # the first in Settings -> AI models
    entry = db.query("SELECT * FROM job_queue")[0]
    assert entry["state"] == "done" and entry["run_id"] == run["id"]
    assert not (Path(os.environ["NOTE_STUDIO_HOME"]) / "queue" / str(entry["id"])).exists()   # nothing left behind

    page = client.get(f"/runs/{run['id']}").text
    assert "Done" in page and "Lecture 01 Concise Notes.pdf" in page and "by Saleem Ahmad Sir" in page
    import re

    link = re.search(rf'href="(/runs/{run["id"]}/files/\d+)"', page).group(1)
    download = client.get(link)
    assert download.status_code == 200 and download.content.startswith(b"%PDF")
    assert "Lecture%2001%20Concise%20Notes.pdf" in download.headers["content-disposition"]
    assert "hx-trigger" not in client.get(f"/runs/{run['id']}/card").text      # finished: no more polling
    assert f"/runs/{run['id']}" in client.get("/runs?state=done").text
    assert f"/runs/{run['id']}" not in client.get("/runs?state=attention").text
    assert f"/runs/{run['id']}" in client.get("/runs?q=Lecture").text


@pytest.mark.parametrize("line, words", [
    ("lecture: notes call - INR 4.20", "Notes done (₹4.20)"),
    ("Physics 02: source review call - INR 1.5", "Source review done (₹1.50)"),
    ("lecture: notes call started", "Asking PW AI: notes"),
    ("lecture: notes call budget spent on gemini-3.7-flash; using gpt-5.6-terra-ard-content",
     "That AI model's budget is spent; switched to gpt-5.6-terra-ard-content"),
    ("Writing the notes", "Writing the notes"),
])
def test_the_engines_call_lines_read_as_plain_words(line, words):
    assert make_service.plain_step(line) == words


def test_each_pdf_is_its_own_run_in_order(client, fake_model, lecture_pdf):
    response = _post(client, [("A.pdf", lecture_pdf), ("B.pdf", lecture_pdf)])
    assert response.status_code == 303
    assert job_queue.wait_idle(180)
    assert [(r["title"], r["state"]) for r in _runs()] == [("A", "complete"), ("B", "complete")]


def test_the_same_request_twice_is_added_once(client, lecture_pdf):
    job_queue.hold("paused")                                   # nothing starts
    _post(client, [("L.pdf", lecture_pdf)])
    second = _post(client, [("L.pdf", lecture_pdf)])
    assert "not%20added%20twice" in second.headers["location"]
    assert len(db.query("SELECT id FROM job_queue")) == 1


@pytest.mark.parametrize("changes, words", [
    ({"teacher_name": ""}, "Fill in teacher name"),
    ({"note_type": "upsc_dcn"}, "Choose a note type this category offers"),
    ({"category": "upsc"}, "Choose a note type this category offers"),
    ({"category": "no_such_exam"}, "Choose a category"),
    ({"subject": "astrology"}, "Choose a subject"),
    ({"language": "marathi"}, "Choose a language"),
])
def test_form_problems_are_said_in_plain_words(client, lecture_pdf, changes, words):
    response = _post(client, [("L.pdf", lecture_pdf)], **changes)
    assert response.status_code == 400 and words in response.text and "Traceback" not in response.text
    assert db.query("SELECT COUNT(*) AS n FROM job_queue")[0]["n"] == 0


def test_a_file_that_is_not_a_pdf_is_refused(client):
    response = _post(client, [("notes.pdf", b"PK\x03\x04 a zip")])
    assert response.status_code == 400 and "is not a PDF file" in response.text


def test_without_sign_in_the_run_says_what_to_do_and_the_queue_holds(client, lecture_pdf):
    """No fake: the real sign-in check, which needs the Settings choice first."""
    _post(client, [("L.pdf", lecture_pdf), ("M.pdf", lecture_pdf)])
    assert job_queue.wait_idle(60)
    runs = _runs()
    assert len(runs) == 1                                       # the second never started
    assert runs[0]["state"] == "failed" and "Settings" in runs[0]["error"] and runs[0]["cost_inr"] == 0
    assert job_queue.held() == "signin"
    page = client.get(f"/runs/{runs[0]['id']}").text
    assert "Open Sign-in" in page and "Try again" in page and "Traceback" not in page
    assert "sign-in is missing" in client.get("/queue").text


def test_a_waiting_lecture_can_be_removed(client, lecture_pdf):
    job_queue.hold("paused")
    added = make_service.submit("neet_jee", "concise_handdrawn", "auto", "auto",
                                {"teacher_name": "Sir", "logo_watermark": True}, [("L.pdf", lecture_pdf)])
    entry_id = added[0][0]
    assert "Remove" in client.get("/queue").text
    client.post(f"/queue/{entry_id}/remove")
    assert job_queue.get(entry_id)["state"] == "removed"
    assert not (Path(os.environ["NOTE_STUDIO_HOME"]) / "queue" / str(entry_id)).exists()
    assert _runs() == []                                        # nothing was made for it


def test_after_a_restart_the_queue_holds_until_continue(client, fake_model, lecture_pdf):
    job_queue.hold("paused")
    make_service.submit("neet_jee", "concise_handdrawn", "auto", "auto",
                        {"teacher_name": "Sir", "logo_watermark": True}, [("L.pdf", lecture_pdf)])
    # As if NoteCraft had just been closed and opened: what start-up does. The
    # "paused" hold stays until then, so the pump cannot start the item first.
    db.recover()
    assert job_queue.held() == "restart"
    assert "Smart Queue is holding" in client.get("/").text and "Continue the queue" in client.get("/queue").text
    client.post("/queue/continue")
    assert job_queue.wait_idle(120)
    assert _runs()[0]["state"] == "complete"


def test_make_again_adds_the_same_lecture_to_the_queue(client, fake_model, lecture_pdf):
    _post(client, [("L.pdf", lecture_pdf)])
    assert job_queue.wait_idle(120)
    first = _runs()[0]
    response = client.post(f"/runs/{first['id']}/again", follow_redirects=False)
    assert response.headers["location"].startswith("/queue")
    assert job_queue.wait_idle(120)
    second = _runs()[1]
    assert second["state"] == "complete" and second["title"] == first["title"]
