"""The PW logo on B.Sc.-style (typeset) notes: Nursing and other categories added
from Settings carry it in the top-right corner and as the faded page watermark,
as the NEET-JEE notes do; the built-in B.Sc. Wallah look stays as it was; and
Settings -> Categories switches it per category. No model calls."""
from __future__ import annotations

import io
import json
from pathlib import Path

import fitz
import pytest
from PIL import Image
from pypdf import PdfReader

from note_studio import categories
from note_studio.engines import typeset
from note_studio.typeset import document, render

NURSING = render.Branding("Nursing", "Nursing Notes", "Nursing", "Concept • Understand • Apply", pw_logo=True)
LONG = ("Pulmonary ventilation moves air in and out of the lungs by pressure changes made by the respiratory "
        "muscles and the elastic recoil of the lungs and chest wall during each breath. ")
DOC = {"title": "UNIT 2", "subject": "Respiratory System", "blocks": [
    {"kind": "section", "text": "Respiratory System"},
    *[{"kind": "paragraph", "text": LONG * 3} for _ in range(9)],
    *[{"kind": "box", "style": "definition", "title": "Definition: Tidal volume",
       "body": [{"kind": "paragraph", "text": LONG * 2}]} for _ in range(4)],
    {"kind": "table", "columns": ["Volume", "Value"], "rows": [["Tidal volume", "500 mL"], ["Vital capacity", "4800 mL"]],
     "title": "Table 1: Lung volumes"},
]}


def _render(branding: render.Branding) -> bytes:
    data, info = render.render_pdf(document.tidy(DOC)[0], branding)
    assert info["issues"] == []
    return data


def _images(page) -> list[fitz.Rect]:
    return [fitz.Rect(info["bbox"]) for info in page.get_image_info()]


def _near(rect: fitz.Rect, x0, y0, x1, y1, slack=1.0) -> bool:
    return all(abs(a - b) <= slack for a, b in zip(rect, (x0, y0, x1, y1)))


def test_every_page_has_the_corner_logo_and_the_watermark_as_neet_jee():
    data = _render(NURSING)
    with fitz.open(stream=data, filetype="pdf") as pdf:
        assert pdf.page_count >= 2
        for page in pdf:
            images = _images(page)
            corner = [r for r in images if r.x0 >= render.LOGO_X - 1 and r.y1 <= render.LOGO_MARGIN + render.LOGO + 1]
            assert len(corner) == 1, page.number
            # 11 mm, 5 mm in from the top and right edges (the logo is a little taller than wide)
            assert corner[0].height == pytest.approx(render.LOGO, abs=0.5)
            assert corner[0].y0 == pytest.approx(render.LOGO_MARGIN, abs=0.5)
            assert corner[0].x1 <= render.PAGE_W - render.LOGO_MARGIN + 0.5
            middle = [r for r in images if r.height == pytest.approx(render.WATERMARK, abs=1)]
            assert len(middle) == 1, page.number
            centre = (middle[0].x0 + middle[0].x1) / 2, (middle[0].y0 + middle[0].y1) / 2
            assert centre == pytest.approx((render.PAGE_W / 2, render.PAGE_H / 2), abs=1)
            # the header's right label and rule end before the logo
            label = page.search_for("Nursing Notes")
            assert label and all(r.x1 < render.LOGO_X for r in label)
            rules = [p for d in page.get_drawings() for item in d["items"] if item[0] == "l"
                     for p in (item[1], item[2]) if abs(p.y - 22.1) < 0.5]
            assert rules and max(p.x for p in rules) <= render.LOGO_X - render.LOGO_GAP + 0.5


def test_the_watermark_is_faded_and_the_corner_logo_is_not():
    corner, faded = render.pw_logos()
    assert Image.open(io.BytesIO(corner)).getchannel("A").getextrema()[1] == 255
    assert Image.open(io.BytesIO(faded)).getchannel("A").getextrema()[1] <= round(255 * 0.5)
    assert render.pw_logo_path().is_file()


def test_the_logo_changes_no_words_and_the_bsc_look_has_none():
    plain = _render(render.Branding("Nursing", "Nursing Notes", "Nursing", "Concept • Understand • Apply"))
    logo = _render(NURSING)
    words = [[p.extract_text() for p in PdfReader(io.BytesIO(d)).pages] for d in (plain, logo)]
    assert words[0] == words[1]
    with fitz.open(stream=plain, filetype="pdf") as pdf:
        assert all(not _images(page) for page in pdf)
    with fitz.open(stream=_render(render.Branding()), filetype="pdf") as pdf:     # B.Sc. Wallah defaults
        assert all(not _images(page) for page in pdf)
        assert pdf[0].search_for("B.Sc. Notes")[0].x1 == pytest.approx(render.HEAD_RIGHT, abs=0.5)


def test_the_same_notes_render_to_the_same_bytes():
    assert _render(NURSING) == _render(NURSING)          # paid quality receipts stay reusable


@pytest.mark.parametrize("pack, wanted", [
    (None, False), ({}, False),
    ({"id": "bsc", "builtin": True, "branding": {"header_left": "B.Sc. Wallah"}}, False),
    ({"id": "nursing", "builtin": False, "branding": {"signature_label": "Teacher name", "watermark": True}}, True),
    ({"id": "nursing", "builtin": False, "branding": {"pw_logo": False}}, False),
    ({"id": "bsc", "builtin": True, "branding": {"pw_logo": True}}, True),
])
def test_which_categories_have_the_logo(pack, wanted):
    assert categories.pw_logo(pack) is wanted
    assert typeset.run_settings({}, pack or {}, "", "", "", {}).branding["pw_logo"] is wanted


def test_the_builtin_bsc_pack_keeps_its_look():
    assert categories.pw_logo(categories.get("bsc")) is False


def test_settings_switches_the_logo_per_category(client):
    added = client.post("/settings/categories/add", data={
        "label": "Test Nursing", "subjects": "Anatomy\nPhysiology\n", "languages": ["english", "hindi"],
        "note_types": ["typeset_notes"]}, follow_redirects=False)
    assert "kind=ok" in added.headers["location"]
    assert categories.pw_logo(categories.get("test_nursing")) is True             # a new category has it
    page = client.get("/settings/categories").text
    assert 'action="/settings/categories/test_nursing/pw_logo"' in page and "Remove the PW logo" in page
    assert 'action="/settings/categories/bsc/pw_logo"' in page and "Add the PW logo" in page
    assert 'action="/settings/categories/neet_jee/pw_logo"' not in page          # not a B.Sc.-style category

    off = client.post("/settings/categories/test_nursing/pw_logo", data={"on": "0"}, follow_redirects=False)
    assert "kind=ok" in off.headers["location"]
    folder = Path(categories.get("test_nursing")["folder"])
    meta = json.loads((folder / "category.json").read_text(encoding="utf-8"))
    assert meta["branding"]["pw_logo"] is False and meta["branding"]["signature_label"] == "Teacher name"
    assert "pw_logo" not in json.loads((folder / "category.json.bak").read_text(encoding="utf-8"))["branding"]
    assert categories.pw_logo(categories.get("test_nursing")) is False

    client.post("/settings/categories/test_nursing/pw_logo", data={"on": "1"})
    assert categories.pw_logo(categories.get("test_nursing")) is True
    gone = client.post("/settings/categories/no_such/pw_logo", data={"on": "1"}, follow_redirects=False)
    assert "kind=warn" in gone.headers["location"]
