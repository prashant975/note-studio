"""Deterministic source-visual fixtures shared by inventory tests."""

from __future__ import annotations

import hashlib
import io
import math
import struct
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Literal

from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class FixtureTruth:
    local_page: int
    box: tuple[float, float, float, float]
    kind: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class VisualPdfFixture:
    pdf_bytes: bytes
    width: int
    height: int
    truth: tuple[FixtureTruth, ...]


def _page() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (800, 1000), "white")
    return image, ImageDraw.Draw(image)


def make_four_page_visual_pdf() -> VisualPdfFixture:
    """Return four raster PDF pages with five hand-checked source visuals."""
    pages: list[Image.Image] = []

    page, draw = _page()
    draw.rectangle((70, 100, 310, 360), outline="black", width=5)
    draw.line((100, 300, 260, 150), fill="black", width=5)
    draw.polygon(((260, 150), (235, 158), (252, 180)), fill="black")
    draw.text((110, 185), "A", fill="black")
    draw.ellipse((470, 120, 700, 350), outline="black", width=5)
    draw.line((500, 235, 670, 235), fill="black", width=5)
    draw.text((565, 205), "B", fill="black")
    pages.append(page)

    page, draw = _page()
    draw.line((180, 500, 400, 360, 620, 500), fill="black", width=7, joint="curve")
    draw.ellipse((380, 340, 420, 380), outline="black", width=5)
    draw.text((155, 485), "H", fill="black")
    draw.text((390, 320), "O", fill="black")
    draw.text((625, 485), "H", fill="black")
    pages.append(page)

    page, draw = _page()
    draw.line((120, 820, 120, 160), fill="black", width=5)
    draw.line((120, 820, 720, 820), fill="black", width=5)
    for tick in range(220, 721, 100):
        draw.line((tick, 812, tick, 828), fill="black", width=3)
    for tick in range(220, 821, 100):
        draw.line((112, tick, 128, tick), fill="black", width=3)
    draw.line([(120, 780), (260, 650), (400, 430), (560, 300), (720, 210)], fill="black", width=5)
    draw.line([(120, 300), (260, 390), (400, 520), (560, 660), (720, 760)], fill="black", width=5)
    draw.text((700, 835), "x", fill="black")
    draw.text((90, 145), "y", fill="black")
    draw.text((420, 420), "V", fill="black")
    pages.append(page)

    page, draw = _page()
    draw.rectangle((120, 170, 680, 790), fill=(20, 25, 35), outline="black", width=3)
    draw.ellipse((260, 330, 540, 610), outline="white", width=7)
    draw.line((400, 330, 400, 610), fill="white", width=5)
    draw.text((330, 300), "N", fill="white")
    draw.text((455, 615), "S", fill="white")
    pages.append(page)

    output = io.BytesIO()
    pages[0].save(
        output,
        format="PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=100.0,
    )
    return VisualPdfFixture(
        pdf_bytes=output.getvalue(),
        width=800,
        height=1000,
        truth=(
            FixtureTruth(1, (8.0, 9.0, 40.0, 37.0), "diagram", ("A",)),
            FixtureTruth(1, (57.0, 11.0, 89.0, 36.0), "diagram", ("B",)),
            FixtureTruth(2, (18.0, 31.0, 82.0, 53.0), "structure", ("H", "O", "H")),
            FixtureTruth(3, (10.0, 13.0, 92.0, 85.0), "graph", ("x", "y", "V")),
            FixtureTruth(4, (14.0, 15.0, 86.0, 81.0), "diagram", ("N", "S")),
        ),
    )


def make_probe_pdf(
    *,
    width: int = 400,
    height: int = 400,
    dark_box: tuple[int, int, int, int] = (170, 170, 188, 188),
    thin_arrow: bool = False,
    text_heavy: bool = False,
) -> bytes:
    """Return a one-page raster PDF for deterministic probe edge cases."""
    page = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle(dark_box, fill="black")
    if text_heavy:
        for y in (115, 133, 151, 223, 241):
            draw.line((105, y, 245, y), fill="black", width=2)
    if thin_arrow:
        x0, y0, x1, y1 = dark_box
        draw.line((x1, (y0 + y1) // 2, min(width - 1, x1 + 50), (y0 + y1) // 2), fill="black", width=1)
        draw.polygon(
            (
                (min(width - 1, x1 + 50), (y0 + y1) // 2),
                (min(width - 1, x1 + 44), (y0 + y1) // 2 - 4),
                (min(width - 1, x1 + 44), (y0 + y1) // 2 + 4),
            ),
            fill="black",
        )
    output = io.BytesIO()
    page.save(output, format="PDF", resolution=100.0)
    return output.getvalue()


def make_adaptive_grid_pdf(*, size: int = 1600) -> bytes:
    """Return nested components that force legal adaptive grid subdivision."""
    page = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(page)
    scale = size / 1600.0
    for x, y in ((20, 20), (70, 70), (150, 150), (300, 300)):
        x0, y0 = round(x * scale), round(y * scale)
        side = max(4, round(20 * scale))
        draw.rectangle((x0, y0, x0 + side, y0 + side), fill="black")
    output = io.BytesIO()
    page.save(output, format="PDF", resolution=100.0)
    return output.getvalue()


def make_many_component_pdf(*, count: int = 270, size: int = 1200) -> bytes:
    """Return one page with more than 256 separated retained components."""
    page = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(page)
    columns = 18
    spacing = 60
    for index in range(count):
        x = 20 + (index % columns) * spacing
        y = 20 + (index // columns) * spacing
        draw.rectangle((x, y, x + 5, y + 5), fill="black")
    output = io.BytesIO()
    page.save(output, format="PDF", resolution=100.0)
    return output.getvalue()


FidelityMutation = Literal[
    "remove_direction_arrow",
    "add_unapproved_component",
    "change_label_case",
    "remove_curve",
    "shift_label_anchor",
]

FidelityComponentRole = Literal[
    "path",
    "direction_arrow",
    "lens",
    "ray",
    "bonds",
    "atom_nodes",
    "axes",
    "ticks",
    "curve",
    "dark_foreground",
    "anchor",
]


@dataclass(frozen=True)
class SourceFidelityLabelTruth:
    """One exact source label in crop-relative percentage coordinates."""

    text: str
    box: tuple[float, float, float, float]
    anchor: tuple[float, float] | None


@dataclass(frozen=True)
class SourceFidelityTruth:
    """Immutable source-order truth for one fidelity-fixture visual."""

    visual_id: str
    local_page: int
    source_box: tuple[float, float, float, float]
    kind: str
    labels: tuple[SourceFidelityLabelTruth, ...]
    is_dark: bool
    negative_mutation: FidelityMutation

    @property
    def box(self) -> tuple[float, float, float, float]:
        """Match the short source-box name used by the legacy fixture."""
        return self.source_box


@dataclass(frozen=True)
class SourceFidelityComponentTruth:
    """Independent semantic truth for one source-raster component."""

    component_id: str
    visual_id: str
    role: FidelityComponentRole
    box: tuple[float, float, float, float]
    mask_sha256: str
    foreground_pixels: int
    connected_components: int


@dataclass(frozen=True)
class SourceFidelityComponentObservation:
    """Observed binary component facts from one real extracted crop."""

    mask_sha256: str
    foreground_pixels: int
    connected_components: int
    support_box: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class SourceFidelityPdfFixture:
    pdf_bytes: bytes
    width: int
    height: int
    truth: tuple[SourceFidelityTruth, ...]
    components: tuple[SourceFidelityComponentTruth, ...]
    font_family: str


_SOURCE_FIDELITY_WIDTH = 800
_SOURCE_FIDELITY_HEIGHT = 1000
_SOURCE_FIDELITY_FONT_SIZE = 30
_SOURCE_FIDELITY_PAGE_WIDTH_PT = 363.635
_SOURCE_FIDELITY_PDF_RESOLUTION = (
    _SOURCE_FIDELITY_WIDTH * 72.0 / _SOURCE_FIDELITY_PAGE_WIDTH_PT
)
_FIXED_PDF_DATE = time.struct_time((2000, 1, 1, 0, 0, 0, 5, 1, 0))

_SOURCE_FIDELITY_TRUTH = (
    SourceFidelityTruth(
        visual_id="sv-0001",
        local_page=1,
        source_box=(7.0, 9.0, 45.0, 39.0),
        kind="diagram",
        labels=(
            SourceFidelityLabelTruth(
                "I=2.50 A", (6.0, 5.0, 56.0, 20.0), (60.0, 25.0)
            ),
            SourceFidelityLabelTruth(
                "R₁", (38.0, 68.0, 60.0, 83.0), (50.0, 60.0)
            ),
            SourceFidelityLabelTruth(
                "α", (73.0, 65.0, 88.0, 81.0), (75.0, 58.0)
            ),
        ),
        is_dark=False,
        negative_mutation="remove_direction_arrow",
    ),
    SourceFidelityTruth(
        visual_id="sv-0002",
        local_page=1,
        source_box=(55.0, 9.0, 93.0, 39.0),
        kind="diagram",
        labels=(),
        is_dark=False,
        negative_mutation="add_unapproved_component",
    ),
    SourceFidelityTruth(
        visual_id="sv-0003",
        local_page=2,
        source_box=(16.0, 27.0, 84.0, 61.0),
        kind="structure",
        labels=(
            SourceFidelityLabelTruth(
                "NH₄⁺", (4.0, 5.0, 28.0, 22.0), (37.0, 34.0)
            ),
            SourceFidelityLabelTruth(
                "Cl⁻", (75.0, 5.0, 91.0, 22.0), (67.0, 34.0)
            ),
            SourceFidelityLabelTruth(
                "pH=7.00", (37.0, 79.0, 64.0, 96.0), (50.0, 70.0)
            ),
            SourceFidelityLabelTruth(
                "Anode", (3.0, 45.0, 24.0, 62.0), (29.0, 54.0)
            ),
            SourceFidelityLabelTruth(
                "anode", (76.0, 45.0, 97.0, 62.0), (71.0, 54.0)
            ),
        ),
        is_dark=False,
        negative_mutation="change_label_case",
    ),
    SourceFidelityTruth(
        visual_id="sv-0004",
        local_page=3,
        source_box=(10.0, 12.0, 92.0, 86.0),
        kind="graph",
        labels=(
            SourceFidelityLabelTruth(
                "t (s)", (77.0, 91.0, 92.0, 98.0), (91.0, 86.0)
            ),
            SourceFidelityLabelTruth(
                "V (m/s)", (2.0, 3.0, 21.0, 11.0), (13.0, 16.0)
            ),
        ),
        is_dark=False,
        negative_mutation="remove_curve",
    ),
    SourceFidelityTruth(
        visual_id="sv-0005",
        local_page=4,
        source_box=(14.0, 15.0, 86.0, 81.0),
        kind="diagram",
        labels=(
            SourceFidelityLabelTruth(
                "λ₀", (42.0, 5.0, 58.0, 14.0), (50.0, 25.0)
            ),
        ),
        is_dark=True,
        negative_mutation="shift_label_anchor",
    ),
)

_SOURCE_FIDELITY_COMPONENTS = (
    SourceFidelityComponentTruth(
        "sv1-path",
        "sv-0001",
        "path",
        (8.0, 26.0, 74.0, 33.0),
        "a42b3211232c969c5818b9d554079455d10754073f10b793dfc8b9c2011c9156",
        1041,
        1,
    ),
    SourceFidelityComponentTruth(
        "sv1-direction-arrow",
        "sv-0001",
        "direction_arrow",
        (75.0, 22.0, 87.0, 36.0),
        "b4e92e7a05bd2277d7b8a134deb244796d8140e0af3f5cf2a341873fc72fedd6",
        511,
        1,
    ),
    SourceFidelityComponentTruth(
        "sv2-lens",
        "sv-0002",
        "lens",
        (52.0, 10.0, 67.0, 90.0),
        "a1f88e6d3573c1b72086f9d3c216d1ffcc3382cfc43e09908a215acacd80c165",
        1694,
        3,
    ),
    SourceFidelityComponentTruth(
        "sv2-ray-upper",
        "sv-0002",
        "ray",
        (7.0, 18.0, 38.0, 42.0),
        "9794d15e30c7217a434268a9d1c445cec4b2459ba7bcecb55e1b9d3a9129bc37",
        637,
        1,
    ),
    SourceFidelityComponentTruth(
        "sv2-ray-lower",
        "sv-0002",
        "ray",
        (7.0, 54.0, 38.0, 78.0),
        "1f38137ebb3543fca3ae439a26be27c727b9701d435d78109484d9f180280e2d",
        605,
        1,
    ),
    SourceFidelityComponentTruth(
        "sv3-bonds",
        "sv-0003",
        "bonds",
        (54.0, 35.0, 61.0, 43.0),
        "067f713d16e504128375ae38ccb6e71534a241f8c9d103a090ab5ef7c1a7f7c2",
        335,
        1,
    ),
    SourceFidelityComponentTruth(
        "sv3-atom-nodes",
        "sv-0003",
        "atom_nodes",
        (46.0, 18.0, 54.0, 30.0),
        "4b03e20eac418c9d33fb5a6cf3cb67dbf23c96cd0a3d9371e308980e6b68b400",
        627,
        1,
    ),
    SourceFidelityComponentTruth(
        "sv4-axes",
        "sv-0004",
        "axes",
        (10.0, 71.0, 34.0, 88.0),
        "323cbfca734f7a04d5f0bd76719b224ac36cb3f6bde418aa4109e1fb16540892",
        2131,
        1,
    ),
    SourceFidelityComponentTruth(
        "sv4-ticks",
        "sv-0004",
        "ticks",
        (22.0, 84.0, 82.0, 89.0),
        "5408e25f16c5fe738c6481664735e1fe8659f6504105d687a5fd77773b677ea3",
        2240,
        1,
    ),
    SourceFidelityComponentTruth(
        "sv4-curve-1",
        "sv-0004",
        "curve",
        (53.0, 17.0, 88.0, 43.0),
        "f4a6cb3b5df57866e8f99d21f3bdb23c0db81229e99d2176a5c4c19ed1cc590c",
        2117,
        1,
    ),
    SourceFidelityComponentTruth(
        "sv4-curve-2",
        "sv-0004",
        "curve",
        (53.0, 53.0, 88.0, 80.0),
        "78e3dbc719b0f364257ee93b4c87ac455c5e2bdb38b9e1c832720dc268cc25a9",
        1722,
        1,
    ),
    SourceFidelityComponentTruth(
        "sv5-foreground-diagram",
        "sv-0005",
        "dark_foreground",
        (15.0, 22.0, 90.0, 88.0),
        "18cd9bc64b569d912d71957c91914c8593670310d9c441d10ce2400c92c65d1d",
        17976,
        2,
    ),
    SourceFidelityComponentTruth(
        "sv5-anchor",
        "sv-0005",
        "anchor",
        (46.0, 20.0, 54.0, 29.0),
        "23be3023b5027f993407127d521f16dd919869cf85a8ad8584f21ad8d2d6d10f",
        785,
        1,
    ),
)

_COMPONENT_FOREGROUND_DISTANCE_SQUARED = 48 * 48


def _eight_connected_components(mask: bytes, width: int, height: int) -> int:
    visited = bytearray(len(mask))
    count = 0
    for start, is_foreground in enumerate(mask):
        if not is_foreground or visited[start]:
            continue
        count += 1
        visited[start] = 1
        pending = deque((start,))
        while pending:
            index = pending.pop()
            y, x = divmod(index, width)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < width and 0 <= ny < height):
                        continue
                    neighbour = ny * width + nx
                    if mask[neighbour] and not visited[neighbour]:
                        visited[neighbour] = 1
                        pending.append(neighbour)
    return count


def inspect_source_fidelity_component(
    source_png: bytes,
    width_px: int,
    height_px: int,
    box: tuple[float, float, float, float],
) -> SourceFidelityComponentObservation:
    """Measure a component from source pixels without consulting its truth facts."""
    with Image.open(io.BytesIO(source_png)) as opened:
        image = opened.convert("RGBA")
    try:
        if image.size != (width_px, height_px):
            raise ValueError("component source dimensions do not match")
        pixels = image.load()
        border = [pixels[x, 0][:3] for x in range(width_px)]
        border.extend(pixels[x, height_px - 1][:3] for x in range(width_px))
        border.extend(pixels[0, y][:3] for y in range(1, height_px - 1))
        border.extend(pixels[width_px - 1, y][:3] for y in range(1, height_px - 1))
        background = tuple(
            float(median(pixel[channel] for pixel in border)) for channel in range(3)
        )

        x0 = math.floor(width_px * box[0] / 100.0)
        y0 = math.floor(height_px * box[1] / 100.0)
        x1 = math.ceil(width_px * box[2] / 100.0)
        y1 = math.ceil(height_px * box[3] / 100.0)
        if not (0 <= x0 < x1 <= width_px and 0 <= y0 < y1 <= height_px):
            raise ValueError("component box must be an ordered crop percentage")

        mask = bytearray()
        support_x: list[int] = []
        support_y: list[int] = []
        for y in range(y0, y1):
            for x in range(x0, x1):
                red, green, blue, alpha = pixels[x, y]
                distance_squared = (
                    (red - background[0]) ** 2
                    + (green - background[1]) ** 2
                    + (blue - background[2]) ** 2
                )
                foreground = alpha > 0 and distance_squared > _COMPONENT_FOREGROUND_DISTANCE_SQUARED
                mask.append(1 if foreground else 0)
                if foreground:
                    support_x.append(x)
                    support_y.append(y)

        mask_bytes = bytes(mask)
        region_width, region_height = x1 - x0, y1 - y0
        digest = hashlib.sha256(
            struct.pack(">II", region_width, region_height) + mask_bytes
        ).hexdigest()
        support_box = (
            (
                min(support_x),
                min(support_y),
                max(support_x),
                max(support_y),
            )
            if support_x
            else None
        )
        return SourceFidelityComponentObservation(
            mask_sha256=digest,
            foreground_pixels=sum(mask),
            connected_components=_eight_connected_components(
                mask_bytes, region_width, region_height
            ),
            support_box=support_box,
        )
    finally:
        image.close()


def _load_source_fidelity_font(
    size: int, required_text: str
) -> ImageFont.FreeTypeFont:
    """Load bundled DejaVu Sans and prove every requested glyph is present."""
    try:
        from matplotlib import get_data_path
    except ImportError as error:  # pragma: no cover - dependency contract
        raise RuntimeError(
            "Matplotlib's bundled DejaVu Sans is required for the fidelity fixture"
        ) from error

    font_path = Path(get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf"
    if not font_path.is_file():
        raise RuntimeError(f"DejaVu Sans font file is missing: {font_path}")

    try:
        from fontTools.ttLib import TTFont

        font_file = TTFont(str(font_path), lazy=True)
        try:
            cmap = font_file["cmap"].getBestCmap() or {}
        finally:
            font_file.close()
    except Exception as error:
        raise RuntimeError(
            f"Unable to validate DejaVu Sans glyph coverage: {font_path}"
        ) from error

    missing = sorted(
        codepoint
        for codepoint in {ord(character) for character in required_text}
        if not chr(codepoint).isspace() and codepoint not in cmap
    )
    if missing:
        names = ", ".join(f"U+{codepoint:04X}" for codepoint in missing)
        raise RuntimeError(f"DejaVu Sans lacks required glyph coverage: {names}")

    try:
        font = ImageFont.truetype(str(font_path), size=size)
    except OSError as error:
        raise RuntimeError(f"Unable to load DejaVu Sans: {font_path}") from error
    family, _style = font.getname()
    if family != "DejaVu Sans":
        raise RuntimeError(
            f"Expected DejaVu Sans but loaded {family!r} from {font_path}"
        )
    return font


def _percentage_box_pixels(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int]:
    return (
        math.floor(width * box[0] / 100.0),
        math.floor(height * box[1] / 100.0),
        math.ceil(width * box[2] / 100.0),
        math.ceil(height * box[3] / 100.0),
    )


def _label_page_box(
    source_box: tuple[float, float, float, float],
    label_box: tuple[float, float, float, float],
) -> tuple[int, int, int, int]:
    sx0, sy0, sx1, sy1 = _percentage_box_pixels(
        source_box, _SOURCE_FIDELITY_WIDTH, _SOURCE_FIDELITY_HEIGHT
    )
    lx0, ly0, lx1, ly1 = _percentage_box_pixels(
        label_box, sx1 - sx0, sy1 - sy0
    )
    return sx0 + lx0, sy0 + ly0, sx0 + lx1, sy0 + ly1


def _draw_exact_labels(
    draw: ImageDraw.ImageDraw,
    truth: SourceFidelityTruth,
    font: ImageFont.FreeTypeFont,
    *,
    foreground: tuple[int, int, int],
    background: tuple[int, int, int],
) -> None:
    for label in truth.labels:
        x0, y0, x1, y1 = _label_page_box(truth.source_box, label.box)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=background)
        text_box = draw.textbbox((0, 0), label.text, font=font)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        if text_width + 8 > x1 - x0 or text_height + 8 > y1 - y0:
            raise RuntimeError(
                f"DejaVu Sans label {label.text!r} does not fit its fidelity box"
            )
        text_x = x0 + ((x1 - x0 - text_width) // 2) - text_box[0]
        text_y = y0 + ((y1 - y0 - text_height) // 2) - text_box[1]
        glyph_mask = Image.new("1", (x1 - x0, y1 - y0), 0)
        try:
            glyph_draw = ImageDraw.Draw(glyph_mask)
            glyph_draw.text(
                (text_x - x0, text_y - y0),
                label.text,
                font=font,
                fill=1,
            )
            draw.bitmap((x0, y0), glyph_mask, fill=foreground)
        finally:
            glyph_mask.close()


def _panel_bounds(truth: SourceFidelityTruth) -> tuple[int, int, int, int]:
    return _percentage_box_pixels(
        truth.source_box, _SOURCE_FIDELITY_WIDTH, _SOURCE_FIDELITY_HEIGHT
    )


def _draw_inset_panel(
    draw: ImageDraw.ImageDraw,
    truth: SourceFidelityTruth,
    *,
    background: tuple[int, int, int],
    outline: tuple[int, int, int],
) -> None:
    x0, y0, x1, y1 = _panel_bounds(truth)
    draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=background)
    inset = 8
    draw.rectangle(
        (x0 + inset, y0 + inset, x1 - inset - 1, y1 - inset - 1),
        outline=outline,
        width=3,
    )


def _draw_fidelity_page_one(
    image: Image.Image, font: ImageFont.FreeTypeFont
) -> None:
    draw = ImageDraw.Draw(image)
    circuit, ray = _SOURCE_FIDELITY_TRUTH[:2]
    light = (250, 250, 247)

    _draw_inset_panel(
        draw, circuit, background=light, outline=(45, 52, 63)
    )
    draw.line((82, 178, 315, 178), fill=(25, 34, 48), width=5)
    draw.polygon(((315, 178), (290, 164), (290, 192)), fill=(25, 34, 48))
    draw.line((315, 178, 315, 270, 260, 270), fill=(25, 34, 48), width=5)
    resistor = (
        (260, 270),
        (246, 256),
        (232, 284),
        (218, 256),
        (204, 284),
        (190, 256),
        (176, 270),
    )
    draw.line(resistor, fill=(25, 34, 48), width=5, joint="curve")
    draw.line((176, 270, 118, 270), fill=(25, 34, 48), width=5)
    draw.line((104, 238, 104, 302), fill=(25, 34, 48), width=4)
    draw.line((118, 226, 118, 314), fill=(25, 34, 48), width=7)
    draw.line((104, 270, 82, 270, 82, 178), fill=(25, 34, 48), width=5)
    draw.line((250, 262, 318, 220), fill=(70, 82, 96), width=4)
    draw.arc((252, 232, 314, 286), start=190, end=235, fill=(70, 82, 96), width=3)
    _draw_exact_labels(
        draw, circuit, font, foreground=(15, 22, 32), background=light
    )

    _draw_inset_panel(draw, ray, background=light, outline=(45, 52, 63))
    for x in range(462, 723, 22):
        draw.line((x, 241, min(x + 11, 722), 241), fill=(120, 128, 137), width=2)
    draw.arc((562, 125, 620, 355), start=270, end=90, fill=(23, 89, 155), width=5)
    draw.arc((584, 125, 642, 355), start=90, end=270, fill=(23, 89, 155), width=5)
    draw.line((466, 165, 592, 220, 716, 184), fill=(179, 58, 58), width=5)
    draw.line((466, 300, 592, 258, 716, 310), fill=(28, 103, 88), width=5)
    draw.polygon(((540, 197), (516, 197), (525, 178)), fill=(179, 58, 58))
    draw.polygon(((540, 275), (518, 266), (523, 288)), fill=(28, 103, 88))


def _draw_fidelity_page_two(
    image: Image.Image, font: ImageFont.FreeTypeFont
) -> None:
    draw = ImageDraw.Draw(image)
    truth = _SOURCE_FIDELITY_TRUTH[2]
    light = (250, 248, 242)
    ink = (40, 48, 57)
    _draw_inset_panel(draw, truth, background=light, outline=ink)
    draw.rounded_rectangle((270, 372, 300, 522), radius=8, outline=ink, width=5)
    draw.rounded_rectangle((500, 372, 530, 522), radius=8, outline=ink, width=5)
    draw.line((285, 372, 285, 350, 515, 350, 515, 372), fill=ink, width=4)
    draw.ellipse((368, 386, 432, 450), outline=(42, 91, 136), width=6)
    for center in ((330, 386), (470, 386), (400, 350), (400, 486)):
        cx, cy = center
        draw.line((400, 418, cx, cy), fill=(42, 91, 136), width=5)
        draw.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), outline=ink, width=4)
    draw.ellipse((472, 366, 512, 406), outline=(130, 73, 37), width=5)
    draw.arc((310, 458, 490, 548), start=5, end=175, fill=(69, 120, 101), width=4)
    _draw_exact_labels(draw, truth, font, foreground=(20, 26, 33), background=light)


def _draw_fidelity_page_three(
    image: Image.Image, font: ImageFont.FreeTypeFont
) -> None:
    draw = ImageDraw.Draw(image)
    truth = _SOURCE_FIDELITY_TRUTH[3]
    light = (248, 250, 252)
    ink = (34, 42, 54)
    _draw_inset_panel(draw, truth, background=light, outline=ink)
    draw.line((158, 755, 692, 755), fill=ink, width=5)
    draw.polygon(((700, 755), (677, 743), (677, 767)), fill=ink)
    draw.line((158, 755, 158, 225), fill=ink, width=5)
    draw.polygon(((158, 213), (146, 237), (170, 237)), fill=ink)
    for x in (245, 335, 425, 515, 605):
        draw.line((x, 744, x, 766), fill=ink, width=3)
    for y in (660, 565, 470, 375, 280):
        draw.line((147, y, 169, y), fill=ink, width=3)
    rising = ((158, 704), (240, 660), (326, 580), (412, 455), (510, 330), (650, 248))
    falling = ((158, 288), (240, 326), (326, 402), (412, 500), (510, 614), (650, 690))
    draw.line(rising, fill=(31, 103, 171), width=7, joint="curve")
    draw.line(falling, fill=(184, 72, 55), width=6, joint="curve")
    for x, y in falling[1:-1]:
        draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill=light, outline=(184, 72, 55), width=4)
    _draw_exact_labels(draw, truth, font, foreground=(18, 25, 35), background=light)


def _draw_fidelity_page_four(
    image: Image.Image, font: ImageFont.FreeTypeFont
) -> None:
    draw = ImageDraw.Draw(image)
    truth = _SOURCE_FIDELITY_TRUTH[4]
    dark = (36, 36, 36)
    white = (244, 248, 252)
    _draw_inset_panel(draw, truth, background=dark, outline=dark)
    draw.ellipse((220, 300, 580, 650), outline=white, width=7)
    draw.ellipse((270, 245, 530, 705), outline=(164, 210, 240), width=6)
    draw.ellipse((365, 440, 435, 510), outline=white, width=6)
    draw.line((400, 315, 400, 438), fill=white, width=6)
    draw.polygon(((400, 297), (387, 324), (413, 324)), fill=white)
    wave = (
        (180, 590),
        (220, 555),
        (260, 590),
        (300, 625),
        (340, 590),
        (380, 555),
        (420, 590),
        (460, 625),
        (500, 590),
        (540, 555),
        (580, 590),
        (620, 625),
    )
    draw.line(wave, fill=(248, 207, 84), width=6, joint="curve")
    _draw_exact_labels(draw, truth, font, foreground=white, background=dark)


def make_source_fidelity_pdf() -> SourceFidelityPdfFixture:
    """Return a deterministic four-page raster PDF with five fidelity visuals."""
    required_text = "".join(
        label.text for truth in _SOURCE_FIDELITY_TRUTH for label in truth.labels
    )
    font = _load_source_fidelity_font(_SOURCE_FIDELITY_FONT_SIZE, required_text)
    pages = [
        Image.new(
            "RGB", (_SOURCE_FIDELITY_WIDTH, _SOURCE_FIDELITY_HEIGHT), "white"
        )
        for _ in range(4)
    ]
    try:
        _draw_fidelity_page_one(pages[0], font)
        _draw_fidelity_page_two(pages[1], font)
        _draw_fidelity_page_three(pages[2], font)
        _draw_fidelity_page_four(pages[3], font)

        output = io.BytesIO()
        pages[0].save(
            output,
            format="PDF",
            save_all=True,
            append_images=pages[1:],
            resolution=_SOURCE_FIDELITY_PDF_RESOLUTION,
            quality=100,
            subsampling=0,
            title="shortnotes-source-fidelity-fixture",
            creator="shortnotes test fixture",
            producer="Pillow deterministic raster fixture",
            creationDate=_FIXED_PDF_DATE,
            modDate=_FIXED_PDF_DATE,
        )
        return SourceFidelityPdfFixture(
            pdf_bytes=output.getvalue(),
            width=_SOURCE_FIDELITY_WIDTH,
            height=_SOURCE_FIDELITY_HEIGHT,
            truth=_SOURCE_FIDELITY_TRUTH,
            components=_SOURCE_FIDELITY_COMPONENTS,
            font_family=font.getname()[0],
        )
    finally:
        for page in pages:
            page.close()
