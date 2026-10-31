"""Regression coverage for responsive independent studygram cards."""
from __future__ import annotations

import io
import json
import math
import re
import unittest
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pdfplumber
import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from notesbot import config
from notesbot.errors import VisualFidelityError
from notesbot.notes_pdf import PAGE_MARGIN, NotesPDF, build_notes_pdf
from notesbot.prompts import (
    Branch,
    Compare,
    Curve,
    Graph,
    MindMap,
    Section,
    ShortNotes,
    Table,
    WhereItem,
)
from notesbot.visual_manifest import (
    SourceTopicAnchor,
    SourceVisual,
    VisualAsset,
    VisualManifest,
    VisualMemoryBudget,
    VisualRunBudget,
)


def _diagram_png() -> bytes:
    """A landscape coloured-pencil-style fixture without any network call."""
    image = Image.new("RGBA", (640, 360), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((72, 158, 270, 312), outline=(36, 52, 77, 255), width=6)
    draw.ellipse((370, 158, 568, 312), outline=(36, 52, 77, 255), width=6)
    draw.rectangle((135, 66, 174, 236), fill=(122, 132, 146, 255))
    draw.rectangle((465, 66, 504, 236), fill=(177, 105, 60, 255))
    draw.arc((184, 25, 456, 155), start=180, end=360, fill=(36, 52, 77, 255), width=6)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _sized_png(width: int, height: int, marker: int = 0) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (max(1, width // 8), max(1, height // 8), max(2, width - 2), max(2, height - 2)),
        outline=(20 + marker, 70, 150, 255),
        width=max(1, min(width, height) // 20),
    )
    output = io.BytesIO()
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


def _runtime_visual(
    visual_id: str,
    source_order: int,
    *,
    kind: str = "diagram",
) -> SourceVisual:
    return SourceVisual(
        visual_id=visual_id,
        chunk_index=0,
        local_page=1,
        global_page=1,
        box=(10.0, 10.0, 90.0, 90.0),
        kind=kind,
        topic_anchor=SourceTopicAnchor(
            topic_key=f"topic-{source_order}",
            local_page=1,
            box=(1.0, 1.0, 40.0, 8.0),
            exact_heading=f"Source heading {source_order}",
        ),
        source_order=source_order,
    )


def _runtime_budget() -> VisualRunBudget:
    return VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)


def _runtime_memory() -> VisualMemoryBudget:
    return VisualMemoryBudget(
        max_total_bytes=32 * 1024 * 1024,
        max_source_bytes=8 * 1024 * 1024,
        max_context_bytes=8 * 1024 * 1024,
        max_asset_bytes=16 * 1024 * 1024,
        max_page_cache_bytes=8 * 1024 * 1024,
    )


def _manifest_notes(
    assets: OrderedDict[str, VisualAsset],
    *,
    visuals: tuple[SourceVisual, ...] | None = None,
    sections: list[Section] | None = None,
    budget: VisualRunBudget | None = None,
    memory: VisualMemoryBudget | None = None,
) -> tuple[ShortNotes, VisualManifest, VisualRunBudget, VisualMemoryBudget]:
    actual_visuals = visuals or tuple(
        _runtime_visual(visual_id, index)
        for index, visual_id in enumerate(assets, start=1)
    )
    manifest = VisualManifest(visuals=actual_visuals)
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="STRICT-RENDER-HOSTILE-CONTENT",
        sections=sections or [
            Section(
                heading=f"Section {index}",
                visual_ids=[visual.visual_id],
                points=[f"Point {index}"],
            )
            for index, visual in enumerate(actual_visuals, start=1)
        ],
    )
    actual_budget = budget or _runtime_budget()
    actual_memory = memory or _runtime_memory()
    object.__setattr__(notes, "_visual_manifest", manifest)
    object.__setattr__(notes, "_source_crops", OrderedDict())
    object.__setattr__(notes, "_visual_assets", assets)
    object.__setattr__(notes, "_visual_run_budget", actual_budget)
    object.__setattr__(notes, "_visual_memory_budget", actual_memory)
    return notes, manifest, actual_budget, actual_memory


def _asset(
    visual_id: str,
    *,
    width: int = 160,
    height: int = 90,
    state: str = "sketch_source_fallback",
    primary: bytes | None = None,
    fallback: bytes | None = None,
) -> VisualAsset:
    fallback_png = fallback if fallback is not None else _sized_png(width, height)
    primary_png = primary if primary is not None else fallback_png
    return VisualAsset(
        visual_id=visual_id,
        state=state,
        primary_png=primary_png,
        fallback_png=fallback_png,
        width_px=width,
        height_px=height,
    )


def _notes(section: Section) -> ShortNotes:
    return ShortNotes(
        lecture_no="",
        subject="Chemistry",
        title="Electrochemistry",
        sections=[section],
    )


class DiagramLayoutTests(unittest.TestCase):
    def test_daniell_cell_preview_asset_has_transparent_background(self):
        asset = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "diagrams"
            / "daniell-cell-reference.png"
        )

        self.assertTrue(asset.is_file(), f"missing polished diagram asset: {asset}")
        with Image.open(asset) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_draw_it_yourself_diagram_uses_wide_dotted_section(self):
        section = Section(
            heading="Electrolytic Cell",
            diagram="Label anode, cathode and electrolyte.",
        )

        self.assertTrue(NotesPDF._is_wide(section))

    def test_generated_diagram_is_centred_and_wider_than_text_column(self):
        section = Section(
            heading="Daniell Cell",
            points=["**Zinc** acts as the anode.", "Electrons flow to **copper**."],
        )
        object.__setattr__(section, "_image", _diagram_png())
        pdf = NotesPDF(_notes(section), "by PW Faculty", watermark=False)
        pdf.add_page()
        pdf._use_full_width()

        placements: list[tuple[float, float]] = []
        original_image = pdf.image

        def record_image(name, *args, **kwargs):
            placements.append((kwargs["x"], kwargs["w"]))
            return original_image(name, *args, **kwargs)

        pdf.image = record_image
        pdf._render_section(section)

        self.assertEqual(len(placements), 1)
        x, width = placements[0]
        self.assertAlmostEqual(x + width / 2, pdf.w / 2, delta=1.0)
        self.assertGreater(width, pdf._col_w)

    def test_sequence_optional_image_and_text_only_section_share_renderer_safely(self):
        image_section = Section(
            heading="Source diagram",
            points=["**Image-backed** teacher point."],
        )
        text_section = Section(
            heading="Text only",
            points=["**Text-only** teacher point."],
        )
        object.__setattr__(image_section, "_image", _diagram_png())
        pdf = NotesPDF(
            ShortNotes(
                lecture_no="",
                subject="Physics",
                title="Sequence renderer",
                sections=[image_section, text_section],
            ),
            "by PW Faculty",
            watermark=False,
        )
        pdf.add_page()
        pdf._use_full_width()

        with (
            patch.object(pdf, "_wide_image", wraps=pdf._wide_image) as wide_image,
            patch.object(
                pdf,
                "_diagram_box",
                side_effect=AssertionError("manual diagram fallback must stay inactive"),
            ),
            patch.object(
                pdf,
                "_draw_svg",
                side_effect=AssertionError("SVG fallback must stay inactive"),
            ),
        ):
            pdf._render_section(image_section)
            pdf._render_section(text_section)

        self.assertEqual(wide_image.call_count, 1)
        rendered_text = " ".join(
            (page.extract_text() or "")
            for page in PdfReader(io.BytesIO(bytes(pdf.output()))).pages
        )
        self.assertIn("source diagram", rendered_text.casefold())
        self.assertIn("text only", rendered_text.casefold())

    def test_feature_frame_closes_before_a_diagram_moves_to_next_page(self):
        class FrameRecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.frames: list[tuple[float, float]] = []

            def _dotted_frame(self, top: float, bottom: float) -> None:
                self.frames.append((top, bottom))

        section = Section(heading="Daniell Cell", points=["**Zinc** is an anode."])
        pdf = FrameRecordingPDF(_notes(section), "by PW Faculty", watermark=False)
        pdf.add_page()
        pdf._in_band = True
        pdf._use_full_width()
        pdf._band_frame_top = pdf.get_y()
        pdf.set_y(pdf.page_break_trigger - 24.0)

        pdf._wide_image(_diagram_png())

        self.assertEqual(len(pdf.frames), 1)
        self.assertGreater(pdf.frames[0][1], pdf.frames[0][0])

    def test_normal_sections_receive_soft_card_frames(self):
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.frames = []

            def _soft_card_frame(self, x, y, width, height):
                self.frames.append((x, y, width, height))

        sections = [
            Section(heading="One", points=["**One** short fact."]),
            Section(heading="Two", points=["**Two** short fact."]),
        ]
        pdf = RecordingPDF(ShortNotes(lecture_no="", subject="Chemistry", title="Cards", sections=sections), "by PW", watermark=False)
        pdf.add_page()
        pdf.render_body()

        self.assertEqual(len(pdf.frames), 2)
        self.assertTrue(all(frame[2] > 0 and frame[3] > 0 for frame in pdf.frames))

    def test_differently_weighted_sections_share_complementary_non_equal_widths(self):
        small = Section(heading="Definition", points=["**Bond** joins atoms."])
        medium = Section(heading="Properties", points=["**Strong** attraction."] * 4)
        pdf = NotesPDF(_notes(small), "by PW", watermark=False)
        pdf.add_page()

        rows = pdf._pack_card_rows([small, medium])

        self.assertEqual(len(rows), 1)
        _, left_width = rows[0][0]
        _, right_width = rows[0][1]
        self.assertNotAlmostEqual(left_width, right_width)
        self.assertAlmostEqual(left_width + right_width + pdf.CARD_GUTTER, pdf.epw)

    def test_row_cursor_advances_below_its_tallest_card(self):
        short = Section(heading="Short", points=["**One** fact."])
        tall = Section(heading="Tall", points=["**Long** fact."] * 6)
        pdf = NotesPDF(_notes(short), "by PW", watermark=False)
        pdf.add_page()
        row = pdf._pack_card_rows([short, tall])[0]
        start = pdf.get_y()
        expected = max(pdf._measure_card(section, width, index + 1) for index, (section, width) in enumerate(row))

        pdf._render_card_row(row)

        self.assertGreaterEqual(pdf.get_y(), start + expected)

    def test_generated_diagram_cards_use_full_page_width(self):
        section = Section(heading="Daniell Cell", points=["**Zinc** is anode."])
        object.__setattr__(section, "_image", _diagram_png())
        pdf = NotesPDF(_notes(section), "by PW", watermark=False)
        pdf.add_page()

        rows = pdf._pack_card_rows([section])

        self.assertEqual(rows, [[(section, pdf.epw)]])

    def test_complex_table_cards_use_full_page_width(self):
        section = Section(
            heading="Bond comparison",
            table=Table(headers=["Type", "Formation"], rows=[
                ["Ionic", "Transfer"], ["Covalent", "Sharing"],
                ["Metallic", "Sea"], ["Hydrogen", "Attraction"],
                ["Coordinate", "Donation"],
            ]),
        )

        self.assertTrue(NotesPDF._is_wide(section))

    def test_preflight_detects_a_wrapped_table_that_cannot_fit_in_a_paired_card(self):
        """Changing preflight back to a row-count estimate must fail this test."""
        table = Table(
            headers=["Type", "Formation"],
            rows=[
                ["Very long rich text " * 35, "Another extended cell value " * 35]
                for _ in range(4)
            ],
        )
        section = Section(heading="Wrapped table", table=table)
        mate = Section(heading="Mate", points=["Short fact."])
        pdf = NotesPDF(_notes(section), "by PW", watermark=False)
        pdf.add_page()
        row = pdf._pack_card_rows([section, mate])[0]
        _, width = row[0]

        measured = pdf._measure_card(section, width, 1)

        self.assertGreater(measured, pdf.page_break_trigger - pdf.get_y())

    def test_wrapped_table_that_overflows_a_pair_is_promoted_to_framed_full_width(self):
        """A real overflow must not continue in the retired fixed columns."""
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.frames: list[tuple[int, float]] = []

            def _soft_card_frame(self, x, y, width, height):
                self.frames.append((self.page_no(), width))
                super()._soft_card_frame(x, y, width, height)

        table = Table(
            headers=["Type", "Formation"],
            rows=[
                ["Very long rich text " * 35, "Another extended cell value " * 35]
                for _ in range(4)
            ],
        )
        section = Section(heading="Wrapped table", table=table)
        mate = Section(heading="Mate", points=["Short fact."])
        notes = ShortNotes(
            lecture_no="", subject="Chemistry", title="Tables",
            sections=[section, mate],
        )
        pdf = RecordingPDF(notes, "by PW", watermark=False)
        pdf.add_page()

        pdf.render_body()

        self.assertTrue(pdf.frames)
        self.assertEqual(pdf.frames[0][0], 1,
                         "an unviable pair splits where it stands: turning the page "
                         "first would abandon the page we are already on")
        self.assertTrue(all(width == pdf.epw for _, width in pdf.frames))

    def test_oversized_long_bullet_card_gets_a_continuation_frame_on_each_page(self):
        """An oversized card has a defined continuation policy, never column spill."""
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.frames: list[tuple[int, float]] = []

            def _soft_card_frame(self, x, y, width, height):
                self.frames.append((self.page_no(), width))
                super()._soft_card_frame(x, y, width, height)

        section = Section(
            heading="Long explanation",
            points=["Very long point " * 80 for _ in range(16)],
        )
        pdf = RecordingPDF(_notes(section), "by PW", watermark=False)
        pdf.add_page()

        pdf.render_body()

        self.assertGreater(len(pdf.frames), 1)
        self.assertTrue(all(width == pdf.epw for _, width in pdf.frames))
        self.assertEqual(sorted({page for page, _ in pdf.frames}),
                         [page for page, _ in pdf.frames])

    def test_forced_page_boundary_moves_a_long_card_before_drawing_its_frame(self):
        """A card fitting on a fresh page must not leave a stranded prior-page frame."""
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.frames: list[int] = []

            def _soft_card_frame(self, x, y, width, height):
                self.frames.append(self.page_no())
                super()._soft_card_frame(x, y, width, height)

        section = Section(
            heading="Long but finite",
            points=["A detailed explanatory sentence " * 18 for _ in range(5)],
        )
        pdf = RecordingPDF(_notes(section), "by PW", watermark=False)
        pdf.add_page()
        pdf._use_full_width()
        pdf.set_y(pdf.page_break_trigger - 18.0)

        pdf.render_body()

        self.assertEqual(pdf.frames, [2])

    def test_card_that_fits_a_page_starts_in_the_room_left_and_continues(self):
        """Turning the page first would leave the lower part of page 1 blank."""
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.frames: list[int] = []

            def _soft_card_frame(self, x, y, width, height):
                self.frames.append(self.page_no())
                super()._soft_card_frame(x, y, width, height)

        section = Section(
            heading="Long but finite",
            points=["A detailed explanatory sentence " * 18 for _ in range(5)],
        )
        pdf = RecordingPDF(_notes(section), "by PW", watermark=False)
        pdf.add_page()
        pdf._use_full_width()
        pdf.set_y(pdf.page_break_trigger - 60.0)
        # Fits a fresh page on its own, but not the 60 mm left here.
        self.assertFalse(pdf._preflight_card(section, PAGE_MARGIN, pdf._body_top,
                                             pdf.epw, 1)[1])

        pdf.render_body()

        self.assertEqual(pdf.frames, [1, 2])
        first_page = PdfReader(io.BytesIO(bytes(pdf.output()))).pages[0].extract_text()
        self.assertIn("LONG BUT FINITE", first_page)

    def test_short_key_terms_share_rows_and_each_renders_once(self):
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.boxes: list[tuple[float, float]] = []

            def _key_terms_frame(self, x, y, w, h, pad_y, head_h):
                self.boxes.append((w, h))
                super()._key_terms_frame(x, y, w, h, pad_y, head_h)

        markers = [f"Word{index:02d}" for index in range(12)]
        section = Section(heading="Glossary",
                          key_terms=[f"{marker} : meaning" for marker in markers])
        pdf = RecordingPDF(_notes(section), "by QA", watermark=False)
        pdf.add_page()
        pdf._use_full_width()   # a lone section is a full-width card

        columns, _layouts = pdf._key_terms_grid(section.key_terms, pdf.epw - 6.0)
        pdf.render_body()

        self.assertEqual(columns, 3)
        self.assertEqual(len(pdf.boxes), 1)
        one_column = sum(
            pdf._measure_rich(term, pdf.epw - 9.5, 11.5, 12.5)
            for term in section.key_terms
        )
        self.assertLess(pdf.boxes[0][1], one_column / 2)
        extracted = PdfReader(io.BytesIO(bytes(pdf.output()))).pages[0].extract_text()
        for marker in markers:
            self.assertEqual(extracted.count(marker), 1, marker)

    def test_long_key_term_definitions_keep_one_full_width_column(self):
        terms = [f"Term{index} : " + "a longer source definition " * 6 for index in range(4)]
        pdf = NotesPDF(_notes(Section(heading="G", key_terms=terms)), "by QA", watermark=False)
        pdf.add_page()
        pdf._use_full_width()

        columns, _layouts = pdf._key_terms_grid(terms, pdf.epw - 6.0)

        self.assertEqual(columns, 1)

    def test_small_mindmap_sits_under_the_notes_when_it_fits(self):
        mindmap = MindMap(
            center="Cells",
            branches=[Branch(label=f"Branch {index}", children=["One fact."])
                      for index in range(3)],
        )
        notes = ShortNotes(
            lecture_no="", subject="Biology", title="Cells",
            sections=[Section(heading="Cell", points=["**Cell** is the unit of life."])],
            mindmap=mindmap,
        )
        pdf = NotesPDF(notes, "by QA", watermark=False)
        pdf.add_page()

        pdf.render_body()

        self.assertEqual(pdf.page_no(), 1)
        self.assertLessEqual(pdf.get_y(), pdf.page_break_trigger)

    def test_mindmap_just_over_a_page_is_shrunk_onto_one_page(self):
        def notes_with(branch_count: int) -> ShortNotes:
            return ShortNotes(
                lecture_no="", subject="Biology", title="Cells", sections=[],
                mindmap=MindMap(center="Cells", branches=[
                    Branch(label=f"Branch {index}",
                           children=[f"Fact {index}.{child} about the cell."
                                     for child in range(5)])
                    for index in range(branch_count)
                ]),
            )

        def render_after_full_page(branch_count: int) -> NotesPDF:
            pdf = NotesPDF(notes_with(branch_count), "by QA", watermark=False)
            pdf.add_page()
            pdf.set_y(pdf.page_break_trigger)   # the notes filled page 1
            pdf._render_mindmap(pdf.notes.mindmap)
            return pdf

        # The smallest map that needs two pages at full size.
        with patch.object(NotesPDF, "MINDMAP_SCALES", (1.0,)):
            branch_count = next(
                count for count in range(6, 40)
                if render_after_full_page(count).page_no() > 2
            )

        pdf = render_after_full_page(branch_count)

        self.assertEqual(pdf.page_no(), 2)
        self.assertLessEqual(pdf.get_y(), pdf.page_break_trigger)

    def test_four_column_mo_energy_order_math_stays_inside_its_table_cell(self):
        """A wide equation must wrap or scale inside the narrow fourth cell."""
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.rectangles: list[tuple[float, float, float, float]] = []
                self.images: list[tuple[float, float, float, float]] = []

            def rect(self, x, y, w, h, *args, **kwargs):
                self.rectangles.append((x, y, w, h))
                return super().rect(x, y, w, h, *args, **kwargs)

            def image(self, name, *args, **kwargs):
                self.images.append((kwargs["x"], kwargs["y"], kwargs["w"], kwargs["h"]))
                return super().image(name, *args, **kwargs)

        order = (
            r"$\sigma_{1s} < \sigma^*_{1s} < \sigma_{2s} < \sigma^*_{2s} "
            r"< \pi_{2p_x} = \pi_{2p_y} < \sigma_{2p_z} < \pi^*_{2p_x} "
            r"= \pi^*_{2p_y} < \sigma^*_{2p_z}$"
        )
        table = Table(
            headers=["Species", "Mixing", "Bond order", "MO energy order"],
            rows=[["B2 to N2", "Strong", "Variable", order]],
        )
        pdf = RecordingPDF(_notes(Section(heading="MO order", table=table)), "by PW", watermark=False)
        pdf.add_page()
        pdf._use_full_width()
        pdf.rectangles.clear()
        pdf.images.clear()

        pdf._render_table(table)

        col_w = pdf.epw / 4
        table_cells = [
            rect for rect in pdf.rectangles if abs(rect[2] - col_w) < 0.01
        ]
        self.assertTrue(pdf.images)
        self.assertTrue(table_cells)
        for ix, iy, iw, ih in pdf.images:
            self.assertTrue(
                any(
                    cx <= ix
                    and ix + iw <= cx + cw
                    and cy <= iy
                    and iy + ih <= cy + ch
                    for cx, cy, cw, ch in table_cells
                ),
                f"math image {(ix, iy, iw, ih)} escaped every table cell",
            )

    def test_long_unbroken_note_text_stays_inside_its_box(self):
        """Long generated tokens must hard-wrap rather than cross a box edge."""
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.rectangles: list[tuple[float, float, float, float]] = []
                self.text_runs: list[tuple[float, float, float]] = []

            def rect(self, x, y, w, h, *args, **kwargs):
                self.rectangles.append((x, y, w, h))
                return super().rect(x, y, w, h, *args, **kwargs)

            def text(self, x, y, text=""):
                self.text_runs.append((x, y, self.get_string_width(text)))
                return super().text(x, y, text)

        section = Section(heading="Long note")
        pdf = RecordingPDF(_notes(section), "by PW", watermark=False)
        pdf.add_page()
        pdf._use_full_width()
        pdf.rectangles.clear()
        pdf.text_runs.clear()
        token = "SpectroscopyConfigurationWithoutAnyGeneratedWhitespace" * 6

        pdf._note_box(f"Important! {token}")

        box = max(pdf.rectangles, key=lambda rect: rect[2] * rect[3])
        bx, _by, bw, _bh = box
        self.assertTrue(pdf.text_runs)
        for tx, _ty, tw in pdf.text_runs:
            self.assertGreaterEqual(tx, bx)
            self.assertLessEqual(tx + tw, bx + bw)

    def test_wrapped_section_heading_centres_each_rich_line_in_arrow_body(self):
        """Wrapped section-heading lines must center within the rectangular body."""
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.boxes: list[tuple[float, float, float, float]] = []
                self.words: list[tuple[float, float, float]] = []
                self.rich_blocks: list[tuple[float, float]] = []

            def _crayon_box(self, x, y, w, h, fill, edge):
                self.boxes.append((x, y, w, h))
                return super()._crayon_box(x, y, w, h, fill, edge)

            def text(self, x, y, text=""):
                self.words.append((x, y, self.get_string_width(text)))
                return super().text(x, y, text)

            def _draw_rich(self, text, x_left, y_top, width, body_pt, math_pt, color):
                height = super()._draw_rich(
                    text, x_left, y_top, width, body_pt, math_pt, color,
                )
                self.rich_blocks.append((y_top, height))
                return height

        heading = (
            "Electrochemical Energy Conversion and Electrode Potential "
            "Relationships Under Standard and Nonstandard Conditions"
        )
        pdf = RecordingPDF(_notes(Section(heading=heading)), "by QA", watermark=False)
        pdf.add_page()
        pdf.boxes.clear()
        pdf.words.clear()
        pdf.rich_blocks.clear()

        pdf._section_heading(heading)

        box_x, box_y, box_width, box_height = pdf.boxes[-1]
        rich_top, rich_height = pdf.rich_blocks[-1]
        lines: dict[float, list[tuple[float, float]]] = {}
        for x, baseline, width in pdf.words:
            lines.setdefault(round(baseline, 3), []).append((x, width))

        self.assertGreaterEqual(len(lines), 2)
        expected_center = box_x + box_width / 2
        for words in lines.values():
            left = min(x for x, _ in words)
            right = max(x + width for x, width in words)
            self.assertAlmostEqual((left + right) / 2, expected_center, delta=0.5)
        self.assertAlmostEqual(rich_top + rich_height / 2,
                               box_y + box_height / 2, delta=0.2)

    def test_section_heading_supports_legacy_draw_rich_override(self):
        """Section headings must keep the prior seven-argument override contract."""
        class LegacyOverridePDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.rich_calls = 0

            def _draw_rich(self, text, x_left, y_top, width, body_pt, math_pt, color):
                self.rich_calls += 1
                return super()._draw_rich(
                    text, x_left, y_top, width, body_pt, math_pt, color,
                )

        pdf = LegacyOverridePDF(
            _notes(Section(heading="Legacy override")), "by QA", watermark=False,
        )
        pdf.add_page()

        pdf._section_heading("Legacy override")

        self.assertEqual(pdf.rich_calls, 1)

    def test_single_oversized_table_row_continues_in_page_local_slices(self):
        """A row taller than a fresh page must retain every cell on bounded pages."""
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.rectangles: list[tuple[int, float, float, float, float]] = []

            def rect(self, x, y, w, h, *args, **kwargs):
                self.rectangles.append((self.page_no(), x, y, w, h))
                return super().rect(x, y, w, h, *args, **kwargs)

        repeated = "cell " * 1200
        table = Table(
            headers=["Left value", "Right value"],
            rows=[[repeated, repeated]],
        )
        notes = ShortNotes(
            lecture_no="", subject="Chemistry", title="Tall table",
            sections=[Section(heading="Oversized row", table=table)],
        )
        pdf = RecordingPDF(notes, "by QA", watermark=False)
        pdf.add_page()

        pdf.render_body()

        col_w = pdf.epw / 2
        cells = [
            rect for rect in pdf.rectangles if abs(rect[3] - col_w) < 0.01
        ]
        self.assertGreater(len({page for page, *_ in cells}), 1)
        self.assertTrue(cells)
        for page, _x, y, _w, h in cells:
            self.assertLessEqual(
                y + h, pdf.page_break_trigger + 0.01,
                f"table cell crossed page {page}'s body boundary",
            )
        self.assertLessEqual(pdf.get_y(), pdf.page_break_trigger)

        reader = PdfReader(io.BytesIO(bytes(pdf.output())))
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertEqual(len(re.findall(r"\bcell\b", extracted)), 2400)

    def test_oversized_note_box_continues_without_clipping_or_text_loss(self):
        """A note taller than a fresh page gets one bounded box per page slice."""
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.rectangles: list[tuple[int, float, float, float, float]] = []

            def rect(self, x, y, w, h, *args, **kwargs):
                self.rectangles.append((self.page_no(), x, y, w, h))
                return super().rect(x, y, w, h, *args, **kwargs)

        token = "LongToken" * 800
        notes = ShortNotes(
            lecture_no="", subject="Chemistry", title="Tall note",
            sections=[Section(
                heading="Continuation note",
                notes=[f"Important! {token}"],
            )],
        )
        pdf = RecordingPDF(notes, "by QA", watermark=False)
        pdf.add_page()

        pdf.render_body()

        note_w = pdf.epw * 0.92
        boxes = [
            rect for rect in pdf.rectangles if abs(rect[3] - note_w) < 0.01
        ]
        self.assertGreater(len({page for page, *_ in boxes}), 1)
        self.assertTrue(boxes)
        for page, _x, y, _w, h in boxes:
            self.assertLessEqual(
                y + h, pdf.page_break_trigger + 0.01,
                f"note box crossed page {page}'s body boundary",
            )
        self.assertLessEqual(pdf.get_y(), pdf.page_break_trigger)

        rendered = io.BytesIO(bytes(pdf.output()))
        extracted_fragments: list[str] = []
        with pdfplumber.open(rendered) as document:
            for page_number, page in enumerate(document.pages, start=1):
                page_boxes = [rect for rect in boxes if rect[0] == page_number]
                for word in page.extract_words():
                    for _number, x, y, w, h in page_boxes:
                        scale = 72 / 25.4
                        if (
                            word["x0"] >= x * scale - 0.5
                            and word["x1"] <= (x + w) * scale + 0.5
                            and word["top"] >= y * scale - 0.5
                            and word["bottom"] <= (y + h) * scale + 0.5
                        ):
                            extracted_fragments.append(word["text"])
                            break
        compact = re.sub(r"\s+", "", "".join(extracted_fragments))
        self.assertIn(token, compact)

    def test_where_list_renders_every_item_once_across_page_boundaries(self):
        markers = [f"WhereMarker{index:02d}" for index in range(12)]
        items = [
            WhereItem(
                symbol=f"W{index}",
                meaning=f"{marker} " + "measured explanation " * 35,
            )
            for index, marker in enumerate(markers)
        ]
        section = Section(heading="Symbol legend", where=items)
        pdf = NotesPDF(_notes(section), "by QA", watermark=False)
        pdf.add_page()

        pdf.render_body()

        self.assertGreater(pdf.page_no(), 1)
        self.assertLessEqual(pdf.get_y(), pdf.page_break_trigger)
        extracted = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(io.BytesIO(bytes(pdf.output()))).pages
        )
        for marker in markers:
            self.assertEqual(extracted.count(marker), 1, marker)

    def test_key_terms_render_every_term_once_in_bounded_continuation_boxes(self):
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.rectangles: list[tuple[int, float, float, float, str]] = []

            def rect(self, x, y, w, h, *args, **kwargs):
                self.rectangles.append((self.page_no(), y, w, h, kwargs.get("style", "")))
                return super().rect(x, y, w, h, *args, **kwargs)

        markers = [f"KeyTermMarker{index:02d}" for index in range(8)]
        terms = [
            f"{marker} : " + "source backed definition " * 45
            for marker in markers
        ]
        section = Section(heading="Glossary", key_terms=terms)
        pdf = RecordingPDF(_notes(section), "by QA", watermark=False)
        pdf.add_page()

        pdf.render_body()

        boxes = [
            rect for rect in pdf.rectangles
            if abs(rect[2] - pdf.epw) < 0.01 and "F" in rect[4]
        ]
        self.assertGreater(len({page for page, *_ in boxes}), 1)
        for page, y, _w, h, _style in boxes:
            self.assertLessEqual(y + h, pdf.page_break_trigger + 0.01, page)
        extracted = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(io.BytesIO(bytes(pdf.output()))).pages
        )
        for marker in markers:
            self.assertEqual(extracted.count(marker), 1, marker)

    def test_compare_renders_every_point_once_in_bounded_continuation_cards(self):
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.rectangles: list[tuple[int, float, float, float]] = []

            def rect(self, x, y, w, h, *args, **kwargs):
                self.rectangles.append((self.page_no(), y, w, h))
                return super().rect(x, y, w, h, *args, **kwargs)

        left_markers = [f"LeftCompareMarker{index:02d}" for index in range(9)]
        right_markers = [f"RightCompareMarker{index:02d}" for index in range(9)]
        compare = Compare(
            left_title="Left source",
            left_points=[
                f"{marker} " + "left sourced explanation " * 35
                for marker in left_markers
            ],
            right_title="Right source",
            right_points=[
                f"{marker} " + "right sourced explanation " * 35
                for marker in right_markers
            ],
        )
        section = Section(heading="Full comparison", compare=compare)
        pdf = RecordingPDF(_notes(section), "by QA", watermark=False)
        pdf.add_page()

        pdf.render_body()

        col_w = (pdf.epw - 4.0) / 2
        cards = [rect for rect in pdf.rectangles if abs(rect[2] - col_w) < 0.01]
        self.assertGreater(len({page for page, *_ in cards}), 1)
        for page, y, _w, h in cards:
            self.assertLessEqual(y + h, pdf.page_break_trigger + 0.01, page)
        extracted = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(io.BytesIO(bytes(pdf.output()))).pages
        )
        for marker in left_markers + right_markers:
            self.assertEqual(extracted.count(marker), 1, marker)

    def test_mindmap_paginates_all_branches_and_children_exactly_once(self):
        class RecordingPDF(NotesPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.rectangles: list[tuple[int, float, float, float]] = []

            def rect(self, x, y, w, h, *args, **kwargs):
                self.rectangles.append((self.page_no(), y, w, h))
                return super().rect(x, y, w, h, *args, **kwargs)

        branch_markers = [f"BranchMarker{index:02d}" for index in range(12)]
        child_markers = [
            [f"ChildMarker{branch:02d}x{child:02d}" for child in range(7)]
            for branch in range(12)
        ]
        mindmap = MindMap(
            center="Source map",
            branches=[
                Branch(label=branch_markers[index], children=child_markers[index])
                for index in range(12)
            ],
        )
        notes = ShortNotes(
            lecture_no="", subject="Chemistry", title="Large mind map",
            sections=[], mindmap=mindmap,
        )
        pdf = RecordingPDF(notes, "by QA", watermark=False)
        pdf.add_page()

        pdf.render_body()

        full_epw = pdf.w - 2 * PAGE_MARGIN
        mindmap_col_w = (
            full_epw - NotesPDF.MINDMAP_CENTER_W - 2 * NotesPDF.MINDMAP_GAP_X
        ) / 2
        cards = [
            rect for rect in pdf.rectangles if abs(rect[2] - mindmap_col_w) < 0.01
        ]
        self.assertGreater(len({page for page, *_ in cards}), 1)
        for page, y, _w, h in cards:
            self.assertLessEqual(y + h, pdf.page_break_trigger + 0.01, page)
        extracted = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(io.BytesIO(bytes(pdf.output()))).pages
        )
        for marker in branch_markers:
            self.assertEqual(extracted.count(marker), 1, marker)
        for marker in [item for group in child_markers for item in group]:
            self.assertEqual(extracted.count(marker), 1, marker)


def test_manifest_visuals_render_in_section_and_source_order_exactly_once():
    first_png = _sized_png(160, 90, marker=1)
    second_png = _sized_png(160, 90, marker=2)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", fallback=first_png)),
        ("sv-0002", _asset("sv-0002", fallback=second_png)),
    ))
    notes, manifest, _budget, _memory = _manifest_notes(assets)
    scratch: list[bytes] = []
    writes: list[bytes] = []

    class RecordingPDF(NotesPDF):
        def _preflight_visual_png(self, png, width_mm, height_mm):
            del width_mm, height_mm
            scratch.append(png)

        def image(self, name, *args, **kwargs):
            del args, kwargs
            writes.append(name.getvalue())

    pdf = RecordingPDF(notes, "by QA", watermark=False)
    pdf.add_page()
    pdf.render_body()
    pdf._assert_visual_render_integrity()

    assert scratch == [first_png, second_png]
    assert writes == [first_png, second_png]
    assert tuple(item.visual_id for item in pdf._rendered_visuals) == (
        "sv-0001",
        "sv-0002",
    )
    assert tuple(item.state for item in pdf._rendered_visuals) == (
        "sketch_source_fallback",
        "sketch_source_fallback",
    )
    assert tuple(pdf._visual_assets) == tuple(
        visual.visual_id for visual in manifest.visuals
    )


def test_manifest_runtime_maps_are_immutable_snapshots_and_card_preflight_handles_shared_locks():
    fallback = _sized_png(160, 90)
    source_assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", fallback=fallback)),
    ))
    notes, _manifest, _budget, _memory = _manifest_notes(source_assets)
    pdf = NotesPDF(notes, "by QA", watermark=False)

    source_assets.clear()
    assert tuple(pdf._visual_assets) == ("sv-0001",)
    with pytest.raises(TypeError):
        pdf._visual_assets["sv-9999"] = _asset("sv-9999")

    pdf.add_page()
    pdf.render_body()
    assert [item.visual_id for item in pdf._rendered_visuals] == ["sv-0001"]


def test_visual_preserves_aspect_ratio_and_moves_to_fresh_page_before_shrinking():
    # Full width is slightly too tall even on a fresh page, but fresh-page
    # height scaling retains well above the 70% readable-width floor.
    png = _sized_png(640, 900)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", width=640, height=900, fallback=png)),
    ))
    notes, _manifest, _budget, _memory = _manifest_notes(assets)
    placements = []

    class RecordingPDF(NotesPDF):
        def _preflight_visual_png(self, png, width_mm, height_mm):
            del png, width_mm, height_mm

        def image(self, name, *args, **kwargs):
            del name, args
            placements.append((self.page_no(), kwargs))

    pdf = RecordingPDF(notes, "by QA", watermark=False)
    pdf.add_page()
    pdf._use_full_width()
    pdf.set_y(pdf.page_break_trigger - 20.0)
    full_width = pdf.epw

    pdf._render_source_visual("sv-0001")

    assert len(placements) == 1
    page, placement = placements[0]
    assert page == 2
    assert placement["w"] <= full_width
    assert placement["w"] >= 0.70 * full_width
    assert placement["w"] / placement["h"] == pytest.approx(640 / 900)
    assert pdf._rendered_visuals[0].page_number == 2


def test_visual_below_minimum_readable_width_fails_instead_of_squeezing_labels():
    png = _sized_png(100, 1000)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", width=100, height=1000, fallback=png)),
    ))
    notes, _manifest, _budget, _memory = _manifest_notes(assets)
    writes = []

    class RecordingPDF(NotesPDF):
        def _preflight_visual_png(self, png, width_mm, height_mm):
            del png, width_mm, height_mm

        def image(self, name, *args, **kwargs):
            writes.append((name, args, kwargs))

    pdf = RecordingPDF(notes, "by QA", watermark=False)
    pdf.add_page()
    pdf._use_full_width()

    with pytest.raises(VisualFidelityError) as caught:
        pdf._render_source_visual("sv-0001")

    assert caught.value.code == "render_failed"
    assert writes == []
    assert pdf._rendered_visuals == []


def test_visual_exact_minimum_readable_width_boundary_renders_once():
    png = _sized_png(100, 100)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", width=100, height=100, fallback=png)),
    ))
    notes, _manifest, _budget, memory = _manifest_notes(assets)
    scratch = []
    writes = []

    class BoundaryPDF(NotesPDF):
        @property
        def epw(self):
            return 100.0

        def _preflight_visual_png(self, png, width_mm, height_mm):
            scratch.append((png, width_mm, height_mm))

        def image(self, name, *args, **kwargs):
            del args
            writes.append((name.getvalue(), kwargs))

    try:
        pdf = BoundaryPDF(notes, "by QA", watermark=False)
        pdf.add_page()
        pdf.set_y(0.0)
        boundary = 70.0
        pdf.page_break_trigger = pdf.get_y() + 1.0 + 2.4 + boundary

        pdf._render_source_visual("sv-0001")

        assert scratch == [(png, 70.0, 70.0)]
        assert len(writes) == 1
        assert writes[0][0] == png
        assert writes[0][1]["w"] == 70.0
        assert writes[0][1]["h"] == 70.0
        assert len(pdf._rendered_visuals) == 1
        assert pdf._rendered_visuals[0].width_mm == 70.0
    finally:
        memory.close()


def test_visual_nextafter_below_minimum_width_fails_before_any_image_work():
    png = _sized_png(100, 100)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", width=100, height=100, fallback=png)),
    ))
    notes, _manifest, _budget, memory = _manifest_notes(assets)
    scratch = []
    writes = []

    class BoundaryPDF(NotesPDF):
        @property
        def epw(self):
            return 100.0

        def _preflight_visual_png(self, png, width_mm, height_mm):
            scratch.append((png, width_mm, height_mm))

        def image(self, name, *args, **kwargs):
            writes.append((name, args, kwargs))

    try:
        pdf = BoundaryPDF(notes, "by QA", watermark=False)
        pdf.add_page()
        pdf.set_y(0.0)
        below_boundary = math.nextafter(70.0, -math.inf)
        pdf.page_break_trigger = pdf.get_y() + 1.0 + 2.4 + below_boundary

        with pytest.raises(VisualFidelityError) as caught:
            pdf._render_source_visual("sv-0001")

        assert caught.value.code == "render_failed"
        assert scratch == []
        assert writes == []
        assert pdf._rendered_visuals == []
    finally:
        memory.close()


@pytest.mark.parametrize("failure_mode", ["decode", "placement"])
def test_verified_redraw_decode_or_placement_failure_uses_same_slot_fallback_once(
    failure_mode,
):
    primary = b"not-a-png" if failure_mode == "decode" else _sized_png(160, 90, marker=3)
    fallback = _sized_png(160, 90, marker=4)
    assets = OrderedDict((
        (
            "sv-0001",
            _asset(
                "sv-0001",
                state="verified_redraw",
                primary=primary,
                fallback=fallback,
            ),
        ),
    ))
    notes, _manifest, _budget, _memory = _manifest_notes(assets)
    scratch = []
    writes = []

    class RecordingPDF(NotesPDF):
        def _preflight_visual_png(self, png, width_mm, height_mm):
            scratch.append((png, width_mm, height_mm))
            if failure_mode == "placement" and png is primary:
                raise RuntimeError("primary placement failed privately")

        def image(self, name, *args, **kwargs):
            del args
            writes.append((name.getvalue(), kwargs))

    pdf = RecordingPDF(notes, "by QA", watermark=False)
    pdf.add_page()
    pdf._use_full_width()
    pdf._render_source_visual("sv-0001")

    assert [png for png, _w, _h in scratch] == (
        [fallback] if failure_mode == "decode" else [primary, fallback]
    )
    assert len(writes) == 1
    assert writes[0][0] == fallback
    rendered = pdf._rendered_visuals[0]
    assert rendered.state == "sketch_source_fallback"
    assert (writes[0][1]["x"], writes[0][1]["y"]) == pytest.approx(
        (rendered.x_mm, rendered.y_mm)
    )


def test_primary_preflight_failure_leaves_no_primary_marks_before_fallback():
    primary = _sized_png(160, 90, marker=5)
    fallback = _sized_png(160, 90, marker=6)
    assets = OrderedDict((
        (
            "sv-0001",
            _asset(
                "sv-0001",
                state="verified_redraw",
                primary=primary,
                fallback=fallback,
            ),
        ),
    ))
    notes, _manifest, _budget, _memory = _manifest_notes(assets)
    scratch = []
    real_pdf_marks = []

    class RecordingPDF(NotesPDF):
        def _preflight_visual_png(self, png, width_mm, height_mm):
            scratch.append((png, width_mm, height_mm))
            if png is primary:
                raise RuntimeError("scratch-only primary failure")

        def image(self, name, *args, **kwargs):
            del args
            real_pdf_marks.append((name.getvalue(), self.page_no(), kwargs))

    pdf = RecordingPDF(notes, "by QA", watermark=False)
    pdf.add_page()
    pdf._render_source_visual("sv-0001")

    assert [item[0] for item in scratch] == [primary, fallback]
    assert scratch[0][1:] == pytest.approx(scratch[1][1:])
    assert len(real_pdf_marks) == 1
    written_png, written_page, placement = real_pdf_marks[0]
    assert written_png == fallback
    assert len(pdf._rendered_visuals) == 1
    rendered = pdf._rendered_visuals[0]
    assert (
        rendered.visual_id,
        rendered.state,
        rendered.page_number,
    ) == ("sv-0001", "sketch_source_fallback", written_page)
    assert (
        rendered.x_mm,
        rendered.y_mm,
        rendered.width_mm,
        rendered.height_mm,
    ) == pytest.approx((
        placement["x"],
        placement["y"],
        placement["w"],
        placement["h"],
    ))


def test_render_time_fallback_records_sketch_source_fallback_as_final_state():
    primary = _sized_png(160, 90, marker=7)
    fallback = _sized_png(160, 90, marker=8)
    assets = OrderedDict((
        (
            "sv-0001",
            _asset(
                "sv-0001",
                state="verified_redraw",
                primary=primary,
                fallback=fallback,
            ),
        ),
    ))
    notes, _manifest, _budget, _memory = _manifest_notes(assets)

    class FallbackPDF(NotesPDF):
        def _preflight_visual_png(self, png, width_mm, height_mm):
            del width_mm, height_mm
            if png is primary:
                raise ValueError("primary cannot be placed")

        def image(self, name, *args, **kwargs):
            del name, args, kwargs

    pdf = FallbackPDF(notes, "by QA", watermark=False)
    pdf.add_page()
    pdf._render_source_visual("sv-0001")

    assert assets["sv-0001"].state == "verified_redraw"
    assert [item.state for item in pdf._rendered_visuals] == [
        "sketch_source_fallback"
    ]


def test_fallback_render_failure_raises_instead_of_disappearing():
    fallback = _sized_png(160, 90)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", fallback=fallback)),
    ))
    notes, _manifest, _budget, _memory = _manifest_notes(assets)

    class BrokenWritePDF(NotesPDF):
        def _preflight_visual_png(self, png, width_mm, height_mm):
            del png, width_mm, height_mm

        def image(self, name, *args, **kwargs):
            del name, args, kwargs
            raise RuntimeError("private final write failure")

    pdf = BrokenWritePDF(notes, "by QA", watermark=False)
    pdf.add_page()

    with pytest.raises(VisualFidelityError) as caught:
        pdf._render_source_visual("sv-0001")

    assert caught.value.code == "render_failed"
    assert "private" not in str(caught.value).lower()
    assert pdf._rendered_visuals == []


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "duplicate",
        "unknown",
        "pending",
        "missing-budget",
        "missing-memory",
        "missing-crops",
    ],
)
def test_build_notes_pdf_rejects_missing_duplicate_unknown_or_pending_assets(
    corruption,
):
    first = _runtime_visual("sv-0001", 1)
    second = _runtime_visual("sv-0002", 2)
    visuals = (first, second)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001")),
        ("sv-0002", _asset("sv-0002")),
    ))
    if corruption == "missing":
        del assets["sv-0002"]
    elif corruption == "duplicate":
        visuals = (first, first.model_copy(update={"source_order": 2}))
    elif corruption == "unknown":
        del assets["sv-0002"]
        assets["sv-9999"] = _asset("sv-9999")
    elif corruption == "pending":
        assets["sv-0002"] = replace(assets["sv-0002"], state="pending")

    notes, _manifest, _budget, _memory = _manifest_notes(
        assets,
        visuals=visuals,
        sections=[Section(heading="Visuals", visual_ids=["sv-0001", "sv-0002"])],
    )
    if corruption == "missing-budget":
        object.__delattr__(notes, "_visual_run_budget")
    elif corruption == "missing-memory":
        object.__delattr__(notes, "_visual_memory_budget")
    elif corruption == "missing-crops":
        object.__delattr__(notes, "_source_crops")

    with pytest.raises(VisualFidelityError) as caught:
        NotesPDF(notes, "by QA", watermark=False)

    assert caught.value.code == "link_invalid"


def test_swapped_asset_key_or_visual_id_is_rejected_before_page1():
    first = _asset("sv-0001")
    second = _asset("sv-0002")
    swapped = OrderedDict((
        ("sv-0001", second),
        ("sv-0002", first),
    ))
    notes, _manifest, _budget, _memory = _manifest_notes(swapped)

    with pytest.raises(VisualFidelityError) as caught:
        NotesPDF(notes, "by QA", watermark=False)

    assert caught.value.code == "link_invalid"


def test_fallback_png_dimensions_must_equal_declared_metadata_before_page1():
    fallback = _sized_png(160, 90)
    assets = OrderedDict((
        (
            "sv-0001",
            VisualAsset(
                visual_id="sv-0001",
                state="sketch_source_fallback",
                primary_png=fallback,
                fallback_png=fallback,
                width_px=161,
                height_px=90,
            ),
        ),
    ))
    notes, _manifest, _budget, _memory = _manifest_notes(assets)

    with pytest.raises(VisualFidelityError) as caught:
        NotesPDF(notes, "by QA", watermark=False)

    assert caught.value.code == "fallback_damaged"


@pytest.mark.parametrize(
    "actual_size",
    [
        (2, 1),
        (config.MAX_VISUAL_CROP_PIXELS + 1, 1),
        (0, 1),
    ],
)
def test_png_header_bounds_reject_before_pixel_decode(
    monkeypatch,
    actual_size,
):
    events = []

    class OpenedPng:
        format = "PNG"
        size = actual_size

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *_args):
            events.append("close")

        def load(self):
            events.append("load")

    monkeypatch.setattr(Image, "open", lambda *_args, **_kwargs: OpenedPng())

    with pytest.raises(VisualFidelityError) as caught:
        NotesPDF._decode_visual_png(b"header-only", 1, 1, "render_failed")

    assert caught.value.code == "render_failed"
    assert events == ["enter", "close"]


def test_png_exact_pixel_cap_loads_but_declared_one_pixel_over_never_opens(
    monkeypatch,
):
    events = []
    exact_size = (config.MAX_VISUAL_CROP_PIXELS, 1)

    class OpenedPng:
        format = "PNG"
        size = exact_size

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *_args):
            events.append("close")

        def load(self):
            events.append("load")

    monkeypatch.setattr(Image, "open", lambda *_args, **_kwargs: OpenedPng())

    NotesPDF._decode_visual_png(
        b"header-only",
        exact_size[0],
        exact_size[1],
        "render_failed",
    )
    assert events == ["enter", "load", "close"]

    events.clear()
    with pytest.raises(VisualFidelityError) as caught:
        NotesPDF._decode_visual_png(
            b"header-only",
            config.MAX_VISUAL_CROP_PIXELS + 1,
            1,
            "render_failed",
        )
    assert caught.value.code == "render_failed"
    assert events == []


def test_verified_primary_dimension_mismatch_is_unusable_and_routes_same_slot_fallback():
    primary = _sized_png(159, 90)
    fallback = _sized_png(160, 90)
    assets = OrderedDict((
        (
            "sv-0001",
            _asset(
                "sv-0001",
                state="verified_redraw",
                primary=primary,
                fallback=fallback,
            ),
        ),
    ))
    notes, _manifest, _budget, _memory = _manifest_notes(assets)
    scratch = []
    writes = []

    class RecordingPDF(NotesPDF):
        def _preflight_visual_png(self, png, width_mm, height_mm):
            del width_mm, height_mm
            scratch.append(png)

        def image(self, name, *args, **kwargs):
            del args, kwargs
            writes.append(name.getvalue())

    pdf = RecordingPDF(notes, "by QA", watermark=False)
    pdf.add_page()
    pdf._render_source_visual("sv-0001")

    assert scratch == [fallback]
    assert writes == [fallback]
    assert pdf._rendered_visuals[0].state == "sketch_source_fallback"


def test_new_manifest_path_never_calls_diagram_box_or_semantic_graph_for_drawn_graph():
    fallback = _sized_png(160, 90)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", fallback=fallback)),
    ))
    visual = _runtime_visual("sv-0001", 1, kind="graph")
    section = Section(
        heading="Drawn graph",
        visual_ids=["sv-0001"],
        diagram="LEGACY DIAGRAM MUST NOT RUN",
        graph=Graph(
            title="Duplicate graph",
            curves=[Curve(label="duplicate", points=[[0.0, 0.0], [1.0, 1.0]])],
        ),
        points=["Explanation after source visual."],
    )
    object.__setattr__(section, "_svg", "<svg></svg>")
    object.__setattr__(section, "_image", fallback)
    notes, _manifest, _budget, _memory = _manifest_notes(
        assets,
        visuals=(visual,),
        sections=[section],
    )
    events = []

    class RecordingPDF(NotesPDF):
        def _render_source_visual(self, visual_id):
            events.append(("source", visual_id))

        def _draw_svg(self, *args, **kwargs):
            events.append(("svg", args, kwargs))

        def _wide_image(self, *args, **kwargs):
            events.append(("image", args, kwargs))

        def _diagram_box(self, *args, **kwargs):
            events.append(("diagram", args, kwargs))

        def _render_graph(self, *args, **kwargs):
            events.append(("graph", args, kwargs))

        def _bullet(self, point):
            events.append(("bullet", point))

    pdf = RecordingPDF(notes, "by QA", watermark=False)
    pdf.add_page()
    pdf._render_section(section)

    assert events == [
        ("source", "sv-0001"),
        ("bullet", "Explanation after source visual."),
    ]


def test_manifest_diagram_does_not_suppress_distinct_semantic_graph():
    fallback = _sized_png(160, 90)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", fallback=fallback)),
    ))
    section = Section(
        heading="Diagram plus graph",
        visual_ids=["sv-0001"],
        graph=Graph(
            title="Semantic relationship",
            curves=[Curve(label="trend", points=[[0.0, 0.0], [1.0, 1.0]])],
        ),
        points=["Explanation after source visual."],
    )
    notes, _manifest, _budget, _memory = _manifest_notes(
        assets,
        visuals=(_runtime_visual("sv-0001", 1, kind="diagram"),),
        sections=[section],
    )
    events = []

    class RecordingPDF(NotesPDF):
        def _render_source_visual(self, visual_id):
            events.append(("source", visual_id))

        def _bullet(self, point):
            events.append(("bullet", point))

        def _render_graph(self, graph):
            events.append(("graph", graph.title))

    pdf = RecordingPDF(notes, "by QA", watermark=False)
    pdf.add_page()
    pdf._render_section(section)

    assert events == [
        ("source", "sv-0001"),
        ("bullet", "Explanation after source visual."),
        ("graph", "Semantic relationship"),
    ]


def test_headingless_visual_continuation_skips_heading_band_and_renders_visual():
    fallback = _sized_png(160, 90)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", fallback=fallback)),
    ))
    section = Section(
        heading="",
        visual_ids=["sv-0001"],
        points=["Continuation explanation."],
    )
    notes, _manifest, _budget, _memory = _manifest_notes(
        assets,
        sections=[section],
    )
    events = []
    positions = []

    class RecordingPDF(NotesPDF):
        def _section_heading(self, *args, **kwargs):
            events.append(("heading", args, kwargs))

        def _render_source_visual(self, visual_id):
            positions.append(self.get_y())
            events.append(("source", visual_id))

        def _bullet(self, point):
            events.append(("bullet", point))

    pdf = RecordingPDF(notes, "by QA", watermark=False)
    pdf.add_page()
    start_y = pdf.get_y()
    pdf._render_section(section)

    assert events == [
        ("source", "sv-0001"),
        ("bullet", "Continuation explanation."),
    ]
    assert positions == [start_y]


def test_pdf_render_checks_shared_deadline_before_each_visual_and_preflight():
    first = _sized_png(160, 90, marker=10)
    second = _sized_png(160, 90, marker=11)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", fallback=first)),
        ("sv-0002", _asset("sv-0002", fallback=second)),
    ))
    budget = _runtime_budget()
    events = []
    budget.check_deadline = lambda: events.append("deadline")  # type: ignore[method-assign]
    notes, _manifest, _budget, _memory = _manifest_notes(assets, budget=budget)

    class RecordingPDF(NotesPDF):
        def _preflight_visual_png(self, png, width_mm, height_mm):
            del width_mm, height_mm
            events.append(("scratch", png))

        def image(self, name, *args, **kwargs):
            del args, kwargs
            events.append(("write", name.getvalue()))

    pdf = RecordingPDF(notes, "by QA", watermark=False)
    pdf.add_page()
    events.clear()
    pdf._render_source_visual("sv-0001")
    pdf._render_source_visual("sv-0002")

    assert events[0] == "deadline"
    for index, event in enumerate(events):
        if isinstance(event, tuple) and event[0] in {"scratch", "write"}:
            assert events[index - 1] == "deadline"
            assert events[index + 1] == "deadline"


def test_prepage_checks_deadline_around_fallback_and_verified_primary_validation(
    monkeypatch,
):
    primary = _sized_png(160, 90, marker=12)
    fallback = _sized_png(160, 90, marker=13)
    assets = OrderedDict((
        (
            "sv-0001",
            _asset(
                "sv-0001",
                state="verified_redraw",
                primary=primary,
                fallback=fallback,
            ),
        ),
    ))
    budget = _runtime_budget()
    events = []
    budget.check_deadline = lambda: events.append("deadline")  # type: ignore[method-assign]
    notes, _manifest, _budget, _memory = _manifest_notes(assets, budget=budget)
    actual_decode = NotesPDF._decode_visual_png

    def record_decode(png, width_px, height_px, error_code):
        events.append(("decode", png))
        return actual_decode(png, width_px, height_px, error_code)

    monkeypatch.setattr(NotesPDF, "_decode_visual_png", staticmethod(record_decode))
    NotesPDF(notes, "by QA", watermark=False)

    decode_positions = [
        index for index, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "decode"
    ]
    assert len(decode_positions) == 2
    for index in decode_positions:
        assert events[index - 1] == "deadline"
        assert events[index + 1] == "deadline"


def test_out_of_order_manifest_visual_is_rejected_before_any_real_write():
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001")),
        ("sv-0002", _asset("sv-0002")),
    ))
    notes, _manifest, _budget, _memory = _manifest_notes(assets)
    writes = []

    class RecordingPDF(NotesPDF):
        def _preflight_visual_png(self, png, width_mm, height_mm):
            del png, width_mm, height_mm

        def image(self, name, *args, **kwargs):
            writes.append((name, args, kwargs))

    pdf = RecordingPDF(notes, "by QA", watermark=False)
    pdf.add_page()

    with pytest.raises(VisualFidelityError) as caught:
        pdf._render_source_visual("sv-0002")

    assert caught.value.code == "render_failed"
    assert writes == []
    assert pdf._rendered_visuals == []


def test_pdf_emits_only_aggregate_verified_redraw_and_fallback_counts(capsys):
    fallback = _sized_png(160, 90)
    assets = OrderedDict((
        ("sv-0001", _asset("sv-0001", fallback=fallback)),
    ))
    memory = _runtime_memory()
    assert memory.reserve_buffer("asset", fallback) is not None
    notes, _manifest, _budget, _memory = _manifest_notes(assets, memory=memory)

    rendered = build_notes_pdf(notes, "by QA", watermark=False)

    lines = [
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith("[notesbot.visual-render] ")
    ]
    assert rendered.startswith(b"%PDF")
    assert len(lines) == 1
    payload = json.loads(lines[0].split(" ", 1)[1])
    assert payload == {
        "manifest": 1,
        "rendered": 1,
        "sketch_source_fallback": 1,
        "verified_redraw": 0,
    }
    assert all(type(value) is int for value in payload.values())
    assert "STRICT-RENDER-HOSTILE-CONTENT" not in lines[0]
    assert "sv-0001" not in lines[0]
    assert memory.resident_bytes() == 0


if __name__ == "__main__":
    unittest.main()
