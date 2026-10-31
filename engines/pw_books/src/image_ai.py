"""AI redraw of lecture diagrams into a handwritten notes style.

Uses the Gemini image model to convert a slide diagram (usually light text on a
dark slide) into a clean handwritten note: white background, blue handwritten
text/formulas, original diagram-line colours preserved but darkened where they
would vanish on white.

TWO PATHS, because the proxy's transports differ in what they can carry:
1. DIRECT image-to-image via gemini_generate + responseModalities — the
   historically proven recipe, but the current proxy generation routes it
   through a chat-completions gateway that has been observed to drop image
   output (billed HTTP 200, finishReason=STOP, no image part).
2. FALLBACK two-stage on the kit's DOCUMENTED image endpoint: a text model
   (which works on this proxy) writes an exhaustive drawing spec from the
   slide crop, then pw_access.gemini_image renders it. Output is re-imagined
   from the description rather than pixel-conditioned, so successes via this
   path are flagged for label verification.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time
from pathlib import Path

from .config import DEFAULT_IMAGE_MODEL

logger = logging.getLogger(__name__)

HANDWRITTEN_PROMPT = (
    "Create a handwritten notes-style image with a white background and blue "
    "handwritten text. The diagram's structure should remain the same as in the "
    "original image. All text and formulas should be handwritten, clear, "
    "high-resolution, and in blue. The diagram lines will retain their original "
    "color, but any light colors that would not be visible on a white background "
    "will be changed to a dark color. The image should be high resolution."
)

# The SAME instruction, rewritten for the text-to-image route — the only route
# that currently works on this proxy.
#
# HANDWRITTEN_PROMPT must NOT be used there. It says "remain the same as in the
# original image" and "retain their original color", but /api/gemini/image is
# text-to-image: the model never receives the slide. It was being told to
# preserve an image it cannot see and to keep colours the describe stage is
# explicitly told not to mention — an instruction it can only guess at, on the
# one route redraws actually take. It also spent ~390 characters saying it, and
# prompt SIZE is what trips this endpoint's rate limit (measured: 557 chars OK,
# 2700 chars -> 429), so those characters came straight out of the diagram's
# own description budget.
RENDER_PROMPT = (
    "Draw this as a handwritten notes-style diagram on a plain white background. "
    "Write every label, number and formula by hand in blue, clearly and at high "
    "resolution. Draw the diagram's lines in a dark colour so they stay visible "
    "on white. Reproduce exactly what is described below and nothing else: add no "
    "title, no extra labels, no decoration, no logo and no watermark."
)

# Stage 1 of the fallback: the image model never sees the original, so this
# must carry every LABEL — but it must also stay SHORT. Measured on this proxy:
# a 557-char render prompt succeeds, a 2700-char one hits
# litellm.RateLimitError, and our first 7400-char "exhaustive" description
# failed every time. Content over styling prose is what buys fidelity per token.
DESCRIBE_PROMPT = (
    "Describe this lecture diagram so an artist who cannot see it can redraw it. "
    "Be COMPACT — under 1200 characters total. Include every text label, number "
    "and formula VERBATIM, the layout (what is above/below/left/right of what), "
    "and every arrow with its direction and what it connects. Do NOT describe "
    "colours, fonts, weights, backgrounds, logos or watermarks, and do not "
    "comment on the image. Output only the description, as terse lines."
)

# Substrings that mean the image MODEL is quota-blocked — the fallback renders
# on the same model, so retrying through a different endpoint cannot help.
_QUOTA_MARKERS = ("429", "quota", "rate limit", "resource exhausted",
                  "temporarily unavailable")

# A real slide description runs to several thousand characters. Anything much
# shorter means the describe stage failed to read the slide, and rendering from
# it would fabricate a diagram rather than reproduce one.
_MIN_SPEC_CHARS = 600

# /api/gemini/image intermittently 502s with "no image in gateway response";
# the same prompt then succeeds. Measured ~1 failure in 6 calls.
_IMAGE_ENDPOINT_ATTEMPTS = 3

# Retry budget for the describe stage. It is a TEXT call, so left alone it
# inherits note generation's 6 attempts (2+4+8+16+32s of backoff ~= 62s). But a
# redraw is optional — failure just keeps the original crop — so paying a
# minute per rate-limited slide to learn that buys nothing. 2 attempts still
# absorbs a one-off blip. Raise with PW_AI_DESCRIBE_RETRIES if ever needed.
try:
    _DESCRIBE_ATTEMPTS = max(1, min(6, int(os.getenv("PW_AI_DESCRIBE_RETRIES", "2"))))
except ValueError:
    _DESCRIBE_ATTEMPTS = 2

# Render-prompt ceiling: measured on this proxy, ~557 chars succeeds and ~2700
# chars returns litellm.RateLimitError. Leave room for RENDER_PROMPT.
_MAX_SPEC_CHARS = 1400

# Image models this proxy serves (src/model_router.py). When the configured one
# renders nothing for a NON-quota reason — an unavailable/renamed model, a
# gateway that keeps returning content:null — the other one is tried once rather
# than losing the diagram. Quota is per-project, so it never triggers this.
# Override with PW_AI_IMAGE_FALLBACKS="model-a,model-b" (empty disables).
_DEFAULT_IMAGE_FALLBACKS = ("gemini-3.1-flash-image", "gemini-3-pro-image")


def _image_model_chain(primary: str) -> list[str]:
    """The configured model first, then any other known image model."""
    raw = os.getenv("PW_AI_IMAGE_FALLBACKS")
    if raw is not None:
        pool = [m.strip() for m in raw.split(",") if m.strip()]
    else:
        pool = list(_DEFAULT_IMAGE_FALLBACKS)
    chain = [primary] if primary else []
    for model in pool:
        if model and model not in chain:
            chain.append(model)
    if raw is not None:
        # The operator set PW_AI_IMAGE_FALLBACKS explicitly — even to "" —
        # so never resurrect the defaults behind their back.
        return chain
    return chain or list(_DEFAULT_IMAGE_FALLBACKS)


def _looks_like_quota(reason: str) -> bool:
    """Whether a failure means the image MODEL is blocked (so stage two is futile).

    An explicit server-side 5xx is a TRANSIENT gateway problem, never quota —
    without this carve-out a single 502 on the first (canary) redraw read as
    "temporarily unavailable", tripped the quota breaker and silently cancelled
    every remaining redraw in the run, fallback route included.

    Hyphens are normalised to spaces first. This is not cosmetic: the app's own
    _terminal_error_message writes "rate-limited", while the marker is
    "rate limit" — so every 429 read as NOT-quota, the breaker never tripped,
    and each slide went on to spend the full billed describe budget discovering
    the same rate limit for itself. That was the bulk of a slow run's wall time.
    """
    low = reason.lower().replace("-", " ")
    if re.search(r"http 5\d\d", low):
        return False
    return any(m.replace("-", " ") in low for m in _QUOTA_MARKERS)


# Failure text meaning the DIRECT image-to-image route is not carrying images on
# this proxy generation (billed HTTP 200, finishReason=STOP, zero image parts) —
# as opposed to a quota block or a bad model id. The route is either working or
# not for the whole run, so one probe is enough to know.
_DEAD_ROUTE_MARKERS = (
    "no image bytes", "no image found", "returned no image",
    "empty response",
)
# NOT in the list: "not a valid image" (_save_validated_png). Corrupt bytes
# mean the route DID deliver an image — a one-off bad payload, the opposite of
# the billed-200-with-no-parts signature — so it must not condemn the route.


def _looks_like_dead_route(reason: str) -> bool:
    low = (reason or "").lower()
    return any(m in low for m in _DEAD_ROUTE_MARKERS)


class _QuotaBreaker:
    """Per-run state shared by every redraw: what has already been proven futile.

    Two independent facts get remembered here, both so the rest of the deck does
    not pay to rediscover them:

    * ``tripped`` — the image model reported quota exhaustion. The limit is
      per-project, not per-request, so EVERY further redraw is doomed. Without
      this a 22-slide deck kept paying: each remaining diagram still ran the
      billed describe stage (~4 INR) before discovering its render would 429.
    * ``direct_route_dead`` — the direct image-to-image call returned a billed
      HTTP 200 carrying no image. That is a property of the proxy's transport,
      not of the slide, so once the first diagram has proven it, the remaining
      diagrams skip straight to the working describe-then-render route instead
      of each buying the same dead call again.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tripped = False
        self.reason = ""
        self.direct_route_dead = False
        self.direct_reason = ""

    def trip(self, reason: str) -> None:
        with self._lock:
            if not self.tripped:
                self.tripped = True
                self.reason = reason
                logger.warning("image quota breaker tripped: %s — "
                               "skipping all remaining redraws this run", reason)

    def mark_direct_dead(self, reason: str) -> None:
        with self._lock:
            if not self.direct_route_dead:
                self.direct_route_dead = True
                self.direct_reason = reason
                logger.info("direct image-to-image route is not returning images "
                            "(%s) — remaining redraws go straight to "
                            "/api/gemini/image", reason)


def _save_validated_png(data: bytes, image_path: Path, out_dir: Path, *,
                        transparent_bg: bool) -> tuple[Path | None, str]:
    """Write bytes, verify they are a real image, optionally alpha the white
    background. Returns (path, "") or (None, reason); invalid bytes are deleted
    so ai_diagrams/ never contains files that look like successes."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{image_path.stem}_handwritten.png"
        out_path.write_bytes(data)
        try:
            from PIL import Image

            with Image.open(out_path) as im:
                im.verify()
        except Exception as e:
            logger.warning("redraw produced an unreadable image for %s: %r", image_path.name, e)
            try:
                out_path.unlink()
            except OSError:
                pass
            return None, f"returned bytes were not a valid image ({type(e).__name__})"
        if transparent_bg:
            from .image_tools import make_white_transparent

            trans = make_white_transparent(out_path, out_dir / f"{image_path.stem}_handwritten_t.png")
            return trans, ""
        return out_path, ""
    except Exception as e:
        logger.warning("redraw post-processing failed for %s: %r", image_path.name, e)
        return None, f"post-processing failed: {type(e).__name__}: {e}"


def _redraw_via_image_endpoint(client, image_path: Path, out_dir: Path, *,
                               image_model: str, transparent_bg: bool,
                               breaker: "_QuotaBreaker | None" = None) -> tuple[Path | None, str]:
    """Fallback: describe the diagram with the (working) text model, then render
    the description through the kit's documented /api/gemini/image endpoint.

    The describe stage is BILLED (~4 INR/slide), so it must never run when the
    render is already known to be impossible — hence the breaker check first.
    """
    import pw_access

    if breaker is not None and breaker.tripped:
        return None, f"skipped: {breaker.reason}"
    try:
        # 12000, not 2000: these are THINKING models and the budget is shared.
        # At 2000 the model spent 1916 tokens thinking and emitted 80 — a
        # description that stopped at the logo. The image model then invented
        # an entire unrelated lecture and we shipped it as the slide's diagram.
        spec_resp = client.generate(DESCRIBE_PROMPT, [image_path],
                                    max_output_tokens=12000, temperature=0.1, max_images=1,
                                    max_attempts=_DESCRIBE_ATTEMPTS)
        spec = (spec_resp.text or "").strip()
        truncated = spec_resp.finish_reason == "MAX_TOKENS"
    except Exception as e:
        reason = f"describe stage failed: {type(e).__name__}: {e}"
        # A rate-limited describe means the whole run is throttled, not just
        # this slide. Trip the breaker so the remaining diagrams stop instantly
        # instead of each rediscovering it at the cost of its own describe call.
        if _looks_like_quota(str(e)) and breaker is not None:
            breaker.trip("image/text model rate-limited during diagram redraw")
        return None, reason
    # NEVER render from a partial description: the image model does not leave
    # gaps, it invents plausible content, and fabricated diagrams in student
    # notes are far worse than keeping the original crop.
    if not spec:
        return None, "describe stage returned no text"
    if truncated:
        return None, f"description was truncated at the token limit ({len(spec)} chars) — refusing to render a partial diagram"
    if len(spec) < _MIN_SPEC_CHARS:
        return None, f"description too short to redraw faithfully ({len(spec)} chars)"
    # Hard cap the render prompt: prompt size drives the image endpoint's rate
    # limit (557 chars OK, 2700 chars -> 429). Trim styling prose first, then
    # cut at a line boundary so a label is never sliced in half.
    if len(spec) > _MAX_SPEC_CHARS:
        kept: list[str] = []
        for line in spec.splitlines():
            if re.search(r"colou?r|font|bold|background|logo|watermark|styl", line, re.I):
                continue
            if sum(len(x) + 1 for x in kept) + len(line) > _MAX_SPEC_CHARS:
                break
            kept.append(line)
        spec = "\n".join(kept).strip() or spec[:_MAX_SPEC_CHARS]
        logger.info("trimmed diagram spec for %s to %d chars", image_path.name, len(spec))
    prompt = f"{RENDER_PROMPT}\n\nDraw exactly this diagram, changing nothing:\n{spec}"
    # The image endpoint is intermittently flaky: it returns 502 "no image in
    # gateway response" (gateway content:null) on maybe one call in six, and
    # the SAME prompt succeeds on retry. That is the opposite of a quota block,
    # so these few retries are worth it — measured, not assumed.
    # When a model exhausts its attempts for a non-quota reason, the next known
    # image model gets one go: a renamed or withdrawn model id must degrade to
    # "the other model drew it", not to "no diagrams in the whole document".
    last = ""
    data = None
    used_model = image_model
    for model in _image_model_chain(image_model):
        for attempt in range(_IMAGE_ENDPOINT_ATTEMPTS):
            if attempt:
                time.sleep(2.0 * attempt)
            try:
                resp = pw_access.gemini_image(
                    client.google_token, prompt=prompt, model=model,
                    task_id=getattr(client, "task_id", ""),
                )
            except Exception as e:
                last = f"image endpoint failed on {model}: {type(e).__name__}: {e}"
                if _looks_like_quota(str(e)):
                    if breaker is not None:
                        breaker.trip("image quota exhausted earlier in this run")
                    return None, last      # quota: no retry, no other model
                logger.warning("image endpoint attempt %d/%d failed for %s on %s: %s",
                               attempt + 1, _IMAGE_ENDPOINT_ATTEMPTS, image_path.name,
                               model, str(e)[:160])
                continue
            result = (resp or {}).get("result") or {}
            b64 = result.get("image_base64")
            if not b64:
                note = str(result.get("text") or "")[:120]
                last = (f"image endpoint on {model} returned no image"
                        + (f" (model said: {note!r})" if note else ""))
                continue
            try:
                data = base64.b64decode(b64)
                used_model = model
                break
            except Exception:
                last = f"image endpoint on {model} returned undecodable base64"
                continue
        if data is not None:
            break
        logger.info("image model %s produced nothing for %s (%s)", model, image_path.name, last[:160])
    if data is None:
        return None, last or "image endpoint produced no image"
    path, reason = _save_validated_png(data, image_path, out_dir, transparent_bg=transparent_bg)
    if path is not None:
        logger.info("redraw for %s succeeded via /api/gemini/image on %s", image_path.name, used_model)
        return path, "described"
    return None, reason


def redraw_diagram_handwritten(client, image_path: Path, out_dir: Path, *, image_model: str = DEFAULT_IMAGE_MODEL, transparent_bg: bool = False, breaker: "_QuotaBreaker | None" = None) -> tuple[Path | None, str]:
    """Redraw one diagram via AI. Returns (new image path or None, note).

    ``note`` is "" for a direct image-to-image success, "described" when the
    two-stage fallback produced the image (caller should flag the slide for a
    label check), and otherwise the REAL failure text so the caller can surface
    it — quota vs empty-response vs bad-model used to be indistinguishable.

    Every failed direct attempt also writes the redacted raw request/response
    to ``out_dir/_debug/`` so failures are diagnosable from disk.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        return None, f"source image missing: {image_path}"
    # Nothing at all is attempted once the quota is known gone — not even the
    # direct call, which is billed for its input tokens even when it 429s.
    if breaker is not None and breaker.tripped:
        return None, f"skipped: {breaker.reason}"
    direct_reason = ""
    if breaker is not None and breaker.direct_route_dead:
        # Already proven futile by an earlier diagram in this run. The call is
        # BILLED for its input tokens even when it comes back empty, so buying
        # it once per slide to relearn the same fact is pure waste.
        direct_reason = f"skipped the direct route: {breaker.direct_reason}"
    else:
        try:
            data = client.generate_image(HANDWRITTEN_PROMPT, image_path,
                                         image_model=image_model, debug_dir=out_dir / "_debug")
            if data:
                path, reason = _save_validated_png(data, image_path, out_dir,
                                                   transparent_bg=transparent_bg)
                if path is not None:
                    return path, ""
                direct_reason = reason
            else:
                direct_reason = "model returned no image bytes"
        except Exception as e:
            logger.warning("direct redraw failed for %s on %s: %r", image_path.name, image_model, e)
            direct_reason = f"{type(e).__name__}: {e}"

        # Quota blocks the MODEL, not the route — the fallback would hit the same
        # wall and burn another billed call. Only route problems justify stage two.
        if _looks_like_quota(direct_reason):
            if breaker is not None:
                breaker.trip("image quota exhausted earlier in this run")
            return None, direct_reason
        if breaker is not None and _looks_like_dead_route(direct_reason):
            breaker.mark_direct_dead(direct_reason)

    path, note = _redraw_via_image_endpoint(client, image_path, out_dir,
                                            image_model=image_model,
                                            transparent_bg=transparent_bg, breaker=breaker)
    if path is not None:
        return path, note
    return None, f"{direct_reason}; fallback via /api/gemini/image also failed ({note})"


def redraw_slides_handwritten(client, slides, slide_numbers, out_dir: Path, *, image_model: str = DEFAULT_IMAGE_MODEL, max_workers: int = 4) -> tuple[int, list[str]]:
    """Redraw the images of the given slide numbers in place (mutates image_path).

    Redraws run concurrently (network-bound). Returns (count_redrawn, warnings).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    warnings: list[str] = []
    count = 0
    targets = [s for s in slides if s.slide_no in slide_numbers and s.image_path and Path(s.image_path).exists()]
    if not targets:
        return 0, warnings

    breaker = _QuotaBreaker()

    def _do(s):
        new, note = redraw_diagram_handwritten(client, s.image_path, out_dir,
                                               image_model=image_model, breaker=breaker)
        return s, new, note

    def _record(s, new, note):
        nonlocal count
        if new:
            s.image_path = new
            # Flag the slide so the DOCX writer inserts this image WHOLE. A
            # redraw is one coherent figure the model composed for this note;
            # re-running region detection on it splits it up and keeps only one
            # piece — and since the redraw re-imagines the layout, which piece
            # survives has nothing to do with the original slide.
            meta = getattr(s, "metadata", None)
            if isinstance(meta, dict):
                meta["ai_redrawn"] = True
            else:
                try:
                    s.metadata = {"ai_redrawn": True}
                except Exception:      # frozen/slotted stand-ins in tests
                    pass
            count += 1
            if note == "described":
                warnings.append(
                    f"Slide {s.slide_no}: diagram was re-rendered from an AI description "
                    "(the proxy could not do a direct image-to-image redraw) — "
                    "please verify its labels against the original."
                )
        elif not note.startswith("skipped:"):
            # Carry the REAL reason into the warning; a bare "redraw failed"
            # hides quota vs empty-response vs bad-model and blocks diagnosis.
            warnings.append(
                f"AI redraw failed for slide {s.slide_no} ({note}); "
                "kept the original diagram image."
            )

    # CANARY: run the first diagram alone. If the image quota is gone, this
    # single call discovers it and the breaker stops the rest before they cost
    # anything — a 22-slide deck used to pay a billed describe (~4 INR) per
    # diagram only to 429 on every render.
    first, rest = targets[0], targets[1:]
    _record(*_do(first))

    if rest and not breaker.tripped:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(rest))) as ex:
            for fut in as_completed([ex.submit(_do, s) for s in rest]):
                _record(*fut.result())   # redraw_diagram_handwritten never raises

    skipped = len(targets) - count - sum(
        1 for w in warnings if w.startswith("AI redraw failed"))
    if breaker.tripped and skipped > 0:
        warnings.append(
            f"Skipped AI redraw for {skipped} more diagram(s): {breaker.reason}. "
            "The original slide crops were kept — no further image calls were "
            "made, so this cost nothing."
        )
    return count, warnings
