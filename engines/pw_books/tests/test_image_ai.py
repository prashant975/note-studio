import base64
import io
from pathlib import Path

from PIL import Image, ImageDraw

from src.config import DEFAULT_IMAGE_MODEL
from src.image_ai import HANDWRITTEN_PROMPT, RENDER_PROMPT, redraw_diagram_handwritten


EXPECTED_PROMPT = (
    "Create a handwritten notes-style image with a white background and blue "
    "handwritten text. The diagram's structure should remain the same as in the "
    "original image. All text and formulas should be handwritten, clear, "
    "high-resolution, and in blue. The diagram lines will retain their original "
    "color, but any light colors that would not be visible on a white background "
    "will be changed to a dark color. The image should be high resolution."
)


def _png_bytes(color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (12, 12), color).save(buf, format="PNG")
    return buf.getvalue()


class RecordingImageClient:
    def __init__(self, generated_bytes: bytes):
        self.generated_bytes = generated_bytes
        self.calls = []

    def generate_image(self, prompt, image_path, *, image_model, debug_dir=None):
        self.calls.append((prompt, Path(image_path), image_model))
        return self.generated_bytes


def test_redraw_uses_required_prompt_and_keeps_white_background(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (12, 12), "black").save(source)
    client = RecordingImageClient(_png_bytes("white"))

    result, note = redraw_diagram_handwritten(client, source, tmp_path / "out")

    assert HANDWRITTEN_PROMPT == EXPECTED_PROMPT
    assert client.calls == [
        (EXPECTED_PROMPT, source, DEFAULT_IMAGE_MODEL)
    ]
    assert result is not None
    assert note == ""
    assert result.name == "source_handwritten.png"
    with Image.open(result) as image:
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (255, 255, 255)


def test_every_image_model_default_comes_from_config():
    """No hardcoded image-model default may drift from config.

    These defaults used to be a stale "gemini-2.5-flash-image" -- a model this
    proxy does not serve. Runtime passed the real id explicitly so it never
    fired, but any caller omitting the argument would have hit a dead model.
    """
    import inspect

    from src.ai_client import GeminiClient
    from src.image_ai import redraw_diagram_handwritten, redraw_slides_handwritten
    from src.pipeline import run_pipeline

    for func in (redraw_diagram_handwritten, redraw_slides_handwritten,
                 GeminiClient.generate_image, run_pipeline):
        default = inspect.signature(func).parameters["image_model"].default
        assert default == DEFAULT_IMAGE_MODEL, (
            f"{func.__qualname__} defaults to {default!r}, not {DEFAULT_IMAGE_MODEL!r}"
        )


def test_quota_failure_reports_reason_and_skips_fallback(tmp_path, monkeypatch):
    """Quota blocks the MODEL, not the route -- the two-stage fallback renders
    on the same model, so it must NOT be attempted (it would burn another
    billed call against the same wall)."""
    import pw_access

    source = tmp_path / "source.png"
    Image.new("RGB", (12, 12), "black").save(source)
    fallback_calls = []
    monkeypatch.setattr(pw_access, "gemini_image",
                        lambda *a, **k: fallback_calls.append(1))

    class QuotaClient:
        def generate_image(self, prompt, image_path, *, image_model, debug_dir=None):
            raise RuntimeError("gemini proxy error 429: quota exhausted")

    result, note = redraw_diagram_handwritten(QuotaClient(), source, tmp_path / "out")

    assert result is None
    assert "429" in note and "quota exhausted" in note
    assert fallback_calls == [], "fallback must not run on quota errors"


def test_empty_response_falls_back_to_image_endpoint(tmp_path, monkeypatch):
    """A billed-200-empty direct call is a ROUTE problem -- the fallback must
    describe the diagram with the text model and render via the kit's
    documented /api/gemini/image endpoint."""
    import pw_access

    source = tmp_path / "source.png"
    Image.new("RGB", (12, 12), "black").save(source)
    endpoint_calls = {}

    def fake_gemini_image(token, *, prompt, model, task_id="", **kw):
        endpoint_calls["prompt"] = prompt
        endpoint_calls["model"] = model
        return {"ok": True, "result": {
            "image_base64": base64.b64encode(_png_bytes("white")).decode(),
            "content_type": "image/png", "text": ""}}

    monkeypatch.setattr(pw_access, "gemini_image", fake_gemini_image)

    class EmptyThenDescribeClient:
        google_token = "tok"
        task_id = ""

        def generate_image(self, prompt, image_path, *, image_model, debug_dir=None):
            raise RuntimeError("Gemini returned an empty response (single attempt; "
                               "image calls are not retried) [finish_reason=STOP].")

        def generate(self, prompt, image_paths, **kw):
            class R:
                text = ("A free-body diagram of a block on an incline. " * 20)
                finish_reason = "STOP"
            return R()

    result, note = redraw_diagram_handwritten(EmptyThenDescribeClient(), source, tmp_path / "out")

    assert result is not None, "fallback should have produced an image"
    assert note == "described"
    assert endpoint_calls["model"] == DEFAULT_IMAGE_MODEL
    assert RENDER_PROMPT in endpoint_calls["prompt"]
    assert "free-body diagram" in endpoint_calls["prompt"]
    # /api/gemini/image is TEXT-to-image: the model never receives the slide.
    # Telling it to preserve "the original image" and "their original color"
    # points at something it cannot see -- and the describe stage is explicitly
    # told not to mention colour, so there is nothing to preserve either.
    prompt = endpoint_calls["prompt"]
    assert HANDWRITTEN_PROMPT not in prompt
    assert "original image" not in prompt
    assert "original color" not in prompt


def test_truncated_description_is_never_rendered(tmp_path, monkeypatch):
    """A partial description must NOT be rendered. The image model does not
    leave gaps -- it invents. Measured live: a 356-char truncated description
    produced a fabricated chemistry lecture for a cell-biology slide."""
    import pw_access

    source = tmp_path / "source.png"
    Image.new("RGB", (12, 12), "black").save(source)
    rendered = []
    monkeypatch.setattr(pw_access, "gemini_image", lambda *a, **k: rendered.append(1))

    class TruncatedDescribeClient:
        google_token = "tok"
        task_id = ""

        def generate_image(self, prompt, image_path, *, image_model, debug_dir=None):
            raise RuntimeError("empty response [finish_reason=STOP]")

        def generate(self, prompt, image_paths, **kw):
            class R:
                text = "An image of a lecture slide with a black background. " * 10
                finish_reason = "MAX_TOKENS"       # truncated
            return R()

    result, note = redraw_diagram_handwritten(TruncatedDescribeClient(), source, tmp_path / "out")

    assert result is None
    assert "truncated" in note
    assert rendered == [], "must not render from a truncated description"


def test_quota_breaker_stops_spending_after_the_first_failure(tmp_path):
    """One quota error must stop EVERY remaining redraw in the run.

    The image quota is per-project: once exhausted, further diagrams cannot
    succeed. Previously each remaining slide still paid for a billed describe
    call (~4 INR) before discovering its render would 429."""
    from types import SimpleNamespace

    from src.image_ai import redraw_slides_handwritten

    slides = []
    for n in range(1, 7):
        p = tmp_path / f"s{n}.png"
        Image.new("RGB", (12, 12), "black").save(p)
        slides.append(SimpleNamespace(slide_no=n, image_path=p))

    calls = {"image": 0, "describe": 0}

    class QuotaClient:
        google_token = "tok"
        task_id = ""

        def generate_image(self, prompt, image_path, *, image_model, debug_dir=None):
            calls["image"] += 1
            raise RuntimeError('429 {"error":"gemini image busy","detail":"RESOURCE_EXHAUSTED"}')

        def generate(self, prompt, image_paths, **kw):        # the BILLED describe stage
            calls["describe"] += 1
            raise AssertionError("describe must never run once quota is known gone")

    count, warnings = redraw_slides_handwritten(
        QuotaClient(), slides, {s.slide_no for s in slides}, tmp_path / "out")

    assert count == 0
    assert calls["image"] == 1, f"only the canary may call the image model, got {calls['image']}"
    assert calls["describe"] == 0, "no billed describe calls once quota is exhausted"
    assert any("Skipped AI redraw for 5" in w for w in warnings), warnings


def test_quota_detection_matches_the_apps_own_rate_limit_wording():
    """The two halves of the rate-limit path must agree on wording.

    They silently disagreed: _terminal_error_message writes "rate-limited"
    (hyphen) while the marker list had "rate limit" (space), so EVERY 429 read
    as not-quota. The breaker never tripped and each slide went on to spend its
    own full describe budget rediscovering the same limit — the bulk of a slow
    run's wall time. Assert against the real generated message, not a synthetic
    one; the old test passed a hand-written '429 ...' string and missed this.
    """
    from src.ai_client import _terminal_error_message
    from src.image_ai import _looks_like_quota

    for attempts in (1, 6):
        msg = _terminal_error_message(RuntimeError("boom"), 429, True, attempts)
        assert _looks_like_quota(msg), f"rate-limit message not recognised as quota: {msg!r}"
        assert _looks_like_quota(f"GeminiError: {msg}"), msg

    exhausted = _terminal_error_message(RuntimeError("quota"), 429, False, 1)
    assert _looks_like_quota(exhausted), exhausted

    # ...and a genuine ROUTE problem must still NOT look like quota, or the
    # fallback that fixes it would be skipped.
    assert not _looks_like_quota(
        "GeminiError: Gemini returned an empty response (single attempt) [finish_reason=STOP]")
    assert not _looks_like_quota(_terminal_error_message(RuntimeError("bad"), 404, False, 1))


def test_a_transient_5xx_is_not_mistaken_for_quota():
    """One 502 on the canary used to cancel EVERY remaining redraw in the run.

    The 5xx and 429 transient messages share their wording, so the breaker
    could only tell them apart once the message carried the HTTP status."""
    from src.ai_client import _terminal_error_message
    from src.image_ai import _looks_like_quota

    for status in (500, 502, 503):
        blip = _terminal_error_message(RuntimeError("boom"), status, True, 1)
        assert not _looks_like_quota(blip), blip
        assert not _looks_like_quota(f"GeminiError: {blip}"), blip
    # A real 429 keeps tripping the breaker.
    assert _looks_like_quota(_terminal_error_message(RuntimeError("boom"), 429, True, 1))


def test_rate_limited_canary_stops_the_run_without_paying_for_describes(tmp_path):
    """A 429 on the canary must stop the run using the REAL error wording."""
    from types import SimpleNamespace

    from src.ai_client import GeminiError, _terminal_error_message
    from src.image_ai import redraw_slides_handwritten

    slides = []
    for n in range(1, 7):
        p = tmp_path / f"s{n}.png"
        Image.new("RGB", (12, 12), "black").save(p)
        slides.append(SimpleNamespace(slide_no=n, image_path=p))

    calls = {"image": 0, "describe": 0}
    real_429 = _terminal_error_message(RuntimeError("boom"), 429, True, 1)

    class RateLimitedClient:
        google_token = "tok"
        task_id = ""

        def generate_image(self, prompt, image_path, *, image_model, debug_dir=None):
            calls["image"] += 1
            raise GeminiError(real_429)

        def generate(self, prompt, image_paths, **kw):
            calls["describe"] += 1
            raise GeminiError(real_429)

    count, warnings = redraw_slides_handwritten(
        RateLimitedClient(), slides, {s.slide_no for s in slides}, tmp_path / "out")

    assert count == 0
    assert calls["image"] == 1, f"canary only; got {calls['image']} image calls"
    assert calls["describe"] == 0, (
        f"a rate-limited direct call must skip the billed describe fallback; "
        f"got {calls['describe']}")
    assert any("Skipped AI redraw for 5" in w for w in warnings), warnings


def test_describe_stage_uses_a_capped_retry_budget(tmp_path):
    """The describe stage is optional work and must not inherit note
    generation's 6-attempt / ~62s backoff budget."""
    from src.ai_client import GeminiError
    from src.image_ai import _DESCRIBE_ATTEMPTS, redraw_diagram_handwritten
    from src import ai_client

    assert _DESCRIBE_ATTEMPTS < ai_client._REQUEST_SETTINGS.max_attempts, (
        "describe budget must be smaller than the note-generation budget")

    source = tmp_path / "source.png"
    Image.new("RGB", (12, 12), "black").save(source)
    seen = {}

    class Client:
        google_token = "tok"
        task_id = ""

        def generate_image(self, prompt, image_path, *, image_model, debug_dir=None):
            raise GeminiError("Gemini returned an empty response [finish_reason=STOP]")

        def generate(self, prompt, image_paths, **kw):
            seen.update(kw)
            raise GeminiError("nope")

    redraw_diagram_handwritten(Client(), source, tmp_path / "out",
                               image_model=DEFAULT_IMAGE_MODEL)

    assert seen.get("max_attempts") == _DESCRIBE_ATTEMPTS, (
        f"describe stage did not cap its retries: {seen}")


def test_image_bytes_extracted_from_every_known_shape():
    """The transport migration proved that parsing only inline_data re-masks a
    delivered image as 'empty'. Every shape the gateway can return must parse."""
    from src.ai_client import _image_bytes_from_result, _response_is_empty

    png = _png_bytes("white")
    b64 = base64.b64encode(png).decode()
    shapes = {
        "gemini snake_case": {"candidates": [{"content": {"parts": [
            {"inline_data": {"mime_type": "image/png", "data": b64}}]}}]},
        "gemini camelCase": {"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": b64}}]}}]},
        "fileData with bytes": {"candidates": [{"content": {"parts": [
            {"fileData": {"data": b64}}]}}]},
        "kit image endpoint": {"image_base64": b64},
        "litellm b64_json": {"choices": [{"message": {"images": [{"b64_json": b64}]}}]},
        "litellm data URI": {"choices": [{"message": {"images": [
            {"image_url": {"url": f"data:image/png;base64,{b64}"}}]}}]},
    }
    for name, result in shapes.items():
        assert _image_bytes_from_result(result) == png, f"shape not parsed: {name}"
        assert not _response_is_empty(result, expect="image"), f"shape read as empty: {name}"

    truly_empty = {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]}
    assert _image_bytes_from_result(truly_empty) is None
    assert _response_is_empty(truly_empty, expect="image")


def test_redrawn_diagram_is_inserted_whole_not_cut_into_regions(tmp_path):
    """A redraw is ONE finished figure; re-cutting it keeps a fragment.

    Measured before the fix: a 1400x760 redraw (aspect 1.84) was re-segmented by
    the DOCX writer and inserted at aspect 1.04 -- one region of it, with the
    rest silently dropped. And because the redraw re-composes the layout, the
    surviving fragment need not even be the part the note asked for.
    """
    import math

    from docx import Document

    from src.docx_writer import write_notes_docx
    from src.models import SlideData

    redrawn = tmp_path / "slide_007_handwritten.png"
    img = Image.new("RGB", (1400, 760), "white")
    d = ImageDraw.Draw(img)
    d.polygon([(120, 600), (560, 600), (560, 250)], outline=(20, 40, 160), width=8)
    d.line([(820, 150), (820, 650)], fill=(20, 40, 160), width=8)
    d.line([(820, 650), (1330, 650)], fill=(20, 40, 160), width=8)
    prev = None
    for i in range(500):
        x, y = 820 + i, int(650 - 200 * (0.5 + 0.5 * math.sin(i / 50)))
        if prev:
            d.line([prev, (x, y)], fill=(20, 40, 160), width=6)
        prev = (x, y)
    img.save(redrawn)

    slide = SlideData(
        slide_no=7, heading="Triangle and Graph",
        text="Ray diagram and graph of sin theta with labelled axes.",
        image_path=redrawn, metadata={"ai_redrawn": True},
    )
    notes = ('Triangle and Graph\n- Key idea.\n'
             '(Note to DTP: Insert the image with "37" and "graph" given on '
             'slide no. 7 under the heading "Triangle and Graph".)\n')
    out, _ = write_notes_docx(notes, tmp_path / "o.docx", [slide], run_dir=tmp_path)

    shapes = Document(str(out)).inline_shapes
    assert len(shapes) == 1
    aspect = int(shapes[0].width) / int(shapes[0].height)

    # The inserted picture must span ALL the drawn content. Margin trimming is
    # expected and fine, so compare against the content bounding box (both
    # figures) rather than the raw canvas. Re-cutting kept one region and gave
    # aspect 1.04; the full content spans ~2.3.
    from PIL import ImageChops

    with Image.open(redrawn) as im:
        bbox = ImageChops.difference(
            im.convert("RGB"), Image.new("RGB", im.size, (255, 255, 255))).getbbox()
    content_aspect = (bbox[2] - bbox[0]) / (bbox[3] - bbox[1])
    assert abs(aspect - content_aspect) < 0.35, (
        f"redrawn figure was re-cut: inserted aspect {aspect:.2f}, "
        f"full-content aspect {content_aspect:.2f}")


def test_redraw_marks_the_slide_so_the_writer_keeps_it_whole(tmp_path):
    from src.image_ai import redraw_slides_handwritten
    from src.models import SlideData

    source = tmp_path / "s1.png"
    Image.new("RGB", (12, 12), "black").save(source)
    slide = SlideData(slide_no=1, image_path=source)

    count, _ = redraw_slides_handwritten(
        RecordingImageClient(_png_bytes("white")), [slide], {1}, tmp_path / "out")

    assert count == 1
    assert slide.metadata.get("ai_redrawn") is True
    assert slide.image_path != source


def test_the_dead_direct_route_is_probed_once_per_run_not_once_per_slide(tmp_path, monkeypatch):
    """A billed-200-empty direct call is a transport fact, not a slide fact.

    Every slide re-buying that call was pure waste: the call is billed for its
    input tokens even when it comes back with no image part.
    """
    import pw_access

    from src.image_ai import redraw_slides_handwritten
    from src.models import SlideData

    slides = []
    for n in range(1, 6):
        p = tmp_path / f"s{n}.png"
        Image.new("RGB", (12, 12), "black").save(p)
        slides.append(SlideData(slide_no=n, image_path=p))

    calls = {"direct": 0}

    monkeypatch.setattr(pw_access, "gemini_image", lambda token, *, prompt, model, task_id="", **kw: {
        "result": {"image_base64": base64.b64encode(_png_bytes("white")).decode()}})

    class DeadDirectClient:
        google_token = "tok"
        task_id = ""

        def generate_image(self, prompt, image_path, *, image_model, debug_dir=None):
            calls["direct"] += 1
            return b""                      # billed HTTP 200, no image part

        def generate(self, prompt, image_paths, **kw):
            class R:
                text = "A labelled ray diagram of a plane mirror. " * 20
                finish_reason = "STOP"
            return R()

    count, _ = redraw_slides_handwritten(DeadDirectClient(), slides, {s.slide_no for s in slides},
                                         tmp_path / "out")

    assert count == 5, "every slide should still be redrawn via the working route"
    assert calls["direct"] == 1, (
        f"the dead direct route should be probed once per run, was called {calls['direct']}x")


def test_a_failing_image_model_falls_back_to_the_other_one(tmp_path, monkeypatch):
    """A withdrawn/renamed model id must cost one diagram's retry, not the lot."""
    import pw_access

    from src.image_ai import redraw_diagram_handwritten

    source = tmp_path / "source.png"
    Image.new("RGB", (12, 12), "black").save(source)
    tried = []

    def fake_gemini_image(token, *, prompt, model, task_id="", **kw):
        tried.append(model)
        if model == DEFAULT_IMAGE_MODEL:
            return {"result": {"text": "model not found"}}     # never an image
        return {"result": {"image_base64": base64.b64encode(_png_bytes("white")).decode()}}

    monkeypatch.setattr(pw_access, "gemini_image", fake_gemini_image)

    class Client:
        google_token = "tok"
        task_id = ""

        def generate_image(self, prompt, image_path, *, image_model, debug_dir=None):
            return b""

        def generate(self, prompt, image_paths, **kw):
            class R:
                text = "A labelled ray diagram of a plane mirror. " * 20
                finish_reason = "STOP"
            return R()

    result, note = redraw_diagram_handwritten(Client(), source, tmp_path / "out")

    assert result is not None, "the fallback image model should have drawn it"
    assert note == "described"
    assert DEFAULT_IMAGE_MODEL in tried
    assert any(m != DEFAULT_IMAGE_MODEL for m in tried), tried
