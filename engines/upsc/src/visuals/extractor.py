"""Find candidate visuals in the lecture file and crop them at high resolution.

Works on the ORIGINAL input, not on the pipeline's page renders: the DCN run
renders pages at scale 1.5 (~108 DPI) purely for existence checks, which is not
print quality. Here each candidate region is re-rendered from the PDF at the
zoom that makes it readable on paper, so a small map on a big slide still comes
out sharp.

Three kinds of candidate, in order of preference:

* ``embedded_image`` — a raster the slide actually embeds (photo of the board,
  a pasted map). Located via its display rect, then re-rendered from the page
  so annotations drawn OVER the image are kept with it.
* ``region_render`` — a diagram drawn from shapes/lines/SmartArt. There is no
  image file to pull, so the drawing cluster's bounding box is rendered.
* ``full_slide`` — the slide IS the visual (board-screenshot decks). The page
  is rendered whole and then border-trimmed.

Nothing here decides educational value — that is the classifier's job. This
stage only refuses things that cannot possibly be a diagram (tiny icons,
hairlines, page furniture). Per-slide failures are recorded and skipped; a bad
slide never takes the run down (spec §24).
"""
from __future__ import annotations

from pathlib import Path

from ..models import SlideData
from .metadata import VISUALS_DIRNAME, VisualCandidate

#: A candidate must occupy at least this fraction of its slide to be worth
#: classifying — below it lives the world of bullets, icons and logos.
MIN_AREA_FRACTION = 0.03
#: A raster covering this much of the page means the slide is the visual.
FULL_SLIDE_FRACTION = 0.55
#: Rendered crop target width in pixels (readable at 5.3 in ≈ 300 DPI ceiling).
TARGET_RENDER_WIDTH_PX = 1600
MAX_RENDER_ZOOM = 4.0
#: How far apart two drawing rects may sit and still be one diagram (points).
CLUSTER_GAP_PT = 14.0
#: A shape cluster needs this many drawing items to look like a diagram.
MIN_CLUSTER_ITEMS = 5
MAX_CANDIDATES_PER_SLIDE = 3


# --------------------------------------------------------------------------
# Small image helpers (PIL, lazily imported like the rest of the project)
# --------------------------------------------------------------------------
def average_hash(image_path: Path) -> str:
    """64-bit average hash as hex. "" when PIL is missing or the file is bad."""
    try:
        from PIL import Image

        with Image.open(image_path) as im:
            gray = im.convert("L").resize((8, 8), Image.LANCZOS)
            pixels = list(gray.getdata())
            mean = sum(pixels) / 64
            bits = 0
            for p in pixels:
                bits = (bits << 1) | (1 if p > mean else 0)
            return f"{bits:016x}"
    except Exception:
        return ""


def hash_distance(a: str, b: str) -> int:
    """Hamming distance between two average hashes; 64 (max) when unknown."""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (ValueError, TypeError):
        return 64


def trim_borders(image_path: Path, *, padding: int = 16, tolerance: int = 24) -> None:
    """Crop uniform border colour off a saved crop, in place.

    Refuses to cut away more than 60% of either dimension — a trim that big
    means the background detection got it wrong, and the untrimmed original is
    the safer output.
    """
    try:
        from PIL import Image, ImageChops

        with Image.open(image_path) as im:
            img = im.convert("RGB")
        w, h = img.size
        corners = [img.getpixel((0, 0)), img.getpixel((w - 1, 0)),
                   img.getpixel((0, h - 1)), img.getpixel((w - 1, h - 1))]
        bg = tuple(int(sum(c[i] for c in corners) / 4) for i in range(3))
        diff = ImageChops.difference(img, Image.new("RGB", img.size, bg)).convert("L")
        bbox = diff.point(lambda p: 255 if p > tolerance else 0).getbbox()
        if not bbox:
            return
        left = max(0, bbox[0] - padding)
        top = max(0, bbox[1] - padding)
        right = min(w, bbox[2] + padding)
        bottom = min(h, bbox[3] + padding)
        if (right - left) < w * 0.4 or (bottom - top) < h * 0.4:
            return
        if (right - left, bottom - top) != (w, h):
            img.crop((left, top, right, bottom)).save(image_path)
    except Exception:
        return


def image_size(image_path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(image_path) as im:
            return im.size
    except Exception:
        return (0, 0)


def is_near_blank(image_path: Path) -> bool:
    """True for crops with almost no visual content (solid fills, faint rules)."""
    try:
        from PIL import Image, ImageStat

        with Image.open(image_path) as im:
            gray = im.convert("L").resize((64, 64))
        return ImageStat.Stat(gray).stddev[0] < 8.0
    except Exception:
        return False


def _slide_heading(slides: list[SlideData], slide_no: int) -> str:
    for s in slides:
        if s.slide_no == slide_no:
            return s.heading or ""
    return ""


def _finish_candidate(candidate: VisualCandidate) -> VisualCandidate | None:
    """Trim, measure and hash a freshly saved crop; None when it is junk."""
    path = candidate.image_path
    if path is None or not Path(path).exists():
        return None
    trim_borders(Path(path))
    width, height = image_size(Path(path))
    if width < 140 or height < 110:
        return None                        # icon-sized after trimming
    aspect = width / height if height else 99
    if aspect > 8 or aspect < 1 / 8:
        return None                        # a rule or a side banner
    if is_near_blank(Path(path)):
        return None
    candidate.width_px, candidate.height_px = width, height
    candidate.phash = average_hash(Path(path))
    return candidate


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def _cluster_rects(rects: list, gap: float):
    """Merge overlapping/nearby rects until stable; returns (rect, count) pairs."""
    import fitz

    clusters: list[list] = [[fitz.Rect(r), 1] for r in rects]
    changed = True
    while changed:
        changed = False
        result: list[list] = []
        for rect, count in clusters:
            grown = fitz.Rect(rect.x0 - gap, rect.y0 - gap, rect.x1 + gap, rect.y1 + gap)
            for entry in result:
                if grown.intersects(entry[0]):
                    entry[0] |= rect
                    entry[1] += count
                    changed = True
                    break
            else:
                result.append([fitz.Rect(rect), count])
        clusters = result
    return [(rect, count) for rect, count in clusters]


def _render_clip(page, rect, out_path: Path) -> None:
    import fitz

    zoom = TARGET_RENDER_WIDTH_PX / max(rect.width, 1.0)
    zoom = max(1.0, min(MAX_RENDER_ZOOM, zoom))
    clip = fitz.Rect(rect) + (-4, -4, 4, 4)
    clip &= page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    pix.save(str(out_path))


def _pdf_page_candidates(page, page_no: int, out_dir: Path,
                         slides: list[SlideData]) -> list[VisualCandidate]:
    import fitz

    page_area = max(page.rect.width * page.rect.height, 1.0)
    regions: list[tuple] = []              # (rect, origin)

    # Raster images placed on the page, located by their display rects.
    image_rects: list = []
    for item in page.get_images(full=True):
        try:
            rects = page.get_image_rects(item)
        except Exception:
            continue
        for rect in rects:
            if rect.is_empty or rect.width < 40 or rect.height < 40:
                continue
            fraction = (rect.width * rect.height) / page_area
            if fraction < MIN_AREA_FRACTION:
                continue
            origin = "full_slide" if fraction >= FULL_SLIDE_FRACTION else "embedded_image"
            image_rects.append(fitz.Rect(rect))
            regions.append((fitz.Rect(rect), origin))

    # Vector diagrams: cluster the drawing commands' rects.
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    drawing_rects = [d["rect"] for d in drawings
                     if d.get("rect") and not fitz.Rect(d["rect"]).is_empty]
    for rect, count in _cluster_rects(drawing_rects, CLUSTER_GAP_PT):
        fraction = (rect.width * rect.height) / page_area
        if fraction < 0.08 or count < MIN_CLUSTER_ITEMS:
            continue
        # Already covered by a raster region (annotations over an image)?
        if any((rect & taken).get_area() > 0.7 * rect.get_area() for taken in image_rects):
            continue
        # A near-page-sized cluster of few items is page furniture, not a diagram.
        if fraction > 0.9 and count < 12:
            continue
        regions.append((rect & page.rect, "region_render"))

    # Biggest first; one slide contributes at most a handful of candidates.
    regions.sort(key=lambda entry: entry[0].get_area(), reverse=True)
    candidates: list[VisualCandidate] = []
    for index, (rect, origin) in enumerate(regions[:MAX_CANDIDATES_PER_SLIDE], start=1):
        visual_id = f"visual_{page_no:03d}_{index:02d}"
        out_path = out_dir / f"{visual_id}.png"
        _render_clip(page, rect, out_path)
        candidate = _finish_candidate(VisualCandidate(
            visual_id=visual_id, slide_no=page_no, source_type="pdf",
            image_path=out_path, origin=origin,
            slide_heading=_slide_heading(slides, page_no)))
        if candidate:
            candidates.append(candidate)
        else:
            out_path.unlink(missing_ok=True)
    return candidates


def _extract_from_pdf(pdf_path: Path, slides: list[SlideData], out_dir: Path,
                      issues: list[str]) -> list[VisualCandidate]:
    import fitz

    candidates: list[VisualCandidate] = []
    with fitz.open(str(pdf_path)) as doc:
        for page_no, page in enumerate(doc, start=1):
            try:
                candidates.extend(_pdf_page_candidates(page, page_no, out_dir, slides))
            except Exception as e:  # noqa: BLE001 — one bad page must not kill the stage
                issues.append(f"Visual extraction failed for slide {page_no}: {e}")
    return candidates


# --------------------------------------------------------------------------
# PPTX
# --------------------------------------------------------------------------
_EMU_PER_PX_BASE = 9525  # EMU per pixel at 96 DPI


def _pptx_shape_rect(shape) -> tuple[int, int, int, int] | None:
    try:
        left, top = int(shape.left), int(shape.top)
        width, height = int(shape.width), int(shape.height)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return left, top, width, height


def _crop_rendered_slide(slide_data: SlideData, bbox_emu, slide_w_emu: int,
                         slide_h_emu: int, out_path: Path) -> bool:
    """Crop an EMU bbox out of the slide's rendered PNG (PowerPoint/LibreOffice)."""
    if not slide_data or not slide_data.image_path or not Path(slide_data.image_path).exists():
        return False
    try:
        from PIL import Image

        with Image.open(slide_data.image_path) as im:
            img = im.convert("RGB")
        px_w, px_h = img.size
        left, top, width, height = bbox_emu
        pad_x, pad_y = int(px_w * 0.01), int(px_h * 0.01)
        x0 = max(0, int(left / slide_w_emu * px_w) - pad_x)
        y0 = max(0, int(top / slide_h_emu * px_h) - pad_y)
        x1 = min(px_w, int((left + width) / slide_w_emu * px_w) + pad_x)
        y1 = min(px_h, int((top + height) / slide_h_emu * px_h) + pad_y)
        if x1 - x0 < 60 or y1 - y0 < 60:
            return False
        img.crop((x0, y0, x1, y1)).save(out_path)
        return True
    except Exception:
        return False


def _extract_from_pptx(ppt_path: Path, slides: list[SlideData], out_dir: Path,
                       issues: list[str]) -> list[VisualCandidate]:
    try:
        from pptx import Presentation
    except Exception as e:
        issues.append(f"python-pptx unavailable for visual extraction: {e}")
        return []

    prs = Presentation(str(ppt_path))
    slide_w = int(prs.slide_width or 1)
    slide_h = int(prs.slide_height or 1)
    slide_area = slide_w * slide_h
    by_no = {s.slide_no: s for s in slides}

    candidates: list[VisualCandidate] = []
    for slide_no, slide in enumerate(prs.slides, start=1):
        try:
            found = 0
            drawing_rects: list[tuple[int, int, int, int]] = []
            drawing_count = 0
            for shape in slide.shapes:
                rect = _pptx_shape_rect(shape)
                if rect is None:
                    continue
                try:
                    shape_type = int(getattr(shape, "shape_type", 0) or 0)
                except (TypeError, ValueError):
                    shape_type = 0
                fraction = (rect[2] * rect[3]) / slide_area
                if shape_type in (13, 11) and fraction >= MIN_AREA_FRACTION:  # PICTURE
                    found += 1
                    visual_id = f"visual_{slide_no:03d}_{found:02d}"
                    out_path = out_dir / f"{visual_id}.png"
                    saved = False
                    try:
                        from io import BytesIO

                        from PIL import Image

                        with Image.open(BytesIO(shape.image.blob)) as im:
                            im.convert("RGB").save(out_path)
                        saved = True
                    except Exception:
                        # Fall back to cropping the rendered slide PNG.
                        saved = _crop_rendered_slide(
                            by_no.get(slide_no), rect, slide_w, slide_h, out_path)
                    if saved:
                        origin = ("full_slide" if fraction >= FULL_SLIDE_FRACTION
                                  else "embedded_image")
                        candidate = _finish_candidate(VisualCandidate(
                            visual_id=visual_id, slide_no=slide_no, source_type="pptx",
                            image_path=out_path, origin=origin,
                            slide_heading=(by_no.get(slide_no).heading
                                           if by_no.get(slide_no) else "")))
                        if candidate:
                            candidates.append(candidate)
                        else:
                            out_path.unlink(missing_ok=True)
                            found -= 1
                    else:
                        found -= 1
                elif shape_type in (1, 5, 6, 9):   # autoshape/freeform/group/line
                    drawing_rects.append(rect)
                    drawing_count += 1
                elif shape_type in (3, 21) or getattr(shape, "has_chart", False):
                    # A chart or SmartArt frame is a diagram on its own.
                    drawing_rects.append(rect)
                    drawing_count += MIN_CLUSTER_ITEMS

            # Shape-built diagram (SmartArt, arrows, boxes): crop the union
            # region out of the rendered slide, since no image file exists.
            if drawing_count >= MIN_CLUSTER_ITEMS and found < MAX_CANDIDATES_PER_SLIDE:
                x0 = min(r[0] for r in drawing_rects)
                y0 = min(r[1] for r in drawing_rects)
                x1 = max(r[0] + r[2] for r in drawing_rects)
                y1 = max(r[1] + r[3] for r in drawing_rects)
                union = (x0, y0, x1 - x0, y1 - y0)
                if (union[2] * union[3]) / slide_area >= 0.15:
                    found += 1
                    visual_id = f"visual_{slide_no:03d}_{found:02d}"
                    out_path = out_dir / f"{visual_id}.png"
                    if _crop_rendered_slide(by_no.get(slide_no), union, slide_w,
                                            slide_h, out_path):
                        candidate = _finish_candidate(VisualCandidate(
                            visual_id=visual_id, slide_no=slide_no, source_type="pptx",
                            image_path=out_path, origin="region_render",
                            slide_heading=(by_no.get(slide_no).heading
                                           if by_no.get(slide_no) else "")))
                        if candidate:
                            candidates.append(candidate)
                        else:
                            out_path.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001 — isolate per-slide failures
            issues.append(f"Visual extraction failed for slide {slide_no}: {e}")
    return candidates


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def extract_candidates(input_path: Path, slides: list[SlideData], run_dir: Path,
                       issues: list[str]) -> list[VisualCandidate]:
    """All plausible visual regions in the lecture file, cropped and hashed.

    ``issues`` collects per-slide failures; the function itself only raises on
    a completely unreadable file, which the caller treats as "no visuals".
    """
    input_path = Path(input_path)
    out_dir = Path(run_dir) / VISUALS_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = input_path.suffix.lower()
    if ext == ".pdf":
        return _extract_from_pdf(input_path, slides, out_dir, issues)
    if ext in {".pptx", ".ppt"}:
        if ext == ".ppt":
            # Legacy binary .ppt: python-pptx cannot open it. The rendered slide
            # PNGs (PowerPoint COM export) are still available, so full-slide
            # candidates are made from any slide whose render exists.
            candidates: list[VisualCandidate] = []
            for s in slides:
                if not s.image_path or not Path(s.image_path).exists():
                    continue
                visual_id = f"visual_{s.slide_no:03d}_01"
                out_path = out_dir / f"{visual_id}.png"
                try:
                    from PIL import Image

                    with Image.open(s.image_path) as im:
                        im.convert("RGB").save(out_path)
                except Exception as e:
                    issues.append(f"Visual extraction failed for slide {s.slide_no}: {e}")
                    continue
                candidate = _finish_candidate(VisualCandidate(
                    visual_id=visual_id, slide_no=s.slide_no, source_type="pptx",
                    image_path=out_path, origin="full_slide",
                    slide_heading=s.heading or ""))
                if candidate:
                    candidates.append(candidate)
                else:
                    out_path.unlink(missing_ok=True)
            return candidates
        return _extract_from_pptx(input_path, slides, out_dir, issues)
    return []
