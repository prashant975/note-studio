"""Central configuration, read from environment variables.

On Vercel set these in Project Settings -> Environment Variables (or `vercel env add`).
For local dev they are read from the `.env` file (see `.env.example`).
"""
from __future__ import annotations

import math
import os


def _load_dotenv() -> None:
    """Tiny .env loader so local runs work without extra deps.

    On Vercel the variables already live in the real environment, so the
    `.env` file simply will not exist there and this is a no-op.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(here, ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # Do not overwrite variables already set in the real environment.
                os.environ.setdefault(key, value)
    except OSError:
        pass


_load_dotenv()


def _get(name: str, default: str = "") -> str:
    # BOM-strip guards against values pasted/piped via Windows tools, which
    # once shipped "﻿16" and crashed the function at import time.
    return (os.environ.get(name) or default).strip().lstrip("﻿").strip()


# --- Telegram ---------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = _get("TELEGRAM_BOT_TOKEN")
# Optional SECOND bot (e.g. @Pw_Shortnotes_bot): same features, but it ASKS the
# teacher whose name should appear on the notes instead of a fixed signature.
TELEGRAM_BOT_TOKEN_2: str = _get("TELEGRAM_BOT_TOKEN_2")
# Secret that Telegram echoes back in the `X-Telegram-Bot-Api-Secret-Token`
# header on every webhook call. We set it when registering the webhook and
# verify it on each request so randoms cannot POST fake updates to us.
TELEGRAM_WEBHOOK_SECRET: str = _get("TELEGRAM_WEBHOOK_SECRET")


def get_bot(key: str) -> dict:
    """Bot registry. 'main' = fixed signature; 'ask' = per-chat teacher name."""
    if key == "ask":
        return {
            "token": TELEGRAM_BOT_TOKEN_2,
            "signature": "",  # resolved per chat (pinned config message)
            "ask_name": True,
            "watermark": False,   # public-facing bot: clean pages
        }
    return {
        "token": TELEGRAM_BOT_TOKEN,
        "signature": _get("TEACHER_SIGNATURE", "by PW Faculty"),
        "ask_name": False,
        "watermark": True,
    }

# --- Gemini -----------------------------------------------------------------
# Simple API-key mode (Google AI Studio key, looks like "AIza...").
GEMINI_API_KEY: str = _get("GEMINI_API_KEY")
GEMINI_MODEL: str = _get("GEMINI_MODEL", "gemini-2.5-pro")

# --- LiteLLM gateway (PenPencil platform) — PREFERRED when key is set -------
# OpenAI-compatible proxy in front of Vertex AI. Models use a "vertex_ai/"
# prefix. Set LITELLM_API_KEY (sk-...) to activate this path.
LITELLM_BASE_URL: str = _get(
    "LITELLM_BASE_URL", "https://litellm-platform.penpencil.guru"
)
LITELLM_API_KEY: str = _get("LITELLM_API_KEY")
LITELLM_MODEL: str = _get("LITELLM_MODEL", "vertex_ai/gemini-3.6-flash")
LITELLM_REASONING_EFFORT: str = _get("LITELLM_REASONING_EFFORT", "high")
# User-facing cost display (INR). Defaults match what the PenPencil LiteLLM
# gateway actually bills this key per token ($1.50/$7.50 per 1M × ₹88/USD,
# solved exactly from its own dashboard rows) — NOT Gemini Flash list price
# ($0.30/$2.50), which the gateway overrides. Override via env if the
# gateway's registry pricing changes.
LLM_COST_INPUT_INR_PER_1M = max(0.0, float(_get("LLM_COST_INPUT_INR_PER_1M", "132") or "132"))
LLM_COST_OUTPUT_INR_PER_1M = max(0.0, float(_get("LLM_COST_OUTPUT_INR_PER_1M", "660") or "660"))
IMAGE_COST_INR_PER_IMAGE = max(0.0, float(_get("IMAGE_COST_INR_PER_IMAGE", "3.5") or "3.5"))

# Output-token ceiling for notes generation. The model occasionally falls into
# a runaway JSON/thinking loop; at 65,536 that burns 250+ s (past the 180 s
# client timeout, killing the job) and ~₹44 at gateway rates. 24,576 is 3× the
# largest legitimate chunk output ever observed (~8.5k) and ends a runaway
# inside the timeout so the bounded retry can save the job.
GENERATION_MAX_OUTPUT_TOKENS = min(
    65_536, max(8_192, int(_get("GENERATION_MAX_OUTPUT_TOKENS", "24576") or "24576"))
)


def key_kind() -> str:
    """Classify GEMINI_API_KEY without ever exposing its value."""
    k = GEMINI_API_KEY
    if not k:
        return "none"
    if k.startswith("sk-"):
        return "litellm"          # a gateway key pasted into GEMINI_API_KEY
    if k.startswith("AIza"):
        return "google_api_key"   # AI Studio developer key
    if k.startswith("AQ."):
        return "vertex_express"   # Vertex express-mode key
    return "unknown"


# A gateway key may arrive in either variable.
def litellm_key() -> str:
    return LITELLM_API_KEY or (GEMINI_API_KEY if key_kind() == "litellm" else "")

# Optional PROXY in front of Gemini/Vertex (e.g. the PW apps proxy). When set,
# ALL Gemini traffic is routed through this base URL instead of Google's.
#   GEMINI_BASE_URL    e.g. https://pw-apps-proxy.vercel.app/<service-path>
#   GEMINI_PROXY_AUTH_HEADER / GEMINI_PROXY_AUTH_VALUE  optional auth header
GEMINI_BASE_URL: str = _get("GEMINI_BASE_URL")
GEMINI_PROXY_AUTH_HEADER: str = _get("GEMINI_PROXY_AUTH_HEADER")
GEMINI_PROXY_AUTH_VALUE: str = _get("GEMINI_PROXY_AUTH_VALUE")

# Vertex AI mode (optional). Set GOOGLE_GENAI_USE_VERTEXAI=true plus the
# project/location and provide service-account credentials, if you prefer
# Vertex over an API key.
USE_VERTEXAI: bool = _get("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() in (
    "1",
    "true",
    "yes",
)
GCP_PROJECT: str = _get("GOOGLE_CLOUD_PROJECT")
GCP_LOCATION: str = _get("GOOGLE_CLOUD_LOCATION", "global")

# Vertex AI via SERVICE ACCOUNT (preferred when set): the full service-account
# JSON, either in this env var or in a `service_account.json` file at the
# project root (gitignored; on Vercel use the env var).
GOOGLE_SERVICE_ACCOUNT_JSON: str = _get("GOOGLE_SERVICE_ACCOUNT_JSON")
_SA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "service_account.json",
)
if not GOOGLE_SERVICE_ACCOUNT_JSON and os.path.exists(_SA_FILE):
    try:
        with open(_SA_FILE, "r", encoding="utf-8") as _fh:
            GOOGLE_SERVICE_ACCOUNT_JSON = _fh.read().strip()
    except OSError:
        pass

# Generate the planned hand-drawn "core diagram" artwork (gateway image model).
NOTES_IMAGES: bool = _get("NOTES_IMAGES", "true").lower() in ("1", "true", "yes")
IMAGE_MODEL: str = _get("IMAGE_MODEL", "vertex_ai/gemini-3.1-flash-image")
# svg   = model writes SVG (real text labels, crisp)
# image = raster image model (coloured-pencil sketch)
# auto  = raster sketch first, SVG as reliable fallback
DIAGRAM_MODE: str = _get("DIAGRAM_MODE", "auto").lower()
# Source-page image attempts remain sequential and are hard-capped for cost.
MAX_IMAGES: int = min(2, max(0, int(_get("MAX_IMAGES", "2") or "2")))

# Optional legacy ART DIRECTOR pass.  It is off by default because a second
# generative pass has no access to the lecture PDF.  When explicitly enabled,
# the runtime accepts only a validated decorative icon from its response.
NOTES_DESIGN: bool = _get("NOTES_DESIGN", "false").lower() in ("1", "true", "yes")

# Optional legacy QC audit.  It is off by default and its generative response
# is source-locked at runtime, so it can never rewrite lecture content.
NOTES_QC: bool = _get("NOTES_QC", "false").lower() in ("1", "true", "yes")

# --- Notes branding / behaviour --------------------------------------------
# Shown in the header line and as the diagonal watermark on every page.
TEACHER_SIGNATURE: str = _get("TEACHER_SIGNATURE", "by PW Faculty")
# auto | english | hindi | hinglish | sanskrit
NOTES_LANGUAGE: str = _get("NOTES_LANGUAGE", "auto").lower()
# Retained for deployment compatibility only. Missing source metadata stays
# empty; the runtime never invents a subject label from this setting.
DEFAULT_SUBJECT: str = _get("DEFAULT_SUBJECT")

_MB = 1024 * 1024

# Telegram cloud Bot API hard-caps file *downloads* by bots at 20 MB. Bigger
# PDFs must arrive as a link (Drive/URL) which we fetch ourselves.
TELEGRAM_DOWNLOAD_MAX_BYTES: int = 20 * _MB

# Overall cap for PDFs fetched from a link (protects memory/time). Configurable.
MAX_PDF_MB: int = int(_get("MAX_PDF_MB", "100") or "100")
MAX_PDF_BYTES: int = MAX_PDF_MB * _MB

# Gemini (Vertex) accepts roughly 20 MB of inline PDF per request. Anything
# bigger we split into page-chunks no larger than this, then merge the notes.
GEMINI_INLINE_MAX_BYTES: int = 18 * _MB
CHUNK_TARGET_BYTES: int = 12 * _MB

# Source-visual preflight, attachment, and resident-memory hard limits.
# Environment values may make configurable bounds stricter, never looser.
MAX_SOURCE_VISUALS = min(64, max(1, int(_get("MAX_SOURCE_VISUALS", "64"))))
MAX_SOURCE_PAGES = min(300, max(1, int(_get("MAX_SOURCE_PAGES", "300"))))
MAX_VISUAL_CROP_PIXELS = min(8_000_000, max(160_000, int(_get("MAX_VISUAL_CROP_PIXELS", "8000000"))))
MIN_VISUAL_CROP_WIDTH_PX = max(96, min(512, int(_get("MIN_VISUAL_CROP_WIDTH_PX", "96"))))
MIN_VISUAL_CROP_HEIGHT_PX = max(64, min(512, int(_get("MIN_VISUAL_CROP_HEIGHT_PX", "64"))))
MIN_VISUAL_CROP_PIXELS = max(8_192, min(262_144, int(_get("MIN_VISUAL_CROP_PIXELS", "8192"))))
MAX_VISUAL_PNG_BYTES = min(10 * _MB, max(256_000, int(_get("MAX_VISUAL_PNG_BYTES", str(10 * _MB)))))
MAX_LABEL_LAYER_PNG_BYTES = min(2 * _MB, max(64_000, int(_get("MAX_LABEL_LAYER_PNG_BYTES", str(2 * _MB)))))
MAX_LABEL_LAYER_PIXELS = min(2_000_000, max(4_096, int(_get("MAX_LABEL_LAYER_PIXELS", "2000000"))))
MAX_VISUAL_PAGE_DIMENSION_PX = min(5_000, max(1_200, int(_get("MAX_VISUAL_PAGE_DIMENSION_PX", "5000"))))
MAX_VISUAL_PAGE_PIXELS = min(25_000_000, max(1_440_000, int(_get("MAX_VISUAL_PAGE_PIXELS", "25000000"))))
VISUAL_PAGE_RENDER_SCALE = min(2.5, max(1.5, float(_get("VISUAL_PAGE_RENDER_SCALE", "2.2"))))
MAX_CONTEXT_AREA_FRACTION = 0.45
MAX_CONTEXT_PIXELS = 2_000_000
MAX_CONTEXT_PNG_BYTES = 3 * _MB
MAX_CONTEXT_TOTAL_BYTES = 6 * _MB
MAX_CONTEXT_TEXT_CHARS = 1_200
MAX_COVERAGE_PROBES_PER_PAGE = 256
# Fair-share pool per chunk: dense lectures spread this across their pages
# instead of failing once cumulative probes cross a global cap.
MAX_COVERAGE_PROBES_PER_CHUNK = 8_192
# 16 == the hard ceiling MAX_CHUNKS enforces further below.
MAX_COVERAGE_PROBES_TOTAL = MAX_COVERAGE_PROBES_PER_CHUNK * 16
VISUAL_MODEL_ATTACHMENT_TOTAL_BYTES = GEMINI_INLINE_MAX_BYTES
VISUAL_MODEL_ENCODED_ATTACHMENT_TOTAL_BYTES = 24 * _MB
VISUAL_SOURCE_CHUNK_MAX_BYTES = min(9 * _MB, max(1 * _MB, int(_get("VISUAL_SOURCE_CHUNK_MAX_BYTES", str(9 * _MB)))))
MAX_AUDIT_PDF_BYTES = min(9 * _MB, max(1 * _MB, int(_get("MAX_AUDIT_PDF_BYTES", str(9 * _MB)))))
COVERAGE_PROBE_MIN_CELL_PX = 96
COVERAGE_PROBE_MAX_DEPTH = 3
COVERAGE_FOREGROUND_DISTANCE_RGB = 18.0
COVERAGE_COMPONENT_MIN_PX = 6
AUDIT_SUPPORT_CONTAINMENT_EPSILON_PCT = 1e-6
AUDIT_SUPPORT_OUTWARD_MARGIN_PCT = 2.0
VISUAL_PIPELINE_DEADLINE_SECONDS = min(780, max(60, int(_get("VISUAL_PIPELINE_DEADLINE_SECONDS", "780"))))
VISUAL_MODEL_TIMEOUT_SECONDS = min(180, max(20, int(_get("VISUAL_MODEL_TIMEOUT_SECONDS", "180"))))
MAX_REDRAW_PROVIDER_ATTEMPTS = 1
MAX_VISUAL_SOURCE_TOTAL_BYTES = 128 * _MB
MAX_VISUAL_CONTEXT_TOTAL_BYTES = 64 * _MB
MAX_VISUAL_ASSET_TOTAL_BYTES = 192 * _MB
MAX_VISUAL_PAGE_CACHE_BYTES = 128 * _MB
MAX_VISUAL_RESIDENT_BYTES = 384 * _MB
MATHPIX_ENABLED = _get("MATHPIX_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
MATHPIX_APP_ID = _get("MATHPIX_APP_ID", "")
MATHPIX_APP_KEY = _get("MATHPIX_APP_KEY", "")
# DNS strategy for the Mathpix session: "native" keeps the thread-free
# dnspython resolver (cancellable, deadline-exact); "getaddrinfo" opts in
# to aiohttp.ThreadedResolver for sandboxes that block raw UDP DNS (e.g.
# some serverless runtimes) where only the platform getaddrinfo path works.
MATHPIX_DNS_RESOLVER = (
    "getaddrinfo"
    if _get("MATHPIX_DNS_RESOLVER", "native").strip().lower() == "getaddrinfo"
    else "native"
)
MATHPIX_REQUEST_TIMEOUT_SECONDS = min(30, max(5, int(_get("MATHPIX_REQUEST_TIMEOUT_SECONDS", "20"))))
MATHPIX_SOCKET_TIMEOUT_SECONDS = 5
MATHPIX_MAX_POLL_ATTEMPTS = min(12, max(1, int(_get("MATHPIX_MAX_POLL_ATTEMPTS", "12"))))
MATHPIX_POLL_INTERVAL_SECONDS = min(5.0, max(0.25, float(_get("MATHPIX_POLL_INTERVAL_SECONDS", "4"))))
MATHPIX_MAX_RESPONSE_BYTES = min(8 * _MB, max(256_000, int(_get("MATHPIX_MAX_RESPONSE_BYTES", str(8 * _MB)))))
MATHPIX_MAX_MMD_CHARS = min(200_000, max(8_000, int(_get("MATHPIX_MAX_MMD_CHARS", "120000"))))
MATHPIX_MAX_EVIDENCE_LINES = min(2_048, max(64, int(_get("MATHPIX_MAX_EVIDENCE_LINES", "1024"))))
MATHPIX_MAX_EVIDENCE_TEXT_CHARS = min(60_000, max(4_000, int(_get("MATHPIX_MAX_EVIDENCE_TEXT_CHARS", "40000"))))
MATHPIX_MAX_POLYGON_POINTS_PER_LINE = min(64, max(4, int(_get("MATHPIX_MAX_POLYGON_POINTS_PER_LINE", "32"))))
MATHPIX_MAX_EVIDENCE_JSON_BYTES = min(1 * _MB, max(64_000, int(_get("MATHPIX_MAX_EVIDENCE_JSON_BYTES", str(1 * _MB)))))
MATHPIX_MAX_EVIDENCE_ENCODED_BYTES = 2 * _MB
MATHPIX_CLEANUP_TIMEOUT_SECONDS = 5
MATHPIX_CLEANUP_RESERVE_SECONDS = 10

if (
    VISUAL_SOURCE_CHUNK_MAX_BYTES + MAX_AUDIT_PDF_BYTES
    > VISUAL_MODEL_ATTACHMENT_TOTAL_BYTES
):
    raise RuntimeError("visual PDF attachments exceed the raw byte ceiling")
if (
    4 * math.ceil(VISUAL_SOURCE_CHUNK_MAX_BYTES / 3)
    + 4 * math.ceil(MAX_AUDIT_PDF_BYTES / 3)
    > VISUAL_MODEL_ENCODED_ATTACHMENT_TOTAL_BYTES
):
    raise RuntimeError("visual PDF attachments exceed the base64 byte ceiling")

# Cap on how many chunks we will process for one PDF (safety/time bound).
# 16 chunks x ~50s notes calls on 3 parallel workers stays inside the 720s
# function budget with headroom for upload, merge, and rendering.
MAX_CHUNKS: int = min(16, max(1, int(_get("MAX_CHUNKS", "16") or "16")))


def missing_required() -> list[str]:
    """Return the names of required env vars that are not set."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    # Without the webhook secret the endpoint would accept unauthenticated
    # POSTs (api/index.py fails closed on it), so treat it as required.
    if not TELEGRAM_WEBHOOK_SECRET:
        missing.append("TELEGRAM_WEBHOOK_SECRET")
    # Gemini auth is satisfied by ANY of: LiteLLM gateway key, service-account
    # JSON, an API key, or explicit Vertex ADC (project set).
    if not (LITELLM_API_KEY or GOOGLE_SERVICE_ACCOUNT_JSON or GEMINI_API_KEY
            or (USE_VERTEXAI and GCP_PROJECT)):
        missing.append("LITELLM_API_KEY or GOOGLE_SERVICE_ACCOUNT_JSON")
    return missing
