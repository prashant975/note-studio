"""Render legacy diagrams or strict manifest-backed source-visual assets.

The legacy one-argument API keeps its best-effort manual-placeholder behavior.
The manifest API instead uses bounded provider-neutral redraw and verification,
immutable source-label compositing, and a complete prepared-source fallback for
every visual.
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np

from . import config
from . import cost_meter
from .errors import VisualFidelityError
from .generation_policy import GenerationRun
from .pdf_utils import PdfChunk, extract_chunk_page
from .prompts import Section, ShortNotes, VISUAL_STYLE, Visual
from .visual_assets import (
    PreparedFallback,
    composite_source_labels,
    prepare_source_fallback,
    validate_composited_labels,
)
from .visual_inventory import ModelFile
from .visual_manifest import (
    BufferReservation,
    RedrawVerdict,
    SourceCrop,
    VisualAsset,
    VisualManifest,
    VisualMemoryBudget,
    VisualRunBudget,
    manifest_ids,
)

_DATA_URL_RE = re.compile(r"^data:image/[a-zA-Z]+;base64,")

_SOURCE_REDRAW_INSTRUCTION = (
    "SOURCE REDRAW LOCK: the drawing specification below was transcribed "
    "ONLY from the uploaded teacher PDF. Faithfully redraw ONLY the "
    "components, relationships, arrows, and labels explicitly named in it. "
    "Do not add, infer, complete, correct, or label any standard textbook "
    "detail that the specification does not name. If a detail is uncertain, "
    "omit it."
)

_MANIFEST_SKETCH_STYLE = (
    "Use a restrained hand-drawn coloured-pencil and fine-liner notebook style. "
    "Keep every source component, relationship, arrow, bond, axis, curve, tick, "
    "and blank label position at the exact source geometry. Draw no letters, "
    "numbers, symbols, equations, or other textual glyphs: immutable source-label "
    "layers are added locally after verification. Return one PNG at the exact "
    "requested pixel dimensions with a genuinely transparent RGBA background, "
    "not white/dark paper, a frame, or a painted transparency checkerboard."
)

_CONTEXT_BOUNDARY = (
    "Context images may clarify only the teacher's existing topic or graph in "
    "the exact visual crop. They cannot authorize any component, mark, arrow, "
    "curve, value, or label absent from that crop and its approved manifest."
)

RedrawFn = Callable[
    [SourceCrop, PreparedFallback, VisualRunBudget, RedrawVerdict | None],
    bytes | None,
]
VerifyFn = Callable[
    [SourceCrop, bytes, PreparedFallback, VisualRunBudget],
    RedrawVerdict,
]


def enabled() -> bool:
    return bool(config.NOTES_IMAGES and config.litellm_key())


def _model() -> str:
    m = (config.IMAGE_MODEL or "").strip()
    return m if m.startswith("vertex_ai/") else f"vertex_ai/{m}"


@dataclass(frozen=True)
class CoreVisualJob:
    key: str
    section: Section
    plan: Visual
    source_page_pdf: bytes


def _bounded_unique(
    values: list[str],
    *,
    maximum: int,
    chars: int,
) -> tuple[str, ...] | None:
    if not isinstance(values, list) or len(values) > maximum:
        return None
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            return None
        clean = re.sub(r"\s+", " ", value.strip())
        key = clean.casefold()
        if not clean or len(clean) > chars or key in seen:
            return None
        seen.add(key)
        result.append(clean)
    return tuple(result)


def _select_sequence_visual_jobs(
    notes: ShortNotes,
    run: GenerationRun,
) -> list[CoreVisualJob]:
    """Validate source-locked plans and extract at most two exact pages."""
    jobs: list[CoreVisualJob] = []
    limit = min(
        2,
        max(0, int(config.MAX_IMAGES)),
        max(0, int(run.max_images)),
    )
    for section_index, section in enumerate(notes.sections):
        run.check_deadline()
        plan = section.visual
        if plan is None or (plan.kind or "").strip().casefold() != "core_diagram":
            continue

        chunk = getattr(section, "_source_chunk", None)
        page = plan.source_local_page
        order = section.source_order
        key = ""
        if (
            isinstance(chunk, PdfChunk)
            and isinstance(page, int)
            and not isinstance(page, bool)
            and isinstance(order, int)
            and not isinstance(order, bool)
        ):
            key = f"c{chunk.index}-p{page}-s{order}-i{section_index}"

        components = _bounded_unique(
            plan.exact_components,
            maximum=24,
            chars=120,
        )
        labels = _bounded_unique(
            plan.exact_labels,
            maximum=24,
            chars=80,
        )
        composition_note = re.sub(
            r"\s+", " ", (plan.composition_note or "").strip()
        )
        valid = (
            isinstance(chunk, PdfChunk)
            and bool(key)
            and 1 <= page <= chunk.page_count
            and not (plan.prompt or "").strip()
            and components is not None
            and bool(components)
            and labels is not None
            and 1 <= len(composition_note) <= 400
        )
        if not valid:
            if key:
                run.mark_image_omitted(key)
            continue

        normalized_plan = plan.model_copy(
            update={
                "exact_components": list(components),
                "exact_labels": list(labels),
                "composition_note": composition_note,
                "prompt": "",
            }
        )
        run.mark_visual_eligible(key)
        if len(jobs) >= limit:
            run.mark_image_omitted(key)
            continue
        try:
            source_page_pdf = extract_chunk_page(chunk, page, budget=run)
        except Exception:
            run.mark_image_omitted(key)
            continue
        jobs.append(
            CoreVisualJob(
                key=key,
                section=section,
                plan=normalized_plan,
                source_page_pdf=source_page_pdf,
            )
        )
    return jobs


def _field(value, name: str, default=None):
    """Read a LiteLLM field from either its SDK object or dict form."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _manifest_metadata(crop: SourceCrop) -> dict[str, object]:
    """Return only provider-approved, non-text redraw metadata."""
    return {
        "visual_kind": crop.visual.kind,
        "canvas": {"width_px": crop.width_px, "height_px": crop.height_px},
        "approved_label_regions": [
            {
                "label_index": index,
                "box": list(label.box),
                "anchor": None if label.anchor is None else list(label.anchor),
            }
            for index, label in enumerate(crop.visual.exact_labels)
        ],
    }


def _correction_differences(verdict: RedrawVerdict) -> dict[str, list[str]]:
    """Keep one correction bounded to the first verdict's ordered deltas."""
    alignment: list[str] = []
    if not verdict.label_regions_clear:
        alignment.append("clear the approved label regions")
    if not verdict.anchors_aligned:
        alignment.append("restore approved label-anchor alignment")
    if not verdict.geometry_aligned:
        alignment.append("restore source geometry alignment")
    return {
        "missing": list(verdict.missing_elements),
        "extra": [*verdict.extra_elements, *verdict.unapproved_text],
        "alignment": alignment,
    }


def _redraw_prompt(
    crop: SourceCrop,
    correction: RedrawVerdict | None,
) -> str:
    metadata = json.dumps(
        _manifest_metadata(crop),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=False,
    )
    prompt = (
        "Redraw only the first attached exact source-visual crop. "
        f"{_CONTEXT_BOUNDARY} {_MANIFEST_SKETCH_STYLE}\n"
        f"APPROVED_NON_TEXT_METADATA={metadata}"
    )
    if correction is not None:
        differences = json.dumps(
            _correction_differences(correction),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=False,
        )
        prompt += (
            "\nONE_CORRECTION_ONLY: change only these ordered first-verdict "
            f"differences; all original restrictions remain unchanged: {differences}"
        )
    return prompt


def _verify_prompt(crop: SourceCrop) -> str:
    metadata = json.dumps(
        _manifest_metadata(crop),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=False,
    )
    return (
        "Compare the final attached candidate PNG with the first attached exact "
        "source crop. Ordered middle images, when present, are audited same-topic "
        f"context only. {_CONTEXT_BOUNDARY} Verify source components, relationships, "
        "geometry, arrows, bonds, axes, curves, ticks, empty source-label slots, "
        "and approved anchors. Do not judge source-label readability because exact "
        "source glyph layers have not yet been composited. candidate_text_free is "
        "true only if the candidate contains no textual glyph anywhere. faithful "
        "is true only when there is no uncertainty. Return the strict schema and "
        "preserve the observed order in all difference lists.\n"
        f"APPROVED_NON_TEXT_METADATA={metadata}"
    )


def _crop_model_files(crop: SourceCrop) -> list[ModelFile]:
    files = [
        ModelFile(
            filename=f"{crop.visual.visual_id}-source.png",
            mime_type="image/png",
            data=crop.source_png,
        )
    ]
    files.extend(
        ModelFile(
            filename=f"context-{index:02d}.png",
            mime_type="image/png",
            data=context.source_png,
        )
        for index, context in enumerate(crop.context_crops, start=1)
    )
    return files


def _data_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _extract_litellm_image(response: object) -> bytes | None:
    choices = _field(response, "choices", []) or []
    choice = choices[0] if choices else None
    message = _field(choice, "message") if choice else None
    images = _field(message, "images") if message else None
    if not images:
        return None
    image_url = _field(images[0], "image_url")
    url = image_url if isinstance(image_url, str) else _field(image_url, "url", "")
    if not isinstance(url, str) or not url:
        return None
    encoded = _DATA_URL_RE.sub("", url)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    if decoded:
        cost_meter.record_images(1)
    return decoded


def _sequence_image_prompt(plan: Visual) -> str:
    allowed = json.dumps(
        {
            "exact_components": list(plan.exact_components),
            "exact_labels": list(plan.exact_labels),
            "composition_note": plan.composition_note,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "Use the attached one-page teacher PDF as the sole visual authority. "
        "Draw only the allowlisted visible or explicitly taught items. Do not "
        "add, infer, complete, correct, relabel, or decorate any component, "
        "arrow, value, equation, or example. If uncertain, omit it.\n"
        f"ALLOWED_SOURCE_CONTENT={allowed}\n"
        f"{VISUAL_STYLE}"
    )


def _safe_sequence_png(data: bytes) -> bytes | None:
    if type(data) is not bytes or not data or len(data) > config.MAX_VISUAL_PNG_BYTES:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                return None
            image.load()
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width > config.MAX_VISUAL_PAGE_DIMENSION_PX
                or height > config.MAX_VISUAL_PAGE_DIMENSION_PX
                or width * height > config.MAX_VISUAL_CROP_PIXELS
            ):
                return None
        flattened = _flatten(data)
        if (
            type(flattened) is not bytes
            or not flattened
            or len(flattened) > config.MAX_VISUAL_PNG_BYTES
        ):
            return None
        return flattened
    except Exception:
        return None


def _one_sequence_image(
    job: CoreVisualJob,
    run: GenerationRun,
) -> bytes | None:
    """Make one non-retrying source-page-grounded image attempt."""
    from . import gemini_client

    raw_client = None
    try:
        if not run.try_take_image(job.key):
            run.mark_image_omitted(job.key)
            return None
        raw_client = gemini_client._build_visual_lite_client()
        timeout, _ = gemini_client._bounded_model_timeout(
            run, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
        )
        response = raw_client.chat.completions.create(
            model=_model(),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "file",
                            "file": {
                                "filename": "source-page.pdf",
                                "file_data": (
                                    "data:application/pdf;base64,"
                                    + base64.b64encode(job.source_page_pdf).decode("ascii")
                                ),
                            },
                        },
                        {"type": "text", "text": _sequence_image_prompt(job.plan)},
                    ],
                }
            ],
            temperature=0.1,
            timeout=timeout,
            max_tokens=1024,
            extra_body={"modalities": ["image", "text"]},
        )
        cost_meter.record_openai_usage(response)
        png = _safe_sequence_png(_extract_litellm_image(response) or b"")
        if png is None:
            run.mark_image_omitted(job.key)
        return png
    except Exception:
        run.mark_image_omitted(job.key)
        return None
    finally:
        if raw_client is not None:
            try:
                raw_client.close()
            except Exception:
                pass


def render_sequence_visuals(notes: ShortNotes, run: GenerationRun) -> None:
    """Attach optional images; every image-stage failure degrades to text only."""
    for section in notes.sections:
        section.diagram = ""
        object.__setattr__(section, "_image", None)
        object.__setattr__(section, "_svg", None)
    try:
        jobs = _select_sequence_visual_jobs(notes, run)
    except Exception:
        for section_index, section in enumerate(notes.sections):
            plan = section.visual
            chunk = getattr(section, "_source_chunk", None)
            if (
                plan is not None
                and (plan.kind or "").strip().casefold() == "core_diagram"
                and isinstance(chunk, PdfChunk)
                and isinstance(plan.source_local_page, int)
                and isinstance(section.source_order, int)
            ):
                run.mark_image_omitted(
                    f"c{chunk.index}-p{plan.source_local_page}"
                    f"-s{section.source_order}-i{section_index}"
                )
        return
    if not enabled():
        for job in jobs:
            run.mark_image_omitted(job.key)
        return
    for job in jobs:
        png = _one_sequence_image(job, run)
        if png is not None:
            object.__setattr__(job.section, "_image", png)


def _extract_direct_image(response: object) -> bytes | None:
    candidates = _field(response, "candidates", []) or []
    candidate = candidates[0] if candidates else None
    content = _field(candidate, "content") if candidate else None
    parts = _field(content, "parts", []) if content else []
    for part in parts or []:
        inline_data = _field(part, "inline_data")
        mime_type = _field(inline_data, "mime_type", "") if inline_data else ""
        data = _field(inline_data, "data") if inline_data else None
        if mime_type == "image/png" and type(data) is bytes:
            cost_meter.record_images(1)
            return data
    return None


def _one_redraw(
    crop: SourceCrop,
    fallback: PreparedFallback,
    budget: VisualRunBudget,
    correction: RedrawVerdict | None = None,
) -> bytes | None:
    """Generate one text-free, exact-layout sketch candidate from one crop."""
    # gemini_client imports this module, so its provider seams must stay lazy.
    from google.genai import types

    from . import gemini_client

    try:
        if (
            not isinstance(crop, SourceCrop)
            or not isinstance(fallback, PreparedFallback)
            or fallback.visual_id != crop.visual.visual_id
            or not isinstance(budget, VisualRunBudget)
            or (correction is not None and not isinstance(correction, RedrawVerdict))
        ):
            return None
        files = _crop_model_files(crop)
        gemini_client._validate_model_files(files, "render_failed")
        gemini_client._bounded_model_timeout(
            budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
        )
        prompt = _redraw_prompt(crop, correction)

        if gemini_client._use_litellm():
            content = [
                {"type": "image_url", "image_url": {"url": _data_url(item.data)}}
                for item in files
            ]
            content.append({"type": "text", "text": prompt})
            raw_client = gemini_client._build_visual_lite_client()
            try:
                option_timeout, _ = gemini_client._bounded_model_timeout(
                    budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
                )
                client = raw_client.with_options(
                    timeout=option_timeout,
                    max_retries=0,
                )
                timeout_seconds, _ = gemini_client._bounded_model_timeout(
                    budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
                )
                budget.check_deadline()
                budget.take_redraw(crop.visual.visual_id)
                response = client.chat.completions.create(
                    model=_model(),
                    messages=[{"role": "user", "content": content}],
                    temperature=0.2,
                    max_tokens=1024,
                    timeout=timeout_seconds,
                    extra_body={"modalities": ["image", "text"]},
                )
                cost_meter.record_openai_usage(response)
                budget.check_deadline()
                result = _extract_litellm_image(response)
            except BaseException:
                try:
                    raw_client.close()
                except BaseException:
                    pass
                raise
            else:
                raw_client.close()
                return result

        contents = [
            *(types.Part.from_bytes(data=item.data, mime_type=item.mime_type) for item in files),
            prompt,
        ]
        _, client_timeout_ms = gemini_client._bounded_model_timeout(
            budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
        )
        client = gemini_client._build_visual_client(client_timeout_ms)
        try:
            _, timeout_ms = gemini_client._bounded_model_timeout(
                budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
            )
            request_config = types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                temperature=0.2,
                max_output_tokens=1024,
                http_options=gemini_client._direct_request_http_options(timeout_ms),
            )
            budget.check_deadline()
            budget.take_redraw(crop.visual.visual_id)
            response = client.models.generate_content(
                model=_model().removeprefix("vertex_ai/"),
                contents=contents,
                config=request_config,
            )
            cost_meter.record_genai_usage(response)
            budget.check_deadline()
            result = _extract_direct_image(response)
        except BaseException:
            try:
                client.close()
            except BaseException:
                pass
            raise
        else:
            client.close()
            return result
    except Exception:
        # A redraw is optional. The state machine consumes this one failed
        # attempt and retains the prepared source fallback.
        return None


def _verify_redraw(
    crop: SourceCrop,
    candidate_png: bytes,
    fallback: PreparedFallback,
    budget: VisualRunBudget,
) -> RedrawVerdict:
    """Compare source and candidate in one structured, bounded call."""
    from . import gemini_client

    if (
        not isinstance(crop, SourceCrop)
        or type(candidate_png) is not bytes
        or not isinstance(fallback, PreparedFallback)
        or fallback.visual_id != crop.visual.visual_id
        or not isinstance(budget, VisualRunBudget)
    ):
        raise VisualFidelityError("render_failed")
    files = _crop_model_files(crop)
    files.append(
        ModelFile(
            filename=f"{crop.visual.visual_id}-candidate.png",
            mime_type="image/png",
            data=candidate_png,
        )
    )
    gemini_client._validate_model_files(files, "render_failed")
    gemini_client._bounded_model_timeout(
        budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
    )
    budget.take_verifier(crop.visual.visual_id)
    return gemini_client._generate_pdf_schema(
        files,
        _verify_prompt(crop),
        RedrawVerdict,
        2048,
        0.0,
        budget,
    )


def _one_image(
    prompt: str,
    source_pdf: bytes,
    attempts: int = 3,
) -> "bytes | None":
    """Generate one PDF-grounded image; retry only gateway capacity blips."""
    import time

    from .gemini_client import _lite_client

    if (
        not isinstance(source_pdf, bytes)
        or not source_pdf
        or len(source_pdf) > config.GEMINI_INLINE_MAX_BYTES
    ):
        return None
    pdf_data = "data:application/pdf;base64," + base64.b64encode(source_pdf).decode()
    instruction = (
        f"{_SOURCE_REDRAW_INSTRUCTION}\n\n"
        f"DRAWING SPECIFICATION:\n{prompt.strip()}\n\n"
        f"{VISUAL_STYLE}"
    )
    resp = None
    for attempt in range(attempts):
        try:
            resp = _lite_client().chat.completions.create(
                model=_model(),
                messages=[{"role": "user", "content": [
                    {"type": "file", "file": {
                        "filename": "source-chunk.pdf",
                        "file_data": pdf_data,
                    }},
                    {"type": "text", "text": instruction},
                ]}],
                extra_body={"modalities": ["image", "text"]},
            )
            break
        except Exception as exc:  # noqa: BLE001
            transient = "429" in str(exc) or "RateLimit" in type(exc).__name__
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(4.0 * (attempt + 1))  # 4s, then 8s
    if resp is None:
        return None
    choices = _field(resp, "choices", []) or []
    choice = choices[0] if choices else None
    message = _field(choice, "message") if choice else None
    images = _field(message, "images") if message else None
    if not images:
        return None
    url = ""
    first = images[0]
    image_url = _field(first, "image_url")
    url = image_url if isinstance(image_url, str) else _field(image_url, "url", "")
    if not url:
        return None
    b64 = _DATA_URL_RE.sub("", url)
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        return None
    return _flatten(raw)


def _flatten(png: bytes) -> "bytes | None":
    """Make the white paper transparent so the sketch sits on the dot grid.

    Also rejects the "transparency checkerboard" some image models literally
    draw when a transparent background is requested.
    """
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(png)).convert("RGBA")
        img = _autocrop(img)
        if _looks_checkered(img):
            return None
        px = img.load()
        w, h = img.size
        for y in range(h):
            for x in range(w):
                r, g, b, a = px[x, y]
                if a == 0:
                    continue
                m = min(r, g, b)
                if m >= 244 and max(r, g, b) - m <= 12:
                    px[x, y] = (r, g, b, 0)          # paper -> transparent
                elif m >= 224:
                    px[x, y] = (r, g, b, int((248 - m) * 12))  # soft edge
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001 - undecodable output is never a raster
        return None


def _autocrop(img):
    """Trim uniform margins so the drawing fills its slot."""
    from PIL import Image, ImageChops

    rgb = Image.new("RGB", img.size, (255, 255, 255))
    rgb.paste(img, mask=img.split()[3])
    bg = Image.new("RGB", img.size, rgb.getpixel((0, 0)))
    box = ImageChops.difference(rgb, bg).convert("L").point(
        lambda v: 255 if v > 18 else 0).getbbox()
    if box:
        pad = 6
        box = (max(0, box[0] - pad), max(0, box[1] - pad),
               min(img.width, box[2] + pad), min(img.height, box[3] + pad))
        return img.crop(box)
    return img


def _looks_checkered(img) -> bool:
    """Detect the grey/white transparency checkerboard as a drawn pattern."""
    small = img.convert("L").resize((64, 64))
    px = small.load()
    flips = 0
    for y in range(0, 64, 4):
        row = [px[x, y] for x in range(64)]
        for a, b in zip(row, row[1:]):
            if abs(a - b) > 18:
                flips += 1
    # A real sketch on white has few horizontal light/dark alternations.
    return flips > 320


def _decode_exact_rgba(
    data: bytes,
    width: int,
    height: int,
    *,
    max_bytes: int,
    max_pixels: int,
) -> np.ndarray:
    if (
        type(data) is not bytes
        or isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
        or len(data) > max_bytes
        or width * height > max_pixels
    ):
        raise VisualFidelityError("render_failed")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise VisualFidelityError("render_failed")
            image.load()
            if image.size != (width, height):
                raise VisualFidelityError("render_failed")
            return np.array(image.convert("RGBA"), dtype=np.uint8, copy=True)
    except VisualFidelityError:
        raise
    except Exception:
        raise VisualFidelityError("render_failed") from None


def _contains_opaque_neutral_rectangle(rgba: np.ndarray) -> bool:
    rgb = rgba[..., :3].astype(np.int16)
    alpha = rgba[..., 3]
    luminance = (
        rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722
    )
    neutral = rgb.max(axis=-1) - rgb.min(axis=-1) <= 18
    for mask in (
        (alpha >= 245) & neutral & (luminance >= 180.0),
        (alpha >= 245) & neutral & (luminance <= 120.0),
    ):
        ys, xs = np.nonzero(mask)
        if not len(xs):
            continue
        box_area = (int(xs.max()) - int(xs.min()) + 1) * (
            int(ys.max()) - int(ys.min()) + 1
        )
        if (
            box_area >= int(mask.size * 0.50)
            and int(np.count_nonzero(mask)) / box_area >= 0.92
        ):
            return True
    return False


def _validate_candidate_png(
    data: bytes,
    crop: SourceCrop,
    fallback: PreparedFallback,
    *,
    labels_composited: bool,
) -> None:
    rgba = _decode_exact_rgba(
        data,
        crop.width_px,
        crop.height_px,
        max_bytes=config.MAX_VISUAL_PNG_BYTES,
        max_pixels=config.MAX_VISUAL_CROP_PIXELS,
    )
    alpha = rgba[..., 3]
    visible = alpha > 16
    rgb = rgba[..., :3]
    meaningful = visible & (
        (rgb.min(axis=-1) < 244)
        | ((rgb.max(axis=-1).astype(np.int16) - rgb.min(axis=-1).astype(np.int16)) > 12)
    )
    if not np.any(meaningful) or not np.any(alpha <= 16):
        raise VisualFidelityError("render_failed")

    border = np.concatenate((alpha[0, :], alpha[-1, :], alpha[1:-1, 0], alpha[1:-1, -1]))
    if not len(border) or float(np.count_nonzero(border <= 16) / len(border)) < 0.85:
        raise VisualFidelityError("render_failed")
    if _contains_opaque_neutral_rectangle(rgba):
        raise VisualFidelityError("render_failed")

    from PIL import Image

    image = Image.fromarray(rgba, mode="RGBA")
    try:
        if _looks_checkered(image):
            raise VisualFidelityError("render_failed")
    finally:
        image.close()

    if not labels_composited:
        label_union = _decode_exact_rgba(
            fallback.label_union_png,
            crop.width_px,
            crop.height_px,
            max_bytes=config.MAX_VISUAL_PNG_BYTES,
            max_pixels=config.MAX_VISUAL_CROP_PIXELS,
        )
        source_glyph_mask = label_union[..., 3] > 0
        if np.any((alpha > 0) & source_glyph_mask):
            # Candidate glyphs or strokes may not occupy the immutable source
            # glyph mask. Outside-region text is rejected by the structured
            # candidate_text_free/unapproved_text verifier contract.
            raise VisualFidelityError("render_failed")
        for label in crop.visual.exact_labels:
            x0 = math.floor(crop.width_px * label.box[0] / 100.0)
            y0 = math.floor(crop.height_px * label.box[1] / 100.0)
            x1 = math.ceil(crop.width_px * label.box[2] / 100.0)
            y1 = math.ceil(crop.height_px * label.box[3] / 100.0)
            if np.any(alpha[y0:y1, x0:x1] > 0):
                # Blank the complete approved slot, not merely the original
                # glyph pixels, so shifted/generated text cannot survive.
                raise VisualFidelityError("render_failed")


def _validate_manifest_inputs(
    notes: object,
    manifest: VisualManifest,
    crops: Mapping[str, SourceCrop],
    budget: VisualRunBudget,
    memory: VisualMemoryBudget,
) -> tuple[str, ...]:
    from .prompts import ShortNotes

    if (
        not isinstance(notes, ShortNotes)
        or not isinstance(manifest, VisualManifest)
        or not isinstance(crops, Mapping)
        or not isinstance(budget, VisualRunBudget)
        or not isinstance(memory, VisualMemoryBudget)
    ):
        raise VisualFidelityError("link_invalid")
    ids = manifest_ids(manifest)
    if tuple(crops.keys()) != ids:
        raise VisualFidelityError("link_invalid")

    for visual_id, visual in zip(ids, manifest.visuals):
        budget.check_deadline()
        crop = crops.get(visual_id)
        if (
            not isinstance(crop, SourceCrop)
            or crop.visual.visual_id != visual_id
            or crop.visual != visual
            or len(crop.context_crops) > 2
            or tuple(item.context for item in crop.context_crops)
            != crop.visual.context_regions
        ):
            raise VisualFidelityError("link_invalid")
        _decode_exact_rgba(
            crop.source_png,
            crop.width_px,
            crop.height_px,
            max_bytes=config.MAX_VISUAL_PNG_BYTES,
            max_pixels=config.MAX_VISUAL_CROP_PIXELS,
        )
        context_bytes = 0
        for context_crop in crop.context_crops:
            context_bytes += len(context_crop.source_png)
            _decode_exact_rgba(
                context_crop.source_png,
                context_crop.width_px,
                context_crop.height_px,
                max_bytes=config.MAX_CONTEXT_PNG_BYTES,
                max_pixels=config.MAX_CONTEXT_PIXELS,
            )
        if context_bytes > config.MAX_CONTEXT_TOTAL_BYTES:
            raise VisualFidelityError("link_invalid")
        budget.check_deadline()
    return ids


def _verdict_passes(verdict: object) -> bool:
    return (
        isinstance(verdict, RedrawVerdict)
        and verdict.faithful
        and verdict.candidate_text_free
        and verdict.label_regions_clear
        and verdict.anchors_aligned
        and verdict.geometry_aligned
        and verdict.missing_elements == ()
        and verdict.extra_elements == ()
        and verdict.unapproved_text == ()
    )


def _release_exact_reservations(
    memory: VisualMemoryBudget,
    reservations: list[BufferReservation],
) -> None:
    first_error: BaseException | None = None
    while reservations:
        reservation = reservations.pop()
        try:
            memory.release_buffer(reservation)
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        if isinstance(first_error, VisualFidelityError):
            raise first_error
        raise VisualFidelityError("memory_limit_exceeded") from first_error


def _attempt_candidate(
    crop: SourceCrop,
    fallback: PreparedFallback,
    budget: VisualRunBudget,
    memory: VisualMemoryBudget,
    redraw: RedrawFn,
    verify: VerifyFn,
    correction: RedrawVerdict | None,
) -> tuple[VisualAsset | None, RedrawVerdict | None]:
    candidate_png: bytes | None = None
    candidate_owner: BufferReservation | None = None
    composite: bytes | None = None
    composite_owner: BufferReservation | None = None
    composite_retained = False
    verdict: RedrawVerdict | None = None
    try:
        try:
            budget.check_deadline()
            candidate_png = redraw(crop, fallback, budget, correction)
            budget.check_deadline()
        except Exception:
            return None, None
        if type(candidate_png) is not bytes:
            return None, None

        try:
            budget.check_deadline()
            candidate_owner = memory.reserve_buffer("asset", candidate_png)
            if candidate_owner is None:
                return None, None
            _validate_candidate_png(
                candidate_png,
                crop,
                fallback,
                labels_composited=False,
            )
            budget.check_deadline()
        except Exception:
            return None, None

        try:
            budget.check_deadline()
            received = verify(crop, candidate_png, fallback, budget)
            budget.check_deadline()
        except Exception:
            return None, None
        if isinstance(received, RedrawVerdict):
            verdict = received
        if not _verdict_passes(received):
            return None, verdict

        try:
            budget.check_deadline()
            composite = composite_source_labels(candidate_png, crop, fallback)
            budget.check_deadline()
            composite_owner = memory.reserve_buffer("asset", composite)
            if composite_owner is None:
                return None, verdict
            _validate_candidate_png(
                composite,
                crop,
                fallback,
                labels_composited=True,
            )
            budget.check_deadline()
            validate_composited_labels(composite, crop, fallback)
            budget.check_deadline()
            asset = VisualAsset(
                visual_id=crop.visual.visual_id,
                state="verified_redraw",
                primary_png=composite,
                fallback_png=fallback.png,
                width_px=crop.width_px,
                height_px=crop.height_px,
            )
            budget.check_deadline()
            composite_retained = True
            return asset, verdict
        except Exception:
            return None, verdict
    finally:
        cleanup_error: BaseException | None = None
        if composite_owner is not None and not composite_retained:
            try:
                memory.release_buffer(composite_owner)
            except BaseException as error:
                cleanup_error = error
        composite_owner = None
        composite = None
        if candidate_owner is not None:
            try:
                memory.release_buffer(candidate_owner)
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        candidate_owner = None
        candidate_png = None
        if cleanup_error is not None:
            if isinstance(cleanup_error, VisualFidelityError):
                raise cleanup_error
            raise VisualFidelityError("memory_limit_exceeded") from cleanup_error


def _fallback_asset(
    crop: SourceCrop,
    fallback_png: bytes,
    budget: VisualRunBudget,
) -> VisualAsset:
    budget.check_deadline()
    asset = VisualAsset(
        visual_id=crop.visual.visual_id,
        state="sketch_source_fallback",
        primary_png=fallback_png,
        fallback_png=fallback_png,
        width_px=crop.width_px,
        height_px=crop.height_px,
    )
    budget.check_deadline()
    return asset


def _render_legacy_visuals(notes) -> None:
    """Fill a planned core diagram's `_image` or `_svg` private render field."""
    jobs = []
    for section in notes.sections:
        vis = getattr(section, "visual", None)
        if (vis and (vis.prompt or "").strip()
                and (vis.kind or "").strip().lower() == "core_diagram"):
            jobs.append((section, vis.prompt.strip()))
    if not jobs:
        return

    # SEQUENTIAL on purpose: parallel image calls trip the gateway's 429
    # capacity limit, which used to silently drop every diagram.
    image_generation_enabled = enabled()
    from . import svg_gen

    for index, (section, prompt) in enumerate(jobs):
        png = None
        source_pdf = getattr(section, "_source_pdf", None)
        grounded_source = bool(
            isinstance(source_pdf, bytes)
            and source_pdf
            and len(source_pdf) <= config.GEMINI_INLINE_MAX_BYTES
        )
        if (
            image_generation_enabled
            and grounded_source
            and index < config.MAX_IMAGES
            and config.DIAGRAM_MODE in ("image", "auto")
        ):
            try:
                png = _one_image(prompt, source_pdf)
            except Exception as exc:  # noqa: BLE001 - a bad image never breaks notes
                print(
                    "[notesbot] Grounded raster unavailable "
                    f"({type(exc).__name__}); using a local fallback."
                )
                png = None
        if png:
            object.__setattr__(section, "_image", png)
            print("[notesbot] Core diagram rendered as raster image.")
            continue
        if (
            image_generation_enabled
            and grounded_source
            and index < config.MAX_IMAGES
            and config.DIAGRAM_MODE in ("svg", "auto")
        ):
            # In auto mode SVG is the reliable fallback if raster art fails.
            try:
                svg = svg_gen.try_svg(prompt, source_pdf)
            except Exception as exc:  # noqa: BLE001 - isolate each visual
                print(
                    "[notesbot] SVG fallback unavailable "
                    f"({type(exc).__name__}); using a manual fallback."
                )
                svg = None
            if svg:
                object.__setattr__(section, "_svg", svg)
                print("[notesbot] Core diagram rendered as SVG fallback.")
                continue
        section.diagram = prompt
        print("[notesbot] Core diagram rendered as manual draw-it-yourself fallback.")


def render_visuals(
    notes,
    manifest: VisualManifest | None = None,
    crops: Mapping[str, SourceCrop] | None = None,
    budget: VisualRunBudget | None = None,
    memory: VisualMemoryBudget | None = None,
    *,
    redraw: RedrawFn = _one_redraw,
    verify: VerifyFn = _verify_redraw,
) -> dict[str, VisualAsset] | None:
    """Use legacy rendering only without a manifest; otherwise fail closed."""
    contracts = (manifest, crops, budget, memory)
    if all(item is None for item in contracts):
        return _render_legacy_visuals(notes)
    if any(item is None for item in contracts):
        if isinstance(memory, VisualMemoryBudget):
            memory.close()
        raise VisualFidelityError("link_invalid")

    assert manifest is not None
    assert crops is not None
    assert budget is not None
    assert memory is not None

    prepared: OrderedDict[str, PreparedFallback] = OrderedDict()
    results: OrderedDict[str, VisualAsset] = OrderedDict()
    try:
        ids = _validate_manifest_inputs(notes, manifest, crops, budget, memory)

        # Mandatory intact fallbacks precede the first optional provider call.
        for visual_id in ids:
            budget.check_deadline()
            fallback = prepare_source_fallback(crops[visual_id], budget, memory)
            budget.check_deadline()
            if fallback.visual_id != visual_id:
                raise VisualFidelityError("fallback_damaged")
            prepared[visual_id] = fallback

        redraw_limit = max(0, int(config.MAX_IMAGES))
        for ordinal, visual_id in enumerate(ids):
            crop = crops[visual_id]
            fallback = prepared.pop(visual_id)
            fallback_png = fallback.png
            temporary_handles = list(fallback.temporary_label_reservations)
            try:
                asset: VisualAsset | None = None
                correction: RedrawVerdict | None = None
                if ordinal < redraw_limit:
                    for attempt in range(2):
                        candidate_asset, first_verdict = _attempt_candidate(
                            crop,
                            fallback,
                            budget,
                            memory,
                            redraw,
                            verify,
                            correction if attempt else None,
                        )
                        if candidate_asset is not None:
                            asset = candidate_asset
                            break
                        if attempt == 0:
                            correction = first_verdict

                if asset is None:
                    asset = _fallback_asset(crop, fallback_png, budget)
                if asset.visual_id != visual_id:
                    raise VisualFidelityError("link_invalid")
                results[visual_id] = asset
            finally:
                # Terminal bytes now contain (fallback) or embed (composite)
                # the immutable labels, so the temporary evidence may die.
                fallback = None
                _release_exact_reservations(memory, temporary_handles)
                temporary_handles = []

        budget.check_deadline()
        if tuple(results.keys()) != ids or any(
            key != asset.visual_id for key, asset in results.items()
        ):
            raise VisualFidelityError("link_invalid")
        budget.check_deadline()
        return dict(results)
    except BaseException as original_error:
        cleanup_error: BaseException | None = None
        remaining = list(prepared.values())
        prepared.clear()
        for fallback in reversed(remaining):
            handles = list(fallback.temporary_label_reservations)
            fallback = None
            try:
                _release_exact_reservations(memory, handles)
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        remaining.clear()
        try:
            memory.close()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
        if cleanup_error is not None:
            if isinstance(cleanup_error, VisualFidelityError):
                raise cleanup_error from original_error
            raise VisualFidelityError("memory_limit_exceeded") from original_error
        raise
