"""Phase 6: Practice (Mains practice and DPP) from finished UPSC notes.

Each chain starts from a real mock class (no sign-in, no network, no cost).
Mains practice uses a canned-reply client (no cost); DPP uses the real question
bank in the UPSC engine copy's data folder (no AI at all). The lecture's own
folder, ours or the old app's, must be byte-for-byte unchanged afterwards."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

import pytest

from note_studio import db, engine_catalog, job_queue, make_service, paths, practice_service
from note_studio.engines import upsc as upsc_engine
from tests.test_upsc_engine import CLASS, needs_engine, seed_transcript

BANK = engine_catalog.engine_dir("upsc") / "data"
needs_bank = pytest.mark.skipif(not all((BANK / name).is_file() for name in practice_service.MASTER_FILES.values()),
                                reason="the DPP workbooks are not in the engine copy's data folder")


def _hashes(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture
def made_class(client, monkeypatch, lecture_pdf):
    """One finished mock class, as New notes makes it; returns its engine run folder."""
    monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "false")
    monkeypatch.setattr(make_service, "class_token", lambda: "")
    real = upsc_engine.make_notes

    def mocked(run_id, folder, request, **kwargs):
        seed_transcript(Path(folder) / "engine_runs", request["video_url"])
        return real(run_id, folder, {**request, "mock": True, "test_stub_pdf": True}, **kwargs)

    monkeypatch.setattr(upsc_engine, "make_notes", mocked)
    client.post("/new", data={**CLASS, "language": "english"},
                files=[("slides", ("Polity 07.pdf", lecture_pdf, "application/pdf"))])
    assert job_queue.wait_idle(300)
    run = db.query("SELECT * FROM runs WHERE engine='upsc' AND source='upload'")[0]
    assert run["state"] in ("complete", "needs_attention"), run["error"]
    monkeypatch.setattr(practice_service, "TEST_REQUEST", {"test_stub_pdf": True})
    return run, next(path for path in (Path(run["run_dir"]) / "engine_runs").glob("run_*") if path.is_dir())


def _start(client, practice: str, kind: str, ident: str) -> int:
    response = client.post("/practice/start", data={"practice": practice, "kind": kind, "id": ident},
                           follow_redirects=False)
    found = re.match(r"^/practice/(\d+)", response.headers["location"])
    assert found, response.headers["location"]
    return int(found.group(1))


@needs_engine
def test_mains_practice_from_a_class_never_touches_the_class(client, made_class, monkeypatch):
    run, engine_run = made_class
    before = _hashes(engine_run)
    page = client.get("/practice").text
    assert "Writs and Article 32" in page and "Mains practice" in page
    practice_id = _start(client, "dmp", "notecraft", str(run["id"]))
    assert "No practice questions yet" in client.get(f"/practice/{practice_id}").text

    monkeypatch.setattr(practice_service, "practice_token", lambda: "")
    monkeypatch.setattr(practice_service, "TEST_REQUEST", {"test_stub_pdf": True, "test_fake_client": {
        "question": "Examine the role of writs", "answer": "Introduction.\n\nBody: Article 32.\n\nConclusion."}})
    response = client.post(f"/practice/{practice_id}/dmp/questions", follow_redirects=False)
    assert "Smart%20Queue" in response.headers["location"]                     # a paid step waits its turn
    assert job_queue.wait_idle(300)
    page = client.get(f"/practice/{practice_id}").text
    questions = re.findall(r'name="q" value="(q_[0-9a-f]+)"', page)
    assert len(questions) == 10 and "Easy / Direct" in page and "Applied / Analytical" in page

    client.post(f"/practice/{practice_id}/dmp/answers", data={"q": questions[:2]})
    assert job_queue.wait_idle(300)
    page = client.get(f"/practice/{practice_id}").text
    assert page.count("Model answer") >= 2 and "Model answers sheet (2)" in page

    client.post(f"/practice/{practice_id}/dmp/sheet/questions", data={"q": questions[:4]})
    assert practice_service.wait_idle(practice_id, 300)
    client.post(f"/practice/{practice_id}/dmp/sheet/answers", data={"q": questions[:4]})
    assert practice_service.wait_idle(practice_id, 300)
    page = client.get(f"/practice/{practice_id}").text
    sheets = re.findall(rf'href="(/practice/{practice_id}/files/sheets/[^"]+)"', page)
    names = " ".join(sheets)
    assert "Question_Paper.docx" in names and "Model_Answers.docx" in names
    assert "DMP_07" in names                                                   # the lecture's DCN No. (no DMP No.)
    assert client.get(sheets[0]).content.startswith(b"PK")
    assert _hashes(engine_run) == before                                       # the class's folder is untouched
    assert client.get(f"/practice/{practice_id}/files/sheets/..%2F..%2Fnotes_final.md").status_code == 404
    assert client.get(f"/runs/{practice_id}", follow_redirects=False).headers["location"] == f"/practice/{practice_id}"

    # The Subject / DMP No. boxes print on the sheet and name it; the page keeps what was typed.
    client.post(f"/practice/{practice_id}/dmp/sheet/answers",
                data={"q": questions[:4], "sheet_subject": "Indian Polity", "sheet_dmp_no": "19"})
    assert practice_service.wait_idle(practice_id, 300)
    page = client.get(f"/practice/{practice_id}").text
    assert 'value="Indian Polity"' in page and 'value="19"' in page
    typed = [s for s in re.findall(rf'href="(/practice/{practice_id}/files/sheets/[^"]+)"', page)
             if "DMP_19" in s and s.endswith("Model_Answers.docx")]
    assert typed, page
    with zipfile.ZipFile(io.BytesIO(client.get(typed[0]).content)) as docx:
        body = re.sub(r"<[^>]+>", "", docx.read("word/document.xml").decode("utf-8"))
    assert "Indian Polity DMP 19" in body


def test_a_model_answer_is_shown_without_the_sheet_writers_markers():
    from note_studio.practice_routes import _answer_text

    shown = _answer_text("# APPROACH\n- Introduction: define\n\n# ANSWER\n## INTRODUCTION\nThe Budget.\n\n"
                         "## BODY\n- **Point:** **Revenue** meets costs.\n- **Point: Capital** builds assets.\n"
                         "## CONCLUSION\nSo it is.")
    assert "INTRODUCTION" not in shown and "CONCLUSION" not in shown and "Point" not in shown
    assert shown.startswith("Approach") and "\nAnswer\n" in shown
    assert "- **Revenue** meets costs." in shown and "- **Capital** builds assets." in shown


@needs_engine
@needs_bank
def test_dpp_from_an_old_app_run_reads_it_in_place(client, made_class):
    _run, engine_run = made_class
    old = paths.upsc_original() / "runs" / "run_20260917_173000_cccccccc"
    old.parent.mkdir(parents=True)
    shutil.copytree(engine_run, old, copy_function=shutil.copy2)
    before = _hashes(old)
    practice_id = _start(client, "dpp", "older", old.name)
    assert practice_service.wait_idle(practice_id, 600)                        # topics (first: reads the bank)
    st = practice_service.state(practice_id)
    assert st["last"]["status"] == "complete", st["last"]
    assert st["coverage"] and st["language"] == "English" and st["dpp_no"] == ""
    page = client.get(f"/practice/{practice_id}").text
    rows = re.findall(r'name="row" value="([^"]+)" checked', page)
    assert rows and "Choose by difficulty" in page

    client.post(f"/practice/{practice_id}/dpp/topics", data={"row": rows, "language": "English", "verified": "on",
                                                              "mode": "difficulty"})
    assert practice_service.wait_idle(practice_id, 300)
    available = practice_service.state(practice_id)["available"]
    assert sum(available.values()) > 0
    want = {d: min(2, n) for d, n in available.items()}
    client.post(f"/practice/{practice_id}/dpp/build", data=want)
    assert practice_service.wait_idle(practice_id, 300)
    slots = practice_service.state(practice_id)["slots"]
    assert len(slots) == sum(want.values())
    first = slots[0]
    client.post(f"/practice/{practice_id}/dpp/replace/{first['slot_id']}")
    assert practice_service.wait_idle(practice_id, 300)
    st = practice_service.state(practice_id)
    assert st["flash"].startswith(("Replaced with another", "No unused"))
    client.post(f"/practice/{practice_id}/dpp/remove/{st['slots'][-1]['slot_id']}")
    assert practice_service.wait_idle(practice_id, 300)
    assert len(practice_service.state(practice_id)["slots"]) == len(slots) - 1

    client.post(f"/practice/{practice_id}/dpp/files", data={"dpp_no": "7", "subject": "Polity"})
    assert practice_service.wait_idle(practice_id, 300)
    page = client.get(f"/practice/{practice_id}").text
    files = re.findall(rf'href="(/practice/{practice_id}/files/dpp/[^"]+)"', page)
    assert len(files) == 6 and all("_DPP_07_" in f for f in files)
    pdf = next(f for f in files if f.endswith("_questions.pdf"))
    assert client.get(pdf).content.startswith(b"%PDF")

    client.post(f"/practice/{practice_id}/dpp/new")                             # keeps what was already shown
    assert practice_service.wait_idle(practice_id, 300)
    client.post(f"/practice/{practice_id}/dpp/mode/pick")
    assert practice_service.wait_idle(practice_id, 300)
    eligible = practice_service.state(practice_id)["eligible"]
    shown = {s["question_id"] for s in slots}
    assert eligible and not shown & {e["question_id"] for e in eligible}
    client.post(f"/practice/{practice_id}/dpp/tick/{eligible[0]['question_id']}")
    client.post(f"/practice/{practice_id}/dpp/pick")
    assert practice_service.wait_idle(practice_id, 300)
    assert [s["question_id"] for s in practice_service.state(practice_id)["slots"]] == [eligible[0]["question_id"]]
    assert _hashes(old) == before                                              # the old app's run is untouched


def test_practice_lists_nothing_to_practise_and_guards_its_inputs(client):
    page = client.get("/practice").text
    assert "No finished UPSC notes yet" in page
    for data in ({"practice": "dmp", "kind": "older", "id": "../../etc"}, {"practice": "dmp", "kind": "notecraft", "id": "1"},
                 {"practice": "quiz", "kind": "older", "id": "run_20260917_173000_aaaaaaaa"}):
        response = client.post("/practice/start", data=data, follow_redirects=False)
        assert "kind=warn" in response.headers["location"], data
    assert client.get("/practice/999").status_code == 404


def test_new_notes_sends_practice_types_to_the_practice_page(client):
    page = client.get("/new?category=upsc").text
    assert 'href="/practice"' in page and "Made from finished notes" in page
    response = client.post("/new", data={**CLASS, "note_type": "upsc_mains_practice"}, follow_redirects=False)
    assert response.status_code == 400 and "open Practice" in response.text


def test_the_dpp_folder_is_chosen_in_settings(client, tmp_path):
    page = client.get("/settings/system").text
    assert "DPP question bank" in page
    bad = client.post("/settings/system/dpp", data={"folder": str(tmp_path)}, follow_redirects=False)
    assert "kind=warn" in bad.headers["location"] and practice_service.masters_folder() == ""
    for name in practice_service.MASTER_FILES.values():
        (tmp_path / name).write_bytes(b"x")
    good = client.post("/settings/system/dpp", data={"folder": str(tmp_path)}, follow_redirects=False)
    assert "kind=ok" in good.headers["location"]
    assert practice_service.masters() == {k: str(tmp_path / n) for k, n in practice_service.MASTER_FILES.items()}
    client.post("/settings/system/dpp", data={"folder": ""})
    assert practice_service.masters() == {}


def test_a_paid_step_without_sign_in_holds_the_queue(client):
    folder = paths.upsc_original() / "runs" / "run_20260917_173000_dddddddd"
    folder.mkdir(parents=True)
    (folder / "notes_final.md").write_text("#LECTURE_TITLE: Writs\n" + "Article 32 protects rights. " * 80,
                                           encoding="utf-8")
    (folder / "run_metadata.json").write_text(json.dumps({"language": "en"}), encoding="utf-8")
    practice_id = _start(client, "dmp", "older", folder.name)
    client.post(f"/practice/{practice_id}/dmp/questions")
    assert job_queue.wait_idle(60)
    assert job_queue.held() == "signin"
    assert "sign in" in practice_service.state(practice_id)["last"]["message"].lower()
    assert "Open Sign-in" in client.get(f"/practice/{practice_id}").text
    assert not (make_service.run_dir(practice_id) / "request.json").exists()     # no engine was started
    assert os.listdir(folder) == ["notes_final.md", "run_metadata.json"]
