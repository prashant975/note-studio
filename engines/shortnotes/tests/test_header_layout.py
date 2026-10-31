"""Header regressions: topical doodles and collision-free title furniture."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from notesbot import doodles
from notesbot import notes_pdf as notes_pdf_module
from notesbot.notes_pdf import NotesPDF, build_notes_pdf
from notesbot.prompts import Section, ShortNotes


def _notes(title: str, quote: str = "Learn the structure, not just the names."):
    return ShortNotes(
        lecture_no="", subject="Chemistry", title=title, quote=quote,
        sections=[Section(heading="Bonds", points=["**Atoms bond** for stability."])],
    )


def test_page_numbers_are_on_by_default_and_optional_on_every_page():
    for show in (True, False):
        kwargs = {} if show else {"show_page_numbers": False}
        pdf = NotesPDF(_notes("Revision", ""), "by Teacher", watermark=False, **kwargs)
        pdf.add_page()
        pdf.add_page()
        pages = PdfReader(io.BytesIO(bytes(pdf.output()))).pages
        for number, page in enumerate(pages, 1):
            text = page.extract_text()
            assert (f"Page {number}" in text) is show
            assert "by Teacher" in text


def test_chemical_bonding_gets_distinct_atom_and_chemistry_icons():
    icons = doodles.pick_icons("Chemical Bonding and Molecular Structure", count=2)

    assert len(icons) == 2
    assert len(set(icons)) == 2
    assert "atom" in icons
    assert any(icon in {"flask", "test_tube", "battery", "bolt"} for icon in icons)


def test_physics_header_icons_never_use_chemistry_glassware():
    icons = doodles.pick_icons(
        "Ray Optics and Optical Instruments", count=2, subject="Physics"
    )

    assert len(icons) == 2
    assert len(set(icons)) == 2
    assert not ({"flask", "test_tube"} & set(icons))


def test_unknown_subject_uses_neutral_fallbacks():
    icons = doodles.pick_icons("Unmatched Topic", count=2, subject="Other")

    assert icons == ["book", "star"]


def test_subject_fallback_icon_uses_the_subject_pool_order():
    assert doodles.subject_fallback_icon("Physics") == "bolt"
    assert doodles.subject_fallback_icon("Math") == "balance"
    assert doodles.subject_fallback_icon("Unmatched subject") == "book"


def test_long_title_text_bottom_stays_inside_recorded_chip():
    pdf = NotesPDF(_notes("Chemical Bonding and Molecular Structure in Coordination Compounds"), "by PW", watermark=False)
    pdf.add_page()

    chip = pdf._title_chip_bounds

    assert pdf._title_text_bottom <= chip[1] + chip[3]
    assert pdf._subtitle_top > chip[1] + chip[3]


def test_header_draws_one_subject_specific_doodle(monkeypatch):
    calls: list[str] = []
    picker_calls: list[tuple[str, int, str]] = []

    def pick_icons(topic, count=2, subject=""):
        picker_calls.append((topic, count, subject))
        return ["atom"]

    monkeypatch.setattr(doodles, "pick_icons", pick_icons)
    monkeypatch.setattr(doodles, "draw_icon", lambda _pdf, name, *_args: calls.append(name))
    pdf = NotesPDF(_notes("Chemical Bonding and Molecular Structure"), "by PW", watermark=False)

    pdf.add_page()

    assert picker_calls == [
        ("CHEMICAL BONDING AND MOLECULAR STRUCTURE Chemistry", 1, "Chemistry"),
    ]
    assert calls == ["atom"]


def test_header_draws_subject_safe_fallback_when_picker_returns_empty(monkeypatch):
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(doodles, "pick_icons", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        doodles,
        "draw_icon",
        lambda _pdf, name, _x, _y, size: calls.append((name, size)),
    )
    notes = ShortNotes(
        lecture_no="", subject="Physics", title="Ray Optics", quote="",
        sections=[Section(heading="Rays", points=["Light travels in straight lines."])],
    )

    NotesPDF(notes, "by PW", watermark=False).add_page()

    assert calls == [("bolt", 23.0)]


def test_page_one_left_topic_doodle_is_a_large_sticker(monkeypatch):
    placements: list[tuple[float, float, float]] = []
    monkeypatch.setattr(
        doodles,
        "draw_icon",
        lambda _pdf, _name, x, y, size: placements.append((x, y, size)),
    )
    pdf = NotesPDF(
        _notes("Molecular Orbital Theory", "Energy order tells the bonding story."),
        "by PW",
        watermark=False,
    )

    pdf.add_page()

    assert len(placements) == 1
    assert placements[0][2] == 23.0


def test_quote_cloud_has_no_speech_tail_polygon():
    class PolygonRecordingPDF(NotesPDF):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.polygons: list[list[tuple[float, float]]] = []

        def polygon(self, point_list, *args, **kwargs):
            self.polygons.append(point_list)
            return super().polygon(point_list, *args, **kwargs)

        def _cloud(self, *args, **kwargs):
            # The cloud itself is a polygon; isolate any extra polygon emitted
            # by _quote_bubble so this catches the retired bottom tail.
            return None

    pdf = PolygonRecordingPDF(_notes("MO Theory", ""), "by PW", watermark=False)
    pdf.add_page()
    pdf.polygons.clear()

    pdf._quote_bubble("Orbitals combine only when symmetry and energy agree.")

    assert pdf.polygons == []


def test_long_quote_is_not_silently_truncated_from_the_pdf():
    quote = (
        "Compare every orbital by symmetry energy overlap and phase before "
        "deciding whether its molecular combination is bonding or antibonding"
    )
    pdf = NotesPDF(_notes("Molecular Orbital Energy Ordering", quote), "by PW", watermark=False)
    pdf.add_page()
    extracted = PdfReader(io.BytesIO(bytes(pdf.output()))).pages[0].extract_text()

    assert " ".join(quote.split()) in " ".join(extracted.split())


def test_long_header_elements_reserve_non_overlapping_geometry(monkeypatch):
    title = (
        "Molecular Orbital Theory and Energy Ordering in Homonuclear "
        "and Heteronuclear Diatomic Species"
    )
    quote = (
        "Compare every orbital by symmetry energy overlap and phase before "
        "deciding whether its molecular combination is bonding or antibonding"
    )
    signature = "prepared carefully by Professor PW Sharma for revision"
    lecture = "Lecture 18 - Molecular Orbital Energy Ordering and Applications"
    subtitle = "( Class 12 Board and Competitive Examination Students )"
    bounds: dict[str, tuple[float, float, float, float]] = {}

    doodle_calls: list[tuple[str, float, float, float]] = []

    def record_icon(_pdf, name, x, y, size):
        doodle_calls.append((name, x, y, size))
        bounds["left_doodle"] = (x - size / 2, y - size / 2, size, size)

    monkeypatch.setattr(doodles, "draw_icon", record_icon)

    class HeaderRecordingPDF(NotesPDF):
        _inside_dry_run = False

        def _cloud(self, x, y, w, h, *args, **kwargs):
            # _cloud intentionally wobbles 2% wider and 10% taller than its
            # nominal ellipse; record the actual outer geometry.
            cx, cy = x + w / 2, y + h / 2
            rw, rh = w * 1.02, h * 1.10
            bounds["cloud"] = (cx - rw / 2, cy - rh / 2, rw, rh)
            return super()._cloud(x, y, w, h, *args, **kwargs)

        def multi_cell(self, w, h=None, text="", *args, **kwargs):
            if kwargs.get("dry_run"):
                self._inside_dry_run = True
                try:
                    return super().multi_cell(w, h, text, *args, **kwargs)
                finally:
                    self._inside_dry_run = False
            if not self._inside_dry_run and text in {signature, lecture, subtitle}:
                start_x, start_y = self.get_x(), self.get_y()
                result = super().multi_cell(w, h, text, *args, **kwargs)
                key = (
                    "signature" if text == signature
                    else "lecture" if text == lecture
                    else "subtitle"
                )
                bounds[key] = (start_x, start_y, w, self.get_y() - start_y)
                return result
            return super().multi_cell(w, h, text, *args, **kwargs)

        def cell(self, w=None, h=None, text="", *args, **kwargs):
            if text in {signature, lecture}:
                width = w or (self.w - self.r_margin - self.get_x())
                text_w = self.get_string_width(text)
                align = kwargs.get("align", "")
                text_x = self.get_x() + (width - text_w if align == "R" else 0)
                bounds["signature" if text == signature else "lecture"] = (
                    text_x, self.get_y(), text_w, h,
                )
            return super().cell(w, h, text, *args, **kwargs)

    notes = _notes(title, quote)
    object.__setattr__(notes, "lecture_no", lecture)
    object.__setattr__(notes, "audience", "Class 12 Board and Competitive Examination Students")
    pdf = HeaderRecordingPDF(notes, signature, watermark=False)
    pdf.add_page()
    bounds["title"] = pdf._title_chip_bounds

    expected = {
        "title", "subtitle", "cloud", "left_doodle", "signature", "lecture"
    }
    assert expected <= bounds.keys()
    assert len(doodle_calls) == 1

    def intersects(a, b) -> bool:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return (
            ax < bx + bw and bx < ax + aw
            and ay < by + bh and by < ay + ah
        )

    items = [(name, bounds[name]) for name in sorted(expected)]
    collisions = [
        (left_name, right_name)
        for index, (left_name, left_bounds) in enumerate(items)
        for right_name, right_bounds in items[index + 1:]
        if intersects(left_bounds, right_bounds)
    ]
    assert collisions == []
    assert pdf._body_top >= max(
        bounds["signature"][1] + bounds["signature"][3],
        bounds["lecture"][1] + bounds["lecture"][3],
    )


def test_long_continuation_header_wraps_title_and_signature_in_separate_regions():
    title = (
        "Molecular Orbital Theory and Energy Ordering in Homonuclear "
        "and Heteronuclear Diatomic Species"
    )
    signature = "prepared carefully by Professor PW Sharma for revision"
    bounds: dict[str, tuple[float, float, float, float]] = {}

    class SlimHeaderRecordingPDF(NotesPDF):
        _inside_dry_run = False

        def multi_cell(self, w, h=None, text="", *args, **kwargs):
            if kwargs.get("dry_run"):
                self._inside_dry_run = True
                try:
                    return super().multi_cell(w, h, text, *args, **kwargs)
                finally:
                    self._inside_dry_run = False
            if self.page_no() == 2 and not self._inside_dry_run and text in {title, signature}:
                start_x, start_y = self.get_x(), self.get_y()
                result = super().multi_cell(w, h, text, *args, **kwargs)
                bounds["title" if text == title else "signature"] = (
                    start_x, start_y, w, self.get_y() - start_y,
                )
                return result
            return super().multi_cell(w, h, text, *args, **kwargs)

        def cell(self, w=None, h=None, text="", *args, **kwargs):
            if self.page_no() == 2 and text in {title, signature}:
                width = w or (self.w - self.r_margin - self.get_x())
                text_w = self.get_string_width(text)
                align = kwargs.get("align", "")
                text_x = self.get_x() + (width - text_w if align == "R" else 0)
                bounds["title" if text == title else "signature"] = (
                    text_x, self.get_y(), text_w, h,
                )
            return super().cell(w, h, text, *args, **kwargs)

    pdf = SlimHeaderRecordingPDF(_notes(title, ""), signature, watermark=False)
    pdf.add_page()
    pdf.add_page()

    assert bounds.keys() == {"title", "signature"}
    tx, ty, tw, th = bounds["title"]
    sx, sy, sw, sh = bounds["signature"]
    assert not (
        tx < sx + sw and sx < tx + tw
        and ty < sy + sh and sy < ty + th
    )
    assert pdf._body_top >= max(ty + th, sy + sh)


def test_center_logo_watermark_is_independent_centered_and_drawn_once_per_page():
    class WatermarkRecordingPDF(NotesPDF):
        def __init__(self, *args, **kwargs):
            self.logo_images: list[tuple[int, float, float, float, float]] = []
            self.signature_marks: list[int] = []
            super().__init__(*args, **kwargs)

        def image(self, name, x=None, y=None, w=0, h=0, *args, **kwargs):
            if isinstance(name, io.BytesIO):
                self.logo_images.append((self.page_no(), x, y, w, h))
            return super().image(name, x=x, y=y, w=w, h=h, *args, **kwargs)

        def text(self, x, y, text=""):
            if text == self.signature:
                self.signature_marks.append(self.page_no())
            return super().text(x, y, text)

    pdf = WatermarkRecordingPDF(
        _notes("Molecular Orbital Theory", ""),
        "by PW",
        watermark=True,
        logo_watermark=True,
    )
    pdf.add_page()
    pdf.add_page()

    assert len(pdf.logo_images) == 2
    assert {page for page, *_ in pdf.logo_images} == {1, 2}
    for _page, x, y, width, height in pdf.logo_images:
        assert 115.0 <= width <= 130.0
        assert width == height
        assert abs((x + width / 2) - pdf.w / 2) < 0.01
        assert abs((y + height / 2) - pdf.h / 2) < 0.01
    assert {1, 2} <= set(pdf.signature_marks)


def test_center_logo_watermark_trims_fully_transparent_asset_padding():
    png = NotesPDF._load_logo_watermark()

    assert png is not None
    with Image.open(io.BytesIO(png)).convert("RGBA") as logo:
        assert logo.getchannel("A").getbbox() == (0, 0, *logo.size)


def test_center_logo_asset_is_the_approved_pw_artwork():
    payload = Path(notes_pdf_module.LOGO_WATERMARK_PATH).read_bytes()

    assert hashlib.sha256(payload).hexdigest() == (
        "ab779982fbe1a7b0addb46b1e846431ac4c1950718ecc4fec62d9aee96115c24"
    )


def test_build_notes_pdf_forwards_logo_watermark_option():
    def image_count(pdf_bytes: bytes) -> int:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return sum(
            1
            for page in reader.pages
            for item in page["/Resources"].get("/XObject", {}).values()
            if item.get_object().get("/Subtype") == "/Image"
        )

    kwargs = {
        "notes": _notes("Molecular Orbital Theory", ""),
        "signature": "by PW",
        "watermark": False,
    }
    without_logo = build_notes_pdf(**kwargs, logo_watermark=False)
    with_logo = build_notes_pdf(**kwargs, logo_watermark=True)

    assert image_count(with_logo) > image_count(without_logo)


def test_missing_optional_logo_asset_does_not_block_pdf(monkeypatch, tmp_path):
    monkeypatch.setattr(
        notes_pdf_module,
        "LOGO_WATERMARK_PATH",
        str(tmp_path / "missing-center-logo.png"),
    )
    pdf = NotesPDF(
        _notes("Molecular Orbital Theory", ""),
        "by PW",
        watermark=False,
        logo_watermark=True,
    )

    pdf.add_page()

    assert pdf.page_no() == 1


def test_blank_source_metadata_does_not_inject_header_fallbacks():
    notes = ShortNotes(
        lecture_no="",
        subject="",
        title="",
        audience="",
        quote="",
        sections=[Section(heading="Source section", points=["Source fact"])],
    )
    blank_pdf = NotesPDF(notes, "by PW", watermark=False)
    blank_pdf.add_page()
    first_body_top = blank_pdf._body_top
    blank_pdf.add_page()
    continuation_body_top = blank_pdf._body_top
    extracted = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(io.BytesIO(bytes(blank_pdf.output()))).pages
    )

    populated_pdf = NotesPDF(
        _notes("Molecular Orbital Theory", ""),
        "by PW",
        watermark=False,
    )
    populated_pdf.add_page()

    assert "Short Notes" not in extracted
    assert "Chemistry" not in extracted
    assert "_title_chip_bounds" not in vars(blank_pdf)
    assert first_body_top < populated_pdf._body_top
    assert continuation_body_top < 30.0
