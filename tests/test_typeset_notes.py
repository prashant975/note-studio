"""B.Sc. typeset notes (note_studio/typeset): the document is tidied, drawn in the
sample notes' layout, and made end to end from New notes with a stubbed PW AI
(no network, no cost): notes, the check against the lecture, figures and their
label check, the quality report, sign-in and budget failures."""
from __future__ import annotations

import base64
import copy
import io
import json
import re
from pathlib import Path

import pytest

from note_studio import categories, db, job_queue, make_service, note_types, requirements
from note_studio.typeset import document, formulas, prompts, render

FORM = {"category": "bsc", "note_type": "typeset_notes", "subject": "botany", "language": "english",
        "lecture_title": "", "figures": "6", "review": "on", "content_mode": "rewrite"}

DOC = {
    "title": "*Oedogonium*",
    "subject": "Botany",
    "blocks": [
        {"kind": "section", "text": "4. Systematic Position"},
        {"kind": "paragraph", "text": "*Oedogonium* is a filamentous green alga of [[quiet fresh water]]."},
        {"kind": "box", "style": "definition", "title": "Definition: Thallus",
         "body": [{"kind": "paragraph", "text": "A plant body not divided into root, stem and leaves."}]},
        {"kind": "bullets", "items": [{"text": "**Class:** Chlorophyceae"}, {"text": "**Order:** Oedogoniales",
                                       "sub": ["**Family:** Oedogoniaceae"]}]},
        {"kind": "box", "style": "formula", "title": "Cap cells",
         "body": [{"kind": "math", "text": "$n_{caps} = n_{divisions}$"},
                  {"kind": "paragraph", "text": "where $n$ counts the divisions of the cell."}]},
        {"kind": "table", "columns": ["Form", "Male filament", "Example"],
         "rows": [["Macrandrous", "Normal size", "*O. crispum*"], ["Nannandrous", "Dwarf male", "*O. concatenatum*"]],
         "title": "Table 1: Sexual dimorphism"},
        {"kind": "figure", "title": "Figure 2: Structure of a filament", "description": "A vertical filament with an "
         "apical cell, intercalary cells, a cap cell and a basal holdfast.", "labels": ["Apical cell", "Holdfast"],
         "source_page": 3},
        {"kind": "tree", "nodes": [{"id": "r", "label": "Reproduction"}, {"id": "v", "label": "Vegetative", "parent": "r"},
                                   {"id": "a", "label": "Asexual", "parent": "r"}, {"id": "s", "label": "Sexual", "parent": "r"}]},
        {"kind": "flow", "title": "Life cycle", "cycle": True, "items": [{"text": "Filament ($n$)"}, {"text": "Gametes"},
                                                                          {"text": "Oospore ($2n$)"}]},
        {"kind": "box", "style": "warning", "title": "Common Mistake: Caps", "body": [{"kind": "paragraph",
                                                                                     "text": "Caps mark divisions, not age (of the cell."}]},
        {"kind": "mystery", "text": "dropped"},
        {"kind": "box", "style": "summary", "body": [{"kind": "bullets", "items": [{"text": "Haplontic life cycle."},
                                                                                   {"text": "Oogamous reproduction."}]}]},
    ],
}
OK_LABELS = {"misspelled": [], "missing": [], "extra": [], "usable": True}


def _png(width=800, height=500) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 80, 300, 420), outline="darkgreen", width=6, fill="lightgreen")
    draw.ellipse((420, 120, 700, 380), outline="navy", width=5)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class FakePW:
    """pw_access.gemini_generate / gemini_image, answering by the request's schema."""

    def __init__(self):
        self.calls: list[dict] = []
        self.notes = copy.deepcopy(DOC)
        self.notes["blocks"] = [b for b in self.notes["blocks"] if b["kind"] != "mystery"]
        self.review = {"corrections": [], "missing": []}
        self.labels: list[dict] = []

    def gemini_generate(self, token_provider, *, model, request, filename="", input_unit="", count=None, task_id=""):
        keys = set(((request.get("generationConfig") or {}).get("responseSchema") or {}).get("properties") or {})
        if "blocks" in keys:
            kind, obj = "notes", self.notes
        elif "corrections" in keys:
            kind, obj = "review", self.review
        else:
            kind, obj = "label check", self.labels.pop(0) if self.labels else OK_LABELS
        self.calls.append({"kind": kind, "model": model, "request": request, "token": token_provider(),
                           "task_id": task_id, "count": count})
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}, "finishReason": "STOP"}],
                "cost_inr": 1.5}

    def gemini_image(self, token_provider, *, prompt, model, filename="", task_id="", **kw):
        self.calls.append({"kind": "figure", "model": model, "prompt": prompt})
        return {"ok": True, "result": {"image_base64": base64.b64encode(_png()).decode(), "content_type": "image/png"},
                "cost_inr": 3.0}


@pytest.fixture
def pw(monkeypatch):
    from note_studio import engine_catalog
    from note_studio.typeset import ai

    engine_catalog.put_shortnotes_on_path()
    import pw_access

    fake = FakePW()
    monkeypatch.setattr(pw_access, "gemini_generate", fake.gemini_generate)
    monkeypatch.setattr(pw_access, "gemini_image", fake.gemini_image)
    monkeypatch.setattr(make_service, "typeset_token", lambda: (lambda: "test-token"))
    monkeypatch.setattr(ai, "RETRY_PAUSE", 0.0)
    yield fake
    job_queue.wait_idle(120)


def _post(client, pdf, name="Lect 13.pdf", **changes):
    form = {k: v for k, v in {**FORM, **changes}.items() if v is not None}
    return client.post("/new", data=form, files=[("files", (name, pdf, "application/pdf"))], follow_redirects=False)


def _run():
    rows = db.query("SELECT * FROM runs WHERE note_type='typeset_notes' ORDER BY id DESC")
    return rows[0] if rows else None


def _report(run) -> dict:
    return json.loads((Path(run["run_dir"]) / "quality_report.json").read_text(encoding="utf-8"))


def _pdf_text(path) -> tuple[str, int, set]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    fonts = set()
    for page in reader.pages:
        for font in (page.get("/Resources") or {}).get("/Font", {}).values():
            fonts.add(str(font.get_object().get("/BaseFont")))
    return "\n".join(p.extract_text() for p in reader.pages), len(reader.pages), fonts


# ------------------------------------------------------------- document
def test_the_models_json_is_tidied_before_it_is_drawn():
    doc, issues = document.tidy(DOC)
    kinds = [b["kind"] for b in doc["blocks"]]
    assert "mystery" not in kinds and len(kinds) == len(DOC["blocks"]) - 1
    assert doc["blocks"][0]["text"] == "Systematic Position"                       # "4. " was typed in
    assert doc["blocks"][2]["title"] == "Thallus"                                  # "Definition:" is added when drawn
    assert doc["blocks"][5]["title"] == "Sexual dimorphism" and doc["blocks"][6]["title"] == "Structure of a filament"
    assert doc["blocks"][9]["body"][0]["text"].endswith("(of the cell)")           # the bracket left open is closed
    assert doc["blocks"][-1]["title"] == "Key Takeaways"
    assert any("removed the number" in i for i in issues) and any("left out" in i for i in issues)


@pytest.mark.parametrize("heading, kept", [("E. coli Genetics", "E. coli Genetics"), ("V. cholerae", "V. cholerae"),
                                           ("3.4 Cell Wall", "Cell Wall"), ("II. Types", "Types"),
                                           ("(a) Habitat", "Habitat"), ("5 Kingdom System", "5 Kingdom System")])
def test_heading_numbers_go_but_genus_abbreviations_stay(heading, kept):
    doc, _ = document.tidy({"title": "T", "blocks": [{"kind": "section", "text": heading}]})
    assert doc["blocks"][0]["text"] == kept


def test_a_tree_with_a_loop_or_two_roots_still_has_one_root():
    nodes = [{"id": "a", "label": "A", "parent": "b"}, {"id": "b", "label": "B", "parent": "a"},
             {"id": "c", "label": "C"}, {"id": "d", "label": "D"}]
    doc, _ = document.tidy({"title": "T", "blocks": [{"kind": "tree", "title": "Types", "nodes": nodes}]})
    tree = doc["blocks"][0]["nodes"]
    roots = [n for n in tree if not n["parent"]]
    assert len(roots) == 1 and len(tree) == 5
    ids = [n["id"] for n in tree]
    assert all(not n["parent"] or ids.index(n["parent"]) < ids.index(n["id"]) for n in tree)   # parents first


def test_inline_markup_and_a_stray_marker():
    spans = document.parse_inline("A **bold** *Genus* [[key term]] ==fact== $x^2$ and a lone * star")
    styles = {s.text.strip(): s for s in spans if s.text.strip()}
    assert styles["bold"].bold and styles["Genus"].italic and styles["key term"].key and styles["fact"].mark
    assert styles["x^2"].math
    assert "lone * star" in "".join(s.text for s in spans)


def test_a_correction_applies_only_to_the_text_it_was_made_for():
    doc, _ = document.tidy(DOC)
    pointer, text = next((p, t) for p, t in document.texts(doc) if "quiet fresh water" in t)
    assert document.replace(doc, pointer, text, text.replace("quiet", "still"))
    assert not document.replace(doc, pointer, text, "again")                      # it changed meanwhile
    assert not document.replace(doc, "/title", doc["title"], "x")                 # only block text is corrected


@pytest.mark.parametrize("latex, spaced", [
    (r"a=b", r"a\;=\;b"), (r"x_{i+1} = -y", r"x_{i+1}\;=\;-y"), (r"(-1)^n", r"(-1)^n"),
    (r"\frac{a+b}{c}", r"\frac{a+b}{c}"), (r"1\,\mathrm{kg} + 2", r"1\,\mathrm{kg}\>+\>2"),
    (r"P(x, y)", r"P(x,\, y)"), (r"\text{if } x = 2", r"\text{if } x\;=\;2")])
def test_formulas_get_texs_spacing(latex, spaced):
    assert formulas.spaced(latex).replace(" ", "") == spaced.replace(" ", "")


def test_a_formula_mathtext_cannot_draw_is_refused():
    assert formulas.render(r"\vec{r} = x\hat{i}", 10.91).width > 10
    with pytest.raises(formulas.FormulaError):
        formulas.render(r"\begin{matrix} a \end{matrix}", 10.91)


# ------------------------------------------------------------- drawing
def _render(doc, figures_dir=None):
    tidied, _ = document.tidy(doc)
    for block, image in zip(document.figures(tidied), [b.get("image") for b in doc["blocks"] if b["kind"] == "figure"]):
        if image:
            block["image"] = image
    return render.render_pdf(tidied, render.Branding(), figures_dir)


def test_the_pdf_has_the_sample_notes_layout(tmp_path):
    (tmp_path / "fig.jpg").write_bytes(_png())
    doc = copy.deepcopy(DOC)
    doc["blocks"][6]["image"] = "fig.jpg"
    data, info = _render(doc, tmp_path)
    (tmp_path / "notes.pdf").write_bytes(data)
    text, pages, fonts = _pdf_text(tmp_path / "notes.pdf")
    for words in ("B.Sc. Wallah", "B.Sc. Notes", "Concept • Understand • Apply", "Oedogonium", "Systematic Position",
                  "Definition 1: Thallus", "Table 1: Sexual dimorphism", "Figure 1: Structure of a filament",
                  "Common Mistake: Caps", "Key Takeaways", "Family:"):
        assert words in text, words
    assert re.search(r"\b1\s*Systematic Position", text)                          # numbered when drawn
    assert any("Lato" in f for f in fonts) and any("CMUSansSerif" in f for f in fonts)
    assert info["pages"] == pages and max(info["fills"]) <= 1.0 and info["issues"] == []
    assert info["figures"] == 1 and info["diagrams"] == 2 and info["tables"] == 1


def _page_texts(data: bytes) -> list[str]:
    from pypdf import PdfReader

    return [p.extract_text() for p in PdfReader(io.BytesIO(data)).pages]


def test_a_split_never_leaves_one_line_of_a_box_or_a_heading_alone():
    filler = [{"kind": "paragraph", "text": "A line of plain text that fills the page. " * 9} for _ in range(10)]
    points = [{"text": f"Point {n} of the takeaways."} for n in range(1, 15)]     # taller than a box that moves whole
    takeaways = {"kind": "box", "style": "summary", "body": [{"kind": "bullets", "items": points}]}
    splits = 0
    for extra in range(0, 16):                     # the box meets the page end at every height
        doc = {"title": "T", "blocks": [*filler, *[{"kind": "paragraph", "text": "One more line."}] * extra, takeaways]}
        data, info = _render(doc)
        assert max(info["fills"]) <= 1.0
        counts = [text.count("of the takeaways") for text in _page_texts(data)]
        shown = [c for c in counts if c]
        assert sum(shown) == 14 and all(c >= 2 for c in shown), (extra, counts)
        splits += len(shown) > 1
    assert splits                                  # some of them did split
    for extra in range(0, 12):                     # a heading always has its text below it
        doc = {"title": "T", "blocks": [*filler, *[{"kind": "paragraph", "text": "One more line."}] * extra,
                                        {"kind": "section", "text": "Last section"},
                                        {"kind": "paragraph", "text": "Its own text follows here. " * 12}]}
        data, _ = _render(doc)
        page = next(text for text in _page_texts(data) if "Last section" in text)
        assert "Its own text follows here." in page, extra


def test_hindi_is_drawn_with_a_devanagari_face(tmp_path):
    doc = {"title": "प्रकाश संश्लेषण", "blocks": [{"kind": "paragraph", "text": "हरे पौधों में **प्रकाश संश्लेषण** (Photosynthesis) होता है।"}]}
    data, info = _render(doc)
    (tmp_path / "h.pdf").write_bytes(data)
    _, _, fonts = _pdf_text(tmp_path / "h.pdf")
    assert any("Hind" in f for f in fonts) and info["issues"] == []


def test_the_branding_comes_from_the_category(tmp_path):
    branding = render.Branding("M.Sc. Wallah", "M.Sc. Notes", "M.Sc. Wallah", "Learn • Apply")
    data, _ = render.render_pdf(document.tidy(DOC)[0], branding)
    (tmp_path / "m.pdf").write_bytes(data)
    text, _, _ = _pdf_text(tmp_path / "m.pdf")
    assert "M.Sc. Notes" in text and "Learn • Apply" in text and "B.Sc." not in text


# ------------------------------------------------------- the category
def test_the_bsc_category_offers_the_typeset_notes(client):
    pack = categories.get("bsc")
    assert pack["label"] == "B.Sc." and pack["enabled"] is True and pack["note_types"] == ["typeset_notes"]
    entry = note_types.get("typeset_notes")
    assert [l["id"] for l in categories.language_choices(pack, entry)] == ["english", "hindi", "hinglish"]
    assert [s["id"] for s in categories.subject_choices(pack, entry)] == [
        "auto", "physics", "chemistry", "mathematics", "botany", "zoology", "other"]
    page = client.get("/new?category=bsc").text
    for words in ("B.Sc.", "Typeset notes (B.Sc. style)", "AI notes in the B.Sc. template",
                  "Lecture title", "Hinglish"):
        assert words in page, words
    rules = client.get("/new/rules?category=bsc&note_type=typeset_notes&subject=botany").text
    assert "categories/bsc/rules/botany.txt" in rules and "categories/bsc/rules/all-subjects.txt" in rules
    from note_studio import category_admin

    assert not [c for c in category_admin.check("bsc") if c["kind"] == "bad"]


def test_its_engine_and_models_are_in_settings(client):
    system = client.get("/settings/system").text
    assert "Typeset notes" in system and "Part of" in system
    ai_page = client.get("/settings/ai").text
    assert "Typeset notes (B.Sc. style)" in ai_page and "Drawing the figures" in ai_page


def test_the_prompt_is_the_engines_then_every_rule_layer():
    categories.seed()
    resolved = requirements.resolve("bsc", "typeset_notes", "botany", "english", extra="Keep every date.")
    assert [l.layer for l in resolved.applied()] == ["category", "subject", "run"]
    rules = resolved.text(*requirements.LAYERS[1:])
    text = prompts.notes(brand="B.Sc. Wallah", audience="B.Sc.", language="english", figures=6, rules=rules)
    assert text.index("THE BLOCKS") < text.index("RULES FOR THIS CATEGORY") < text.index("systematic position")
    assert text.rstrip().endswith("Keep every date.") and "Write in English." in text and "at most 6 figure" in text
    assert "RULES FOR" not in prompts.notes(brand="B", audience="B", language="hindi", figures=0, rules="")
    assert "{" not in prompts.figure("Draw {it}.", ["A"]).replace("{it}", "")


# --------------------------------------------------------- end to end
def test_a_lecture_becomes_typeset_notes(client, pw, lecture_pdf):
    response = _post(client, lecture_pdf)
    assert response.status_code == 303 and response.headers["location"].startswith("/queue")
    assert job_queue.wait_idle(120)
    run = _run()
    assert run["state"] == "complete", (run["error"], run["message"])
    assert [c["kind"] for c in pw.calls] == ["notes", "review", "figure", "label check"]
    assert run["cost_inr"] == pytest.approx(1.5 + 1.5 + 3.0 + 1.5)
    notes_call = pw.calls[0]
    assert notes_call["model"] == "gemini-3.7-flash" and notes_call["token"] == "test-token"
    assert pw.calls[2]["model"] == "gemini-3.1-flash-image" and '"Apical cell"' in pw.calls[2]["prompt"]
    parts = notes_call["request"]["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "application/pdf"
    system = notes_call["request"]["systemInstruction"]["parts"][0]["text"]
    assert "B.Sc. Wallah" in system and "systematic position" in system and "Write in English." in system

    folder = Path(run["run_dir"])
    pdf = folder / "Lect 13 Notes.pdf"
    text, pages, _ = _pdf_text(pdf)
    assert "Figure 1: Structure of a filament" in text and "Definition 1: Thallus" in text
    report = _report(run)
    assert report["verdict"] == "passed" and report["engine"] == "typeset" and report["revision"] == prompts.REVISION
    assert (folder / "figures" / "figure-01.jpg").is_file() and json.loads((folder / "notes.json").read_text("utf-8"))

    page = client.get(f"/runs/{run['id']}").text
    assert "Lect 13 Notes.pdf" in page and "Quality check report" in page and "Traceback" not in page
    link = re.search(rf'href="(/runs/{run["id"]}/files/\d+)"', page).group(1)
    download = client.get(link)
    assert download.status_code == 200 and download.content.startswith(b"%PDF")


def test_the_check_against_the_lecture_corrects_and_reports(client, pw, lecture_pdf):
    doc, _ = document.tidy(DOC)
    pointer, text = next((p, t) for p, t in document.texts(doc) if "quiet fresh water" in t)
    pw.review = {"corrections": [{"pointer": pointer, "old": text, "new": text.replace("quiet", "still"), "reason": "x"},
                                 {"pointer": pointer, "old": "not the text", "new": "y", "reason": "stale"}],
                 "missing": [{"topic": "Germination of the oospore", "page": 9}]}
    _post(client, lecture_pdf, figures="0")
    assert job_queue.wait_idle(120)
    run = _run()
    assert run["state"] == "needs_attention" and "quality report" in run["message"]
    assert [c["kind"] for c in pw.calls] == ["notes", "review"]                 # no figures asked for
    saved = json.dumps(json.loads((Path(run["run_dir"]) / "notes.json").read_text("utf-8")))
    assert "still fresh water" in saved and "quiet fresh water" not in saved and "Structure of a filament" not in saved
    checks = {c["check"]: c for c in _report(run)["checks"]}
    assert "1 correction(s) made, 1 not applied" in checks["Checked against the lecture"]["detail"]
    assert "Germination of the oospore (page 9)" in checks["Topics the lecture teaches that the notes leave out"]["detail"]


def test_a_misspelled_figure_is_drawn_again(client, pw, lecture_pdf):
    pw.labels = [{"misspelled": [{"found": "Holdfst", "expected": "Holdfast"}], "missing": [], "extra": [], "usable": True},
                 OK_LABELS]
    _post(client, lecture_pdf, review=None)
    assert job_queue.wait_idle(120)
    run = _run()
    assert run["state"] == "complete"
    figures = [c for c in pw.calls if c["kind"] == "figure"]
    assert len(figures) == 2 and '"Holdfst" should read "Holdfast"' in figures[1]["prompt"]
    assert [c["kind"] for c in pw.calls if c["kind"] in ("review",)] == []        # switched off
    assert _report(run)["figures"][0]["result"] == "ok"


def test_a_figure_that_stays_wrong_is_kept_but_needs_attention(client, pw, lecture_pdf):
    wrong = {"misspelled": [{"found": "Holdfst", "expected": "Holdfast"}], "missing": [], "extra": [], "usable": True}
    pw.labels = [wrong, wrong]
    _post(client, lecture_pdf)
    assert job_queue.wait_idle(120)
    run = _run()
    figure = _report(run)["figures"][0]
    assert run["state"] == "needs_attention" and figure["result"] == "attention" and figure["image"]


def test_without_sign_in_the_run_says_what_to_do_and_the_queue_holds(client, lecture_pdf):
    """The real sign-in check, which needs the Settings choice first."""
    _post(client, lecture_pdf)
    assert job_queue.wait_idle(60)
    run = _run()
    assert run["state"] == "failed" and "Settings" in run["error"] and run["cost_inr"] == 0
    assert job_queue.held() == "signin"


def test_a_spent_budget_stops_the_run_in_plain_words(client, pw, lecture_pdf, monkeypatch):
    import pw_access

    def spent(*args, **kwargs):
        raise pw_access.PWAccessError("gemini proxy error 429: {\"error\": \"Budget has been exceeded\"}")

    monkeypatch.setattr(pw_access, "gemini_generate", spent)
    _post(client, lecture_pdf)
    assert job_queue.wait_idle(60)
    run = _run()
    assert run["state"] == "failed" and "budget" in run["error"].lower() and run["cost_inr"] == 0


def test_a_refused_schema_is_asked_again_with_the_shape_in_words(client, pw, lecture_pdf, monkeypatch):
    import pw_access

    real = pw.gemini_generate
    asked = []

    def no_schemas(token_provider, *, model, request, **kwargs):
        config = request.get("generationConfig") or {}
        asked.append("responseSchema" in config)
        if "responseSchema" in config:
            raise pw_access.PWAccessError("gemini proxy error 400: schema too complex")
        config = {**config, "responseSchema": document.schema()}          # the fake answers by schema
        return real(token_provider, model=model, request={**request, "generationConfig": config}, **kwargs)

    monkeypatch.setattr(pw_access, "gemini_generate", no_schemas)
    _post(client, lecture_pdf, figures="0", review=None)
    assert job_queue.wait_idle(120)
    assert _run()["state"] == "complete" and asked == [True, False]
    request_text = pw.calls[0]["request"]["contents"][0]["parts"][1]["text"]
    assert '"kind": "section"' in request_text and '"style": "summary"' in request_text


def test_an_unreadable_answer_tries_the_next_model(client, pw, lecture_pdf, monkeypatch):
    import pw_access

    real = pw.gemini_generate
    seen = []

    def first_model_garbles(token_provider, *, model, request, **kwargs):
        seen.append(model)
        if model == "gemini-3.7-flash" and "blocks" in json.dumps(request.get("generationConfig")):
            return {"candidates": [{"content": {"parts": [{"text": "not json"}]}, "finishReason": "MAX_TOKENS"}],
                    "cost_inr": 0.5}
        return real(token_provider, model=model, request=request, **kwargs)

    monkeypatch.setattr(pw_access, "gemini_generate", first_model_garbles)
    _post(client, lecture_pdf, figures="0", review=None)
    assert job_queue.wait_idle(120)
    run = _run()
    assert run["state"] == "complete" and seen[:4] == ["gemini-3.7-flash"] * 3 + ["gemini-3.6-flash"]
    assert run["cost_inr"] == pytest.approx(3 * 0.5 + 1.5)
