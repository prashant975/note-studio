"""Audio notes: a finished chapter's lecture PDFs -> merged PDF, the team's-prompt script (checked against
the notes) and its audio, spoken by PW AI's Gemini voices. PW AI is replaced by a fake: nothing is paid,
and no program on the computer is used (the audio is joined in Python and written as MP3 by the bundled lameenc library)."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import wave
from pathlib import Path
from urllib.parse import unquote

import pytest

from note_studio import categories, db, job_queue, make_service, models, note_types
from note_studio.audio import chapter, script, voice

HOSTS = ("Raju", "Ram")
RATE = 24000
MP3_SYNC = b"\xff\xfb"                  # an MPEG-1 Layer III frame (44.1 kHz, no CRC): the episode's form


# ------------------------------------------------------------------ helpers and fakes
_PDFS: dict = {}


def pdf(text: str, pages: int = 1) -> bytes:
    """A small PDF; the same text gives the same bytes (PyMuPDF writes a new file ID on every save)."""
    import fitz

    if (text, pages) not in _PDFS:
        with fitz.open() as doc:
            for n in range(pages):
                doc.new_page().insert_text((72, 72), f"{text} page {n + 1}")
            _PDFS[text, pages] = doc.tobytes()
    return _PDFS[text, pages]


def wav(seconds: float, rate: int = RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\x02\x00" * int(rate * seconds))
    return out.getvalue()


def long_lines(count: int = 14) -> list[dict]:
    """Lines of about 300 characters: 14 of them make three voice groups."""
    return [{"speaker": HOSTS[n % 2], "text": f"Line {n + 1}: bhai ye funda chamak gaya, Faraday law yaad hai? " * 5}
            for n in range(count)]


class FakePW:
    """pw_access.gemini_generate (script, check), gemini_native (two voices) and gemini_tts (one line)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.lines = long_lines()
        third = self.lines[2]["text"]
        self.review = {"corrections": [{"line": 3, "old": third, "new": "Ram ne sahi bola: Faraday's first law.",
                                        "reason": "wording"}],
                       "missing": [{"topic": "Kohlrausch law"}]}
        self.native_error = ""                     # e.g. "gemini native proxy error 400: ..." to refuse
        self.line_error = ""

    def gemini_generate(self, token_provider, *, model, request, filename="", input_unit="", count=None, task_id=""):
        keys = set(((request.get("generationConfig") or {}).get("responseSchema") or {}).get("properties") or {})
        kind = "script" if "lines" in keys else "check"
        answer = {"title": "Electrochemistry ka full revision", "lines": self.lines} if kind == "script" else self.review
        self.calls.append({"kind": kind, "model": model, "request": request, "token": token_provider(), "count": count})
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}, "finishReason": "STOP"}],
                "cost_inr": 2.0}

    def gemini_native(self, token_provider, *, model, request, filename="", input_unit="", count=None, task_id=""):
        import pw_access

        self.calls.append({"kind": "voice", "model": model, "request": request})
        if self.native_error:
            raise pw_access.PWAccessError(self.native_error)
        data = base64.b64encode(b"\x01\x00" * (RATE // 10)).decode()      # 0.1 s of raw PCM, as Gemini sends it
        return {"result": {"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": data}}]}}]}, "cost_inr": 1.0}

    def gemini_tts(self, token_provider, *, text, voice, model, filename="", count=None, task_id="", **kw):
        import pw_access

        self.calls.append({"kind": "voice line", "model": model, "voice": voice, "text": text})
        if self.line_error:
            raise pw_access.PWAccessError(self.line_error)
        return {"result": {"audio_base64": base64.b64encode(wav(0.05)).decode(), "content_type": "audio/wav"},
                "cost_inr": 0.1}

    def kinds(self) -> list[str]:
        return [c["kind"] for c in self.calls]


@pytest.fixture
def pw(monkeypatch):
    from note_studio import engine_catalog
    from note_studio.typeset import ai

    engine_catalog.put_shortnotes_on_path()
    import pw_access

    fake = FakePW()
    monkeypatch.setattr(pw_access, "gemini_generate", fake.gemini_generate)
    monkeypatch.setattr(pw_access, "gemini_native", fake.gemini_native)
    monkeypatch.setattr(pw_access, "gemini_tts", fake.gemini_tts)
    monkeypatch.setattr(make_service, "typeset_token", lambda: (lambda: "test-token"))
    monkeypatch.setattr(ai, "RETRY_PAUSE", 0.0)
    yield fake
    job_queue.wait_idle(120)


FORM = {"category": "neet_jee", "note_type": "audio_notes", "subject": "chemistry", "language": "hinglish",
        "chapter_title": "Electrochemistry", "teacher_name": "Rahul Sir", "length": "15", "audio_step": "now",
        "host_one": "Raju", "host_two": "Ram", "voice_one": "Puck", "voice_two": "Fenrir", "from_page": "audio"}
LECTURES = [("Lecture 10.pdf", "ten"), ("Lecture 2.pdf", "two"), ("Lecture 1.pdf", "one")]


def _post(client, lectures=LECTURES, **changes):
    form = {k: v for k, v in {**FORM, **changes}.items() if v is not None}
    files = [("files", (name, pdf(text), "application/pdf")) for name, text in lectures]
    return client.post("/new", data=form, files=files, follow_redirects=False)


def _runs() -> list[dict]:
    return db.query("SELECT * FROM runs WHERE note_type='audio_notes' ORDER BY id")


def _files(run) -> dict:
    return {r["label"]: Path(r["output_path"]) for r in
            db.query("SELECT label, output_path FROM run_items WHERE run_id=? ORDER BY position", (run["id"],))}


# ------------------------------------------------------------------ the prompt and the type
def test_the_prompt_is_the_teams_word_for_word_and_only_its_example_names_change():
    text = script.prompt_text()
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == script.PROMPT_SHA256
    assert text.startswith("Main ek merged PDF provide kar raha hu") and text.endswith("mast Hinglish script generate karo.")
    assert script.prompt("Rahul Sir", HOSTS) == text
    mine = script.prompt("Neha Ma'am", ("Aman", "Rohit"))
    assert "Rahul Sir" not in mine and mine.count("Neha Ma'am") == text.count("Rahul Sir") == 4
    assert "(jaise Aman aur Rohit)" in mine
    assert mine.replace("Neha Ma'am", "Rahul Sir").replace("Aman aur Rohit", "Raju aur Ram") == text
    system = script.system(HOSTS, "Rahul Sir", 15, "Spend more time on numericals.", ("man", "woman"))
    assert '"Raju" and "Ram"' in system and "about 15 minutes" in system
    assert "about 2775 words in about 167 turns" in system                  # the reference: 2,823 words, 170 turns
    assert "Raju is a young man" in system and 'Ram is a young woman (her Hindi verbs are feminine' in system
    assert system.rstrip().endswith("Spend more time on numericals.")


def test_the_reference_episode_shows_the_manner_with_this_runs_friends_and_teacher():
    """The team's sample (heard once, 26 Nov 2026): its turns go to the writer as a model of manner only."""
    system = script.system(("Aman", "Neha"), "Rahul Sir", 10)
    assert "Never copy its content, sentences or opening." in system
    excerpt = system.split("Never copy its content, sentences or opening.\n", 1)[1]
    assert excerpt.startswith("Aman: Aaj toh sach mein is baat ki party banti hai yaar.")
    assert "\nNeha: haha Bilkul, kaafi lamba chapter tha ye." in excerpt
    assert "Amit Sir" not in system and "par Rahul Sir ne kya mast tareeqe se bataya tha" in excerpt
    assert "A: " not in excerpt and "(hans ke)" not in excerpt
    assert script.WORDS_PER_MINUTE == 185 and script.target_turns(15) == 167
    assert voice.gender("Puck") == "man" and voice.gender("Kore") == "woman" and voice.gender("x") == ""
    style = voice.style(("Raju", "Riya"), ("Puck", "Kore"))
    assert "Raju (a young man) and Riya (a young woman)" in style and "urban Indian Hinglish accent" in style


def test_the_type_is_ready_in_every_category_in_hinglish_with_its_own_voices(client):
    entry = note_types.get("audio_notes")
    assert note_types.ready("audio_notes") and entry["engine"] == "audio"
    assert entry["models"]["notes"] == ["gemini-3.7-flash"] and entry["models"]["qc"] == ["gemini-3.7-flash"]
    for key in ("voice_one", "voice_two"):
        assert entry["options"][key]["choices"] == voice.VOICES
    assert entry["options"]["voice_one"]["default"] != entry["options"]["voice_two"]["default"]
    defaults = note_types.option_defaults("audio_notes")      # a young man and a young woman, as in the reference
    assert (defaults["host_one"], defaults["voice_one"], defaults["host_two"], defaults["voice_two"]) == (
        "Raju", "Puck", "Riya", "Kore")
    categories.seed()
    for pack in categories.all_packs():
        assert "audio_notes" in [t["id"] for t in categories.offered_types(pack)], pack["id"]
        assert categories.language_choices(pack, entry) == [{"id": "hinglish", "label": "Hinglish"}]
    assert [m["id"] for m in models.selectable("audio_notes", "voice")] == [
        "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
    assert "Speaking the script" in client.get("/settings/ai").text
    assert "A two-friend Hinglish revision podcast" in client.get("/settings/system").text


def test_the_make_notes_menu_has_an_audio_notes_section(client):
    home = client.get("/").text
    assert 'href="/audio"' in home and "A finished chapter becomes a Hinglish revision podcast" in home
    page = client.get("/audio").text
    assert "<h1>Audio notes</h1>" in page and 'name="note_type" value="audio_notes"' in page
    assert 'name="from_page" value="audio"' in page and 'value="typeset_notes"' not in page
    assert "data-ordered" in page and "Make audio notes" in page and "Teacher name" in page
    assert 'href="/audio?category=neet_jee"' in page and "How Audio notes work" in page
    assert '<option value="hinglish"' in page and "for the chapter" in page and "Traceback" not in page
    new = client.get("/new?category=bsc&type=audio_notes").text
    assert 'value="audio_notes" checked' in new and "The chapter's lecture PDFs" in new


def test_settings_can_stop_offering_audio_notes_and_offer_them_again(client):
    page = client.get("/settings/categories").text
    assert 'action="/settings/categories/neet_jee/audio"' in page and "Stop offering audio notes" in page
    off = client.post("/settings/categories/neet_jee/audio", data={"on": "0"}, follow_redirects=False)
    assert "kind=ok" in off.headers["location"]
    folder = Path(categories.get("neet_jee")["folder"])
    meta = json.loads((folder / "category.json").read_text("utf-8"))
    assert meta["offer_audio"] is False and meta["note_types"] == ["concise_handdrawn", "pw_books_handwritten"]
    assert "offer_audio" not in json.loads((folder / "category.json.bak").read_text("utf-8"))
    assert "audio_notes" not in [t["id"] for t in categories.offered_types(categories.get("neet_jee"))]
    assert 'href="/audio?category=neet_jee"' not in client.get("/audio").text
    client.post("/settings/categories/neet_jee/audio", data={"on": "1"})
    assert "offer_audio" not in json.loads((folder / "category.json").read_text("utf-8"))
    assert categories.audio_offered(categories.get("neet_jee"))


# ------------------------------------------------------------------ joining, script, voice
def test_lectures_join_in_lecture_order_with_a_bookmark_each():
    import fitz

    files = [("Lecture 10.pdf", pdf("ten")), ("Lecture 2.pdf", pdf("two", 2)), ("lecture 1.pdf", pdf("one"))]
    ordered = sorted(files, key=lambda f: chapter.lecture_order(f[0]))
    assert [name for name, _ in ordered] == ["lecture 1.pdf", "Lecture 2.pdf", "Lecture 10.pdf"]
    merged, lectures = chapter.merge(ordered)
    with fitz.open(stream=merged, filetype="pdf") as doc:
        assert doc.page_count == 4 and "ten page 1" in doc[3].get_text()
        assert [entry[:3] for entry in doc.get_toc()] == [[1, "lecture 1", 1], [1, "Lecture 2", 2], [1, "Lecture 10", 4]]
    assert lectures[1] == {"lecture": 2, "file": "Lecture 2.pdf", "first_page": 2, "last_page": 3, "pages": 2}
    assert chapter.lecture_map(lectures).splitlines() == [
        "The PDF joins 3 lectures of one chapter, in order:", "Lecture 1: lecture 1 (page 1)",
        "Lecture 2: Lecture 2 (pages 2-3)", "Lecture 3: Lecture 10 (page 4)"]
    assert chapter.ai_copy(merged) == merged
    with pytest.raises(chapter.ChapterError, match="even as pictures"):
        chapter.ai_copy(merged, limit=10)
    with pytest.raises(chapter.ChapterError, match="could not be opened"):
        chapter.merge([("bad.pdf", b"%PDF-1.4 not really")])
    with fitz.open() as locked:
        locked.new_page()
        data = locked.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    with pytest.raises(chapter.ChapterError, match="password"):
        chapter.merge([("locked.pdf", data)])


def test_a_script_is_tidied_and_given_back_as_text():
    raw = {"title": "**Chapter khatam!**", "lines": [
        {"speaker": "raju", "text": "(laughs) Raju: Bhai **finally** chapter khatam!"},
        {"speaker": "Narrator", "text": "Ram bolta hai"}, *long_lines(12), {"speaker": "Ram", "text": "  "}]}
    doc, tidied = script.tidy(raw, HOSTS)
    assert doc["title"] == "Chapter khatam!" and len(doc["lines"]) == 14 and tidied == 2
    assert doc["lines"][0] == {"speaker": "Raju", "text": "Bhai finally chapter khatam!"}
    assert doc["lines"][1]["speaker"] == "Ram"                       # turn-taking decides an unknown speaker
    with pytest.raises(script.ScriptError, match="too short"):
        script.tidy({"lines": long_lines(3)}, HOSTS)
    with pytest.raises(script.ScriptError, match="only one"):
        script.tidy({"lines": [{"speaker": "Raju", "text": "x"}] * 14}, HOSTS)

    text = script.to_text(doc, HOSTS, "Rahul Sir")
    assert text.startswith("# Chapter khatam!\n# Two friends: Raju and Ram. Teacher: Rahul Sir.\n\nRaju: Bhai")
    assert script.parse(text, HOSTS) == doc
    edited = script.parse("# Mera title\n\nraju: one\nRam: two\n   still two\n\nRAJU: three", HOSTS)
    assert edited == {"title": "Mera title", "lines": [{"speaker": "Raju", "text": "one"},
                      {"speaker": "Ram", "text": "two still two"}, {"speaker": "Raju", "text": "three"}]}
    with pytest.raises(script.ScriptError, match='Line 2 is said by "Rahul Sir"'):
        script.parse("Raju: hi\nRahul Sir: hello", HOSTS)
    with pytest.raises(script.ScriptError, match="Line 1 does not start"):
        script.parse("hello\nRaju: x", HOSTS)
    with pytest.raises(script.ScriptError, match="at least two lines"):
        script.parse("Raju: alone", HOSTS)


def test_the_checks_corrections_are_placed_only_where_they_fit():
    doc = {"title": "", "lines": [{"speaker": "Raju", "text": "E cell equals E cathode plus E anode."},
                                  {"speaker": "Ram", "text": "Haan bhai, Faraday ka pehla niyam."},
                                  {"speaker": "Raju", "text": "Sir ne kya mast padhaya."}]}
    found = {"corrections": [
        {"line": 1, "old": "E cell equals E cathode plus E anode.", "new": "E cell equals E cathode minus E anode.",
         "reason": "sign"},
        {"line": 2, "old": "pehla niyam", "new": "first law", "reason": "term in English"},
        {"line": 3, "old": "something else", "new": "x", "reason": "no match"},
        {"line": 9, "old": "a", "new": "b", "reason": "no such line"}], "missing": [{"topic": " Nernst  equation "}]}
    outcome = script.apply_review(doc, found, HOSTS)
    assert [line["text"] for line in doc["lines"]] == [
        "E cell equals E cathode minus E anode.", "Haan bhai, Faraday ka first law.", "Sir ne kya mast padhaya."]
    assert [e["line"] for e in outcome["applied"]] == [1, 2] and [e["line"] for e in outcome["skipped"]] == [3, 9]
    assert outcome["missing"] == ["Nernst equation"] and outcome["done"]


def test_audio_joins_in_python_whatever_form_each_part_comes_in():
    lines = [{"speaker": HOSTS[n % 2], "text": "x" * 500} for n in range(10)]
    groups = voice.parts(lines)
    assert [len(g) for g in groups] == [3, 3, 3, 1] and sum(groups, []) == lines
    raw = voice.decode(b"\x01\x00" * RATE, "audio/L16;codec=pcm;rate=24000")
    assert (raw.kind, raw.rate, raw.channels) == ("pcm", RATE, 1)
    from_wav = voice.decode(wav(0.5))
    slower = voice.decode(b"\x01\x00" * 16000, "audio/pcm;rate=16000")            # 1 s at 16 kHz
    audio, ext, seconds = voice.join([raw, from_wav, slower])
    assert ext == "wav" and seconds == pytest.approx(1 + 0.35 + 0.5 + 0.35 + 1, abs=0.05)
    with wave.open(io.BytesIO(audio)) as found:
        assert (found.getframerate(), found.getnchannels(), found.getsampwidth()) == (RATE, 1, 2)
    tagged = b"ID3\x04\x00\x00\x00\x00\x00\x05abcde" + b"\xff\xfb\x90\x00frame"
    mp3 = voice.decode(tagged, "audio/mpeg")
    assert mp3.kind == "mp3" and mp3.data == b"\xff\xfb\x90\x00frame"
    assert voice.join([mp3, mp3]) == (b"\xff\xfb\x90\x00frame" * 2, "mp3", None)
    with pytest.raises(voice.VoiceError, match="different audio forms"):
        voice.join([raw, mp3])
    with pytest.raises(voice.VoiceError, match="cannot join"):
        voice.decode(b"OggS....", "audio/ogg")


def test_the_episode_is_an_mp3_made_in_the_app_and_a_wav_without_the_library(monkeypatch):
    """MP3 like the team's reference (44.1 kHz), written by the bundled lameenc: no program on the computer."""
    pcm = voice.decode(b"".join(int(3000 * ((n // 40) % 2 * 2 - 1)).to_bytes(2, "little", signed=True)
                                for n in range(RATE * 10)), "audio/L16;rate=24000")      # 10 s of tone
    audio, ext, seconds = voice.join([pcm], mp3=True)
    assert ext == "mp3" and audio[:2] == MP3_SYNC and seconds == 10.0
    assert len(audio) == pytest.approx(10 * voice.MP3_KBPS * 1000 / 8, rel=0.1)         # 96 kbps: ~11 MB a quarter hour
    frames = audio.count(b"\xff\xfb")
    assert frames >= 10 * voice.MP3_RATE / 1152                                          # 1152 samples a frame
    monkeypatch.setattr(voice, "mp3_of", lambda pcm_, rate, channels: None)              # no library here
    audio, ext, seconds = voice.join([pcm], mp3=True)
    assert ext == "wav" and audio[:4] == b"RIFF" and seconds == 10.0
    assert voice.join([pcm])[1] == "wav"                                                  # a saved part stays lossless


class FakeSession:
    def __init__(self, native_code: str = ""):
        self.native_code, self.native, self.single = native_code, [], []

    def speech(self, kind, models_, request):
        from note_studio.typeset.ai import AIError

        self.native.append(request)
        if self.native_code:
            raise AIError("PW AI failed (status 400)", self.native_code)
        return b"\x01\x00" * 2400, "audio/L16;codec=pcm;rate=24000"

    def speech_line(self, kind, models_, text, voice_name):
        self.single.append((text, voice_name))
        return wav(0.05), "audio/wav"


def test_two_voices_per_call_and_what_was_spoken_is_never_paid_for_twice(tmp_path):
    session, steps = FakeSession(), []
    lines = long_lines()
    done = voice.speak(lambda: session, lines, HOSTS, ("Puck", "Fenrir"), ["m"], tmp_path, lambda t, p=None: steps.append(t))
    assert done["parts"] == len(session.native) == 3 and done["reused_parts"] == 0 and done["route"] == "two_voices"
    request = session.native[0]
    speakers = request["generationConfig"]["speechConfig"]["multiSpeakerVoiceConfig"]["speakerVoiceConfigs"]
    assert [(s["speaker"], s["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]) for s in speakers] == [
        ("Raju", "Puck"), ("Ram", "Fenrir")]
    assert request["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert "\n\nRaju: Line 1:" in request["contents"][0]["parts"][0]["text"]
    assert done["audio"][:2] == MP3_SYNC and done["ext"] == "mp3"

    again = voice.speak(lambda: session, lines, HOSTS, ("Puck", "Fenrir"), ["m"], tmp_path, lambda t, p=None: None)
    assert again["reused_parts"] == 3 and len(session.native) == 3 and again["audio"] == done["audio"]
    edited = [dict(line) for line in lines]
    edited[-1]["text"] = edited[-1]["text"].replace("bhai", "yaar")              # the last group only
    voice.speak(lambda: session, edited, HOSTS, ("Puck", "Fenrir"), ["m"], tmp_path, lambda t, p=None: None)
    assert len(session.native) == 4
    voice.speak(lambda: session, lines, HOSTS, ("Achird", "Fenrir"), ["m"], tmp_path, lambda t, p=None: None)
    assert len(session.native) == 7                                            # another voice: spoken again


def test_when_pw_ai_refuses_two_voices_each_line_is_spoken_in_its_friends_voice(tmp_path):
    session, steps = FakeSession("unsupported"), []
    lines = long_lines()
    done = voice.speak(lambda: session, lines, HOSTS, ("Puck", "Fenrir"), ["m"], tmp_path, lambda t, p=None: steps.append(t))
    assert len(session.native) == 1 and len(session.single) == len(lines)      # asked once, then line by line
    assert [v for _t, v in session.single[:2]] == ["Puck", "Fenrir"] and done["route"] == "line_by_line"
    assert any("line by line" in s for s in steps) and done["ext"] == "mp3"
    first, second = session.single[0][0], session.single[1][0]                # led by how to say it
    assert first.startswith("Say this as Raju, a young Indian man") and first.endswith(": " + lines[0]["text"])
    assert second.startswith("Say this as Ram, a young Indian man") and "Hinglish" in second


# ------------------------------------------------------------------ through the app
def test_a_chapter_becomes_one_run_with_audio_script_and_merged_pdf(client, pw):
    response = _post(client)
    assert response.status_code == 303 and "the chapter (3 lectures)" in unquote(response.headers["location"])
    assert "not added twice" in unquote(_post(client).headers["location"])
    assert job_queue.wait_idle(120)
    [run] = _runs()
    assert run["state"] == "complete", (run["error"], run["message"])
    assert run["source"] == "audio" and run["title"] == "Electrochemistry" and run["engine"] == "audio"
    assert pw.kinds() == ["script", "check", "voice", "voice", "voice"]
    first = pw.calls[0]
    assert first["model"] == "gemini-3.7-flash" and first["token"] == "test-token" and first["count"] == 3
    parts = first["request"]["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "application/pdf"
    assert parts[1]["text"] == script.prompt_text()                           # the team's prompt, as it is
    assert parts[2]["text"].splitlines()[1:] == ["Lecture 1: Lecture 1 (page 1)", "Lecture 2: Lecture 2 (page 2)",
                                                 "Lecture 3: Lecture 10 (page 3)"]
    assert "about 15 minutes" in first["request"]["systemInstruction"]["parts"][0]["text"]
    assert pw.calls[2]["model"] == "gemini-3.1-flash-tts-preview"
    assert run["cost_inr"] == pytest.approx(2.0 + 2.0 + 3 * 1.0)
    assert run["message"].startswith("Done:")

    files = _files(run)
    assert list(files) == ["Electrochemistry Audio notes.mp3", "Electrochemistry Audio script.txt",
                           "Electrochemistry merged notes.pdf", "Quality report.json"]
    assert files["Electrochemistry Audio notes.mp3"].read_bytes()[:2] == MP3_SYNC
    text = files["Electrochemistry Audio script.txt"].read_text("utf-8")
    assert text.startswith("# Electrochemistry ka full revision") and "\nRaju: Ram ne sahi bola: Faraday's first law.\n" in text
    report = json.loads(files["Quality report.json"].read_text("utf-8"))
    assert report["verdict"] == "passed" and report["prompt_sha256"] == script.PROMPT_SHA256
    assert [c["check"] for c in report["checks"]] == ["Lectures joined", "Script", "Checked against the notes",
                                                      "Topics the episode does not mention", "Audio"]
    assert report["review"]["missing"] == ["Kohlrausch law"]

    page = client.get(f"/runs/{run['id']}").text
    assert "<audio" in page and f'action="/runs/{run["id"]}/audio"' in page and "Make the audio" in page
    assert "Check these notes" not in page and 'href="/audio?category=neet_jee"' in page and "Traceback" not in page
    item = db.query("SELECT id FROM run_items WHERE run_id=? AND label LIKE '%.mp3'", (run["id"],))[0]["id"]
    assert client.get(f"/runs/{run['id']}/files/{item}").content[:2] == MP3_SYNC


def test_a_fixed_script_is_spoken_again_paying_only_for_what_changed(client, pw):
    _post(client)
    assert job_queue.wait_idle(120)
    [run] = _runs()
    text = _files(run)["Electrochemistry Audio script.txt"].read_text("utf-8")
    bad = client.post(f"/runs/{run['id']}/audio", data={"script": "Raju: hi\nRahul Sir: hello"}, follow_redirects=False)
    assert "kind=warn" in bad.headers["location"] and "Rahul Sir" in unquote(bad.headers["location"])
    lines = text.rstrip("\n").split("\n")
    lines[-1] = lines[-1].replace("chamak", "chamka")
    fixed = client.post(f"/runs/{run['id']}/audio", data={"script": "\n".join(lines)}, follow_redirects=False)
    assert fixed.headers["location"].startswith("/queue")
    assert job_queue.wait_idle(120)
    first, second = _runs()
    assert second["state"] == "complete" and second["title"] == "Electrochemistry (audio)", second["error"]
    assert pw.kinds()[5:] == ["voice"]                                       # only the changed group
    assert second["cost_inr"] == pytest.approx(1.0)
    assert json.loads(second["options_json"])["voice_only"] is True
    assert list(_files(second)) == ["Electrochemistry Audio notes.mp3", "Electrochemistry Audio script.txt",
                                    "Quality report.json"]
    assert "chamka" in _files(second)["Electrochemistry Audio script.txt"].read_text("utf-8")


def test_script_first_then_the_audio(client, pw):
    _post(client, audio_step="after_check")
    assert job_queue.wait_idle(120)
    [run] = _runs()
    assert run["state"] == "complete" and pw.kinds() == ["script", "check"]
    assert run["message"].startswith("The script is ready")
    assert not any(name.endswith((".wav", ".mp3")) for name in _files(run))
    text = _files(run)["Electrochemistry Audio script.txt"].read_text("utf-8")
    client.post(f"/runs/{run['id']}/audio", data={"script": text})
    assert job_queue.wait_idle(120)
    assert _runs()[1]["state"] == "complete" and pw.kinds()[2:] == ["voice", "voice", "voice"]


def test_a_run_that_stops_at_the_voice_keeps_its_script_and_try_again_reuses_it(client, pw):
    pw.native_error = "gemini native proxy error 503: busy"
    pw.line_error = "gemini tts proxy error 503: busy"
    _post(client)
    assert job_queue.wait_idle(120)
    [run] = _runs()
    assert run["state"] == "failed" and "The script is ready" in run["error"] and "Try again" in run["error"]
    assert "Electrochemistry Audio script.txt" in _files(run)
    paid = pw.kinds().count("script")
    pw.native_error = pw.line_error = ""
    again = client.post(f"/runs/{run['id']}/again", follow_redirects=False)
    assert again.headers["location"].startswith("/queue")
    assert job_queue.wait_idle(120)
    retried = _runs()[1]
    assert retried["state"] == "complete", retried["error"]
    assert pw.kinds().count("script") == paid == 1                           # the script was not paid for again
    events = [e["text"] for e in db.query("SELECT text FROM run_events WHERE run_id=?", (retried["id"],))]
    assert any("Reusing the script" in e for e in events)


def test_pw_ai_closing_the_two_voice_route_to_the_model_is_not_a_sign_in_problem(client, pw):
    # PW AI's answer on 27 Nov 2026 for the speech models on /api/gemini/native (the account was allowed)
    pw.native_error = ('gemini native proxy error 403: {"error":"gemini native error","detail":"{\\"error\\":'
                       '{\\"message\\":\\"The requested model \'gemini-3.1-flash-tts-preview\' is not available for '
                       'this API key, or the model name is invalid.\\",\\"type\\":\\"key_model_access_denied\\"}}"}')
    _post(client)
    assert job_queue.wait_idle(120)
    [run] = _runs()
    assert run["state"] == "complete", run["error"]
    assert "Electrochemistry Audio notes.mp3" in _files(run)
    voices = [c for c in pw.calls if c["kind"] == "voice"]
    assert len(voices) == 2                                  # both models asked once, then never again
    assert pw.kinds().count("voice line") == len(pw.lines)
    events = [e["text"] for e in db.query("SELECT text FROM run_events WHERE run_id=?", (run["id"],))]
    assert not any("PW list" in e for e in events) and any("line by line" in e for e in events)


def test_a_refused_account_still_stops_the_run_in_plain_words(client, pw):
    pw.native_error = 'gemini native proxy error 403: {"error":"not allowed"}'
    _post(client)
    assert job_queue.wait_idle(120)
    [run] = _runs()
    assert run["state"] == "failed" and "not on the PW list" in run["error"]
    assert pw.kinds().count("voice line") == 0


@pytest.mark.parametrize("changes, words", [
    ({"host_two": "Raju"}, "different names"),
    ({"voice_two": "Puck"}, "two different voices"),
    ({"host_one": "Raju: 1"}, "letters only"),
    ({"teacher_name": ""}, "fill in teacher name"),
])
def test_the_form_says_what_is_wrong_in_plain_words(client, changes, words):
    response = _post(client, **changes)
    assert response.status_code == 400 and words in response.text.lower()
    assert "<h1>Audio notes</h1>" in response.text                         # still on the Audio notes page
    assert not job_queue.counts().get("waiting")


def test_each_lecture_is_added_once_and_only_pdfs(client):
    same = client.post("/new", data=FORM, files=[("files", ("Lecture 1.pdf", pdf("one"), "application/pdf")),
                                                 ("files", ("Copy.pdf", pdf("one"), "application/pdf"))])
    assert same.status_code == 400 and "are the same file" in same.text
    word = client.post("/new", data=FORM, files=[("files", ("Lecture 1.docx", b"PK\x03\x04", "application/zip"))])
    assert word.status_code == 400 and "is not a PDF file" in word.text


def _zip(entries: dict, *, locked: str = "") -> bytes:
    """A ZIP of {name: bytes}; ``locked`` marks that entry as password-locked (the flag is what counts)."""
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    data = bytearray(buffer.getvalue())
    if locked:
        for marker in (b"PK\x03\x04", b"PK\x01\x02"):                    # local header and central directory
            start = 0
            while (at := data.find(marker, start)) >= 0:
                flags = at + (6 if marker == b"PK\x03\x04" else 8)
                name_at = at + (30 if marker == b"PK\x03\x04" else 46)
                if bytes(data[name_at:name_at + len(locked)]) == locked.encode():
                    data[flags] |= 0x1
                start = at + 4
    return bytes(data)


def test_a_zip_of_the_chapter_is_opened_in_the_app_in_lecture_order(client, pw):
    chapter_zip = _zip({"Electrochemistry/Lecture 10.pdf": pdf("ten"), "Electrochemistry/Lecture 2.pdf": pdf("two"),
                        "Electrochemistry/Lecture 1.pdf": pdf("one"), "Electrochemistry/notes.txt": b"not a lecture",
                        "__MACOSX/Electrochemistry/._Lecture 1.pdf": b"mac", "Electrochemistry/.hidden.pdf": b"x"})
    form = {**FORM, "chapter_title": ""}
    response = client.post("/new", data=form, files=[("files", ("Electrochemistry.zip", chapter_zip, "application/zip"))],
                           follow_redirects=False)
    assert response.status_code == 303 and "the chapter (3 lectures)" in unquote(response.headers["location"])
    assert job_queue.wait_idle(120)
    [run] = _runs()
    assert run["state"] == "complete", run["error"]
    assert run["title"] == "Electrochemistry"                            # the ZIP named the chapter
    options = json.loads(run["options_json"])
    assert options["lectures"] == ["Lecture 1.pdf", "Lecture 2.pdf", "Lecture 10.pdf"]
    assert "Electrochemistry Audio notes.mp3" in _files(run)


def test_folders_inside_a_zip_lead_the_names_and_keep_the_order():
    from note_studio import audio_service

    found = audio_service.unpack([("Week.zip", _zip({"Chapter 5/Day 2/notes.pdf": pdf("two"),
                                                     "Chapter 5/Day 10/notes.pdf": pdf("ten"),
                                                     "Chapter 5/Day 1/notes.pdf": pdf("one")})),
                                  ("Extra.pdf", pdf("extra"))])
    names = [name for name, _data in found]
    assert names == ["Day 2 - notes.pdf", "Day 10 - notes.pdf", "Day 1 - notes.pdf", "Extra.pdf"]
    assert sorted(names[:3], key=chapter.lecture_order) == ["Day 1 - notes.pdf", "Day 2 - notes.pdf",
                                                            "Day 10 - notes.pdf"]
    assert dict(found)["Day 1 - notes.pdf"] == pdf("one")
    single = audio_service.unpack([("One.zip", _zip({"Only lecture.pdf": pdf("one")}))])
    assert [n for n, _d in single] == ["Only lecture.pdf"]


@pytest.mark.parametrize("upload, words", [
    (_zip({"readme.txt": b"hello"}), "has no PDF inside"),
    (b"PK\x03\x04 broken", "is not a ZIP file that opens"),
    (_zip({"Lecture 1.pdf": b"%PDF-1.4 locked"}, locked="Lecture 1.pdf"), "locked with a password"),
    (_zip({f"Lecture {n}.pdf": pdf(f"z{n}") for n in range(1, 23)}), "one chapter takes at most 20"),
])
def test_a_zip_that_cannot_be_used_is_named_in_plain_words(client, upload, words):
    response = client.post("/new", data=FORM, files=[("files", ("Chapter.zip", upload, "application/zip"))])
    assert response.status_code == 400 and words in response.text and "Traceback" not in response.text
    assert "Make audio notes" in response.text                               # still the audio page
    assert not _runs()
