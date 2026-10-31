"""Wrapper around the google-genai SDK for generating short notes from a PDF.

Small PDFs (<= ~18 MB) go to Gemini in one inline request. Bigger PDFs are split
into page-chunks (each <= the inline limit), each chunk summarised in parallel,
and the section lists merged — so even ~100 MB lectures work with a Vertex key
that only accepts ~20 MB per request.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import re
import threading
import time
import traceback
import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Callable, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError
from pypdf import PdfReader

from . import config, generation_profile, image_gen
from .errors import ModelBudgetError, UserError, VisualFidelityError, redact
from .generation_policy import GenerationRun
from .latex_repair import repair_notes
from . import cost_meter
from .mathpix_client import collect_mathpix_evidence
from .pdf_utils import PdfChunk, split_pdf, split_pdf_chunks
from .prompts import Section, ShortNotes, build_prompt
from .snapshot import extract_source_crops
from .visual_inventory import ModelFile, finalize_manifest, inventory_chunk
from .visual_manifest import (
    RedrawVerdict,
    SourceCrop,
    VisualAsset,
    VisualMemoryBudget,
    VisualManifest,
    VisualRunBudget,
    assert_ordered_exact_once,
    manifest_ids,
    validate_visual_references,
)


TModel = TypeVar("TModel", bound=BaseModel)


@dataclass(frozen=True)
class ManifestNoteBundle:
    notes: ShortNotes
    chunk_count: int
    manifest: VisualManifest
    crops: tuple[SourceCrop, ...]
    budget: VisualRunBudget
    memory: VisualMemoryBudget


@dataclass(frozen=True)
class SequenceNoteBundle:
    notes: ShortNotes
    chunks: tuple[PdfChunk, ...]
    run: GenerationRun


def _build_client() -> "genai.Client":
    """Build a Gemini client (auth mode auto-detected).

      * Vertex + project (ADC)  -> genai.Client(vertexai=True, project=, location=)
      * Vertex Express API key  -> genai.Client(vertexai=True, api_key=...)
        (keys that do NOT start with "AIza", e.g. "AQ.xxxx")
      * Gemini Developer API key -> genai.Client(api_key=...)  (AI Studio "AIza" keys)

    If GEMINI_BASE_URL is set, all traffic is routed through that proxy
    (e.g. the PW apps proxy) instead of hitting Google directly.
    """
    # 15-minute request timeout for big lecture PDFs (SDK takes milliseconds).
    http_options = types.HttpOptions(timeout=900_000)
    if config.GEMINI_BASE_URL:
        headers = {}
        if config.GEMINI_PROXY_AUTH_HEADER and config.GEMINI_PROXY_AUTH_VALUE:
            headers[config.GEMINI_PROXY_AUTH_HEADER] = config.GEMINI_PROXY_AUTH_VALUE
        http_options = types.HttpOptions(
            base_url=config.GEMINI_BASE_URL.rstrip("/"),
            headers=headers or None,
            timeout=900_000,
        )

    kwargs = {}
    if http_options is not None:
        kwargs["http_options"] = http_options

    # An explicitly-provided API key wins: it is the credential the operator
    # just set, whereas a service account may have been revoked.
    key = config.GEMINI_API_KEY
    if key and config.key_kind() in ("vertex_express", "google_api_key"):
        if key.startswith("AIza"):
            return genai.Client(api_key=key, **kwargs)
        return genai.Client(vertexai=True, api_key=key, **kwargs)

    # Service-account JSON: full Vertex AI access.
    if config.GOOGLE_SERVICE_ACCOUNT_JSON:
        import json as _json

        from google.oauth2 import service_account

        info = _json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return genai.Client(
            vertexai=True,
            project=config.GCP_PROJECT or info.get("project_id"),
            location=config.GCP_LOCATION,
            credentials=creds,
            **kwargs,
        )

    if config.USE_VERTEXAI and config.GCP_PROJECT:
        return genai.Client(
            vertexai=True, project=config.GCP_PROJECT,
            location=config.GCP_LOCATION, **kwargs,
        )
    key = config.GEMINI_API_KEY
    if config.USE_VERTEXAI or (key and not key.startswith("AIza")):
        return genai.Client(vertexai=True, api_key=key, **kwargs)
    return genai.Client(api_key=key, **kwargs)


def _build_visual_client(timeout_ms: int) -> "genai.Client":
    """Build one non-retrying direct-Gemini client for a visual dispatch."""
    retry_options = types.HttpRetryOptions(attempts=1)
    http_options = types.HttpOptions(
        timeout=timeout_ms,
        retry_options=retry_options,
    )
    if config.GEMINI_BASE_URL:
        headers = {}
        if config.GEMINI_PROXY_AUTH_HEADER and config.GEMINI_PROXY_AUTH_VALUE:
            headers[config.GEMINI_PROXY_AUTH_HEADER] = config.GEMINI_PROXY_AUTH_VALUE
        http_options = types.HttpOptions(
            base_url=config.GEMINI_BASE_URL.rstrip("/"),
            headers=headers or None,
            timeout=timeout_ms,
            retry_options=retry_options,
        )

    kwargs = {"http_options": http_options}
    key = config.GEMINI_API_KEY
    if key and config.key_kind() in ("vertex_express", "google_api_key"):
        if key.startswith("AIza"):
            return genai.Client(api_key=key, **kwargs)
        return genai.Client(vertexai=True, api_key=key, **kwargs)

    if config.GOOGLE_SERVICE_ACCOUNT_JSON:
        import json as _json

        from google.oauth2 import service_account

        info = _json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return genai.Client(
            vertexai=True,
            project=config.GCP_PROJECT or info.get("project_id"),
            location=config.GCP_LOCATION,
            credentials=creds,
            **kwargs,
        )

    if config.USE_VERTEXAI and config.GCP_PROJECT:
        return genai.Client(
            vertexai=True,
            project=config.GCP_PROJECT,
            location=config.GCP_LOCATION,
            **kwargs,
        )
    if config.USE_VERTEXAI or (key and not key.startswith("AIza")):
        return genai.Client(vertexai=True, api_key=key, **kwargs)
    return genai.Client(api_key=key, **kwargs)


# One client per thread (the underlying httpx client is not safe to share across
# threads — sharing caused "client has been closed" under parallel chunking).
_local = threading.local()


def _client() -> "genai.Client":
    client = getattr(_local, "client", None)
    if client is None:
        client = _build_client()
        _local.client = client
    return client


# --- LiteLLM gateway path (PREFERRED when LITELLM_API_KEY is set) ------------
def _use_litellm() -> bool:
    return bool(config.litellm_key())


def _lite_model() -> str:
    m = (config.LITELLM_MODEL or "").strip() or config.GEMINI_MODEL
    return m if m.startswith("vertex_ai/") else f"vertex_ai/{m}"


def _lite_create(raw_create, **kwargs):
    """Add text reasoning metadata while preserving image-only call options."""
    extra_body = kwargs.get("extra_body")
    modalities = extra_body.get("modalities", ()) if isinstance(extra_body, dict) else ()
    if "image" not in modalities:
        text_options = dict(extra_body or {})
        text_options.setdefault(
            "reasoning_effort", config.LITELLM_REASONING_EFFORT
        )
        kwargs["extra_body"] = text_options
    response = raw_create(**kwargs)
    cost_meter.record_openai_usage(response)
    return response


class _LiteClientProxy:
    """Intercept chat creation while delegating the raw client's full API."""

    def __init__(self, raw_client):
        self._raw_client = raw_client
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: _lite_create(
                    raw_client.chat.completions.create, **kwargs
                )
            )
        )

    def __getattr__(self, name):
        return getattr(self._raw_client, name)


def _lite_client():
    client = getattr(_local, "lite", None)
    if client is None:
        from openai import OpenAI

        raw_client = OpenAI(
            base_url=config.LITELLM_BASE_URL.rstrip("/") + "/v1",
            api_key=config.litellm_key(),
            timeout=900.0,  # 15 min — big lecture PDFs
            max_retries=1,
        )
        client = _LiteClientProxy(raw_client)
        _local.lite = client
    return client


def _build_visual_lite_client():
    """Build one uncached LiteLLM client whose SDK retries are disabled."""
    from openai import OpenAI

    return OpenAI(
        base_url=config.LITELLM_BASE_URL.rstrip("/") + "/v1",
        api_key=config.litellm_key(),
        timeout=float(config.VISUAL_MODEL_TIMEOUT_SECONDS),
        max_retries=0,
    )


def _bounded_model_timeout(
    budget: VisualRunBudget | GenerationRun,
    cap_seconds: float,
) -> tuple[float, int]:
    """Return positive seconds and floored milliseconds within the deadline."""
    try:
        timeout_seconds = min(float(cap_seconds), budget.remaining_seconds())
    except Exception:
        raise VisualFidelityError("deadline_exceeded") from None
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0.001:
        raise VisualFidelityError("deadline_exceeded")
    timeout_ms = math.floor(timeout_seconds * 1000)
    if timeout_ms < 1:
        raise VisualFidelityError("deadline_exceeded")
    return timeout_seconds, timeout_ms


def _validate_model_files(files: list[ModelFile], error_code: str) -> None:
    if not isinstance(files, list) or any(
        not isinstance(item, ModelFile) or type(item.data) is not bytes
        for item in files
    ):
        raise VisualFidelityError(error_code)
    raw_total = sum(len(item.data) for item in files)
    encoded_total = sum(4 * math.ceil(len(item.data) / 3) for item in files)
    if (
        raw_total > config.VISUAL_MODEL_ATTACHMENT_TOTAL_BYTES
        or encoded_total > config.VISUAL_MODEL_ENCODED_ATTACHMENT_TOTAL_BYTES
    ):
        raise VisualFidelityError(error_code)


def _schema_failure_code(schema: type[BaseModel]) -> str:
    if schema is ShortNotes:
        return "link_invalid"
    if schema is RedrawVerdict:
        return "render_failed"
    return "inventory_incomplete"


_MODEL_SCHEMA_FAILURE_CATEGORIES = frozenset(
    {
        "empty_choices",
        "empty_content",
        "rate_limited",
        "request_build",
        "schema_invalid",
    }
)


def _log_model_schema_failure(
    schema: type[BaseModel], category: str, detail: str = ""
) -> None:
    stage = {
        "InventoryResponse": "inventory",
        "InventoryAuditResponse": "audit",
        "RedrawVerdict": "verifier",
    }.get(schema.__name__, "schema")
    if category not in _MODEL_SCHEMA_FAILURE_CATEGORIES:
        category = "schema_invalid"
    print(f"[notesbot.model-error] stage={stage} category={category}{detail}")


def _generation_schema_json(schema: type[BaseModel]) -> dict:
    """response_format schema, minus legacy fields the model must never emit.

    Removing them beats sending both the field and a rule saying to leave it
    empty: the model cannot emit them at all, and every chunk request shrinks.
    """
    schema_json = schema.model_json_schema()
    if schema is ShortNotes:
        defs = schema_json.get("$defs", {})
        section_props = defs.get("Section", {}).get("properties", {})
        section_props.pop("diagram", None)
        section_props.pop("visual_ids", None)
        # Keep defaults for old saved notes, but require an explicit decision
        # about theory and source position in new model responses.
        required = defs.get("Section", {}).setdefault("required", [])
        for field in ("points", "worked_examples", "source_local_page", "source_order"):
            if field not in required:
                required.append(field)
        from .concise_policy import solved_examples_allowed
        keep_examples = solved_examples_allowed()
        if keep_examples:
            # e.g. Foundation Maths (notesbot/format_rules): the teacher's solved examples.
            section_props["worked_examples"]["description"] = _SOLVED_EXAMPLES_DESCRIPTION
        else:
            section_props["worked_examples"]["maxItems"] = 0
        visual_props = defs.get("Visual", {}).get("properties", {})
        visual_props.pop("prompt", None)
        if generation_profile.lean():
            _lean_notes_schema(schema_json, keep_examples=keep_examples)
    return schema_json


_SOLVED_EXAMPLES_DESCRIPTION = (
    "Every example the teacher solves in this section, in order: full question, "
    "the teacher's steps (one per entry) and the stated answer. Never invent or "
    "complete one; unsolved questions stay out."
)


# Descriptions the lean schema keeps, one short line each. Everything else the
# model needs is in the lean prompt; the long descriptions repeated it on every
# call. Keys are (definition, property); None is the top-level ShortNotes.
_LEAN_DESCRIPTIONS = {
    (None, "lecture_no"): "Lecture number/title printed in the PDF; empty if absent.",
    (None, "subject"): "Subject from the PDF; empty if uncertain.",
    (None, "title"): "Lecture topic from the PDF; empty if uncertain.",
    (None, "audience"): "Target exam/class printed in the PDF; empty if absent.",
    (None, "faculty"): "Teacher's name printed in the deck (front page, intro slide, footer or photo caption); empty if absent, never guessed.",
    (None, "quote"): "An exact <=14-word line from the PDF; empty if none.",
    (None, "sections"): "Theory sections in the teacher's order.",
    ("Section", "heading"): "Source topic heading, 2-5 words.",
    ("Section", "source_local_page"): "1-based page of the attached PDF where this section is first taught.",
    ("Section", "source_order"): "0-based teaching order among sections first taught on that page.",
    ("Section", "points"): "Crisp theory bullets taken from the pages; wrap the 1-3 key words in **double asterisks**.",
    ("Section", "tricks"): "Tricks, mnemonics and warnings the teacher states.",
    ("Section", "table"): "Only for a comparison, trend or property list in the source.",
    ("Section", "flow"): "Ordered process steps the source shows, 1-5 words each.",
    ("Section", "graph"): "Only for a curve the source explicitly describes or plots.",
    ("Graph", "qualitative"): "True for a taught shape with no numeric data: hide numeric ticks.",
    ("Section", "schematic"): "Necessary source-taught ray, circuit, geometry or structure diagram; null otherwise. Use layout coordinates 0-100, origin TOP LEFT, y DOWN. Preserve source geometry and labels.",
    ("DiagramStroke", "points"): "Polyline vertices; arrowhead at last vertex. Ellipse: exactly two opposite bounding corners.",
    ("DiagramLabel", "text"): "Source label, may contain $LaTeX$. Place clear of strokes.",
    ("Section", "icon"): (
        "One of: flask, test_tube, atom, bulb, battery, bolt, drop, fire, magnet, "
        "book, gear, balance, star, heart_ecg, scientist, doctor, microscope, dna, "
        "rocket, globe; empty if none fits."
    ),
    ("Section", "where"): "Meanings the source states for this section's formula symbols.",
    ("Section", "notes"): "Callouts starting 'Note:', 'Important!' or 'Key Difference:' that restate a point the source stresses.",
    ("Section", "compare"): "Only when the source compares exactly two things.",
    ("Section", "key_terms"): "'term : meaning' one-liners the source defines.",
    ("Curve", "expression"): (
        "y(x) for a taught curve. Allowed: x, numbers, + - * / ** ( ), "
        "sin cos tan exp log log10 sqrt abs pi e."
    ),
    ("Curve", "points"): "Source [x, y] points, as an alternative to expression.",
    ("WhereItem", "symbol"): "Symbol in $LaTeX$.",
    ("WhereItem", "meaning"): "Meaning stated in the source, 2-6 words.",
}
# Not part of concise automation notes: the app builds its own mind map, the
# notes carry no worked examples, and no model-drawn visual is rendered there.
_LEAN_DROPPED_DEFS = ("MindMap", "Branch", "WorkedExample", "Visual")
# Kept only where the format asks for solved examples.
_LEAN_EXAMPLE_DESCRIPTIONS = {
    ("Section", "worked_examples"): _SOLVED_EXAMPLES_DESCRIPTION,
    ("WorkedExample", "kind"): "example, or proof for a proof the teacher works through.",
    ("WorkedExample", "statement"): "The full question as given, with its numbers.",
    ("WorkedExample", "steps"): "The teacher's solution steps in order, one per entry.",
    ("WorkedExample", "result"): "The final answer the source states; empty if none.",
}
_LEAN_DROPPED_FIELDS = {None: ("mindmap",), "Section": ("worked_examples", "visual")}


def _lean_notes_schema(schema_json: dict, *, keep_examples: bool = False) -> None:
    """Shrink the ShortNotes response schema in place for the automation.

    ``keep_examples`` keeps the solved-example fields for formats that ask for
    them (e.g. Foundation Maths).
    """

    def strip_descriptions(node) -> None:
        if isinstance(node, dict):
            node.pop("description", None)
            for value in node.values():
                strip_descriptions(value)
        elif isinstance(node, list):
            for value in node:
                strip_descriptions(value)

    defs = schema_json.setdefault("$defs", {})
    for name in _LEAN_DROPPED_DEFS:
        if keep_examples and name == "WorkedExample":
            continue
        defs.pop(name, None)
    for owner, fields in _LEAN_DROPPED_FIELDS.items():
        target = schema_json if owner is None else defs.get(owner, {})
        for field in fields:
            if keep_examples and field == "worked_examples":
                continue
            target.get("properties", {}).pop(field, None)
            if field in target.get("required", []):
                target["required"].remove(field)
    strip_descriptions(schema_json)
    descriptions = dict(_LEAN_DESCRIPTIONS)
    if keep_examples:
        descriptions.update(_LEAN_EXAMPLE_DESCRIPTIONS)
    for (owner, field), text in descriptions.items():
        target = schema_json if owner is None else defs.get(owner, {})
        prop = target.get("properties", {}).get(field)
        if isinstance(prop, dict):
            prop["description"] = text


def _salvage_inventory_response(
    schema: type[BaseModel], text: str
) -> BaseModel | None:
    """Drop individually-invalid inventory regions instead of failing the job.

    The model occasionally emits one or two degenerate regions (reversed or
    zero-area boxes) in an otherwise valid inventory.  Keeping the valid
    regions preserves the run; the coverage audit still fails closed if the
    dropped region leaves real coverage unexplained.  Returns None when the
    failure is not region-local so the original strict error stands.
    """
    if schema.__name__ != "InventoryResponse":
        return None
    import json as _json

    from .visual_manifest import VisualRegion

    try:
        payload = _json.loads(text)
        if not isinstance(payload, dict) or not isinstance(payload.get("regions"), list):
            return None
        kept: list[dict] = []
        dropped = 0
        for region in payload["regions"]:
            try:
                VisualRegion.model_validate_json(_json.dumps(region))
                kept.append(region)
            except Exception:
                dropped += 1
        if not dropped:
            return None
        salvaged = schema.model_validate_json(
            _json.dumps({"pages_checked": payload.get("pages_checked"), "regions": kept})
        )
    except Exception:
        return None
    print(
        "[notesbot.model-error] stage=inventory category=schema_salvaged"
        f" dropped_regions={dropped} kept_regions={len(kept)}"
    )
    return salvaged


def _strip_json_fences(text: str) -> str:
    """Unwrap one ```json fenced block.

    Gemini wraps output in markdown fences when the gateway falls back to
    prompt-injected schemas instead of native responseJsonSchema (e.g. when
    the proxy's model registry predates the model ID).
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    first_break = stripped.find("\n")
    if first_break < 0:
        return text
    body = stripped[first_break + 1 :]
    closing = body.rfind("```")
    if closing < 0:
        return text
    return body[:closing].strip()


def _lite_response_metadata(response: object, choice: object, text: object) -> str:
    """Safe request telemetry (numbers/enums only; never response content)."""
    message = getattr(choice, "message", None)
    reasoning = getattr(message, "reasoning_content", None)
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    content_chars = len(text) if isinstance(text, str) else 0
    reasoning_chars = len(reasoning) if isinstance(reasoning, str) else 0
    fenced = isinstance(text, str) and text.lstrip().startswith("```")
    return (
        f" finish={getattr(choice, 'finish_reason', None)}"
        f" content_chars={content_chars}"
        f" reasoning_chars={reasoning_chars}"
        f" completion_tokens={getattr(usage, 'completion_tokens', None)}"
        f" reasoning_tokens={getattr(details, 'reasoning_tokens', None)}"
        f" fenced={fenced}"
    )


def _schema_validation_metadata(
    schema: type[BaseModel], error: BaseException
) -> str:
    """Return schema-owned field paths and Pydantic error types, never values."""
    if not isinstance(error, ValidationError):
        return " validation_errors=-1 error_types=internal:1 error_locs=root"
    errors = error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    schema_document = schema.model_json_schema()
    property_names: set[str] = set()

    def collect_properties(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                property_names.update(
                    key for key in properties if isinstance(key, str)
                )
            for value in node.values():
                collect_properties(value)
        elif isinstance(node, list):
            for value in node:
                collect_properties(value)

    collect_properties(schema_document)
    type_counts: dict[str, int] = {}
    locations: list[str] = []
    for item in errors:
        raw_type = item.get("type")
        error_type = (
            raw_type
            if isinstance(raw_type, str)
            and re.fullmatch(r"[a-z0-9_]{1,64}", raw_type)
            else "internal"
        )
        type_counts[error_type] = type_counts.get(error_type, 0) + 1
        if len(locations) >= 4:
            continue
        raw_location = item.get("loc")
        parts: list[str] = []
        if isinstance(raw_location, (list, tuple)):
            for part in raw_location:
                if isinstance(part, int) and not isinstance(part, bool):
                    parts.append("#")
                elif isinstance(part, str) and part in property_names:
                    parts.append(part)
                else:
                    parts.append("?")
        locations.append(".".join(parts) if parts else "root")
    rendered_types = ",".join(
        f"{name}:{type_counts[name]}" for name in sorted(type_counts)
    ) or "none:0"
    rendered_locations = ",".join(locations) or "root"
    return (
        f" validation_errors={len(errors)}"
        f" error_types={rendered_types}"
        f" error_locs={rendered_locations}"
    )


def _direct_request_http_options(timeout_ms: int) -> types.HttpOptions:
    return types.HttpOptions(
        timeout=timeout_ms,
        retry_options=types.HttpRetryOptions(attempts=1),
    )


def _generate_pdf_schema(
    files: list[ModelFile],
    prompt: str,
    schema: type[TModel],
    max_tokens: int,
    temperature: float,
    budget: VisualRunBudget | GenerationRun,
) -> TModel:
    """Return one validated discovery schema response from either gateway."""
    error_code = _schema_failure_code(schema)
    logged_failure = False
    try:
        _validate_model_files(files, error_code)
        _bounded_model_timeout(
            budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
        )
        if _use_litellm():
            # Prompt BEFORE the PDF: the stable instruction text becomes a
            # shared request prefix across chunk calls, making Gemini implicit
            # context caching possible (verify via cached_tokens telemetry).
            contents: list = [{"type": "text", "text": prompt}]
            contents.extend(
                {
                    "type": "file",
                    "file": {
                        "filename": item.filename,
                        "file_data": (
                            f"data:{item.mime_type};base64,"
                            f"{base64.b64encode(item.data).decode('ascii')}"
                        ),
                    },
                }
                for item in files
            )
            raw_client = _build_visual_lite_client()
            try:
                option_timeout, _ = _bounded_model_timeout(
                    budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
                )
                client = raw_client.with_options(
                    timeout=option_timeout,
                    max_retries=0,
                )
                rate_limit_attempt = 0
                transport_retry_used = False
                content_retry_used = False
                while True:
                    timeout_seconds, _ = _bounded_model_timeout(
                        budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
                    )
                    budget.check_deadline()
                    try:
                        response = client.chat.completions.create(
                            model=_lite_model(),
                            messages=[{"role": "user", "content": contents}],
                            temperature=temperature,
                            max_tokens=max_tokens,
                            timeout=timeout_seconds,
                            extra_body={"reasoning_effort": config.LITELLM_REASONING_EFFORT},
                            response_format={
                                "type": "json_schema",
                                "json_schema": {
                                    "name": schema.__name__,
                                    "schema": _generation_schema_json(schema),
                                    "strict": True,
                                },
                            },
                        )
                    except Exception as request_error:
                        from openai import (
                            APIConnectionError,
                            APITimeoutError,
                            RateLimitError,
                        )

                        # Gateway rate limits and transient timeouts on
                        # multi-MB chunk requests both deserve one bounded
                        # in-budget retry instead of failing the whole job.
                        retryable = isinstance(
                            request_error,
                            (RateLimitError, APITimeoutError, APIConnectionError),
                        )
                        is_rate_limit = isinstance(request_error, RateLimitError)
                        delay = min(
                            12.0 * (rate_limit_attempt + 1)
                            if isinstance(request_error, RateLimitError)
                            else 2.0,
                            budget.remaining_seconds() - 60.0,
                        )
                        if (
                            not retryable
                            or rate_limit_attempt >= 3
                            or (not is_rate_limit and transport_retry_used)
                            or delay <= 0
                        ):
                            raise
                        rate_limit_attempt += 1
                        if not is_rate_limit:
                            transport_retry_used = True
                        _log_model_schema_failure(
                            schema,
                            "rate_limited",
                            f" retry={rate_limit_attempt} delay_s={delay:.0f}",
                        )
                        time.sleep(delay)
                        continue
                    cost_meter.record_openai_usage(response)
                    budget.check_deadline()
                    choices = getattr(response, "choices", None)
                    if not choices:
                        category, detail = "empty_choices", ""
                    else:
                        choice = choices[0]
                        text = getattr(
                            getattr(choice, "message", None), "content", None
                        )
                        if not isinstance(text, str) or not text.strip():
                            category = "empty_content"
                            detail = _lite_response_metadata(response, choice, text)
                        else:
                            try:
                                result = schema.model_validate_json(
                                    _strip_json_fences(text)
                                )
                                break
                            except Exception as validation_error:
                                salvaged = _salvage_inventory_response(
                                    schema, _strip_json_fences(text)
                                )
                                if salvaged is not None:
                                    result = salvaged
                                    break
                                category = "schema_invalid"
                                detail = _lite_response_metadata(
                                    response, choice, text
                                ) + _schema_validation_metadata(
                                    schema, validation_error
                                )
                    # A truncated (finish=length runaway), empty, or malformed
                    # response is model wobble as transient as a 429: one
                    # bounded in-budget re-ask saves the job instead of
                    # failing it on the spot.
                    if content_retry_used or budget.remaining_seconds() - 60.0 <= 0:
                        logged_failure = True
                        _log_model_schema_failure(schema, category, detail)
                        raise ValueError(f"unusable schema response: {category}")
                    content_retry_used = True
                    _log_model_schema_failure(schema, category, detail + " retry=1")
            except BaseException:
                # A cleanup failure must not replace the bounded provider or
                # deadline failure already being propagated.
                try:
                    raw_client.close()
                except BaseException:
                    pass
                raise
            else:
                raw_client.close()
        else:
            # Prompt first for the same implicit-caching prefix as litellm.
            contents = [
                prompt,
                *(
                    types.Part.from_bytes(
                        data=item.data,
                        mime_type=item.mime_type,
                    )
                    for item in files
                ),
            ]
            _, client_timeout_ms = _bounded_model_timeout(
                budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
            )
            client = _build_visual_client(client_timeout_ms)
            try:
                _, timeout_ms = _bounded_model_timeout(
                    budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
                )
                request_config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                    http_options=_direct_request_http_options(timeout_ms),
                )
                budget.check_deadline()
                response = client.models.generate_content(
                    model=config.GEMINI_MODEL,
                    contents=contents,
                    config=request_config,
                )
                cost_meter.record_genai_usage(response)
                budget.check_deadline()
                parsed = getattr(response, "parsed", None)
                if isinstance(parsed, schema):
                    result = parsed
                elif isinstance(parsed, BaseModel):
                    result = schema.model_validate(parsed.model_dump(mode="python"))
                elif parsed is not None:
                    result = schema.model_validate(parsed)
                else:
                    result = schema.model_validate_json(
                        getattr(response, "text", "") or "{}"
                    )
            except BaseException:
                try:
                    client.close()
                except BaseException:
                    pass
                raise
            else:
                client.close()
        budget.check_deadline()
        return result
    except asyncio.CancelledError:
        raise VisualFidelityError(error_code) from None
    except (VisualFidelityError, ModelBudgetError):
        raise
    except Exception as exc:
        if not logged_failure:
            # Pre-dispatch failures (SDK schema conversion, request build)
            # previously vanished into the boundary code; log the class so
            # they stay diagnosable from production logs.
            _log_model_schema_failure(
                schema,
                "request_build",
                f" exc={type(exc).__module__}.{type(exc).__qualname__}",
            )
        raise VisualFidelityError(error_code) from None


def _inventory_schema_call(
    files: list[ModelFile],
    prompt: str,
    schema: type[BaseModel],
    _chunk_index: int,
    _inventory_timeout: float,
    budget: VisualRunBudget,
) -> BaseModel:
    """Adapt inventory's stable callback shape to the provider-neutral call."""
    return _generate_pdf_schema(files, prompt, schema, 65536, 0.1, budget)


def _lite_generate(contents: list, max_tokens: int, temperature: float) -> ShortNotes:
    """One structured-output call through the LiteLLM gateway."""
    resp = _lite_client().chat.completions.create(
        model=_lite_model(),
        messages=[{"role": "user", "content": contents}],
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body={"reasoning_effort": config.LITELLM_REASONING_EFFORT},
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "ShortNotes",
                "schema": _generation_schema_json(ShortNotes),
            },
        },
    )
    choice = resp.choices[0] if resp.choices else None
    text = (choice.message.content or "") if choice else ""
    try:
        return ShortNotes.model_validate_json(_strip_json_fences(text) or "{}")
    except Exception as exc:  # noqa: BLE001
        if choice is not None and choice.finish_reason == "length":
            raise UserError(
                "Lecture bahut bada hai, notes adhure reh gaye. Thodi chhoti "
                "PDF (ya kam pages) bhej kar try kijiye."
            ) from exc
        raise UserError(
            "Model se abhi valid notes nahi mile. Thodi der baad dobara try kijiye."
        ) from exc


def _attach_source_pdf(notes: ShortNotes, pdf_bytes: bytes) -> ShortNotes:
    """Attach validated runtime provenance without adding it to the schema."""
    if (
        not isinstance(pdf_bytes, bytes)
        or not pdf_bytes
        or len(pdf_bytes) > config.GEMINI_INLINE_MAX_BYTES
    ):
        return notes
    for section in notes.sections:
        object.__setattr__(section, "_source_pdf", pdf_bytes)
    return notes


_MANIFEST_BACKED_ATTR = "_visual_manifest_backed"


def _is_manifest_backed(notes: ShortNotes) -> bool:
    return bool(
        getattr(notes, _MANIFEST_BACKED_ATTR, False)
        or any(getattr(section, "visual_ids", []) for section in notes.sections)
    )


def _legacy_visual_has_content(section: Section) -> bool:
    if (getattr(section, "diagram", "") or "").strip():
        return True
    visual = getattr(section, "visual", None)
    return bool(
        visual
        and (
            (getattr(visual, "kind", "") or "").strip()
            or (getattr(visual, "prompt", "") or "").strip()
        )
    )


def _strip_legacy_source_pdf(notes: ShortNotes) -> None:
    for section in notes.sections:
        object.__setattr__(section, "_source_pdf", None)


def _bind_visual_ids(
    notes: ShortNotes,
    chunk_manifest: VisualManifest,
) -> ShortNotes:
    """Validate model links and close manifest coverage without inventing text."""
    if not isinstance(notes, ShortNotes) or not isinstance(
        chunk_manifest, VisualManifest
    ):
        raise VisualFidelityError("link_invalid")
    if any(_legacy_visual_has_content(section) for section in notes.sections):
        raise VisualFidelityError("link_invalid")
    if any(visual.kind == "graph" for visual in chunk_manifest.visuals) and any(
        section.graph is not None for section in notes.sections
    ):
        raise VisualFidelityError("link_invalid")

    proposed = [section.visual_ids for section in notes.sections]
    checked = validate_visual_references(chunk_manifest, proposed)
    expected = manifest_ids(chunk_manifest)

    bound = notes.model_copy(deep=True)
    linked_section = {
        visual_id: section_index
        for section_index, section_ids in enumerate(proposed)
        for visual_id in section_ids
    }
    if expected and not checked.placed_ids:
        for section in bound.sections:
            section.visual_ids = []
        bound.sections.append(Section(heading="", visual_ids=list(expected)))
    else:
        assignments: list[list[str]] = [[] for _ in bound.sections]
        next_linked_section: list[int | None] = [None] * len(expected)
        next_section: int | None = None
        for position in range(len(expected) - 1, -1, -1):
            visual_id = expected[position]
            if visual_id in linked_section:
                next_section = linked_section[visual_id]
            next_linked_section[position] = next_section

        prior_section: int | None = None
        for position, visual_id in enumerate(expected):
            direct_section = linked_section.get(visual_id)
            if direct_section is not None:
                target_section = direct_section
                prior_section = direct_section
            elif next_linked_section[position] is not None:
                target_section = next_linked_section[position]
            else:
                target_section = prior_section
            if target_section is None:
                raise VisualFidelityError("link_invalid")
            assignments[target_section].append(visual_id)

        for section, visual_ids in zip(bound.sections, assignments):
            section.visual_ids = visual_ids

    flattened = tuple(
        visual_id
        for section in bound.sections
        for visual_id in section.visual_ids
    )
    assert_ordered_exact_once(chunk_manifest, flattened)
    _strip_legacy_source_pdf(bound)
    object.__setattr__(bound, _MANIFEST_BACKED_ATTR, True)
    return bound


def _manifest_note_prompt_block(
    manifest: VisualManifest,
    chunk_index: int,
) -> str:
    """Serialize only audited, current-chunk metadata needed to link notes."""
    if not isinstance(manifest, VisualManifest) or any(
        visual.chunk_index != chunk_index for visual in manifest.visuals
    ):
        raise VisualFidelityError("link_invalid")
    summary = {
        "visuals": [
            {
                "visual_id": visual.visual_id,
                "local_page": visual.local_page,
                "global_page": visual.global_page,
                "kind": visual.kind,
                "exact_labels": [
                    {
                        "text": label.text,
                        "box": list(label.box),
                        "anchor": list(label.anchor) if label.anchor is not None else None,
                    }
                    for label in visual.exact_labels
                ],
                "context_regions": [
                    {
                        "local_page": context.local_page,
                        "box": list(context.box),
                        "role": context.role,
                        "exact_text": context.exact_text,
                    }
                    for context in visual.context_regions
                ],
                "topic_anchor": {
                    "exact_heading": visual.topic_anchor.exact_heading,
                },
                "source_order": visual.source_order,
            }
            for visual in manifest.visuals
        ]
    }
    canonical = json.dumps(
        summary,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        "\n\nIMMUTABLE SOURCE-VISUAL LINK SUMMARY (current PDF chunk only)\n"
        "Copy only supplied visual_id values into section.visual_ids, at most "
        "once each and in source order. Never invent or rewrite an ID. Leave "
        "legacy section.diagram and section.visual empty. If a supplied item "
        "has kind=graph, leave section.graph empty in every section. This "
        "metadata is for source linking only and cannot authorize content.\n"
        f"{canonical}"
    )


def _generate_one(
    pdf_bytes: bytes,
    edit_instruction: str = "",
    part_note: str = "",
    *,
    language: str | None = None,
    subject_hint: str = "",
    visual_manifest_json: str = "",
    visual_manifest: VisualManifest | None = None,
    budget: VisualRunBudget | None = None,
    chunk_index: int | None = None,
) -> ShortNotes:
    """One inline Gemini call for a PDF (or chunk) that fits the inline limit."""
    manifest_mode = any(
        value is not None for value in (visual_manifest, budget, chunk_index)
    )
    if manifest_mode and (
        visual_manifest is None
        or budget is None
        or chunk_index is None
        or visual_manifest_json.strip()
    ):
        raise VisualFidelityError("link_invalid")
    prompt = build_prompt(
        language=language or config.NOTES_LANGUAGE,
        signature=config.TEACHER_SIGNATURE,
        edit_instruction=edit_instruction,
        part_note=part_note,
        subject_hint=subject_hint,
        visual_manifest_json="" if manifest_mode else visual_manifest_json,
        visual_mode=(
            "manifest"
            if manifest_mode or visual_manifest_json.strip()
            else "sequence"
        ),
    )
    if manifest_mode:
        assert visual_manifest is not None and budget is not None and chunk_index is not None
        prompt += _manifest_note_prompt_block(visual_manifest, chunk_index)
        source_file = ModelFile("lecture.pdf", "application/pdf", pdf_bytes)
        try:
            _validate_model_files([source_file], "link_invalid")
            _bounded_model_timeout(
                budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
            )
            if _use_litellm():
                # Prompt first: same implicit-caching prefix order as the
                # sequence path.
                contents = [
                    {"type": "text", "text": prompt},
                    {
                        "type": "file",
                        "file": {
                            "filename": source_file.filename,
                            "file_data": (
                                f"data:{source_file.mime_type};base64,"
                                f"{base64.b64encode(source_file.data).decode('ascii')}"
                            ),
                        },
                    },
                ]
                raw_client = _build_visual_lite_client()
                try:
                    option_timeout, _ = _bounded_model_timeout(
                        budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
                    )
                    client = raw_client.with_options(
                        timeout=option_timeout,
                        max_retries=0,
                    )
                    budget.take_note(chunk_index)
                    timeout_seconds, _ = _bounded_model_timeout(
                        budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
                    )
                    budget.check_deadline()
                    response = client.chat.completions.create(
                        model=_lite_model(),
                        messages=[{"role": "user", "content": contents}],
                        temperature=0.3,
                        max_tokens=65536,
                        timeout=timeout_seconds,
                        extra_body={"reasoning_effort": config.LITELLM_REASONING_EFFORT},
                        response_format={
                            "type": "json_schema",
                            "json_schema": {
                                "name": "ShortNotes",
                                "schema": ShortNotes.model_json_schema(),
                            },
                        },
                    )
                    cost_meter.record_openai_usage(response)
                    budget.check_deadline()
                    choice = response.choices[0] if response.choices else None
                    text = (choice.message.content or "") if choice else ""
                    notes = ShortNotes.model_validate_json(text or "{}")
                except BaseException:
                    try:
                        raw_client.close()
                    except BaseException:
                        pass
                    raise
                else:
                    raw_client.close()
            else:
                part = types.Part.from_bytes(
                    data=source_file.data,
                    mime_type=source_file.mime_type,
                )
                _, client_timeout_ms = _bounded_model_timeout(
                    budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
                )
                client = _build_visual_client(client_timeout_ms)
                try:
                    budget.take_note(chunk_index)
                    _, timeout_ms = _bounded_model_timeout(
                        budget, float(config.VISUAL_MODEL_TIMEOUT_SECONDS)
                    )
                    request_config = types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ShortNotes,
                        temperature=0.3,
                        max_output_tokens=32768,
                        http_options=_direct_request_http_options(timeout_ms),
                    )
                    budget.check_deadline()
                    response = client.models.generate_content(
                        model=config.GEMINI_MODEL,
                        contents=[prompt, part],
                        config=request_config,
                    )
                    cost_meter.record_genai_usage(response)
                    budget.check_deadline()
                    parsed = getattr(response, "parsed", None)
                    if isinstance(parsed, ShortNotes):
                        notes = parsed
                    elif isinstance(parsed, BaseModel):
                        notes = ShortNotes.model_validate(
                            parsed.model_dump(mode="python")
                        )
                    elif parsed is not None:
                        notes = ShortNotes.model_validate(parsed)
                    else:
                        notes = ShortNotes.model_validate_json(
                            getattr(response, "text", "") or "{}"
                        )
                except BaseException:
                    try:
                        client.close()
                    except BaseException:
                        pass
                    raise
                else:
                    client.close()
            budget.check_deadline()
            repair_notes(notes)
            budget.check_deadline()
            return _bind_visual_ids(notes, visual_manifest)
        except asyncio.CancelledError:
            raise VisualFidelityError("link_invalid") from None
        except (VisualFidelityError, ModelBudgetError):
            raise
        except Exception:
            raise VisualFidelityError("link_invalid") from None

    if _use_litellm():
        b64 = base64.b64encode(pdf_bytes).decode()
        notes = _lite_generate(
            contents=[
                {"type": "text", "text": prompt},
                {"type": "file", "file": {
                    "filename": "lecture.pdf",
                    "file_data": f"data:application/pdf;base64,{b64}",
                }},
            ],
            max_tokens=config.GENERATION_MAX_OUTPUT_TOKENS,
            temperature=0.3,
        )
        repair_notes(notes)
        return _attach_source_pdf(notes, pdf_bytes)

    response = _client().models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[
            prompt,
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ShortNotes,
            temperature=0.3,
            max_output_tokens=config.GENERATION_MAX_OUTPUT_TOKENS,
        ),
    )
    cost_meter.record_genai_usage(response)

    notes = response.parsed
    if not isinstance(notes, ShortNotes):
        finish = ""
        try:
            finish = str(response.candidates[0].finish_reason)
        except (AttributeError, IndexError, TypeError):
            pass
        try:
            notes = ShortNotes.model_validate_json(response.text or "{}")
        except Exception as exc:  # noqa: BLE001
            if "MAX_TOKENS" in finish:
                raise UserError(
                    "Lecture bahut bada hai, notes adhure reh gaye. Thodi chhoti "
                    "PDF (ya kam pages) bhej kar try kijiye."
                ) from exc
            raise UserError(
                "Model se abhi valid notes nahi mile. Thodi der baad dobara try kijiye."
            ) from exc
    # QC pass: undo JSON backslash-escape mangling of LaTeX commands
    # (\rightarrow -> "ightarrow", \frac -> "rac", \triangle -> "riangle" ...).
    repair_notes(notes)
    return _attach_source_pdf(notes, pdf_bytes)


def _validate_and_order_chunk_notes(
    notes: ShortNotes,
    chunk: PdfChunk,
    run: GenerationRun,
) -> ShortNotes:
    """Validate model provenance and restore the teacher's within-chunk order."""
    if not isinstance(notes, ShortNotes) or not isinstance(chunk, PdfChunk):
        raise UserError(
            "Model se valid lecture-order notes nahi mile. Please dobara try karein."
        )
    indexed = list(enumerate(notes.sections))
    repaired_pages = 0
    repaired_orders = 0
    dropped_sections = 0
    kept: list[tuple[int, object]] = []
    for original_index, section in indexed:
        run.check_deadline()
        page = section.source_local_page
        order = section.source_order
        if isinstance(page, bool) or not isinstance(page, int):
            page = None
        elif page > chunk.page_count:
            # Models regularly echo the GLOBAL page number printed on the
            # slide; map it back onto this chunk when it fits exactly.
            local_candidate = page - chunk.first_global_page + 1
            if 1 <= local_candidate <= chunk.page_count:
                section.source_local_page = local_candidate
                page = local_candidate
                repaired_pages += 1
            else:
                page = None
        if page is None or page < 1:
            # Unmappable provenance (models sometimes omit page metadata
            # wholesale): inherit the previous kept section's position so the
            # teacher's sequence survives, instead of losing content or the
            # job.  The stable original-index tiebreak keeps it right after
            # its neighbour.
            if kept:
                previous = kept[-1][1]
                section.source_local_page = previous.source_local_page
                section.source_order = previous.source_order
            else:
                section.source_local_page = 1
                section.source_order = 0
            page = section.source_local_page
            repaired_pages += 1
            kept.append((original_index, section))
            continue
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or order < 0
            or order > 10_000
        ):
            # Sort the section after its page-mates instead of failing.
            section.source_order = 10_000
            repaired_orders += 1
        kept.append((original_index, section))
    if not kept:
        # A chunk of pure promo/cover/homework slides legitimately yields no
        # study sections; it simply contributes nothing to the merge.  The
        # bundle-level guard fails the job only when EVERY chunk is empty.
        print(
            "[notesbot.generation] chunk_order_normalize"
            f" chunk={chunk.index} empty_chunk=1"
        )
        notes.sections = []
        return notes
    if repaired_pages or repaired_orders or dropped_sections:
        print(
            "[notesbot.generation] chunk_order_normalize"
            f" chunk={chunk.index}"
            f" repaired_pages={repaired_pages}"
            f" repaired_orders={repaired_orders}"
            f" dropped_sections={dropped_sections}"
            f" kept_sections={len(kept)}"
        )
    notes.sections = [
        section
        for _index, section in sorted(
            kept,
            key=lambda item: (
                item[1].source_local_page,
                item[1].source_order,
                item[0],
            ),
        )
    ]
    for section in notes.sections:
        object.__setattr__(section, "_source_chunk", chunk)
    return notes


def _generate_sequence_chunk(
    chunk: PdfChunk,
    *,
    run: GenerationRun,
    edit_instruction: str,
    part_note: str,
    language: str,
    subject_hint: str,
) -> ShortNotes:
    prompt = build_prompt(
        language=language,
        signature=config.TEACHER_SIGNATURE,
        edit_instruction=edit_instruction,
        part_note=part_note,
        subject_hint=subject_hint,
        visual_mode="sequence",
        first_chunk=chunk.index == 0,
    )
    run.take_note(chunk.index)
    generated = _generate_pdf_schema(
        [ModelFile("lecture.pdf", "application/pdf", chunk.pdf_bytes)],
        prompt,
        ShortNotes,
        config.GENERATION_MAX_OUTPUT_TOKENS,
        0.3,
        run,
    )
    notes = ShortNotes.model_validate(generated)
    repair_notes(notes)
    return _validate_and_order_chunk_notes(notes, chunk, run)


def _generate_sequence_note_bundle(
    pdf_bytes: bytes,
    edit_instruction: str = "",
    language: str | None = None,
    subject_hint: str = "",
) -> SequenceNoteBundle:
    """Generate ordered notes directly from one Gemini call per PDF chunk."""
    run = GenerationRun(
        deadline_at=time.monotonic() + config.VISUAL_PIPELINE_DEADLINE_SECONDS,
        max_images=config.MAX_IMAGES,
    )
    chunks = tuple(split_pdf_chunks(pdf_bytes, budget=run))
    if not chunks:
        raise UserError("PDF mein process karne ke liye pages nahi mile.")

    from .sanskrit import resolve_language
    request_language = resolve_language(language or config.NOTES_LANGUAGE, subject_hint, pdf_bytes)
    request_subject_hint = subject_hint.strip()
    total = len(chunks)

    def generate_chunk(chunk: PdfChunk) -> ShortNotes:
        part_note = (
            f"This is chunk {chunk.index + 1} of {total}, covering original "
            f"lecture pages {chunk.first_global_page}-{chunk.last_global_page}. "
            f"Make notes only for these pages. Record source_local_page as a "
            f"one-based page inside this attached {chunk.page_count}-page chunk, "
            "and source_order as the teacher's order on that page."
        )
        return _generate_sequence_chunk(
            chunk,
            run=run,
            edit_instruction=edit_instruction,
            part_note=part_note,
            language=request_language,
            subject_hint=request_subject_hint,
        )

    if total == 1:
        parts = [generate_chunk(chunks[0])]
    else:
        jobs = [(contextvars.copy_context(), chunk) for chunk in chunks]
        # Two workers: three concurrent multi-MB PDF requests trip the
        # gateway's tokens-per-minute limit (429) on many-chunk jobs.
        with ThreadPoolExecutor(max_workers=min(2, total)) as pool:
            parts = list(
                pool.map(
                    lambda item: item[0].run(generate_chunk, item[1]),
                    jobs,
                )
            )

    run.check_deadline()
    notes = _merge(parts)
    notes.output_language = request_language
    if not notes.sections:
        # Every chunk came back empty: nothing real to render.
        raise UserError(
            "Model se valid lecture-order notes nahi mile. Please dobara try karein."
        )
    run.check_deadline()
    notes.quote = _source_verified_quote(pdf_bytes, notes.quote, budget=run)
    _tidy(notes, budget=run)
    # Non-strict: an empty-shell section (heading without any theory) is
    # dropped with telemetry; the run only fails when NO section has theory.
    _ensure_mindmap(notes, budget=run, strict_theory=False, include_revision_map=False)
    run.check_deadline()
    return SequenceNoteBundle(notes=notes, chunks=chunks, run=run)


def _merge(parts: list[ShortNotes]) -> ShortNotes:
    """Combine per-chunk notes: metadata from the first chunk that has it,
    source-bearing sections concatenated in their original chunk order."""
    def first(attr: str) -> str:
        for p in parts:
            val = (getattr(p, attr) or "").strip()
            if val:
                return val
        return ""

    sections: list[Section] = []
    for p in parts:
        sections.extend(p.sections)

    # Pool and merge valid branches without a storage cap. The renderer
    # paginates them; truncating here would permanently lose source material.
    from .prompts import MindMap

    center = ""
    branches = []
    for p in parts:
        if p.mindmap:
            center = center or (p.mindmap.center or "").strip()
            branches.extend(p.mindmap.branches)
    mindmap = MindMap(center=center or first("title"),
                      branches=_valid_branches(branches)) if branches else None

    merged = ShortNotes(
        output_language=first("output_language"),
        lecture_no=first("lecture_no"),
        subject=first("subject"),
        title=first("title"),
        audience=first("audience"),
        faculty=first("faculty"),
        quote=first("quote"),
        sections=sections,
        mindmap=mindmap,
    )
    if any(_is_manifest_backed(part) for part in parts):
        _strip_legacy_source_pdf(merged)
        object.__setattr__(merged, _MANIFEST_BACKED_ATTR, True)
    return merged


def _check_budget(budget: VisualRunBudget | None) -> None:
    if budget is not None:
        budget.check_deadline()


def _valid_branches(branches, budget: VisualRunBudget | None = None):
    """Merge duplicate labels and retain every unique source child in order."""
    from .prompts import Branch

    groups = []
    group_by_label = {}
    for branch in branches or []:
        _check_budget(budget)
        label = re.sub(r"\s+", " ", (getattr(branch, "label", "") or "").strip())
        label_key = label.casefold()
        if not label:
            _check_budget(budget)
            continue
        if label_key not in group_by_label:
            group_by_label[label_key] = {
                "label": label,
                "children": [],
                "child_seen": set(),
            }
            groups.append(group_by_label[label_key])
        group = group_by_label[label_key]
        for child in getattr(branch, "children", []) or []:
            clean = re.sub(r"\s+", " ", (child or "").strip())
            key = clean.casefold()
            if clean and key not in group["child_seen"]:
                group["children"].append(clean)
                group["child_seen"].add(key)
        _check_budget(budget)
    return [
        Branch(label=group["label"], children=group["children"])
        for group in groups
        if group["children"]
    ]


def _has_renderable_mindmap(notes: ShortNotes) -> bool:
    mm = getattr(notes, "mindmap", None)
    return bool(mm and (mm.center or "").strip() and _valid_branches(mm.branches))


def _preserve_generated_fields(source: ShortNotes, candidate: ShortNotes) -> ShortNotes:
    """Keep source metadata/invariants when a polishing pass omits them."""
    if (getattr(source, "audience", "") or "").strip():
        candidate.audience = source.audience
    if (getattr(source, "quote", "") or "").strip():
        candidate.quote = source.quote
    if (getattr(source, "faculty", "") or "").strip():
        candidate.faculty = source.faculty
    if _has_renderable_mindmap(source):
        candidate.mindmap = source.mindmap.model_copy(deep=True)
    return candidate


_MINDMAP_HOOK_MAX_WORDS = 12
_MINDMAP_HOOK_MAX_CHARS = 96
_MINDMAP_LABEL_MAX_WORDS = 5
_MINDMAP_LABEL_MAX_CHARS = 48
_MINDMAP_BRANCH_LABEL_MAX_WORDS = 12
_MINDMAP_BRANCH_LABEL_MAX_CHARS = 96


def _mindmap_text_fits(text: str, *, max_words: int, max_chars: int) -> bool:
    return bool(text and len(text.split()) <= max_words and len(text) <= max_chars)


def _mindmap_center(
    notes: ShortNotes,
    existing,
    budget: VisualRunBudget | None = None,
) -> str:
    """Select the first bounded value already supported by generated source."""
    candidates = [
        getattr(existing, "center", ""),
        notes.title,
        notes.subject,
    ]
    for section in notes.sections:
        _check_budget(budget)
        candidates.append(section.heading)
        candidates.append(_mindmap_hook(section))
        _check_budget(budget)
    for branch in getattr(existing, "branches", []) or []:
        _check_budget(budget)
        candidates.append(getattr(branch, "label", ""))
        _check_budget(budget)
    for value in candidates:
        text = re.sub(r"\s+", " ", value or "").strip()
        if _mindmap_text_fits(
            text,
            max_words=_MINDMAP_LABEL_MAX_WORDS,
            max_chars=_MINDMAP_LABEL_MAX_CHARS,
        ):
            return text
    return ""


def _mindmap_hook(section: Section) -> str:
    """Copy one concise, complete source hook for the deterministic fallback."""
    for value in list(section.points or []) + list(section.tricks or []):
        text = re.sub(r"\s+", " ", value or "").strip()
        if _mindmap_text_fits(
            text,
            max_words=_MINDMAP_HOOK_MAX_WORDS,
            max_chars=_MINDMAP_HOOK_MAX_CHARS,
        ):
            return text

    # Keep a source heading whole as a bounded fallback. The renderer now
    # paginates long branch cards, so the older center-sized limit would lose
    # legitimate source topics.
    heading = re.sub(r"\s+", " ", (section.heading or "")).strip()
    if _mindmap_text_fits(
        heading,
        max_words=_MINDMAP_BRANCH_LABEL_MAX_WORDS,
        max_chars=_MINDMAP_BRANCH_LABEL_MAX_CHARS,
    ):
        return heading
    return ""


_MINDMAP_LABEL_STOPWORDS = frozenset({
    "a", "an", "and", "for", "in", "of", "on", "the", "to",
    "का", "के", "की", "और", "एवं", "में",
})
_MINDMAP_MAX_HOOKS_PER_SECTION = 4


def _mindmap_label_tokens(value: str) -> frozenset[str]:
    tokens = []
    for token in re.findall(r"[a-z0-9]+|[\u0900-\u097f]+", (value or "").casefold()):
        if token in _MINDMAP_LABEL_STOPWORDS:
            continue
        if (
            len(token) > 3
            and token.endswith("s")
            and not token.endswith(("is", "ss", "us"))
        ):
            token = token[:-1]
        tokens.append(token)
    return frozenset(tokens)


def _mindmap_label_affinity(left: str, right: str) -> float:
    left_tokens = _mindmap_label_tokens(left)
    right_tokens = _mindmap_label_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(
        len(left_tokens), len(right_tokens)
    )


def _content_identity(value: str) -> str:
    from .note_quality import text_identity
    return text_identity(value)


def _append_unique_text(values: list[str], candidate: str) -> None:
    clean = re.sub(r"\s+", " ", candidate or "").strip()
    key = _content_identity(clean)
    if not clean or not key:
        return
    for existing in values:
        existing_key = _content_identity(existing)
        if key == existing_key:
            return
    values.append(clean)


def _section_theory_texts(section: Section) -> list[str]:
    values: list[str] = []
    for example in getattr(section, "worked_examples", []) or []:
        for value in [example.statement, *example.steps, example.result]:
            _append_unique_text(values, value)
    for collection in (
        section.points,
        section.tricks,
        section.flow,
        getattr(section, "notes", []),
        getattr(section, "key_terms", []),
    ):
        for value in collection or []:
            _append_unique_text(values, value)
    compare = getattr(section, "compare", None)
    if compare:
        for value in (
            compare.left_title,
            *(compare.left_points or []),
            compare.right_title,
            *(compare.right_points or []),
        ):
            _append_unique_text(values, value)
    if section.table:
        for value in section.table.headers or []:
            _append_unique_text(values, value)
        for row in section.table.rows or []:
            for value in row:
                _append_unique_text(values, value)
    if section.graph:
        for value in (
            section.graph.title,
            section.graph.x_label,
            section.graph.y_label,
        ):
            _append_unique_text(values, value)
        for curve in section.graph.curves or []:
            _append_unique_text(values, curve.label)
            _append_unique_text(values, curve.expression)
    _append_unique_text(values, getattr(section, "diagram", ""))
    if section.schematic:
        _append_unique_text(values, section.schematic.caption)
        for label in section.schematic.labels:
            _append_unique_text(values, label.text)
    visual = getattr(section, "visual", None)
    if visual:
        _append_unique_text(values, getattr(visual, "composition_note", ""))
        for value in getattr(visual, "exact_labels", []) or []:
            _append_unique_text(values, value)
    return values


def _section_has_theory(section: Section) -> bool:
    return bool(
        _section_theory_texts(section)
        or getattr(section, "schematic", None)
        or getattr(section, "visual_ids", None)
        or getattr(section, "_image", None)
        or getattr(section, "_svg", None)
    )


def _section_has_formula(section: Section) -> bool:
    return any(
        re.search(r"\$[^$]+\$", value or "")
        for value in _section_theory_texts(section)
    )


def _is_heading_echo(value: str, *labels: str) -> bool:
    value_tokens = _mindmap_label_tokens(value)
    if not value_tokens:
        return True
    for label in labels:
        label_tokens = _mindmap_label_tokens(label)
        if value_tokens == label_tokens and label_tokens:
            return True
    return False


def _section_revision_candidates(section: Section) -> list[str]:
    candidates: list[str] = []
    for collection in (
        section.points,
        section.tricks,
        section.flow,
        getattr(section, "key_terms", []),
        getattr(section, "notes", []),
    ):
        for value in collection or []:
            _append_unique_text(candidates, value)
    compare = getattr(section, "compare", None)
    if compare:
        _append_unique_text(
            candidates,
            f"{compare.left_title} vs {compare.right_title}",
        )
        for value in list(compare.left_points or []) + list(compare.right_points or []):
            _append_unique_text(candidates, value)
    if section.table:
        _append_unique_text(candidates, " / ".join(section.table.headers or []))
        for row in section.table.rows or []:
            _append_unique_text(candidates, " | ".join(row))
    if section.graph:
        _append_unique_text(candidates, section.graph.title)
        for curve in section.graph.curves or []:
            _append_unique_text(
                candidates,
                " = ".join(
                    value for value in (curve.label, curve.expression) if value
                ),
            )
    visual = getattr(section, "visual", None)
    if visual:
        _append_unique_text(candidates, getattr(visual, "composition_note", ""))
    return candidates


def _ensure_mindmap(
    notes: ShortNotes,
    budget: VisualRunBudget | None = None,
    *,
    strict_theory: bool = False,
    include_revision_map: bool = True,
) -> None:
    """Reconcile main theory and its source-derived revision map."""
    from .prompts import Branch, MindMap
    from .note_quality import prefer_worked_revision

    existing = getattr(notes, "mindmap", None)
    existing_branches = _valid_branches(
        existing.branches if existing else [], budget
    )
    center = _mindmap_center(notes, existing, budget)

    if not notes.sections:
        if strict_theory:
            raise UserError(
                "Model se complete theory notes nahi mile. Please dobara try karein."
            )
        if not center or not include_revision_map:
            notes.mindmap = None
            return
        branches = existing_branches or [
            Branch(label=center, children=[center])
        ]
        notes.mindmap = MindMap(center=center, branches=branches)
        return

    analysis_sections = []
    for section in notes.sections:
        _check_budget(budget)
        if not (section.heading or "").strip():
            if strict_theory:
                raise UserError(
                    "Model se complete theory notes nahi mile. Please dobara try karein."
                )
        else:
            analysis_sections.append(section)
        _check_budget(budget)

    if not analysis_sections:
        notes.mindmap = None
        return

    assigned: list[list[str]] = [[] for _section in analysis_sections]
    for branch in existing_branches:
        _check_budget(budget)
        children = []
        for child in branch.children or []:
            if not _is_heading_echo(child, branch.label):
                _append_unique_text(children, child)
        if not children:
            _check_budget(budget)
            continue
        scores = [
            _mindmap_label_affinity(branch.label, section.heading)
            for section in analysis_sections
        ]
        best_index = max(range(len(scores)), key=scores.__getitem__)
        if scores[best_index] < 0.5:
            # An optional revision-map label may be generic (for example,
            # "Core Formula") even when the main theory is already complete.
            # Ignore that branch and rebuild the final map from the authoritative
            # section theory below instead of rejecting the whole generation.
            continue
        for child in children:
            _append_unique_text(assigned[best_index], child)
        _check_budget(budget)

    surviving: list[tuple[object, list[str]]] = []
    dropped_sections = 0
    cleared_where = 0
    for section, recovered in zip(analysis_sections, assigned):
        _check_budget(budget)
        existing_theory = _section_theory_texts(section)
        known_hooks = {_content_identity(value) for value in _section_revision_candidates(section)}
        for child in recovered:
            if _content_identity(child) in known_hooks:
                continue
            # A standalone map formula may already occur inside an explanation
            # or solution step. Recover only missing formulas, preserving case,
            # signs and matrix entries when comparing their math spans.
            child_math = re.findall(r"\$([^$]+)\$", child)
            prose = re.sub(r"\$[^$]+\$", "", child).strip(" .,:;")
            if child_math and not prose:
                known_math = {re.sub(r"\s+", "", value)
                              for text in existing_theory
                              for value in re.findall(r"\$([^$]+)\$", text)}
                if all(re.sub(r"\s+", "", value) in known_math for value in child_math):
                    continue
            before = len(existing_theory)
            _append_unique_text(existing_theory, child)
            if len(existing_theory) > before:
                section.points.append(child)
        if not _section_has_theory(section):
            if strict_theory:
                raise UserError(
                    "Model se complete theory notes nahi mile. Please dobara try karein."
                )
            # Non-strict runs drop the empty shell (heading without theory)
            # instead of failing an otherwise complete generation.
            dropped_sections += 1
            _check_budget(budget)
            continue
        if getattr(section, "where", None) and not _section_has_formula(section):
            # A "where it is used" note without its formula is an auxiliary
            # inconsistency, not missing theory: clear it in every mode
            # rather than failing the whole generation.
            try:
                section.where = None
                cleared_where += 1
            except Exception:
                pass
        surviving.append((section, recovered))
        _check_budget(budget)

    if not surviving:
        raise UserError(
            "Model se complete theory notes nahi mile. Please dobara try karein."
        )
    if dropped_sections or cleared_where:
        dropped_ids = {
            id(section)
            for section, _recovered in zip(analysis_sections, assigned)
        } - {id(section) for section, _recovered in surviving}
        notes.sections = [
            section for section in notes.sections if id(section) not in dropped_ids
        ]
        print(
            "[notesbot.generation] mindmap_normalize"
            f" dropped_sections={dropped_sections}"
            f" cleared_where={cleared_where}"
            f" kept_sections={len(notes.sections)}"
        )

    # Recover main theory before suppressing the optional map, or a proof-heavy
    # answer can lose its only copy of a source formula.
    if not include_revision_map or prefer_worked_revision(notes):
        notes.mindmap = None
        return

    branches = []
    for section, recovered in surviving:
        _check_budget(budget)
        if getattr(section, "worked_examples", None):
            continue
        hooks: list[str] = []
        for candidate in recovered + _section_revision_candidates(section):
            if _is_heading_echo(candidate, section.heading):
                continue
            if not _mindmap_text_fits(
                candidate,
                max_words=_MINDMAP_HOOK_MAX_WORDS,
                max_chars=_MINDMAP_HOOK_MAX_CHARS,
            ):
                continue
            _append_unique_text(hooks, candidate)
            if len(hooks) >= _MINDMAP_MAX_HOOKS_PER_SECTION:
                break
        if hooks:
            branches.append(Branch(label=section.heading.strip(), children=hooks))
        _check_budget(budget)

    if not center or not branches:
        notes.mindmap = None
        return
    notes.mindmap = MindMap(center=center, branches=branches)


_QC_PROMPT = """You are a READ-ONLY auditor of revision-notes JSON. The lecture
PDF is not available in this pass, so the supplied JSON is the only source you
may inspect. You may not change any content-bearing field or value. Do not add,
delete, rewrite, simplify, correct, complete, deduplicate, move, or reorder any
string, list, object, or empty value. The checks below are read-only diagnostics;
their change verbs are NOT authorization to modify the JSON:
1. Observe formula and LaTeX formatting without wrapping, unwrapping, replacing,
   completing, or correcting any expression.
2. Observe reactions without adding arrows, conditions, species, or coefficients.
3. Never use scientific knowledge to decide what the source ought to have said.
4. Preserve duplicates, wording, emphasis markers, JSON escaping, field
   placement, and order exactly; an apparent defect may be source-authentic.
5. Preserve every teacher point, formula, trick, table, diagram, graph, visual,
   comparison, note, label, mind-map item, and quote without addition or loss.
Return the same notes in the same schema with identical content. If an issue
cannot be fixed without changing content, leave it unchanged.

NOTES JSON:
"""


_MATH_ONLY = re.compile(r"\s*\$[^$]+\$[\s.:;]*\Z")
_TRIVIAL_SYM = re.compile(
    r"^\s*\$?\s*(e\^?-?|e|H|p|n|x|y)\s*\$?\s*$", re.I)


def _legacy_destructive_tidy(notes: ShortNotes) -> None:
    """Clean up what the model tends to over-produce, in place:

    * `where` legends: only next to a real formula, no duplicate symbol
      anywhere in the document, no trivial "e- = electron" entries, >= 2 items.
    * bullets: plain text first, formula-only bullets after them.
    * sections with no real content at all are dropped.
    """
    seen_syms: set[str] = set()
    kept = []
    for s in notes.sections:
        has_formula = any(_MATH_ONLY.match(p or "") or "$" in (p or "")
                          for p in s.points)
        clean_where = []
        for wi in getattr(s, "where", []) or []:
            sym = (wi.symbol or "").strip()
            key = re.sub(r"[^A-Za-z0-9+\-]", "", sym).lower()
            if (not has_formula or not key or key in seen_syms
                    or _TRIVIAL_SYM.match(sym) or not (wi.meaning or "").strip()):
                continue
            seen_syms.add(key)
            clean_where.append(wi)
        s.where = clean_where if len(clean_where) >= 2 else []

        # Explaining bullets first, standalone formulas after them.
        text_pts = [p for p in s.points if not _MATH_ONLY.match(p or "")]
        math_pts = [p for p in s.points if _MATH_ONLY.match(p or "")]
        s.points = text_pts + math_pts

        if (s.points or s.tricks or s.flow or getattr(s, "notes", None)
                or getattr(s, "worked_examples", None)
                or (s.table and s.table.rows)
                or (s.graph and s.graph.curves)
                or getattr(s, "compare", None)
                or getattr(s, "diagram", "")):
            kept.append(s)
    notes.sections = kept


def _tidy(
    notes: ShortNotes,
    budget: VisualRunBudget | None = None,
) -> None:
    """Source-fidelity cleanup hook; intentionally preserves every field/order."""
    for _section in notes.sections:
        _check_budget(budget)
        _check_budget(budget)
    return None


_DESIGN_PROMPT = """You are a source-locked page decorator. The lecture PDF is
not available. You may change ONLY `icon` on an existing, position-matched
section, selecting one decorative icon from this fixed list: flask, test_tube,
atom, bulb, battery, bolt, drop, fire, magnet, book, gear, balance, star,
heart_ecg, scientist, doctor, microscope, dna, rocket, globe.

Never create key terms, comparisons, visuals, labels, or facts. Do not add,
delete, rewrite, split, merge, reorder, or move any section or any other field.
Preserve all content-bearing values byte-for-byte, including empty values.
Decorative icons must be unlabeled and must not assert instructional content.

Return the same notes in the same schema; only a valid `icon` may differ.

NOTES JSON:
"""


def _design_pass(notes: ShortNotes) -> ShortNotes:
    """Optional decorator proposes icons; the caller accepts no content edits."""
    if _use_litellm():
        planned = _lite_generate(
            contents=[{"type": "text",
                       "text": _DESIGN_PROMPT + notes.model_dump_json()}],
            max_tokens=65536, temperature=0.2,
        )
    else:
        response = _client().models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[_DESIGN_PROMPT + notes.model_dump_json()],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ShortNotes, temperature=0.2,
                max_output_tokens=65536,
            ),
        )
        cost_meter.record_genai_usage(response)
        planned = response.parsed
    if not isinstance(planned, ShortNotes) or not planned.sections:
        raise ValueError("design pass returned nothing usable")
    if _content_size(planned) < 0.6 * _content_size(notes):
        raise ValueError("design pass dropped content")
    return _preserve_generated_fields(notes, planned)


def _content_size(notes: ShortNotes) -> int:
    """Rough unit count used to detect QC passes that silently drop content."""
    total = 0
    for s in notes.sections:
        total += len(s.points) + len(s.tricks) + len(s.flow)
        total += sum(1 + len(example.steps) + bool(example.result)
                     for example in getattr(s, "worked_examples", []) or [])
        if s.table:
            total += len(s.table.rows)
        if s.graph and s.graph.curves:
            total += 1
    return total


_DECORATIVE_ICONS = frozenset({
    "flask", "test_tube", "atom", "bulb", "battery", "bolt", "drop",
    "fire", "magnet", "book", "gear", "balance", "star", "heart_ecg",
    "scientist", "doctor", "microscope", "dna", "rocket", "globe",
})


def _source_locked_polish(
    source: ShortNotes,
    candidate: ShortNotes,
    *,
    allow_icons: bool = False,
) -> ShortNotes:
    """Keep source content byte-for-byte; optionally copy decorative icons."""
    safe = source.model_copy(deep=True)
    manifest_backed = _is_manifest_backed(source)
    if manifest_backed:
        _strip_legacy_source_pdf(safe)
        object.__setattr__(safe, _MANIFEST_BACKED_ATTR, True)
    else:
        for source_section, safe_section in zip(source.sections, safe.sections):
            source_pdf = getattr(source_section, "_source_pdf", None)
            if (
                isinstance(source_pdf, bytes)
                and source_pdf
                and len(source_pdf) <= config.GEMINI_INLINE_MAX_BYTES
            ):
                object.__setattr__(safe_section, "_source_pdf", source_pdf)
    if not allow_icons or not isinstance(candidate, ShortNotes):
        return safe
    if len(candidate.sections) != len(source.sections):
        return safe
    if any(
        candidate_section.heading != source_section.heading
        for source_section, candidate_section in zip(source.sections, candidate.sections)
    ):
        return safe
    for safe_section, candidate_section in zip(safe.sections, candidate.sections):
        icon = (candidate_section.icon or "").strip()
        if icon in _DECORATIVE_ICONS:
            safe_section.icon = icon
    return safe


def _source_verified_quote(
    pdf_bytes: bytes,
    quote: str,
    budget: VisualRunBudget | None = None,
) -> str:
    """Return a quote only when its full normalized text occurs in the PDF."""
    _check_budget(budget)
    quote = (quote or "").strip()
    if not quote:
        _check_budget(budget)
        return ""
    try:
        _check_budget(budget)
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
        _check_budget(budget)
        extracted = []
        for page in reader.pages:
            _check_budget(budget)
            extracted.append(page.extract_text() or "")
            _check_budget(budget)
        source = " ".join(extracted)
    except VisualFidelityError:
        raise
    except Exception:  # noqa: BLE001 - unreadable text means no provenance
        return ""
    _check_budget(budget)
    normalized_quote = re.sub(r"\s+", " ", quote).casefold()
    normalized_source = re.sub(r"\s+", " ", source).casefold()
    whole_quote = rf"(?<!\w){re.escape(normalized_quote)}(?!\w)"
    return quote if re.search(whole_quote, normalized_source) else ""


def _qc_pass(notes: ShortNotes) -> ShortNotes:
    """Optional read-only audit; the caller source-locks its entire response."""
    if _use_litellm():
        fixed = _lite_generate(
            contents=[{"type": "text",
                       "text": _QC_PROMPT + notes.model_dump_json()}],
            max_tokens=65536,
            temperature=0.1,
        )
        if not fixed.sections:
            raise ValueError("QC pass returned no usable notes")
        if _content_size(fixed) < 0.6 * _content_size(notes):
            raise ValueError("QC pass dropped too much content; keeping original")
        return _preserve_generated_fields(notes, fixed)

    response = _client().models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[_QC_PROMPT + notes.model_dump_json()],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ShortNotes,
            temperature=0.1,
            # Full model ceiling: QC reproduces the ENTIRE merged notes JSON,
            # which for a many-chunk PDF is far larger than one chunk's output.
            max_output_tokens=65536,
        ),
    )
    cost_meter.record_genai_usage(response)
    fixed = response.parsed
    if not isinstance(fixed, ShortNotes) or not fixed.sections:
        raise ValueError("QC pass returned no usable notes")
    # Guard against "successful" QC that quietly lost content (truncation or
    # over-aggressive simplification): de-dup may remove a little, never most.
    if _content_size(fixed) < 0.6 * _content_size(notes):
        raise ValueError("QC pass dropped too much content; keeping original")
    return _preserve_generated_fields(notes, fixed)


def _manifest_for_chunk(
    manifest: VisualManifest,
    chunk_index: int,
) -> VisualManifest:
    return VisualManifest(
        visuals=tuple(
            visual
            for visual in manifest.visuals
            if visual.chunk_index == chunk_index
        )
    )


def _close_visual_memory_after_failure(
    memory: VisualMemoryBudget,
    original_error: BaseException,
) -> None:
    """Drain job-owned bytes, failing closed on cleanup-integrity errors."""
    try:
        memory.close()
    except BaseException as cleanup_error:
        if isinstance(cleanup_error, VisualFidelityError):
            raise cleanup_error from original_error
        raise VisualFidelityError("memory_limit_exceeded") from original_error


def _generate_manifest_note_bundle(
    pdf_bytes: bytes,
    edit_instruction: str = "",
    language: str | None = None,
    subject_hint: str = "",
) -> ManifestNoteBundle:
    """Return one live manifest job, or terminally drain its memory on failure."""
    budget = VisualRunBudget(
        deadline_at=time.monotonic() + config.VISUAL_PIPELINE_DEADLINE_SECONDS,
        clock=time.monotonic,
    )
    memory = VisualMemoryBudget(
        max_total_bytes=config.MAX_VISUAL_RESIDENT_BYTES,
        max_source_bytes=config.MAX_VISUAL_SOURCE_TOTAL_BYTES,
        max_context_bytes=config.MAX_VISUAL_CONTEXT_TOTAL_BYTES,
        max_asset_bytes=config.MAX_VISUAL_ASSET_TOTAL_BYTES,
        max_page_cache_bytes=config.MAX_VISUAL_PAGE_CACHE_BYTES,
    )
    try:
        return _generate_manifest_note_bundle_owned(
            pdf_bytes,
            edit_instruction,
            language,
            subject_hint,
            budget=budget,
            memory=memory,
        )
    except BaseException as original_error:
        _close_visual_memory_after_failure(memory, original_error)
        raise


_VISUAL_FAILURE_STAGES = frozenset(
    {
        "split_pdf",
        "mathpix",
        "inventory",
        "finalize_manifest",
        "extract_crops",
        "note_generation",
        "merge_notes",
        "verify_notes",
    }
)


def _run_visual_stage(stage: str, operation: Callable[[], object]):
    """Run one named stage and emit no data beyond an allowlisted failure token."""
    if stage not in _VISUAL_FAILURE_STAGES or not callable(operation):
        raise ValueError("Invalid visual stage")
    try:
        return operation()
    except (VisualFidelityError, asyncio.CancelledError):
        raise
    except Exception:
        print(
            f"[notesbot.visual-error-detail] stage={stage} "
            "category=internal_error"
        )
        raise


def _generate_manifest_note_bundle_owned(
    pdf_bytes: bytes,
    edit_instruction: str,
    language: str | None,
    subject_hint: str,
    *,
    budget: VisualRunBudget,
    memory: VisualMemoryBudget,
) -> ManifestNoteBundle:
    """Build a manifest bundle using the caller-owned job budgets."""
    chunks = _run_visual_stage(
        "split_pdf", lambda: tuple(split_pdf_chunks(pdf_bytes, budget=budget))
    )
    if not chunks:
        raise VisualFidelityError("inventory_incomplete")

    audited_chunks = []
    for chunk in chunks:
        budget.check_deadline()
        evidence = _run_visual_stage(
            "mathpix", lambda chunk=chunk: collect_mathpix_evidence(chunk, budget)
        )
        budget.check_deadline()
        audited = _run_visual_stage(
            "inventory",
            lambda chunk=chunk, evidence=evidence: inventory_chunk(
                chunk,
                _inventory_schema_call,
                budget,
                evidence,
                memory_budget=memory,
            ),
        )
        if audited is None:
            raise VisualFidelityError("inventory_incomplete")
        budget.check_deadline()
        audited_chunks.append(audited)

    manifest = _run_visual_stage(
        "finalize_manifest",
        lambda: finalize_manifest(
            audited_chunks,
            chunks,
            budget,
            total_pages=sum(chunk.page_count for chunk in chunks),
            max_visuals=config.MAX_SOURCE_VISUALS,
        ),
    )
    budget.check_deadline()
    crops = _run_visual_stage(
        "extract_crops",
        lambda: tuple(
            extract_source_crops(
                pdf_bytes,
                manifest.visuals,
                chunks,
                budget,
                memory,
                render_scale=config.VISUAL_PAGE_RENDER_SCALE,
                max_page_dimension_px=config.MAX_VISUAL_PAGE_DIMENSION_PX,
                max_crop_pixels=config.MAX_VISUAL_CROP_PIXELS,
                max_png_bytes=config.MAX_VISUAL_PNG_BYTES,
            )
        ),
    )
    if (
        len(crops) != len(manifest.visuals)
        or any(not isinstance(crop, SourceCrop) for crop in crops)
        or tuple(crop.visual.visual_id for crop in crops) != manifest_ids(manifest)
    ):
        raise VisualFidelityError("crop_failed")
    budget.check_deadline()

    request_language = (language or config.NOTES_LANGUAGE).strip().lower()
    request_subject_hint = subject_hint.strip()
    total = len(chunks)

    def generate_chunk(chunk) -> ShortNotes:
        local_manifest = _manifest_for_chunk(manifest, chunk.index)
        part_note = ""
        if total > 1:
            part_note = (
                f"This PDF is PART {chunk.index + 1} of {total} of one big "
                "lecture (a page range). Make notes ONLY for these pages. Read "
                "batch/lecture metadata only if it appears on these pages, else "
                "leave those fields empty."
            )
        generated = _generate_one(
            chunk.pdf_bytes,
            edit_instruction,
            part_note,
            language=request_language,
            subject_hint=request_subject_hint,
            visual_manifest=local_manifest,
            budget=budget,
            chunk_index=chunk.index,
        )
        budget.check_deadline()
        return _bind_visual_ids(generated, local_manifest)

    def generate_parts() -> list[ShortNotes]:
        workers = min(3, total)
        if workers == 1:
            return [generate_chunk(chunks[0])]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Worker threads do not inherit contextvars; copy per task so the
            # cost meter keeps metering model calls made inside the pool.
            return list(
                pool.map(
                    lambda chunk: contextvars.copy_context().run(
                        generate_chunk, chunk
                    ),
                    chunks,
                )
            )

    parts = _run_visual_stage("note_generation", generate_parts)
    budget.check_deadline()
    notes = _run_visual_stage("merge_notes", lambda: _merge(parts))
    budget.check_deadline()

    def verify_notes() -> ShortNotes:
        notes.quote = _source_verified_quote(pdf_bytes, notes.quote, budget=budget)
        _tidy(notes, budget=budget)
        _ensure_mindmap(notes, budget=budget)
        return notes

    notes = _run_visual_stage("verify_notes", verify_notes)
    budget.check_deadline()
    if any(
        getattr(section, "_source_pdf", None) is not None
        for section in notes.sections
    ):
        raise VisualFidelityError("link_invalid")
    for section in notes.sections:
        if "_source_pdf" in section.__dict__:
            object.__delattr__(section, "_source_pdf")
    if _MANIFEST_BACKED_ATTR in notes.__dict__:
        object.__delattr__(notes, _MANIFEST_BACKED_ATTR)
    return ManifestNoteBundle(
        notes=notes,
        chunk_count=total,
        manifest=manifest,
        crops=crops,
        budget=budget,
        memory=memory,
    )


_VISUAL_RUNTIME_ATTR_NAMES = (
    "_visual_manifest",
    "_source_crops",
    "_visual_assets",
    "_visual_run_budget",
    "_visual_memory_budget",
)

_VISUAL_AGGREGATE_COUNT_KEYS = (
    "mathpix_submit",
    "mathpix_poll",
    "mathpix_lines",
    "mathpix_mmd",
    "mathpix_delete",
    "mathpix_evidence_used_chunks",
    "mathpix_source_fallback_chunks",
    "inventory",
    "audit",
    "note",
    "redraw",
    "verifier",
    "coverage_probes",
)


def _ordered_source_crop_map(bundle: ManifestNoteBundle) -> dict[str, SourceCrop]:
    """Snapshot exact manifest-order crop ownership for the renderer."""
    ids = manifest_ids(bundle.manifest)
    if not isinstance(bundle.crops, tuple) or len(bundle.crops) != len(ids):
        raise VisualFidelityError("crop_failed")
    crops: dict[str, SourceCrop] = {}
    for visual_id, crop in zip(ids, bundle.crops, strict=True):
        if not isinstance(crop, SourceCrop) or crop.visual.visual_id != visual_id:
            raise VisualFidelityError("crop_failed")
        crops[visual_id] = crop
    if tuple(crops) != ids:
        raise VisualFidelityError("crop_failed")
    return crops


def _validated_visual_asset_map(
    manifest: VisualManifest,
    rendered: object,
) -> dict[str, VisualAsset]:
    """Validate and snapshot the complete terminal Task 9 asset mapping."""
    ids = manifest_ids(manifest)
    if not isinstance(rendered, dict) or tuple(rendered.keys()) != ids:
        raise VisualFidelityError("link_invalid")
    assets: dict[str, VisualAsset] = {}
    for visual_id, asset in rendered.items():
        if (
            not isinstance(asset, VisualAsset)
            or asset.visual_id != visual_id
            or asset.state not in (
                "verified_redraw",
                "sketch_source_fallback",
            )
            or type(asset.primary_png) is not bytes
            or type(asset.fallback_png) is not bytes
            or isinstance(asset.width_px, bool)
            or not isinstance(asset.width_px, int)
            or asset.width_px <= 0
            or isinstance(asset.height_px, bool)
            or not isinstance(asset.height_px, int)
            or asset.height_px <= 0
        ):
            raise VisualFidelityError("link_invalid")
        assets[visual_id] = asset
    return assets


def _set_visual_runtime_attr(obj: ShortNotes, name: str, value: object) -> None:
    """Narrow activation seam; runtime fields stay outside Pydantic output."""
    object.__setattr__(obj, name, value)


def _emit_visual_activation_event(bundle: ManifestNoteBundle) -> None:
    counts = dict(bundle.budget.redacted_counts())
    if (
        tuple(counts.keys()) != _VISUAL_AGGREGATE_COUNT_KEYS
        or any(type(value) is not int for value in counts.values())
    ):
        raise VisualFidelityError("link_invalid")
    payload = {
        "chunks": int(bundle.chunk_count),
        "manifest": len(manifest_ids(bundle.manifest)),
        **counts,
    }
    print(
        "[notesbot.visual] "
        + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )


def _emit_generation_ledger(
    bundle: SequenceNoteBundle,
    *,
    success: bool,
) -> None:
    payload = {
        "success": int(success),
        **bundle.run.snapshot(len(bundle.chunks)),
    }
    print(
        "[notesbot.generation] "
        + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )


def generate_short_notes(
    pdf_bytes: bytes,
    edit_instruction: str = "",
    language: str | None = None,
    subject_hint: str = "",
) -> ShortNotes:
    """Generate teacher-ordered notes through the direct Model sequence path."""
    from .sanskrit import generation_language
    _resolved, sanskrit_hindi = generation_language(language or config.NOTES_LANGUAGE, subject_hint, pdf_bytes)
    if sanskrit_hindi:
        language = "hindi"
    with generation_profile.sanskrit_hindi_notes(sanskrit_hindi or generation_profile.sanskrit_hindi()):
        return _generate_short_notes(pdf_bytes, edit_instruction, language, subject_hint)


def _generate_short_notes(
    pdf_bytes: bytes,
    edit_instruction: str,
    language: str | None,
    subject_hint: str,
) -> ShortNotes:
    bundle = _generate_sequence_note_bundle(
        pdf_bytes,
        edit_instruction,
        language,
        subject_hint,
    )
    from .concise_policy import question_content
    # The Sheets automation reviews every page group itself and switches this
    # inner, unlogged review off; the web app still relies on it.
    if generation_profile.inline_source_check_enabled() and question_content(bundle.notes):
        from .source_coverage import ensure_source_coverage
        bundle = replace(bundle, notes=ensure_source_coverage(
            pdf_bytes, bundle.notes, language=bundle.notes.output_language or language or config.NOTES_LANGUAGE, subject_hint=subject_hint))
    # Main-theory recovery has already run; omit the duplicative revision map.
    bundle.notes.mindmap = None
    from . import categories
    if categories.switch("one_word_table"):
        from .sanskrit import keep_one_word_table
        keep_one_word_table(bundle.notes)
    from .sanskrit import require_sanskrit, resolve_language
    bundle.notes.output_language = resolve_language(language or config.NOTES_LANGUAGE, subject_hint, pdf_bytes)
    require_sanskrit(bundle.notes)
    if bundle.notes.output_language != "sanskrit":
        image_gen.render_sequence_visuals(bundle.notes, bundle.run)
    _emit_generation_ledger(bundle, success=True)
    return bundle.notes
