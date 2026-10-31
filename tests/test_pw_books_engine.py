"""Phase 7: the PW Books handwritten notes engine as a child process. Prompt and
app-path parity with the original, one deck end to end through New notes (mock
mode: no sign-in, no network, no cost), the form, and the NEET-JEE pack upgrade.

The original is only read: its own Python and code run with bytecode writing
off, and every file they write goes into tmp_path."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from note_studio import categories, db, engine_catalog, engine_host, job_queue, make_service
from note_studio.engines import pw_books as pw_books_engine

ROOT = Path(__file__).resolve().parents[1]
COPY = engine_catalog.engine_dir("pw_books")
ORIGINAL = Path.home() / "Downloads" / "handwritten_notes_automation_complete" / "handwritten_notes_automation_complete"
needs_engine = pytest.mark.skipif(not os.environ.get("NOTE_STUDIO_PW_BOOKS_PYTHON"),
                                  reason="the PW Books app's own Python is not on this computer")
needs_original = pytest.mark.skipif(not (ORIGINAL / "app.py").is_file(), reason="the original PW Books app is not here")
DECK = {"category": "neet_jee", "note_type": "pw_books_handwritten", "subject": "physics", "language": "english",
        "mode": "summary", "exam": "jee", "math_render_mode": "omml", "strict_math": "on",
        "instructions": "Underline every SI unit."}


def _env() -> dict:
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}


def _prompts(root: Path, extra: str = "") -> dict:
    out = subprocess.run([os.environ["NOTE_STUDIO_PW_BOOKS_PYTHON"], str(ROOT / "tests" / "parity" / "pw_books_prompts.py"),
                          str(root), *([extra] if extra else [])], capture_output=True, text=True, encoding="utf-8",
                         env=_env(), cwd=str(ROOT), timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_engine
@needs_original
def test_the_copys_prompts_are_the_originals_until_extra_instructions_are_given():
    original, copy = _prompts(ORIGINAL), _prompts(COPY)
    assert len(original) >= 100 and not any(k.startswith("missing_") for k in original)
    assert {k: v["sha256"] for k, v in copy.items()} == {k: v["sha256"] for k, v in original.items()}
    extra = "Underline every SI unit."
    for name, value in _prompts(COPY, extra).items():
        assert value["before_extra"] == original[name]["sha256"], name
        assert value["tail"].endswith(extra), name


def _normal(folder: Path) -> dict:
    """The run's deterministic files, with its own folder path taken out."""
    found = {}
    for name in ("notes_raw.txt", "slides_for_ai.json", "filter_report.json", "slides_raw.json"):
        path = folder / name
        if path.is_file():
            found[name] = path.read_text(encoding="utf-8").replace(str(folder), "<RUN>").replace(
                str(folder).replace("\\", "\\\\"), "<RUN>").replace(str(folder).replace("\\", "/"), "<RUN>")
    meta = json.loads((folder / "run_metadata.json").read_text(encoding="utf-8"))
    for key in ("task_id", "input_file", "copied_input", "docx_path", "pdf_path", "warnings"):
        meta.pop(key, None)
    summary = meta.pop("run_summary", {}) or {}
    for key in ("task_id", "started_at", "ended_at", "generated_at", "total_processing_seconds"):
        summary.pop(key, None)
    found["run_metadata"] = meta
    found["run_summary"] = summary
    found["output"] = sorted(p.name for p in (folder / "output").iterdir())
    return found


@needs_engine
@needs_original
def test_a_deck_runs_exactly_as_in_the_old_app(tmp_path, lecture_pdf):
    """The original's own app.py settings, routing and call (from its source)
    against NoteCraft's adapter + engine_main, mock mode."""
    name = "Physics Lecture 05.pdf"
    request = {"subject": "physics", "language": "english", "mode": "summary", "exam": "jee",
               "math_render_mode": "omml", "strict_math": True}
    old_out = tmp_path / "old"
    (old_out / "input").mkdir(parents=True)
    (old_out / "input" / name).write_bytes(lecture_pdf)
    (old_out / "request.json").write_text(json.dumps({**request, "file": str(old_out / "input" / name)}),
                                         encoding="utf-8")
    ran = subprocess.run([os.environ["NOTE_STUDIO_PW_BOOKS_PYTHON"],
                          str(ROOT / "tests" / "parity" / "pw_books_app_driver.py"), str(ORIGINAL), str(old_out),
                          str(old_out / "request.json")], capture_output=True, text=True, encoding="utf-8",
                         env=_env(), cwd=str(old_out), timeout=600)
    assert ran.returncode == 0, ran.stderr[-3000:]
    old_run = Path(json.loads(ran.stdout.strip().splitlines()[-1])["run_dir"])

    new_out = tmp_path / "new"
    (new_out / "input").mkdir(parents=True)
    (new_out / "input" / name).write_bytes(lecture_pdf)
    run = {"category": "neet_jee", "note_type": "pw_books_handwritten", "subject": "physics", "language": "english"}
    built = pw_books_engine.build_request(run, {"file": name, "mode": "summary", "exam": "jee",
                                                "math_render_mode": "omml", "strict_math": True}, new_out, "", {})
    events = []
    result = engine_host.run("pw_books", 1, new_out, {**built, "mock": True, "test_stub_pdf": True}, events.append)
    assert result["status"] == "needs_attention", result                     # only the stubbed PDF is missing
    new_run = Path(next(e for e in events if e["event"] == "done")["run_dir"])
    assert _normal(new_run) == _normal(old_run)


@pytest.fixture
def mock_deck(monkeypatch):
    monkeypatch.setattr(make_service, "class_token", lambda: "")
    real = pw_books_engine.make_notes

    def mocked(run_id, folder, request, **kwargs):
        return real(run_id, folder, {**request, "mock": True, "test_stub_pdf": True}, **kwargs)

    monkeypatch.setattr(pw_books_engine, "make_notes", mocked)
    yield
    job_queue.wait_idle(300)


@needs_engine
def test_a_deck_from_new_notes_to_its_files(client, mock_deck, lecture_pdf):
    page = client.get("/new?category=neet_jee&type=pw_books_handwritten").text
    for words in ("PW Books handwritten notes", "Select subject…", "Concise Notes", "JEE + NEET", "PowerPoints",
                  "Strict equation preservation"):
        assert words in page, words
    assert "Sanskrit" not in page.split('name="subject"')[1].split("</select>")[0]   # the app's four subjects only
    response = client.post("/new", data=DECK, files=[("files", ("Physics 05.pdf", lecture_pdf, "application/pdf"))],
                           follow_redirects=False)
    assert response.status_code == 303, response.text[-500:]
    assert job_queue.wait_idle(300)
    run = db.query("SELECT * FROM runs WHERE engine='pw_books'")[0]
    assert run["state"] == "needs_attention" and "PDF" in run["message"], (run["message"], run["error"])
    files = [f["label"] for f in db.query("SELECT label FROM run_items WHERE run_id=?", (run["id"],))]
    assert any(f.endswith("_Concise_Notes.docx") for f in files) and any(f.endswith(".zip") for f in files)
    page = client.get(f"/runs/{run['id']}").text
    assert "Word document" in page and "Needs attention" in page
    link = re.search(rf'href="(/runs/{run["id"]}/files/\d+)"', page).group(1)
    assert client.get(link).content.startswith(b"PK")
    request = json.loads((Path(run["run_dir"]) / "request.json").read_text(encoding="utf-8"))
    assert request["extra_instructions"] == "Underline every SI unit."           # this run's own words only
    assert request["mode"] == "summary" and request["exam"] == "jee" and request["strict_math"] is True
    assert request["models"]["notes"] == ["gemini-3.5-flash", "gemini-3.6-flash"]


@pytest.mark.parametrize("changes, files, words", [
    ({"subject": ""}, None, "Choose a subject"),
    ({"subject": "auto"}, None, "Choose a subject"),
    ({"subject": "sanskrit"}, None, "Choose a subject"),
    ({"language": "sanskrit_hindi"}, None, "Choose a language"),
    ({}, ("notes.docx", b"PK\x03\x04 a docx"), "is not a PDF or PowerPoint file"),
])
def test_deck_form_problems_are_said_in_plain_words(client, changes, files, words, lecture_pdf):
    upload = [("files", files)] if files else [("files", ("L.pdf", lecture_pdf, "application/pdf"))]
    response = client.post("/new", data={**DECK, **changes}, files=upload, follow_redirects=False)
    assert response.status_code == 400 and words in response.text, response.text[-400:]


def test_a_powerpoint_is_accepted_for_pw_books(client):
    job_queue.hold("paused")
    response = client.post("/new", data=DECK, files=[("files", ("Physics 06.pptx", b"PK\x03\x04 deck", "application/zip"))],
                           follow_redirects=False)
    assert response.status_code == 303
    assert db.query("SELECT title FROM job_queue")[0]["title"] == "Physics 06"


def test_neet_jee_offers_pw_books_and_an_old_pack_is_brought_up_to_date():
    categories.seed()
    assert categories.get("neet_jee")["note_types"] == ["concise_handdrawn", "pw_books_handwritten"]
    assert categories.get("foundation")["note_types"] == ["concise_handdrawn"]
    path = Path(categories.get("neet_jee")["folder"]) / "category.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["note_types"] = ["concise_handdrawn"]                                 # as Phase 2 seeded it
    path.write_text(json.dumps(meta), encoding="utf-8")
    assert "neet_jee" in categories.seed()
    assert categories.get("neet_jee")["note_types"][-1] == "pw_books_handwritten"
    assert json.loads((path.parent / "category.json.bak").read_text(encoding="utf-8"))["note_types"] == ["concise_handdrawn"]
    meta["note_types"] = ["concise_handdrawn", "upsc_dcn"]                    # an operator's own list
    path.write_text(json.dumps(meta), encoding="utf-8")
    categories.seed()
    assert categories.get("neet_jee")["note_types"] == ["concise_handdrawn", "upsc_dcn"]


def test_pw_books_gets_only_its_own_rules(client):
    """NEET-JEE's general rule files are the hand-drawn engine's own format rules."""
    from note_studio import requirements

    folder = Path(categories.get("neet_jee")["folder"]) / "rules"
    (folder / "types").mkdir(exist_ok=True)
    (folder / "types" / "pw_books_handwritten.txt").write_text("Keep every SI unit.\n", encoding="utf-8")
    resolved = requirements.resolve("neet_jee", "pw_books_handwritten", "physics", "english")
    assert [l.layer for l in resolved.applied()] == ["category_type"]
    card = client.get("/new/rules?category=neet_jee&note_type=pw_books_handwritten&subject=physics").text
    assert "Keep every SI unit." in card and "rules/physics.txt" not in card
    settings = client.get("/settings/ai").text
    assert "Reading the slide pictures" in settings and "Gemini 3.1 Flash Image" in settings
