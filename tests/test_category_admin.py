"""Phase 4 check 2: a new category added from Settings only (no code, no files
by hand) is offered on New notes, the engine follows its rule, and editing,
checking and switching it off all work from the page. Stubbed model: no cost."""
from __future__ import annotations

import json
from pathlib import Path

from note_studio import categories, db, job_queue

RULE = "Underline every section of the Act the teacher names."
FORM = {"label": "Test CLAT", "subjects": "Legal Reasoning\nLogical Reasoning\n", "summary": "Law entrance",
        "rules": RULE, "languages": ["auto", "english", "hindi"],            # the form's ticks
        "note_types": ["concise_handdrawn"]}


def _add(client, **changes):
    return client.post("/settings/categories/add", data={**FORM, **changes}, follow_redirects=False)


def test_a_category_added_from_settings_makes_notes_that_follow_its_rule(client, fake_model, lecture_pdf):
    response = _add(client)
    assert response.status_code == 303 and "kind=ok" in response.headers["location"]
    pack = categories.get("test_clat")
    assert pack["label"] == "Test CLAT" and pack["enabled"] is True and pack["builtin"] is False
    assert [s["id"] for s in pack["subjects"]] == ["legal_reasoning", "logical_reasoning"]

    findings = client.get("/settings/categories/test_clat/check").text
    assert "The hand-drawn notes engine sees this category." in findings and "Ready to make" in findings
    preview = client.get("/settings/categories/preview", params={"category": "test_clat",
                                                                 "note_type": "concise_handdrawn"}).text
    assert RULE in preview

    page = client.get("/new?category=test_clat").text
    assert "Test CLAT" in page and "Legal Reasoning" in page
    assert "NEET-JEE, Foundation, Test CLAT" in client.get("/settings/system").text     # who uses the engine

    fake_model.KEEP_REQUESTS = True
    files = [("files", ("Contracts 01.pdf", lecture_pdf, "application/pdf"))]
    made = client.post("/new", data={"category": "test_clat", "note_type": "concise_handdrawn", "subject": "auto",
                                     "language": "auto", "teacher_name": "Test Sir", "logo_watermark": "on"},
                       files=files, follow_redirects=False)
    assert made.status_code == 303, made.text
    assert job_queue.wait_idle(120)
    run = db.query("SELECT * FROM runs WHERE source='upload'")[0]
    assert run["state"] == "complete", run["error"]
    assert run["category"] == "test_clat"
    sent = json.dumps(fake_model.REQUESTS[0], ensure_ascii=False)
    assert "CATEGORY: Test CLAT notes." in sent and RULE in sent


def test_editing_a_rule_keeps_the_old_text_as_a_backup(client):
    _add(client)
    folder = Path(categories.get("test_clat")["folder"]) / "rules"
    before = (folder / "all-subjects.txt").read_text(encoding="utf-8")
    response = client.post("/settings/categories/test_clat/rule",
                           data={"name": "all-subjects", "text": "Keep every case name exactly."}, follow_redirects=False)
    assert "kind=ok" in response.headers["location"]
    assert (folder / "all-subjects.txt").read_text(encoding="utf-8") == "Keep every case name exactly.\n"
    assert (folder / "all-subjects.txt.bak").read_text(encoding="utf-8") == before
    outside = client.post("/settings/categories/test_clat/rule", data={"name": "../../neet_jee/rules/x", "text": "x"},
                          follow_redirects=False)
    assert "kind=warn" in outside.headers["location"]
    assert not (folder.parent.parent / "neet_jee" / "rules" / "x.txt").exists()


def test_switching_a_category_off_takes_it_off_new_notes(client):
    _add(client)
    assert "Test CLAT" in client.get("/new").text
    client.post("/settings/categories/test_clat/enabled", data={"on": "0"})
    assert "Test CLAT" not in client.get("/new").text
    assert "Switched off" in client.get("/settings/categories/test_clat/check").text
    assert (Path(categories.get("test_clat")["folder"]) / "category.json.bak").is_file()
    client.post("/settings/categories/test_clat/enabled", data={"on": "1"})
    assert "Test CLAT" in client.get("/new").text


def test_the_add_form_says_what_is_missing(client):
    for changes, words in (({"label": ""}, "Give the category a name"),
                           ({"languages": []}, "Choose at least one language"),
                           ({"note_types": []}, "Choose at least one note type"),
                           ({"label": "!!!"}, "starts with a letter")):
        location = _add(client, **changes).headers["location"]
        assert "kind=warn" in location and location.endswith("#add"), changes      # the toast is not lost
        assert words.replace(" ", "%20") in location, changes
    assert categories.get("test_clat") is None


def test_the_same_name_twice_gets_its_own_folder(client):
    _add(client)
    _add(client)
    assert categories.get("test_clat") and categories.get("test_clat_2")
