"""The UPSC engine as a child process (Phase 5): prompt parity with the original
app, one class end to end through New notes and the Smart Queue (mock mode: no
sign-in, no network, no cost), sign-in, form problems, older runs and Check setup.

The original is only read: its own Python and code run with bytecode writing
off, and every file they write goes into tmp_path."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from note_studio import db, engine_catalog, engine_host, job_queue, make_service, paths
from note_studio.engines import upsc as upsc_engine

ROOT = Path(__file__).resolve().parents[1]
COPY = engine_catalog.engine_dir("upsc")
ORIGINAL = Path.home() / "Downloads" / "Upsc_Notes_2" / "upsc_Notes"
URL = "https://www.youtube.com/watch?v=abcdefghijk"
TRANSCRIPT = ("Namaskar students, aaj hum Indian Polity ke andar Fundamental Rights, Article 21, Directive Principles "
              "aur judicial review par baat karenge. Article 32 ko Dr Ambedkar ne heart and soul of the Constitution "
              "kaha tha. Writs paanch types ke hote hain: habeas corpus, mandamus, prohibition, certiorari aur quo "
              "warranto. Supreme Court Article 32 ke under aur High Courts Article 226 ke under writs issue karte hain. ") * 4

needs_engine = pytest.mark.skipif(not os.environ.get("NOTE_STUDIO_UPSC_PYTHON"),
                                  reason="the UPSC app's own Python is not on this computer")
needs_original = pytest.mark.skipif(not (ORIGINAL / "app.py").is_file(), reason="the original UPSC app is not here")


def seed_transcript(runs_dir: Path, url: str = URL) -> None:
    """The engine's own transcript cache (src/transcript.py), so a class link needs no network."""
    cache = Path(runs_dir) / "_transcript_cache"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{url}|captions".encode("utf-8")).hexdigest()[:32]
    (cache / f"{key}.json").write_text(json.dumps({
        "full_text": TRANSCRIPT, "source_type": "youtube", "source_label": "YouTube captions", "language": "hi",
        "engine": "captions", "duration_seconds": 600, "gaps": [], "segments": []}), encoding="utf-8")


def _engine_env() -> dict:
    # Same for both sides; it only stops a slow local-transcriber probe in mock mode (no AI client).
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "TRANSCRIPTION_FALLBACK": "false"}


def _prompts(root: Path, extra: str = "") -> dict:
    out = subprocess.run([os.environ["NOTE_STUDIO_UPSC_PYTHON"], str(ROOT / "tests" / "parity" / "upsc_prompts.py"),
                          str(root), *([extra] if extra else [])], capture_output=True, text=True, encoding="utf-8",
                         env=_engine_env(), cwd=str(ROOT), timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------ parity
@needs_engine
@needs_original
def test_the_copys_prompts_are_the_originals_until_extra_instructions_are_given():
    original, copy = _prompts(ORIGINAL), _prompts(COPY)
    assert len(original) == 18
    assert {k: v["sha256"] for k, v in copy.items()} == {k: v["sha256"] for k, v in original.items()}
    extra = "Keep every Article number exactly."
    with_extra = _prompts(COPY, extra)
    for name, value in with_extra.items():
        assert value["before_extra"] == original[name]["sha256"], name      # the original prompt, then the block
        assert value["tail"].endswith(extra), name


@needs_engine
@needs_original
@pytest.mark.parametrize("supplied_transcript", ["", TRANSCRIPT])
def test_a_class_gets_the_same_prompt_as_in_the_old_app(tmp_path, lecture_pdf, monkeypatch, supplied_transcript):
    """The original's own app.py call (taken from its source) against NoteCraft's engine_main, mock mode."""
    monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "false")
    slides_name = "Polity 07 Writs.pdf"
    request = {"language": "english", "subject": "polity", "video_url": URL, "lecture_title": "Writs", "dcn_no": "07",
               "transcript_text_override": supplied_transcript}
    # the old app, from its own folder
    old_out = tmp_path / "old"
    (old_out / "input").mkdir(parents=True)
    (old_out / "input" / slides_name).write_bytes(lecture_pdf)
    seed_transcript(old_out / "engine_runs")
    (old_out / "request.json").write_text(json.dumps({**request, "slides": str(old_out / "input" / slides_name)}),
                                         encoding="utf-8")
    ran = subprocess.run([os.environ["NOTE_STUDIO_UPSC_PYTHON"], str(ROOT / "tests" / "parity" / "upsc_app_driver.py"),
                          str(ORIGINAL), str(old_out), str(old_out / "request.json")], capture_output=True, text=True,
                         encoding="utf-8", env=_engine_env(), cwd=str(old_out), timeout=600)
    assert ran.returncode == 0, ran.stderr[-3000:]
    old_run = Path(json.loads(ran.stdout.strip().splitlines()[-1])["run_dir"])
    # NoteCraft: the same choices through the adapter and the child process
    new_out = tmp_path / "new"
    (new_out / "input").mkdir(parents=True)
    (new_out / "input" / slides_name).write_bytes(lecture_pdf)
    seed_transcript(new_out / "engine_runs")
    run = {"note_type": "upsc_dcn", "category": "upsc", "subject": "polity", "language": "english"}
    built = upsc_engine.build_request(run, {"video_url": URL, "file": slides_name, "lecture_title": "Writs",
                                            "dcn_no": "07", "transcript_text_override": supplied_transcript}, new_out, "", {})
    events = []
    result = engine_host.run("upsc", 1, new_out, {**built, "mock": True, "test_stub_pdf": True}, events.append)
    assert result["status"] == "needs_attention", result          # the stubbed PDF is the only thing missing
    new_run = Path(next(e for e in events if e["event"] == "done")["run_dir"])

    assert (new_run / "prompt_upsc.txt").read_bytes() == (old_run / "prompt_upsc.txt").read_bytes()
    assert sorted(p.name for p in (new_run / "output").iterdir()) == sorted(p.name for p in (old_run / "output").iterdir())
    old_meta = json.loads((old_run / "run_metadata.json").read_text(encoding="utf-8"))
    new_meta = json.loads((new_run / "run_metadata.json").read_text(encoding="utf-8"))
    assert sorted(new_meta) == sorted(old_meta)
    for key in ("subject", "language", "workflow", "note_type", "area", "quality_level", "model", "upsc_output_format",
                "note_depth", "fact_mode", "generate_error_report", "error_report_strictness", "lecture_title", "dcn_no",
                "transcript_words", "total_slides", "active_slide_count"):
        assert new_meta[key] == old_meta[key], key
    assert built["state_dir"] == str(paths.home() / "engines" / "upsc")      # never the old app's per-user folder
    if supplied_transcript:
        transcript = json.loads((new_run / "transcript_segments.json").read_text(encoding="utf-8"))
        assert transcript["source_type"] == "supplied" and transcript["retrieval_seconds"] == 0
        assert (new_run / "transcript.txt").read_text(encoding="utf-8") == supplied_transcript.strip()


# ------------------------------------------------------------- end to end
@pytest.fixture
def mock_class(monkeypatch):
    """Classes run in mock mode: no sign-in, the engine's own mock notes, no Word."""
    monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "false")
    monkeypatch.setattr(make_service, "class_token", lambda: "")
    real = upsc_engine.make_notes

    def mocked(run_id, folder, request, **kwargs):
        seed_transcript(Path(folder) / "engine_runs", request["video_url"])
        return real(run_id, folder, {**request, "mock": True, "test_stub_pdf": True}, **kwargs)

    monkeypatch.setattr(upsc_engine, "make_notes", mocked)
    yield
    job_queue.wait_idle(300)


CLASS = {"category": "upsc", "note_type": "upsc_dcn", "subject": "polity", "language": "hindi", "video_url": URL,
         "lecture_title": "Writs and Article 32", "dcn_no": "07", "instructions": "Keep every Article number exactly."}


@needs_engine
def test_a_class_from_new_notes_to_its_files(client, mock_class, lecture_pdf):
    page = client.get("/new?category=upsc").text
    for words in ("UPSC daily class notes (DCN)", "Class recording link", "Slides (optional)", "Lecture title",
                  "DCN No.", "Choose the notes language", "Marathi"):
        assert words in page, words
    response = client.post("/new", data=CLASS, files=[("slides", ("Polity 07.pdf", lecture_pdf, "application/pdf"))],
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/queue"), response.text[-500:]
    assert job_queue.wait_idle(600)
    run = db.query("SELECT * FROM runs WHERE engine='upsc'")[0]
    assert run["state"] == "needs_attention", (run["message"], run["error"])
    assert "PDF could not be made" in run["message"]                     # stubbed in tests, so expected
    files = db.query("SELECT label FROM run_items WHERE run_id=? ORDER BY position", (run["id"],))
    names = [f["label"] for f in files]
    assert any(n.endswith("_upsc_notes.docx") for n in names) and any(n.endswith("_error_report.xlsx") for n in names)
    page = client.get(f"/runs/{run['id']}").text
    for words in ("Needs attention", "Word document", "Excel workbook", "Class recording", "DCN 07", "Hindi"):
        assert words in page, words
    link = re.search(rf'href="(/runs/{run["id"]}/files/\d+)"', page).group(1)
    assert client.get(link).content.startswith(b"PK")                    # a real .docx
    engine_run = next((Path(run["run_dir"]) / "engine_runs").glob("run_*"))
    prompt = (engine_run / "prompt_upsc.txt").read_text(encoding="utf-8")
    assert prompt.rstrip().endswith("Keep every Article number exactly.")    # this run's instruction, at the end
    request = json.loads((Path(run["run_dir"]) / "request.json").read_text(encoding="utf-8"))
    assert request["models"] == {"notes": ["gemini-3.5-flash", "gemini-3.6-flash"],
                                 "speech": ["gemini-3.5-flash", "gemini-3.6-flash"], "images": ["gemini-2.5-flash-image"]}
    assert request["state_dir"] == str(paths.home() / "engines" / "upsc")
    assert (Path(run["run_dir"]) / "engine.log").is_file()


def test_without_sign_in_a_class_says_so_and_the_queue_holds(client, lecture_pdf):
    client.post("/new", data=CLASS, follow_redirects=False)
    assert job_queue.wait_idle(60)
    run = db.query("SELECT * FROM runs WHERE engine='upsc'")[0]
    assert run["state"] == "failed" and "Settings" in run["error"] and run["cost_inr"] == 0
    assert job_queue.held() == "signin"
    assert not (Path(run["run_dir"]) / "request.json").exists()          # no engine was started


@needs_engine
def test_a_pasted_transcript_from_the_form_is_used_without_fetching_the_recording(client, mock_class):
    page = client.get("/new?category=upsc").text
    assert 'name="transcript_text_override"' in page and "Existing Drive transcript" in page
    response = client.post("/new", data={**CLASS, "transcript_text_override": TRANSCRIPT}, follow_redirects=False)
    assert response.status_code == 303 and job_queue.wait_idle(600)
    run = db.query("SELECT * FROM runs WHERE engine='upsc'")[0]
    assert run["state"] == "needs_attention", run["error"]       # only the test PDF stub is missing
    folder = Path(run["run_dir"])
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    assert request["transcript_text_override"] == TRANSCRIPT.strip()
    engine_run = next((folder / "engine_runs").glob("run_*"))
    transcript = json.loads((engine_run / "transcript_segments.json").read_text(encoding="utf-8"))
    assert transcript["source_type"] == "supplied" and transcript["retrieval_seconds"] == 0
    assert (engine_run / "transcript.txt").read_text(encoding="utf-8") == TRANSCRIPT.strip()
    assert "Using the supplied transcript" in client.get(f"/runs/{run['id']}").text


@pytest.mark.parametrize("changes, files, words", [
    ({"video_url": ""}, None, "Paste the class recording link"),
    ({"video_url": "not a link at all"}, None, "That is not a link"),
    ({"language": ""}, None, "Choose a language"),
    ({"language": "auto"}, None, "Choose a language"),
    ({"subject": "auto"}, None, "Choose a subject"),
    ({"transcript_text_override": "A short excerpt."}, None, "Paste the full lecture transcript"),
    ({}, ("notes.docx", b"PK\x03\x04 a docx"), "is not a PDF or PowerPoint file"),
    ({}, ("slides.pdf", b"PK\x03\x04 not a pdf"), "is not a PDF or PowerPoint file"),
])
def test_class_form_problems_are_said_in_plain_words(client, changes, files, words):
    upload = [("slides", (*files, "application/octet-stream"))] if files else None
    response = client.post("/new", data={**CLASS, **changes}, files=upload, follow_redirects=False)
    assert response.status_code == 400 and words in response.text, response.text[-400:]
    assert db.query("SELECT COUNT(*) AS n FROM job_queue")[0]["n"] == 0


def test_the_same_class_twice_is_added_once(client):
    job_queue.hold("paused")
    client.post("/new", data=CLASS)
    second = client.post("/new", data=CLASS, follow_redirects=False)
    assert "not%20added%20twice" in second.headers["location"]
    assert len(db.query("SELECT id FROM job_queue")) == 1
    assert db.query("SELECT title FROM job_queue")[0]["title"] == "Writs and Article 32"


# -------------------------------------------------------------- older runs
def _old_run(root: Path, name: str, meta: dict, files=(), failed=False) -> Path:
    folder = root / "runs" / name
    (folder / "output").mkdir(parents=True)
    (folder / "run_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    for filename in files:
        (folder / "output" / filename).write_bytes(b"PK\x03\x04 old notes")
    if failed:
        (folder / "error.json").write_text("{}", encoding="utf-8")
    return folder


def test_runs_from_the_old_upsc_app_are_listed_and_read_in_place(client):
    root = paths.upsc_original()
    done = _old_run(root, "run_20260917_173000_aaaaaaaa",
                    {"lecture_title": "Probity in Governance", "language": "Hindi", "upsc_output_format": "dcn",
                     "api_calls": [{"provider": "pw_proxy"}], "warnings": ["Captions were auto-generated."]},
                    files=("Probity_upsc_notes.docx", "Probity_upsc_notes.pdf", "Probity_error_report.xlsx",
                           "Probity_quality_report.docx"))
    _old_run(root, "run_20261004_030022_bbbbbbbb", {"input_file": "C:/x/Economy 03.pdf", "api_calls": [{"provider": "mock"}]},
             failed=True)
    (root / "runs" / "_transcript_cache").mkdir()
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    page = client.get("/runs/older").text
    assert page.index("Economy 03") < page.index("Probity in Governance")          # newest first
    assert "Test run" in page and "Did not finish" in page and "_transcript_cache" not in page
    detail = client.get("/runs/older/upsc/run_20260917_173000_aaaaaaaa").text
    assert "Probity_upsc_notes.docx" in detail and "Excel workbook" in detail and "Hindi" in detail
    assert "Probity_quality_report.docx" not in detail                              # the old page never offered it
    download = client.get("/runs/older/upsc/run_20260917_173000_aaaaaaaa/files/Probity_upsc_notes.pdf")
    assert download.status_code == 200 and download.content == b"PK\x03\x04 old notes"
    for bad in ("/runs/older/upsc/run_20260917_173000_aaaaaaaa/files/..%2Frun_metadata.json",
                "/runs/older/upsc/run_20260917_173000_aaaaaaaa/files/Probity_quality_report.docx",
                "/runs/older/upsc/not-a-run", "/runs/older/upsc/..%2F..%2Fapp.py"):
        assert client.get(bad).status_code == 404, bad
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after and done.is_dir()


def test_check_setup_says_what_the_upsc_engine_has(client):
    page = client.get("/settings/system").text
    assert "Check setup" in page and "UPSC (OnlyIAS)" in page
    found = client.get("/settings/system/check").text
    for name in ("UPSC engine&#39;s Python", "ffmpeg and ffprobe", "PDF from Word notes", "Handwriting fonts"):
        assert name in found, name


def test_settings_offers_the_upsc_model_stages(client):
    page = client.get("/settings/ai").text
    for words in ("UPSC daily class notes (DCN)", "Listening to the class recording", "Looking at slide pictures",
                  "Gemini 2.5 Flash Image", "If the first is unavailable or stays busy"):
        assert words in page, words
    saved = client.post("/settings/ai", data={"type_id": "upsc_dcn", "stage": "speech", "model_0": "gemini-3.6-flash",
                                              "model_1": "gpt-5.6-luna-ard-content"}, follow_redirects=False)
    assert "kind=ok" in saved.headers["location"]
    from note_studio import models

    assert models.order("upsc_dcn", "speech") == ["gemini-3.6-flash"]          # GPT has no speech route


def test_a_seeded_upsc_pack_from_phase_2_is_brought_up_to_date():
    from note_studio import categories

    root = categories.packs_dir()
    folder = root / "upsc"
    folder.mkdir(parents=True)
    old = (ROOT / "tests" / "data" / "upsc_category_phase2.json")
    (folder / "category.json").write_bytes(old.read_bytes())
    assert "upsc" in categories.seed()
    assert categories.get("upsc")["enabled"] is True
    assert (folder / "category.json.bak").read_bytes() == old.read_bytes()
    edited = json.loads(old.read_text(encoding="utf-8"))
    edited["label"] = "UPSC (edited)"
    (folder / "category.json").write_text(json.dumps(edited), encoding="utf-8")
    categories.seed()
    assert categories.get("upsc")["label"] == "UPSC (edited)"                 # an operator's edit is kept


def test_a_running_class_can_be_stopped_from_its_page(client, monkeypatch):
    run_id = db.create_run("upsc", title="Class", note_type="upsc_dcn", category="upsc", source="upload")
    db.execute("UPDATE runs SET state='running' WHERE id=?", (run_id,))
    assert "/stop" not in client.get(f"/runs/{run_id}").text                   # nothing to stop: no engine running
    monkeypatch.setattr(engine_host, "running", lambda rid: rid == run_id)
    stopped = []
    monkeypatch.setattr(engine_host, "stop", lambda rid, wait=10.0: stopped.append(rid) or True)
    assert f'action="/runs/{run_id}/stop"' in client.get(f"/runs/{run_id}").text
    response = client.post(f"/runs/{run_id}/stop", follow_redirects=False)
    assert "Stopped" in response.headers["location"].replace("%20", " ") and stopped == [run_id]
    assert job_queue.held() == "stop"


def test_the_token_handed_to_an_engine_has_at_least_fifty_minutes_left():
    """The UPSC engine opens a Drive class recording with it (engine_main passes
    it as the run's drive_token), possibly well into its first hour."""
    from datetime import datetime, timedelta, timezone

    from note_studio import signin

    class Creds:
        def __init__(self, minutes_left):
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes_left)
            self.refreshed = False

        def refresh(self, request):
            self.refreshed = True

    short, long = Creds(10), Creds(58)
    aware = Creds(10)
    aware.expiry = aware.expiry.replace(tzinfo=timezone.utc)
    signin._renew_if_short(short)
    signin._renew_if_short(long)
    signin._renew_if_short(None)
    signin._renew_if_short(aware)
    assert short.refreshed and aware.refreshed and not long.refreshed
    assert "drive_token=token" in (engine_catalog.engine_dir("upsc") / "engine_main.py").read_text(encoding="utf-8")
