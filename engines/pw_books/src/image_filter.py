"""Academic-relevance gate for the images that reach the notes.

The concise notes used to take an image whenever a slide had one, which filled
the document with question screenshots, teacher photos, banners and repeats of
the same diagram — and made every run pay for expensive downstream work
(crop -> alpha -> AI redraw -> insert) on pictures that were then thrown away
by a human.

This module is the single decision point. It is deliberately CHEAP and runs
EARLY, so a rejected image never enters the redraw/insert path:

    text screen  ->  pixel screen  ->  duplicate screen  ->  section budget

The default is REJECT. An image is kept only when it is a real academic diagram
that carries more than the text around it (rule: visual_value > textual_value).

Nothing here is subject-specific: the same gate serves Physics, Chemistry,
Mathematics and Biology.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .style_config import (
    IMAGE_ASSUMED_DPI,
    IMAGE_MAX_HEIGHT_IN,
    IMAGE_MAX_UPSCALE,
    IMAGE_MAX_WIDTH_IN,
    IMAGE_MIN_WIDTH_IN,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------
# What a KEPT image is allowed to be. Used for reporting/QC, not for gating —
# gating is done by the screens below, whose default is rejection.
ACCEPT_CATEGORIES = (
    "concept_diagram", "graph", "ray_diagram", "circuit_diagram",
    "geometry_construction", "biological_diagram", "chemical_structure",
    "mechanism", "apparatus", "flow_diagram",
)

# Slide text that means the picture belongs to a QUESTION. Mandatory rejection
# (spec: "REMOVE QUESTION IMAGES"), even when a diagram is visible inside it.
_QUESTION_RE = [
    r"\bASQ\s*[-:#]?\s*\d*\b",
    r"\bMCQ\b", r"\bPYQ\b", r"\bDPP\b",
    # "Question 4" needs its number; the bare word appears in normal teaching
    # prose ("this raises an important question about stability"). A slide
    # HEADED just "Question"/"Questions" is still caught by the line form.
    r"\bquestion\s*[-:#]?\s*\d+\b",
    r"^\s*questions?\s*[-:#]?\s*\d*\s*$",
    # "Q.9" / "Q-9" / "Q: 9" / "Q) 9". The separator is REQUIRED so physics
    # symbols like Q1, Q2 (charges) and "Q = mc\Delta T" are not misread as
    # question labels.
    r"(?<![A-Za-z0-9])Q\s*[.\-:#)]\s*\d+",
    # Three lettered options running inline: unmistakably an answer list.
    r"\(\s*[aA]\s*\)[^\n]{0,120}\(\s*[bB]\s*\)[^\n]{0,120}\(\s*[cC]\s*\)",
    # A line that OPENS with an imperative — how a problem statement is written.
    r"^\s*(find|calculate|determine|evaluate|prove|show\s+that)\b",
    r"\bpractice\s+(question|problem|sheet)\b",
    r"\btry\s+(it\s+)?yourself\b",
    r"\bhome\s*work\b",
    # "Exercise 7.1" is a problem set; bare "exercise" is biology prose
    # ("effect of exercise on heart rate") and must NOT reject the slide.
    r"\bexercise\s*[-–:]?\s*\d+(?:\.\d+)?\b",
    r"\b(single|multiple)\s+correct\b",
    r"\bchoose\s+the\s+correct\b",
    r"\bcorrect\s+(option|answer|statement)\b",
    r"\bincorrect\s+statement\b",
    r"\bmark\s+the\s+correct\b",
    r"\bfind\s+the\s+value\s+of\b",
    r"\bthen\s+the\s+value\s+of\b",
    r"\bis\s+equal\s+to\s*[:?]",
    r"\boptions?\s*[:\-]",
    # A lone answer option is SHORT ("(a) 10 cm"). Longer lettered lines are
    # case analyses ("(a) Object beyond C - real, inverted, diminished") that
    # legitimately sit next to a diagram; real MCQ slides are still caught by
    # the Q-number/three-option/choose-the-correct patterns above.
    r"^\s*\(?[a-d]\)[\s.].{0,25}$",
    # Worked-solution labels open a line. Unanchored, these rejected the whole
    # chemistry "Solutions" chapter and any prose crossing "... the solution."
    r"^\s*sol(?:ution)?\s*[:.]",
    r"^\s*answer\s*[:.\-]",
    r"\billustration\s*[-:#]?\s*\d+\b",
    r"\bexample\s*[-:#]?\s*\d+\b",
]

# Slide text that means the picture is DECORATION or BRANDING, never academic.
_DECORATIVE_RE = [
    r"\bthank\s*you\b", r"\bwelcome\b",
    r"\bscan\s*(the\s*)?(qr|code)\b", r"\bqr\s*code\b",
    r"\bdownload\s+.*\bapp\b", r"\bmaster\s+ncert\s+with\b",
    r"\btelegram\b", r"\bsubscribe\b", r"\binstagram\b", r"\bwhats\s*app\b",
    r"\bfollow\s+(me|us)\b", r"\blet[’']?s\s+grow\s+together\b",
    r"\bbatch\b.*\blaunch\b", r"\bfaculty\b", r"\babout\s+me\b",
    r"\bsocial\s+media\b", r"@[a-z0-9_.]{3,}",
    r"\btopics?\s+to\s+be\s+covered\b",
]

# Words that positively indicate an academically useful figure. Their presence
# is what lets an image through the "is this worth a picture?" test.
_DIAGRAM_HINT_RE = [
    (r"\bgraph\b|\bplot\b|\bcurve\b|\bvs\.?\s|\bversus\b|\bx[-\s]?axis\b|\by[-\s]?axis\b", "graph"),
    (r"\bray\s+diagram\b|\bmirror\b|\blens\b|\brefract|\breflect|\bincident\b|\bnormal\b", "ray_diagram"),
    (r"\bcircuit\b|\bresistor\b|\bcapacitor\b|\bammeter\b|\bvoltmeter\b|\bbattery\b|\bemf\b", "circuit_diagram"),
    (r"\btriangle\b|\bangle\b|\bquadrant\b|\bcoordinate\b|\bgeometr|\bconstruction\b|\bunit\s+circle\b", "geometry_construction"),
    (r"\bcell\b|\bnucleus\b|\btissue\b|\borgan\b|\bcycle\b|\bpathway\b|\bstructure\s+of\b|\bl\.?s\.?\b|\bt\.?s\.?\b", "biological_diagram"),
    (r"\bstructure\b|\bbond\b|\bhybrid|\bresonance\b|\bisomer|\borbital\b|\bgeometry\b", "chemical_structure"),
    (r"\bmechanism\b|\bstep\s*\d\b|\bintermediate\b|\breaction\b|\bpathway\b", "mechanism"),
    (r"\bapparatus\b|\bset[-\s]?up\b|\bexperiment\b|\bmanometer\b|\bcalorimeter\b", "apparatus"),
    (r"\bflow\s*chart\b|\bclassificat|\bhierarch|\bsequence\b", "flow_diagram"),
    (r"\bdiagram\b|\bfigure\b|\bfig\.?\s*\d|\blabel|\bshown\s+(in|below)\b|\bsketch\b", "concept_diagram"),
]


def _search_any(text: str, patterns) -> str | None:
    for p in patterns:
        m = re.search(p, text, flags=re.I | re.M)
        if m:
            return m.group(0).strip()[:60]
    return None


@dataclass
class ImageDecision:
    """Why one image was kept or dropped. `accept` is the only gate.

    `code` is the machine-readable rejection class ("budget", "section_cap",
    "duplicate", ...). Callers that branch on WHY something was rejected must
    use it — the `reason` text is for humans and may be reworded freely.
    """
    accept: bool
    reason: str
    category: str = ""
    code: str = ""
    detail: dict = field(default_factory=dict)

    def __bool__(self) -> bool:      # `if gate.accept_image(...):`
        return self.accept


# --------------------------------------------------------------------------
# Pixel-level metrics (cheap: everything runs on a ~200px-wide copy)
# --------------------------------------------------------------------------
_WORK_WIDTH = 200


def _metrics(path: Path) -> dict | None:
    """Shape/ink statistics used to spot icons, blanks and text screenshots.

    Returns None when the file cannot be read (caller then fails CLOSED).
    """
    try:
        from PIL import Image
        import numpy as np
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            im.load()
            width, height = im.size
            if width < 8 or height < 8:
                return {"width": width, "height": height, "unreadable": True}
            sw = min(_WORK_WIDTH, width)
            sh = max(1, int(height * (sw / width)))
            mask = None
            if im.mode in ("RGBA", "LA"):
                small = im.convert("RGBA").resize((sw, sh), Image.BILINEAR)
                alpha = np.asarray(small.split()[-1], dtype=np.int16)
                # The alpha channel is only "where the ink is" when the image
                # actually uses transparency. A fully opaque RGBA (alpha=255
                # everywhere) would read as ink=1.0 and sail past every screen
                # — including a blank white canvas — so it must be measured by
                # colour like any RGB image, flattened onto white first.
                if (alpha < 250).mean() > 0.005:
                    mask = alpha > 24
                else:
                    flat = Image.new("RGB", small.size, (255, 255, 255))
                    flat.paste(small, mask=small.split()[-1])
                    im = flat
            if mask is None:
                small = im.convert("RGB").resize((sw, sh), Image.BILINEAR)
                arr = np.asarray(small, dtype=np.int16)
                corners = np.array([arr[0, 0], arr[0, -1], arr[-1, 0], arr[-1, -1]], dtype=np.int16)
                bg = corners.mean(axis=0)
                mask = np.abs(arr - bg).sum(axis=2) > 60
    except Exception as e:
        logger.debug("image metrics failed for %s: %r", path, e)
        return None

    ink = float(mask.mean())
    row_ink = mask.mean(axis=1)
    col_ink = mask.mean(axis=0)
    # Horizontal ink bands = candidate text lines.
    rows = row_ink > 0.02
    bands: list[int] = []
    gaps: list[int] = []
    run = 0
    gap = 0
    for on in rows:
        if on:
            if gap:
                gaps.append(gap)
                gap = 0
            run += 1
        else:
            if run:
                bands.append(run)
                run = 0
            gap += 1
    if run:
        bands.append(run)
    # Longest UNBROKEN vertical stroke, as a fraction of the height. This is the
    # discriminator between a figure and a block of prose: an axis, a mirror, a
    # ray, a frame or a cell outline runs for a long way down the image, whereas
    # the tallest continuous mark in text is one line of type.
    # (Total per-column ink does NOT work here — left-aligned text puts ink in
    # the same columns on every line and reads as a "long stroke".)
    run_len = np.zeros(mask.shape[1], dtype=np.int32)
    best_run = np.zeros(mask.shape[1], dtype=np.int32)
    for row in mask:
        run_len = np.where(row, run_len + 1, 0)
        best_run = np.maximum(best_run, run_len)
    max_vertical_run = float(best_run.max()) / max(1, sh)
    return {
        "width": width,
        "height": height,
        "aspect": width / height if height else 0.0,
        "ink": ink,
        "bands": len(bands),
        "band_median": (sorted(bands)[len(bands) // 2] if bands else 0),
        "band_max": (max(bands) if bands else 0),
        "gap_median": (sorted(gaps)[len(gaps) // 2] if gaps else 0),
        "small_height": sh,
        "max_vertical_run": max_vertical_run,
        "row_ink_max": float(row_ink.max()),
        "col_ink_max": float(col_ink.max()),
        "unreadable": False,
    }


# A stroke running down at least this fraction of the image means a figure.
_STROKE_FRAC = 0.20
# Lines of type are all about the same height. Once the tallest ink band is this
# much taller than the typical one, something that is NOT a line of text is on
# the image — a triangle, a curve, a labelled figure — and it must be kept.
# Measured: uniform prose 1.0, a formula slide carrying a 3-4-5 triangle 2.6,
# a quadrant diagram 3.7.
_BAND_REGULARITY = 2.0


def _looks_like_text_screenshot(m: dict) -> bool:
    """Several thin, regularly spaced ink bands and no long stroke == prose.

    A screenshot of slide text adds nothing the notes do not already say, so it
    is rejected (spec: "screenshots of text"). A graph or a labelled figure has
    an axis/frame/ray running down the image and survives this test.
    """
    if m["bands"] < 5:
        return False
    if m.get("max_vertical_run", 0.0) >= _STROKE_FRAC:
        return False                      # an axis/frame/ray -> a real figure
    if m["ink"] > 0.42:
        return False                      # dense artwork, not lines of text
    band = m["band_median"]
    gap = m["gap_median"]
    if not band or not gap:
        return False
    if m.get("band_max", 0) > _BAND_REGULARITY * band:
        return False                      # an irregular band -> a figure is present
    # Text lines are thin relative to the block and separated by similar gaps.
    return band <= max(3, 0.12 * m["small_height"]) and gap <= 4 * band


# --------------------------------------------------------------------------
# Duplicate detection
# --------------------------------------------------------------------------
def average_hash(path: Path) -> int | None:
    """64-bit perceptual average hash. None when the file cannot be read."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            im.load()
            if im.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
                im = bg
            small = im.convert("L").resize((8, 8), Image.BILINEAR)
        pixels = list(small.tobytes())      # mode "L" -> one byte per pixel
        avg = sum(pixels) / len(pixels)
        bits = 0
        for i, p in enumerate(pixels):
            if p >= avg:
                bits |= 1 << i
        return bits
    except Exception:
        return None


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Sizing
# --------------------------------------------------------------------------
def fit_image_size(path: Path) -> tuple[float, float]:
    """Width/height in INCHES for this image inside the notes page.

    Width is chosen from the image's own resolution — not forced to one number —
    then clamped so the picture never crosses the margins, never grows taller
    than a section's worth of page, and never gets upscaled into a blur.
    Falls back to a safe default when the file cannot be measured.
    """
    default_w = min(IMAGE_MAX_WIDTH_IN, 5.80)
    try:
        from PIL import Image

        with Image.open(path) as im:
            px_w, px_h = im.size
            dpi = im.info.get("dpi") or ()
        dpi_x = float(dpi[0]) if dpi and dpi[0] else IMAGE_ASSUMED_DPI
    except Exception:
        return default_w, 0.0
    if not px_w or not px_h:
        return default_w, 0.0
    if dpi_x <= 1 or dpi_x > 1200:
        dpi_x = IMAGE_ASSUMED_DPI
    native_w = px_w / dpi_x
    ratio = px_h / px_w

    width = min(IMAGE_MAX_WIDTH_IN, max(IMAGE_MIN_WIDTH_IN, native_w * IMAGE_MAX_UPSCALE))
    height = width * ratio
    if height > IMAGE_MAX_HEIGHT_IN:                    # tall figure: fit by height
        # The height limit wins over the minimum width: a tall narrow strip must
        # stay on the page, and stretching it to a "nicer" width would both
        # break the aspect ratio and push the figure past the bottom margin.
        height = IMAGE_MAX_HEIGHT_IN
        width = height / ratio if ratio else width
    return round(width, 3), round(height, 3)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


class ImageGate:
    """Decides which images may appear in the notes, and how often.

    One instance per run. It is shared by the pipeline (early screening, before
    any billed redraw) and the DOCX writer (final insertion + de-duplication),
    so a picture rejected early can never sneak back in through a hallucinated
    "Note to DTP" reference.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_per_section: int = 1,
        max_total: int | None = None,
        hash_distance: int = 6,
        min_side_px: int = 120,
    ) -> None:
        self.enabled = enabled
        self.max_per_section = max(1, max_per_section)
        if max_total is None:
            max_total = _env_int("PW_MAX_NOTE_IMAGES", 0)
        self.max_total = max_total if max_total and max_total > 0 else None
        self.hash_distance = max(0, hash_distance)
        self.min_side_px = max(1, min_side_px)

        self._slide_cache: dict[int, ImageDecision] = {}
        self._hashes: list[tuple[int, str]] = []      # (ahash, label)
        self._digests: dict[str, str] = {}            # md5 -> label
        self._per_section: dict[str, int] = {}
        self.accepted = 0
        self.rejections: list[dict] = []

    # ---- reporting -------------------------------------------------------
    def _reject(self, reason: str, *, label: str = "", category: str = "", code: str = "", **detail) -> ImageDecision:
        self.rejections.append({"label": label, "reason": reason, **detail})
        return ImageDecision(False, reason, category=category, code=code, detail=detail)

    def report(self) -> dict:
        by_reason: dict[str, int] = {}
        for r in self.rejections:
            key = r["reason"].split("(")[0].strip()
            by_reason[key] = by_reason.get(key, 0) + 1
        return {
            "enabled": self.enabled,
            "accepted": self.accepted,
            "rejected": len(self.rejections),
            "rejected_by_reason": by_reason,
            "rejections": self.rejections,
        }

    # ---- stage 1: slide text (no pixels touched) -------------------------
    def screen_slide(self, slide) -> ImageDecision:
        """Cheapest possible screen: may this slide's picture be used at all?

        Runs before ANY expensive stage (crop, alpha, AI redraw), which is the
        whole point — a question screenshot must cost nothing.
        """
        if not self.enabled:
            return ImageDecision(True, "image filtering disabled")
        key = getattr(slide, "slide_no", None)
        if key is not None and key in self._slide_cache:
            return self._slide_cache[key]

        label = f"slide {key}" if key is not None else "image"
        text = "\n".join(str(getattr(slide, f, "") or "")
                         for f in ("heading", "text", "cleaned_text"))
        decision: ImageDecision
        if not getattr(slide, "image_path", None):
            decision = self._reject("slide has no image", label=label, code="no_image")
        elif (hit := _search_any(text, _QUESTION_RE)):
            decision = self._reject(f"question/MCQ/solution slide ({hit!r})", label=label, code="question")
        elif (hit := _search_any(text, _DECORATIVE_RE)):
            decision = self._reject(f"decorative/branding slide ({hit!r})", label=label, code="decorative")
        elif getattr(slide, "filtered", False) and getattr(slide, "filter_reason", None):
            decision = self._reject(str(slide.filter_reason), label=label, code="legacy_filter")
        else:
            category = ""
            for pattern, name in _DIAGRAM_HINT_RE:
                if re.search(pattern, text, flags=re.I):
                    category = name
                    break
            decision = ImageDecision(True, "slide text allows a diagram", category=category)
        if key is not None:
            self._slide_cache[key] = decision
        return decision

    # ---- stage 2: pixels -------------------------------------------------
    def screen_pixels(self, path: Path, *, label: str = "") -> ImageDecision:
        """Reject icons, blanks, banners and screenshots of text."""
        if not self.enabled:
            return ImageDecision(True, "image filtering disabled")
        path = Path(path)
        label = label or path.name
        if not path.exists():
            return self._reject("image file is missing", label=label, code="missing_file")
        m = _metrics(path)
        if m is None:
            # Fail CLOSED: an image we cannot measure is an image we cannot vouch
            # for, and the default behaviour of this gate is "no image".
            return self._reject("image could not be read for relevance checks", label=label, code="unreadable")
        if m.get("unreadable"):
            return self._reject("image is too small to carry a diagram", label=label, code="too_small")
        if min(m["width"], m["height"]) < self.min_side_px:
            return self._reject(
                f"icon/logo-sized image ({m['width']}x{m['height']} px)", label=label, code="too_small")
        if not 0.15 <= m["aspect"] <= 6.0:
            return self._reject(
                f"banner/strip proportions (aspect {m['aspect']:.2f})", label=label, code="aspect")
        if m["ink"] < 0.012:
            return self._reject(f"almost empty image (ink {m['ink']:.3f})", label=label, code="blank")
        if _looks_like_text_screenshot(m):
            return self._reject("screenshot of text, not a diagram", label=label, code="text_screenshot")
        return ImageDecision(True, "image looks like a real figure", detail=m)

    # ---- stage 3: duplicates + budget, then commit -----------------------
    def _near_dup_distance(self, ahash: int) -> int:
        """How close (Hamming) another hash may be before it counts as a dup.

        A sparse line diagram averages to a near-white 8x8 grid: ~80% of the
        bits agree for ANY two such figures, so the effective entropy is a
        handful of bits and the normal threshold rejects genuinely different
        ray diagrams that merely share a horizontal axis (measured: distance
        4-6 between two DIFFERENT optics figures). When the hash is that
        lopsided, only an (almost) exact match may count as a duplicate —
        the md5 digest still catches byte-identical repeats regardless.
        """
        ones = bin(ahash).count("1")
        effective_bits = min(ones, 64 - ones)
        if effective_bits < 16:
            return min(self.hash_distance, 2)
        return self.hash_distance

    def accept_image(self, path: Path, *, slide=None, section: str = "", label: str = "", commit: bool = True) -> ImageDecision:
        """Full check for an image about to be inserted.

        `section` is the section heading the image sits under; at most
        `max_per_section` images are allowed under one heading. That is a CAP,
        never a quota — sections with no useful diagram simply get none.

        With `commit=True` (the default) an accepted image is registered
        immediately. Callers with a fallible step between the decision and the
        image actually landing on the page (the DOCX writer) pass
        `commit=False` and call `commit_image` AFTER the insertion succeeds —
        otherwise a failed insertion would still consume the section's cap and
        poison the duplicate registry with an image that is not on any page.
        """
        path = Path(path)
        label = label or (f"slide {getattr(slide, 'slide_no', '?')}" if slide is not None else path.name)
        if not self.enabled:
            decision = ImageDecision(True, "image filtering disabled")
            if commit:
                self.accepted += 1
            return decision

        if slide is not None:
            slide_ok = self.screen_slide(slide)
            if not slide_ok.accept:
                return slide_ok

        # Caps first: they are dict lookups, while every screen below has to
        # decode the image. A candidate under an already-served heading must
        # cost nothing.
        if self.max_total is not None and self.accepted >= self.max_total:
            return self._reject(
                f"image budget for the document is used up ({self.max_total})",
                label=label, code="budget")
        used = self._per_section.get(section, 0)
        if section and used >= self.max_per_section:
            return self._reject(
                f"section {section!r} already has {used} diagram(s)",
                label=label, code="section_cap")

        # An AI-redrawn figure has already been validated as a real image and
        # was purpose-drawn for this note AFTER its slide passed the gate; the
        # pixel heuristics are tuned for raw slide screenshots and misread a
        # handwritten flowchart (rows of short labels) as "text". Skipping them
        # here protects an image the run has already paid for. Dedupe and the
        # caps above still apply.
        ai_redrawn = bool(slide is not None
                          and (getattr(slide, "metadata", None) or {}).get("ai_redrawn"))
        if ai_redrawn:
            pixel_ok = ImageDecision(True, "AI-redrawn diagram (pixel screens skipped)")
        else:
            pixel_ok = self.screen_pixels(path, label=label)
            if not pixel_ok.accept:
                return pixel_ok

        digest = _file_digest(path)
        if digest and digest in self._digests:
            return self._reject(
                f"identical image already inserted ({self._digests[digest]})",
                label=label, code="duplicate")
        ahash = average_hash(path)
        if ahash is not None:
            limit = self._near_dup_distance(ahash)
            for known, known_label in self._hashes:
                if hamming(ahash, known) <= limit:
                    return self._reject(
                        f"near-duplicate of the diagram already inserted ({known_label})",
                        label=label, code="near_duplicate")

        decision = ImageDecision(
            True, "accepted", category=pixel_ok.category,
            detail={**pixel_ok.detail, "digest": digest, "ahash": ahash},
        )
        if commit:
            self.commit_image(decision, section=section, label=label)
        return decision

    def commit_image(self, decision: ImageDecision, *, section: str = "", label: str = "") -> None:
        """Register an image accepted with `commit=False` once it is REALLY on
        the page. Consumes the section/document budget and feeds the duplicate
        registry; a decision that was not an accept is ignored."""
        if not decision.accept:
            return
        self.accepted += 1
        if not self.enabled:
            return
        digest = decision.detail.get("digest")
        ahash = decision.detail.get("ahash")
        if digest:
            self._digests[digest] = label
        if ahash is not None:
            self._hashes.append((ahash, label))
        if section:
            self._per_section[section] = self._per_section.get(section, 0) + 1

    # ---- AI generation gate ---------------------------------------------
    def should_generate_ai_image(self, slide, *, source_available: bool = True) -> ImageDecision:
        """IMAGE_NEEDED — the explicit switch in front of any image-model call.

        True only when the slide's diagram is academically approved AND there is
        something to redraw. A source diagram that already reads well is kept
        as-is: redrawing it costs money, costs minutes and risks fabricating
        labels, so it is the last resort, not the default.
        """
        if not source_available:
            key = getattr(slide, "slide_no", None)
            return self._reject("no source image to work from",
                                label=f"slide {key}" if key is not None else "image",
                                code="no_source")
        return self.screen_slide(slide)

    def approve_slides(self, slides, slide_numbers) -> tuple[set[int], list[str]]:
        """Screen a set of candidate slides in one go (used before AI redraw).

        Returns (approved slide numbers, human-readable warnings).
        """
        approved: set[int] = set()
        warnings: list[str] = []
        wanted = set(slide_numbers or ())
        for s in slides:
            if s.slide_no not in wanted:
                continue
            has_image = bool(getattr(s, "image_path", None) and Path(s.image_path).exists())
            decision = self.should_generate_ai_image(s, source_available=has_image)
            if decision.accept:
                approved.add(s.slide_no)
            else:
                warnings.append(
                    f"Slide {s.slide_no}: image not used in the notes — {decision.reason}."
                )
        return approved, warnings
