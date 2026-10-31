"""Layout-verification tests that run without any PDF converter installed.

The DOCX->PDF step is monkeypatched everywhere: each test hands the checker a
PDF built directly with PyMuPDF, with text placed at known inch coordinates.
That keeps the suite green on machines with no LibreOffice and no Word, which is
most CI boxes, while still exercising the real span-scanning logic.
"""
from pathlib import Path
import sys

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import docx_render_check as drc
from src.docx_render_check import RenderCheck, render_and_verify

PT = 72.0  # points per inch, matching the module's coordinate space


def _make_pdf(path: Path, pages: list[list[tuple[float, float, str]]], *, image_on_first: bool = False) -> Path:
    """Build a Letter-sized PDF; each page is a list of (x_in, y_in, text)."""
    doc = fitz.open()
    for page_index, spans in enumerate(pages):
        page = doc.new_page(width=8.5 * PT, height=11.0 * PT)
        if page_index == 0 and image_on_first:
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8))
            pix.set_rect(pix.irect, (200, 30, 30))
            page.insert_image(fitz.Rect(0.5 * PT, 0.2 * PT, 1.5 * PT, 0.7 * PT), pixmap=pix)
        for x_in, y_in, text in spans:
            page.insert_text((x_in * PT, y_in * PT), text, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def _fake_docx(tmp_path: Path, name: str = "notes.docx") -> Path:
    """A file that merely has to exist — conversion is always monkeypatched."""
    path = tmp_path / name
    path.write_bytes(b"not really a docx")
    return path


def _patch_converter(monkeypatch, pdf_path: Path | None, warnings: list[str] | None = None) -> None:
    def fake_convert(docx_path: Path, out_dir: Path) -> tuple[Path | None, list[str]]:
        return pdf_path, list(warnings or [])

    monkeypatch.setattr(drc, "_convert_to_pdf", fake_convert)


# --- conversion failures -----------------------------------------------------


def test_no_converter_returns_not_ok_and_never_raises(tmp_path, monkeypatch):
    _patch_converter(monkeypatch, None, ["Could not render the DOCX for layout verification"])
    result = render_and_verify(_fake_docx(tmp_path), tmp_path / "out")

    assert isinstance(result, RenderCheck)
    assert result.ok is False
    assert result.pdf_path is None
    assert result.page_images == []
    assert result.page_count == 0
    assert any("Could not render" in w for w in result.warnings)


def test_converter_exception_is_contained(tmp_path, monkeypatch):
    def boom(docx_path: Path, out_dir: Path):
        raise RuntimeError("soffice exploded")

    monkeypatch.setattr(drc, "_convert_to_pdf", boom)
    result = render_and_verify(_fake_docx(tmp_path), tmp_path / "out")

    assert result.ok is False
    assert any("soffice exploded" in w for w in result.warnings)


def test_missing_docx_is_reported_not_raised(tmp_path):
    result = render_and_verify(tmp_path / "nope.docx", tmp_path / "out")

    assert result.ok is False
    assert result.pdf_path is None
    assert any("not found" in w.lower() for w in result.warnings)


def test_corrupt_pdf_is_contained(tmp_path, monkeypatch):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"")
    _patch_converter(monkeypatch, broken)
    result = render_and_verify(_fake_docx(tmp_path), tmp_path / "out")

    assert result.ok is False
    assert result.warnings


# --- overlap detection -------------------------------------------------------


def test_clean_page_has_no_overlap(tmp_path, monkeypatch):
    pdf = _make_pdf(
        tmp_path / "clean.pdf",
        [[(0.5, 2.0, "Body text that stays inside the column."), (0.5, 3.0, "More body text.")]],
        image_on_first=True,
    )
    _patch_converter(monkeypatch, pdf)
    result = render_and_verify(_fake_docx(tmp_path), tmp_path / "out")

    assert result.ok is True
    assert result.overlap_pages == []
    assert result.page_count == 1
    assert not any("notes-box column" in w for w in result.warnings)


def test_span_running_into_the_notes_column_is_flagged(tmp_path, monkeypatch):
    # Starts inside the column but is long enough to end at roughly 8 in.
    pdf = _make_pdf(
        tmp_path / "wide.pdf",
        [[(0.5, 4.0, "A deliberately over-wide line of body text that never wraps and keeps going past the column edge")]],
        image_on_first=True,
    )
    _patch_converter(monkeypatch, pdf)
    result = render_and_verify(_fake_docx(tmp_path), tmp_path / "out")

    assert result.ok is False
    assert result.overlap_pages == [1]
    assert any("notes-box column" in w and "over-wide line" in w for w in result.warnings)


def test_text_inside_the_notes_box_is_not_flagged(tmp_path, monkeypatch):
    """The reserved column is where the notes box is drawn, not empty space.

    Flagging everything that sits in it failed the reference document itself on
    all 99 pages, because PDF extractors split the "Space for Notes" caption
    into fragments that never matched the ignore-list.
    """
    pdf = _make_pdf(tmp_path / "boxed.pdf", [[(7.3, 1.6, "Notes")]], image_on_first=True)
    _patch_converter(monkeypatch, pdf)
    result = render_and_verify(_fake_docx(tmp_path), tmp_path / "out")

    assert result.overlap_pages == []


def test_text_in_the_reserved_column_but_outside_the_box_is_flagged(tmp_path, monkeypatch):
    """Nothing legitimate is drawn between the column edge and the box."""
    # In the clear strip: past where a body span may start, before the notes
    # box's own rectangle. Computed rather than written down, so tightening the
    # margins moves the probe with the geometry instead of silently landing it
    # inside the box (where nothing is flagged) and passing for the wrong reason.
    span_start_limit_in = drc.BODY_RIGHT_EDGE_IN + drc.X_TOLERANCE_PT / 72.0
    box_edge_in = drc.NOTES_BOX_LEFT_IN - drc.NOTES_BOX_SLACK_IN
    assert box_edge_in > span_start_limit_in, (
        "no clear strip left between the column and the notes box — this guard "
        "would pass without checking anything")
    stray_x = (span_start_limit_in + box_edge_in) / 2.0
    pdf = _make_pdf(tmp_path / "stray.pdf", [[(stray_x, 5.0, "stray")]], image_on_first=True)
    _patch_converter(monkeypatch, pdf)
    result = render_and_verify(_fake_docx(tmp_path), tmp_path / "out")

    assert result.overlap_pages == [1]
    assert any("stray" in w for w in result.warnings)


def test_justified_overhang_matching_the_reference_is_not_flagged(tmp_path, monkeypatch):
    """The reference's justified lines report bboxes ending at ~6.10 in even
    though the column stops at 6.00 in — glyph side bearings, not an overflow.
    Treating that as a defect would flag the ground-truth document."""
    doc = fitz.open()
    page = doc.new_page(width=8.5 * PT, height=11.0 * PT)
    text = "justified line with overhang"
    width = fitz.get_text_length(text, fontsize=11)
    page.insert_text((6.10 * PT - width, 4.0 * PT), text, fontsize=11)
    pdf = tmp_path / "overhang.pdf"
    doc.save(pdf)
    doc.close()
    _patch_converter(monkeypatch, pdf)

    assert render_and_verify(_fake_docx(tmp_path), tmp_path / "out").overlap_pages == []


def test_text_ending_exactly_at_the_column_edge_is_not_flagged(tmp_path, monkeypatch):
    doc = fitz.open()
    page = doc.new_page(width=8.5 * PT, height=11.0 * PT)
    text = "justified line"
    width = fitz.get_text_length(text, fontsize=11)
    # Right-align the run so it ends precisely on the 6.0 in column edge.
    page.insert_text((6.0 * PT - width, 4.0 * PT), text, fontsize=11)
    pdf = tmp_path / "edge.pdf"
    doc.save(str(pdf))
    doc.close()

    _patch_converter(monkeypatch, pdf)
    result = render_and_verify(_fake_docx(tmp_path), tmp_path / "out")

    assert result.overlap_pages == []
    assert result.ok is True


def test_header_and_footer_bands_are_ignored(tmp_path, monkeypatch):
    pdf = _make_pdf(
        tmp_path / "bands.pdf",
        [[
            (7.0, 0.5, "header logo caption"),   # top 1.0 in band
            (7.0, 10.5, "Daily Class Notes"),    # bottom 1.0 in band
            (0.5, 5.0, "ordinary body text"),
        ]],
        image_on_first=True,
    )
    _patch_converter(monkeypatch, pdf)
    result = render_and_verify(_fake_docx(tmp_path), tmp_path / "out")

    assert result.overlap_pages == []
    assert result.ok is True


def test_notes_box_caption_is_ignored_but_body_mentioning_it_is_not(tmp_path, monkeypatch):
    caption_only = _make_pdf(tmp_path / "caption.pdf", [[(6.3, 3.0, "Space for Notes")]], image_on_first=True)
    _patch_converter(monkeypatch, caption_only)
    assert render_and_verify(_fake_docx(tmp_path), tmp_path / "out1").overlap_pages == []

    # Body text that merely mentions the box, running from the writing column
    # across the edge, is the overflow this check exists to find.
    mentions = _make_pdf(
        tmp_path / "mentions.pdf",
        [[(3.0, 3.0, "this line mentions the Space for Notes column and runs far past the edge")]],
        image_on_first=True,
    )
    _patch_converter(monkeypatch, mentions)
    assert render_and_verify(_fake_docx(tmp_path), tmp_path / "out2").overlap_pages == [1]


def test_overlap_page_numbers_are_one_based_across_pages(tmp_path, monkeypatch):
    pdf = _make_pdf(
        tmp_path / "multi.pdf",
        [
            [(0.5, 3.0, "clean page one")],
            [(3.0, 3.0, "page two intrusion running well past the content column edge")],
            [(0.5, 3.0, "clean page three")],
        ],
        image_on_first=True,
    )
    _patch_converter(monkeypatch, pdf)
    result = render_and_verify(_fake_docx(tmp_path), tmp_path / "out")

    assert result.page_count == 3
    assert result.overlap_pages == [2]


# --- page images -------------------------------------------------------------


def test_first_and_last_pages_are_rendered_within_max_pages(tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path / "five.pdf", [[(0.5, 3.0, f"page {i}")] for i in range(1, 6)], image_on_first=True)
    _patch_converter(monkeypatch, pdf)
    out_dir = tmp_path / "out"
    result = render_and_verify(_fake_docx(tmp_path), out_dir, max_pages=3)

    assert result.page_count == 5
    assert [p.name for p in result.page_images] == ["notes_p1.png", "notes_p2.png", "notes_p5.png"]
    assert all(p.exists() and p.stat().st_size > 0 for p in result.page_images)
    assert all(p.parent == out_dir for p in result.page_images)


def test_dpi_controls_the_raster_size(tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path / "dpi.pdf", [[(0.5, 3.0, "hello")]], image_on_first=True)
    _patch_converter(monkeypatch, pdf)
    low = render_and_verify(_fake_docx(tmp_path), tmp_path / "low", dpi=72).page_images[0]
    high = render_and_verify(_fake_docx(tmp_path), tmp_path / "high", dpi=144).page_images[0]

    assert fitz.Pixmap(str(low)).width == pytest.approx(612, abs=2)
    assert fitz.Pixmap(str(high)).width == pytest.approx(1224, abs=2)


def test_missing_logo_produces_a_warning(tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path / "nologo.pdf", [[(0.5, 3.0, "body only")]], image_on_first=False)
    _patch_converter(monkeypatch, pdf)
    result = render_and_verify(_fake_docx(tmp_path), tmp_path / "out")

    assert any("logo" in w.lower() for w in result.warnings)
    assert result.ok is True  # advisory only: a missing logo is not a layout failure


def test_pages_to_render_selection_rules():
    assert drc._pages_to_render(0, 3) == []
    assert drc._pages_to_render(4, 0) == []
    assert drc._pages_to_render(1, 3) == [0]
    assert drc._pages_to_render(2, 3) == [0, 1]
    assert drc._pages_to_render(10, 1) == [0]
    assert drc._pages_to_render(10, 2) == [0, 9]
    assert drc._pages_to_render(10, 4) == [0, 1, 2, 9]


def test_column_edge_constant_matches_the_dcn_geometry():
    """The check's edge is the document's edge, derived not copied."""
    from src.config import (
        DCN_CONTENT_WIDTH_IN,
        DCN_MARGIN_LEFT_IN,
        DCN_NOTES_BOX_LEFT_IN,
        DCN_NOTES_BOX_WIDTH_IN,
    )

    expected = DCN_MARGIN_LEFT_IN + DCN_CONTENT_WIDTH_IN
    assert drc.BODY_RIGHT_EDGE_IN == pytest.approx(expected)
    assert drc.BODY_RIGHT_EDGE_PT == pytest.approx(expected * 72.0)
    assert drc.NOTES_BOX_LEFT_IN == pytest.approx(DCN_NOTES_BOX_LEFT_IN)
    assert drc.NOTES_BOX_RIGHT_IN == pytest.approx(
        DCN_NOTES_BOX_LEFT_IN + DCN_NOTES_BOX_WIDTH_IN)
    # The gap the check polices has to stay real: body text may overhang by at
    # most the clear space before the notes box starts.
    assert drc.BODY_RIGHT_EDGE_IN + drc.X_END_TOLERANCE_PT / 72.0 <= drc.NOTES_BOX_LEFT_IN
