"""Strict, storage-backed orchestration for the Vercel short-notes web app.

This module owns validation and the finite job workflow.  It deliberately has
no HTTP-handler or environment-variable dependency, which keeps browser auth,
provider calls, and Blob state independently testable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, replace
from typing import Callable, Protocol
from urllib.parse import urlsplit

import requests

from notesbot.blob_store import (
    BlobConflictError,
    BlobNotFoundError,
    BlobStore,
    validate_pdf_bytes,
)
from notesbot import cost_meter, generation_profile
from notesbot.categories import CATEGORIES, DEFAULT as DEFAULT_CATEGORY, LEGACY as LEGACY_CATEGORIES
from notesbot.errors import VisualFidelityError
from notesbot.web_jobs import JobManifest, JobSettings, transition
from notesbot.input_limits import MAX_INPUT_BYTES
from notesbot.remote_pdf import ImportedBlob, acquire_public_pdf_to_blob_sync, REMOTE_IMPORT_RECOVERY_TTL_SECONDS


RECORD_VERSION = 1
MAX_OUTPUT_BYTES = 100 * 1024 * 1024
MAX_RECORD_BYTES = 32 * 1024
RUN_TOKEN_TTL_SECONDS = 5 * 60
DISPATCH_LEASE_SECONDS = 30
DISPATCH_RECOVERY_TTL_SECONDS = 5 * 60
MAX_DISPATCH_DELIVERY_ATTEMPTS = 3
TERMINAL_COMMIT_RECOVERY_LOADS = 2
APP_PREFIX = "shortnotes/v1"
SUBJECTS = frozenset({"auto", "chemistry", "physics", "biology", "mathematics", "sanskrit",
                      "science", "social_science", "english", "hindi", "other"})
LANGUAGES = frozenset({"auto", "english", "hindi", "hinglish", "sanskrit", "sanskrit_hindi"})

_HEX_32 = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_STORE_HOST = re.compile(
    r"[a-z0-9]{8,64}\.private\.blob\.vercel-storage\.com\Z", re.ASCII
)
_RUN_VERSION = "v1"
_MAX_EXPIRY_DIGITS = 11
_INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
# Retry metadata stays inside the already cross-runtime-safe 32-hex claim.
_CLAIM_ATTEMPT_PREFIX = "d15ca11ed15ca11e"
_SOURCE_CLEANUP_STATES = frozenset({"complete", "failed", "expired"})


class JobNotFound(RuntimeError):
    """An exact, owned job record does not exist."""


class JobNotReady(RuntimeError):
    """The requested operation is invalid for the current finite state."""


class DispatchUnavailable(RuntimeError):
    """The one permitted internal delivery attempt failed definitively."""

    def __init__(
        self,
        message: str = "Generation dispatch is unavailable",
        *,
        request_finished: bool = False,
    ) -> None:
        super().__init__(message)
        self.request_finished = request_finished is True


def _is_hex_32(value: object) -> bool:
    return isinstance(value, str) and _HEX_32.fullmatch(value) is not None


def _require_identity(value: object, field: str) -> str:
    if not _is_hex_32(value):
        raise ValueError(f"Invalid {field}")
    return value


def _require_store_host(value: object) -> str:
    if not isinstance(value, str) or _STORE_HOST.fullmatch(value) is None:
        raise ValueError("Invalid Blob store host")
    return value


def _timestamp(value: object | None = None) -> int:
    result = int(time.time()) if value is None else value
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ValueError("Invalid timestamp")
    return result


@dataclass(frozen=True)
class JobPaths:
    input: str
    job: str
    output: str


def owned_paths(owner: str, job_id: str) -> JobPaths:
    """Return all and only the three deterministic paths owned by one job."""
    owner = _require_identity(owner, "job owner")
    job_id = _require_identity(job_id, "job id")
    return JobPaths(
        input=f"{APP_PREFIX}/inputs/{owner}/{job_id}.pdf",
        job=f"{APP_PREFIX}/jobs/{owner}/{job_id}.json",
        output=f"{APP_PREFIX}/outputs/{owner}/{job_id}.pdf",
    )


def derive_owner(secret: str, session_nonce: str) -> str:
    """Derive the non-secret Blob namespace shared with the Node endpoints."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("A session secret is required")
    nonce = _require_identity(session_nonce, "session nonce")
    digest = hmac.new(
        secret.encode("utf-8"), f"owner.{nonce}".encode("ascii"), hashlib.sha256
    ).hexdigest()
    return digest[:32]


def _b64_mac(secret: str, message: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), message.encode("ascii"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def issue_run_token(
    secret: str,
    owner: str,
    job_id: str,
    now: int | None = None,
) -> str:
    """Issue a five-minute token bound to exactly one internal run."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("A run secret is required")
    owner = _require_identity(owner, "job owner")
    job_id = _require_identity(job_id, "job id")
    expiry = _timestamp(now) + RUN_TOKEN_TTL_SECONDS
    body = f"{_RUN_VERSION}.{expiry}.{owner}.{job_id}"
    return f"{body}.{_b64_mac(secret, 'run.' + body)}"


def verify_run_token(
    token: object,
    secret: str,
    owner: str,
    job_id: str,
    now: int | None = None,
) -> bool:
    """Verify structure, binding, lifetime, and MAC without an error oracle."""
    if not isinstance(token, str) or not isinstance(secret, str) or not secret:
        return False
    if not _is_hex_32(owner) or not _is_hex_32(job_id):
        return False
    try:
        version, expiry_text, token_owner, token_job_id, received = token.split(".")
    except ValueError:
        return False
    if (
        version != _RUN_VERSION
        or not expiry_text.isascii()
        or not expiry_text.isdigit()
        or len(expiry_text) > _MAX_EXPIRY_DIGITS
        or token_owner != owner
        or token_job_id != job_id
        or not _is_hex_32(token_owner)
        or not _is_hex_32(token_job_id)
    ):
        return False
    try:
        expiry = int(expiry_text)
        current = _timestamp(now)
    except (ValueError, OverflowError):
        return False
    if expiry <= current or expiry - current > RUN_TOKEN_TTL_SECONDS:
        return False
    body = f"{version}.{expiry_text}.{token_owner}.{token_job_id}"
    expected = _b64_mac(secret, "run." + body)
    try:
        return hmac.compare_digest(received.encode("ascii"), expected.encode("ascii"))
    except UnicodeEncodeError:
        return False


def normalize_teacher_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid teacher name")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 120:
        raise ValueError("Invalid teacher name")
    return normalized


def normalize_signature(teacher_name: str) -> str:
    name = normalize_teacher_name(teacher_name)
    return name if name[:3].lower() == "by " else f"by {name}"


def sanitize_pdf_filename(value: object) -> str:
    """Create one flat, bounded display/download filename."""
    if not isinstance(value, str):
        raise ValueError("Invalid filename")
    name = unicodedata.normalize("NFKC", value)
    name = _INVALID_FILENAME.sub("", name)
    name = " ".join(name.split())
    name = name.strip(". -")
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    name = name.rstrip(". -")[:90].rstrip(". -")
    return f"{name or 'studygram-notes'}.pdf"


@dataclass(frozen=True)
class StartRequest:
    id: str
    source_url: str
    source_size: int
    filename: str
    settings: JobSettings

    def __post_init__(self) -> None:
        _require_identity(self.id, "job id")
        if not isinstance(self.source_url, str):
            raise ValueError("Invalid input URL")
        if (
            isinstance(self.source_size, bool)
            or not isinstance(self.source_size, int)
            or not 1 <= self.source_size <= MAX_INPUT_BYTES
        ):
            raise ValueError("Invalid input size")
        if sanitize_pdf_filename(self.filename) != self.filename:
            raise ValueError("Invalid filename")
        if not isinstance(self.settings, JobSettings):
            raise ValueError("Invalid settings")

@dataclass(frozen=True)
class ImportStartRequest:
    id: str
    remote_url: str
    settings: JobSettings
    def __post_init__(self) -> None:
        _require_identity(self.id, "job id")
        if not isinstance(self.remote_url, str) or not self.remote_url:
            raise ValueError("Invalid PDF link")
        if not isinstance(self.settings, JobSettings): raise ValueError("Invalid settings")

def _parse_settings(value: dict) -> JobSettings:
    subject, language, instructions, watermark = value["subject"], value["language"], value["instructions"], value["watermark"]
    logo_watermark = value.get("logoWatermark", False)
    category = value.get("category", DEFAULT_CATEGORY)
    if isinstance(category, str):
        category = LEGACY_CATEGORIES.get(category, category)
    if not isinstance(category, str) or category not in CATEGORIES:
        raise ValueError("Invalid settings")
    if not isinstance(subject, str) or subject not in SUBJECTS or not isinstance(language, str) or language not in LANGUAGES or not isinstance(instructions, str) or len(instructions)>1000 or not isinstance(watermark,bool) or not isinstance(logo_watermark,bool):
        raise ValueError("Invalid settings")
    return JobSettings(normalize_teacher_name(value["teacherName"]), instructions, language, subject, watermark, logo_watermark, category)

def parse_import_start_request(value: object) -> ImportStartRequest:
    keys={"requestId","pdfUrl","teacherName","subject","language","instructions","watermark","logoWatermark"}
    if not isinstance(value,dict) or set(value) not in (keys, keys|{"category"}): raise ValueError("Invalid import request")
    return ImportStartRequest(_require_identity(value["requestId"], "job id"), value["pdfUrl"], _parse_settings(value))


def parse_start_request(value: object, *, owner: str, store_host: str) -> StartRequest:
    """Parse the browser's exact camel-case start payload."""
    legacy_keys = {
        "requestId",
        "inputUrl",
        "inputSize",
        "filename",
        "teacherName",
        "subject",
        "language",
        "instructions",
        "watermark",
    }
    current_keys = legacy_keys | {"logoWatermark"}
    category_keys = current_keys | {"category"}
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(legacy_keys), frozenset(current_keys), frozenset(category_keys)
    }:
        raise ValueError("Invalid start request")
    owner = _require_identity(owner, "job owner")
    store_host = _require_store_host(store_host)
    job_id = _require_identity(value["requestId"], "job id")
    paths = owned_paths(owner, job_id)
    expected_url = f"https://{store_host}/{paths.input}"
    if value["inputUrl"] != expected_url:
        raise ValueError("Invalid input URL")
    source_size = value["inputSize"]
    if (
        isinstance(source_size, bool)
        or not isinstance(source_size, int)
        or not 1 <= source_size <= MAX_INPUT_BYTES
    ):
        raise ValueError("Invalid input size")
    filename = sanitize_pdf_filename(value["filename"])
    settings = _parse_settings(value)
    return StartRequest(job_id, expected_url, source_size, filename, settings)


@dataclass(frozen=True)
class JobRecord:
    version: int
    manifest: JobManifest
    settings: JobSettings
    filename: str
    store_host: str
    dispatch_claim_id: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or self.version != RECORD_VERSION:
            raise ValueError("Invalid job record version")
        if not isinstance(self.manifest, JobManifest) or not isinstance(self.settings, JobSettings):
            raise ValueError("Invalid job record")
        host = _require_store_host(self.store_host)
        paths = owned_paths(self.manifest.owner, self.manifest.id)
        if self.manifest.source_url != f"https://{host}/{paths.input}":
            raise ValueError("Invalid job source")
        if self.manifest.output_url and self.manifest.output_url != f"https://{host}/{paths.output}":
            raise ValueError("Invalid job output")
        if self.settings.subject not in SUBJECTS or self.settings.language not in LANGUAGES:
            raise ValueError("Invalid job settings")
        if normalize_teacher_name(self.settings.teacher_name) != self.settings.teacher_name:
            raise ValueError("Invalid teacher name")
        if sanitize_pdf_filename(self.filename) != self.filename:
            raise ValueError("Invalid filename")
        claim = self.dispatch_claim_id
        if self.manifest.state == "importing":
            if claim != "":
                raise ValueError("Importing job cannot have a dispatch claim")
        elif self.manifest.state == "queued":
            if claim != "" and not _is_hex_32(claim):
                raise ValueError("Invalid dispatch claim")
        elif self.manifest.state in {"running", "complete"}:
            if not _is_hex_32(claim):
                raise ValueError("Invalid dispatch claim")
        # Import failures and stale importing reservations become terminal
        # before a dispatch claim exists. A claimed queued job can also expire
        # during delivery recovery, so failed/expired records allow either the
        # empty pre-dispatch marker or one valid opaque claim.
        elif claim != "" and not _is_hex_32(claim):
            raise ValueError("Invalid dispatch claim")


_RECORD_KEYS = {
    "version", "dispatch_claim_id", "manifest", "settings", "filename",
}
_MANIFEST_KEYS = {
    "id", "owner", "source_url", "state", "created_at", "updated_at",
    "output_url", "error", "dispatch_count",
}
_LEGACY_SETTINGS_KEYS = {
    "teacher_name", "instruction", "language", "subject", "watermark",
}
_SETTINGS_KEYS = _LEGACY_SETTINGS_KEYS | {"logo_watermark"}
_CATEGORY_SETTINGS_KEYS = _SETTINGS_KEYS | {"category"}


def job_record_to_dict(record: JobRecord) -> dict:
    if not isinstance(record, JobRecord):
        raise ValueError("Invalid job record")
    manifest = record.manifest
    settings = record.settings
    return {
        "version": record.version,
        "dispatch_claim_id": record.dispatch_claim_id,
        "manifest": {
            "id": manifest.id,
            "owner": manifest.owner,
            "source_url": manifest.source_url,
            "state": manifest.state,
            "created_at": manifest.created_at,
            "updated_at": manifest.updated_at,
            "output_url": manifest.output_url,
            "error": manifest.error,
            "dispatch_count": manifest.dispatch_count,
        },
        "settings": {
            "teacher_name": settings.teacher_name,
            "instruction": settings.instruction,
            "language": settings.language,
            "subject": settings.subject,
            "watermark": settings.watermark,
            "logo_watermark": settings.logo_watermark,
            "category": settings.category,
        },
        "filename": record.filename,
    }


def job_record_from_dict(
    value: object,
    *,
    store_host: str,
    expected_owner: str | None = None,
    expected_id: str | None = None,
) -> JobRecord:
    """Reconstruct frozen models from an untrusted cross-runtime JSON object."""
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS:
        raise ValueError("Invalid job record")
    manifest_value = value.get("manifest")
    settings_value = value.get("settings")
    if (
        not isinstance(manifest_value, dict)
        or set(manifest_value) != _MANIFEST_KEYS
        or not isinstance(settings_value, dict)
        or frozenset(settings_value) not in {
            frozenset(_LEGACY_SETTINGS_KEYS), frozenset(_SETTINGS_KEYS), frozenset(_CATEGORY_SETTINGS_KEYS)
        }
    ):
        raise ValueError("Invalid job record")
    try:
        if normalize_teacher_name(settings_value["teacher_name"]) != settings_value["teacher_name"]:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid job record") from None
    try:
        manifest = JobManifest(**manifest_value)
        normalized_settings = dict(settings_value)
        normalized_settings.setdefault("logo_watermark", False)
        normalized_settings.setdefault("category", DEFAULT_CATEGORY)
        settings = JobSettings(**normalized_settings)
        record = JobRecord(
            version=value["version"],
            manifest=manifest,
            settings=settings,
            filename=value["filename"],
            store_host=store_host,
            dispatch_claim_id=value["dispatch_claim_id"],
        )
    except (TypeError, ValueError):
        raise ValueError("Invalid job record") from None
    if expected_owner is not None and record.manifest.owner != _require_identity(
        expected_owner, "job owner"
    ):
        raise ValueError("Invalid job owner")
    if expected_id is not None and record.manifest.id != _require_identity(expected_id, "job id"):
        raise ValueError("Invalid job id")
    return record


@dataclass(frozen=True)
class StoredJob:
    record: JobRecord
    etag: str

    def __post_init__(self) -> None:
        if not isinstance(self.record, JobRecord) or not isinstance(self.etag, str) or not self.etag:
            raise ValueError("Invalid stored job")


class JobRepository(Protocol):
    def load(self, owner: str, job_id: str) -> StoredJob | None: ...
    def create(self, record: JobRecord) -> tuple[StoredJob, bool]: ...
    def replace(self, current: StoredJob, record: JobRecord) -> StoredJob: ...
    def delete(self, owner: str, job_id: str) -> None: ...


class BlobJobRepository:
    """Atomic create and ETag-CAS updates over the accepted Blob adapter."""

    def __init__(self, blob_store: BlobStore) -> None:
        if not hasattr(blob_store, "store_host"):
            raise ValueError("A Blob store is required")
        self.blob_store = blob_store
        self.store_host = _require_store_host(blob_store.store_host)

    def _location(self, owner: str, job_id: str) -> tuple[str, str]:
        path = owned_paths(owner, job_id).job
        return path, f"https://{self.store_host}/{path}"

    @staticmethod
    def _encoded(record: JobRecord) -> dict:
        value = job_record_to_dict(record)
        if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("Job record is too large")
        return value

    def load(self, owner: str, job_id: str) -> StoredJob | None:
        _path, url = self._location(owner, job_id)
        try:
            document = self.blob_store.get_json(url, MAX_RECORD_BYTES)
        except BlobNotFoundError:
            return None
        record = job_record_from_dict(
            document.value,
            store_host=self.store_host,
            expected_owner=owner,
            expected_id=job_id,
        )
        return StoredJob(record, document.etag)

    def create(self, record: JobRecord) -> tuple[StoredJob, bool]:
        if record.store_host != self.store_host:
            raise ValueError("Invalid job store")
        path, _url = self._location(record.manifest.owner, record.manifest.id)
        try:
            ref = self.blob_store.put_json(
                path, self._encoded(record), allow_overwrite=False
            )
        except RuntimeError as original:
            # Vercel has represented no-overwrite collisions with more than
            # one sanitized status family over time. One exact bounded read
            # safely distinguishes an existing idempotency winner. There is
            # deliberately no retry or loop.
            try:
                winner = self.load(record.manifest.owner, record.manifest.id)
            except Exception:
                raise original from None
            if winner is None:
                raise original from None
            return winner, False
        return StoredJob(record, ref.etag), True

    def replace(self, current: StoredJob, record: JobRecord) -> StoredJob:
        if (
            not isinstance(current, StoredJob)
            or record.store_host != self.store_host
            or current.record.store_host != self.store_host
            or current.record.manifest.owner != record.manifest.owner
            or current.record.manifest.id != record.manifest.id
        ):
            raise ValueError("Invalid job replacement")
        path, _url = self._location(record.manifest.owner, record.manifest.id)
        ref = self.blob_store.put_json(
            path,
            self._encoded(record),
            allow_overwrite=True,
            if_match=current.etag,
        )
        return StoredJob(record, ref.etag)

    def delete(self, owner: str, job_id: str) -> None:
        _path, url = self._location(owner, job_id)
        try:
            self.blob_store.delete([url])
        except BlobNotFoundError:
            return


@dataclass(frozen=True)
class PublicJob:
    id: str
    state: str
    filename: str
    created_at: int
    updated_at: int
    error: str
    cost: dict | None = None

    def __post_init__(self) -> None:
        _require_identity(self.id, "job id")
        if self.state not in {"importing", "queued", "running", "complete", "failed", "expired"}:
            raise ValueError("Invalid public job state")
        if self.cost is not None and not isinstance(self.cost, dict):
            raise ValueError("Invalid public job cost")
        if sanitize_pdf_filename(self.filename) != self.filename:
            raise ValueError("Invalid public filename")
        created = _timestamp(self.created_at)
        updated = _timestamp(self.updated_at)
        if updated < created:
            raise ValueError("Invalid public job timestamps")
        expected_error = "Generation failed" if self.state == "failed" else ""
        if self.error != expected_error:
            raise ValueError("Invalid public job error")

    def to_dict(self) -> dict:
        payload = {
            "jobId": self.id,
            "state": self.state,
            "filename": self.filename,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "error": self.error,
        }
        if self.cost is not None:
            payload["cost"] = self.cost
        return payload


def _public(record: JobRecord) -> PublicJob:
    manifest = record.manifest
    return PublicJob(
        manifest.id,
        manifest.state,
        record.filename,
        manifest.created_at,
        manifest.updated_at,
        manifest.error,
    )


@dataclass(frozen=True)
class StartResult:
    job: PublicJob
    created: bool

    def __post_init__(self) -> None:
        if not isinstance(self.job, PublicJob) or not isinstance(self.created, bool):
            raise ValueError("Invalid start result")


@dataclass(frozen=True)
class DownloadResult:
    deleted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.deleted, bool):
            raise ValueError("Invalid download result")


def _cost_path(owner: str, job_id: str) -> str:
    return f"{APP_PREFIX}/costs/{owner}/{job_id}.json"


def _write_cost_blob(
    job: JobRecord,
    blob_store: BlobStore,
    meter: cost_meter.UsageMeter,
    *,
    reason: str = "",
) -> None:
    """Best-effort user-facing cost artifact; never affects the job outcome."""
    try:
        snapshot = meter.snapshot()
        if snapshot["calls"] == 0 and snapshot["images"] == 0 and not reason:
            return
        if reason:
            snapshot["reason"] = reason[:200]
        blob_store.put_bytes(
            _cost_path(job.manifest.owner, job.manifest.id),
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            "application/json",
            True,
        )
    except Exception:
        pass


def run_job(
    job: JobRecord,
    blob_store: BlobStore,
    generate_fn: Callable,
    render_fn: Callable,
    *,
    now: int | None = None,
) -> JobManifest:
    """Run each expensive stage once and always attempt private-input cleanup."""
    if not isinstance(job, JobRecord) or job.manifest.state != "running":
        raise ValueError("Job must be running")
    terminal_time = _timestamp(now)
    source_url = job.manifest.source_url
    output_url = ""
    failure_category = "source_fetch_failed"
    failure_reason = ""
    meter_context = cost_meter.metered_job()
    meter = meter_context.__enter__()
    try:
        try:
            pdf = blob_store.get_pdf(source_url, MAX_INPUT_BYTES)
            failure_category = "generate_failed"
            subject_hint = "" if job.settings.subject == "auto" else job.settings.subject
            # Choosing "Sanskrit" explicitly asks for Sanskrit-only notes; any
            # other Sanskrit lecture keeps its verses with Hindi meanings.
            sanskrit_style = "strict" if job.settings.language == "sanskrit" else "hindi"
            with generation_profile.exam_category(job.settings.category), \
                    generation_profile.exam_subject(job.settings.subject), \
                    generation_profile.sanskrit_style_as(sanskrit_style):
                notes = generate_fn(
                    pdf,
                    job.settings.instruction,
                    language=job.settings.language,
                    subject_hint=subject_hint,
                )
            failure_category = "render_failed"
            rendered = render_fn(
                notes,
                signature=normalize_signature(job.settings.teacher_name),
                watermark=job.settings.watermark,
                logo_watermark=job.settings.logo_watermark,
            )
            failure_category = "output_validation_failed"
            if not isinstance(rendered, bytes) or not rendered or len(rendered) > MAX_OUTPUT_BYTES:
                raise ValueError("Invalid rendered PDF")
            validate_pdf_bytes(rendered)
            output_path = owned_paths(job.manifest.owner, job.manifest.id).output
            failure_category = "output_upload_failed"
            ref = blob_store.put_bytes(
                output_path, rendered, "application/pdf", False
            )
            output_url = ref.url
            return transition(
                job.manifest, "complete", now=terminal_time, output_url=output_url
            )
        except Exception as failure:
            category = (
                failure.code
                if isinstance(failure, VisualFidelityError)
                else failure_category
            )
            names: list[str] = []
            seen: set[int] = set()
            current: BaseException | None = failure
            while current is not None and id(current) not in seen and len(names) < 4:
                seen.add(id(current))
                names.append(
                    f"{type(current).__module__}.{type(current).__qualname__}"
                )
                current = current.__cause__ or (
                    None if current.__suppress_context__ else current.__context__
                )
            # Deepest project frame of the raise: class names and code
            # locations only, never message content.
            frame_note = ""
            trace = failure.__traceback__
            while trace is not None:
                filename = trace.tb_frame.f_code.co_filename.replace("\\", "/")
                if "/notesbot/" in filename or filename.endswith("web.py"):
                    short = filename.rsplit("/notesbot/", 1)[-1].rsplit("/", 1)[-1]
                    frame_note = (
                        f" at={short}:{trace.tb_lineno}:"
                        f"{trace.tb_frame.f_code.co_name}"
                    )
                trace = trace.tb_next
            failure_reason = f"{category} · {names[0] if names else ''}{frame_note}"
            print(
                f"[notesbot.visual-error] category={category}"
                f" exc={'<-'.join(names)}{frame_note}"
            )
            if output_url:
                try:
                    blob_store.delete([output_url])
                except Exception:
                    pass
            return transition(job.manifest, "failed", now=terminal_time, error=failure)
    finally:
        _write_cost_blob(job, blob_store, meter, reason=failure_reason)
        meter_context.__exit__(None, None, None)
        try:
            blob_store.delete([source_url])
        except Exception:
            pass


class WebService:
    """One-dispatch orchestration with durable CAS state transitions."""

    def __init__(
        self,
        repository: JobRepository,
        blob_store: BlobStore,
        dispatcher: Callable[[str, str, str], None],
        generate_fn: Callable,
        render_fn: Callable,
        *,
        run_secret: str,
        clock: Callable[[], int] = lambda: int(time.time()),
        claim_factory: Callable[[], str] = lambda: secrets.token_hex(16),
        acquire_remote_pdf: Callable[[str, BlobStore, str], ImportedBlob] | None = None,
    ) -> None:
        if not isinstance(run_secret, str) or not run_secret:
            raise ValueError("A run secret is required")
        self.repository = repository
        self.blob_store = blob_store
        self.store_host = _require_store_host(blob_store.store_host)
        self.dispatcher = dispatcher
        self.generate_fn = generate_fn
        self.render_fn = render_fn
        self.run_secret = run_secret
        self.clock = clock
        if not callable(claim_factory):
            raise ValueError("A dispatch claim factory is required")
        self.claim_factory = claim_factory
        self.acquire_remote_pdf = acquire_remote_pdf or (lambda url, store, path: acquire_public_pdf_to_blob_sync(url, store, path, max_bytes=MAX_INPUT_BYTES))

    def _dispatch_attempt(self, claim_id: str) -> int:
        if claim_id.startswith(_CLAIM_ATTEMPT_PREFIX):
            offset = len(_CLAIM_ATTEMPT_PREFIX)
            encoded = claim_id[offset:offset + 2]
            attempt = int(encoded, 16)
            if 2 <= attempt <= MAX_DISPATCH_DELIVERY_ATTEMPTS:
                return attempt
        return 1

    def _new_dispatch_claim(self, attempt: int) -> str:
        raw = _require_identity(self.claim_factory(), "dispatch claim")
        if attempt == 1:
            # Keep the original opaque claim format for the first delivery.
            # Avoid emitting a value that could be mistaken for our retry marker.
            if raw.startswith(_CLAIM_ATTEMPT_PREFIX):
                raw = ("0" if raw[0] != "0" else "1") + raw[1:]
            return raw
        if not 2 <= attempt <= MAX_DISPATCH_DELIVERY_ATTEMPTS:
            raise ValueError("Invalid dispatch attempt")
        suffix_length = 32 - len(_CLAIM_ATTEMPT_PREFIX) - 2
        return f"{_CLAIM_ATTEMPT_PREFIX}{attempt:02x}{raw[-suffix_length:]}"

    def _claim_dispatch(
        self,
        stored: StoredJob,
        *,
        now: int,
    ) -> tuple[StoredJob, bool]:
        """Acquire one finite delivery lease with a CAS and no retry loop."""
        record = stored.record
        manifest = record.manifest
        if manifest.state != "queued":
            return stored, False
        attempt = 1
        if record.dispatch_claim_id:
            if now - manifest.updated_at < DISPATCH_LEASE_SECONDS:
                return stored, False
            attempt = self._dispatch_attempt(record.dispatch_claim_id)
            if (
                attempt >= MAX_DISPATCH_DELIVERY_ATTEMPTS
                or now - manifest.created_at > DISPATCH_RECOVERY_TTL_SECONDS
            ):
                settled = self._settle_dispatch_failure(
                    stored,
                    request_finished=False,
                )
                if settled is None:
                    raise RuntimeError("Job storage is unavailable") from None
                return settled, False
            attempt += 1
        claim_id = self._new_dispatch_claim(attempt)
        claim_time = max(now, manifest.updated_at)
        claimed_record = replace(
            record,
            dispatch_claim_id=claim_id,
            manifest=replace(manifest, updated_at=claim_time),
        )
        try:
            return self.repository.replace(stored, claimed_record), True
        except RuntimeError as original:
            try:
                winner = self.repository.load(record.manifest.owner, record.manifest.id)
            except Exception:
                raise original from None
            if winner is None or not winner.record.dispatch_claim_id:
                raise original from None
            return winner, winner.record == claimed_record

    def _deliver_dispatch(self, claimed: StoredJob, *, now: int) -> StoredJob:
        manifest = claimed.record.manifest
        token = issue_run_token(
            self.run_secret,
            manifest.owner,
            manifest.id,
            now=now,
        )
        try:
            self.dispatcher(manifest.owner, manifest.id, token)
        except DispatchUnavailable as failure:
            settled = self._settle_dispatch_failure(
                claimed,
                request_finished=failure.request_finished,
            )
            if settled is None or settled.record.manifest.state == "expired":
                raise
            return settled
        return claimed

    def _settle_dispatch_failure(
        self,
        claimed: StoredJob,
        *,
        request_finished: bool,
    ) -> StoredJob | None:
        """Expire a definitively undelivered claim without racing a live run."""
        manifest = claimed.record.manifest
        try:
            current = self.repository.load(manifest.owner, manifest.id)
        except Exception:
            return None
        if current is None:
            return None
        current_manifest = current.record.manifest
        if current_manifest.state not in {"queued", "running"}:
            return current
        if current_manifest.state == "running" and not request_finished:
            return current
        if current.record.dispatch_claim_id != claimed.record.dispatch_claim_id:
            return None
        expired_record = replace(
            current.record,
            manifest=transition(
                current_manifest, "expired", now=_timestamp(self.clock())
            ),
        )
        try:
            settled = self.repository.replace(current, expired_record)
        except Exception:
            try:
                winner = self.repository.load(manifest.owner, manifest.id)
            except Exception:
                return None
            if winner is None or winner.record.manifest.state in {"queued", "running"}:
                return None
            settled = winner
        if settled.record.manifest.state == "expired":
            try:
                self.blob_store.delete([settled.record.manifest.source_url])
            except Exception:
                pass
        return settled

    def start(self, owner: str, request: StartRequest) -> StartResult:
        owner = _require_identity(owner, "job owner")
        if not isinstance(request, StartRequest):
            raise ValueError("Invalid start request")
        paths = owned_paths(owner, request.id)
        if request.source_url != f"https://{self.store_host}/{paths.input}":
            raise ValueError("Invalid input URL")
        now = _timestamp(self.clock())
        record = JobRecord(
            RECORD_VERSION,
            JobManifest(
                id=request.id,
                owner=owner,
                source_url=request.source_url,
                state="queued",
                created_at=now,
                updated_at=now,
                output_url="",
                error="",
                dispatch_count=0,
            ),
            request.settings,
            request.filename,
            self.store_host,
        )
        stored, created = self.repository.create(record)
        if not created and stored.record.manifest.state == "importing":
            raise BlobConflictError("Job is importing")
        stored, should_dispatch = self._claim_dispatch(stored, now=now)
        if should_dispatch:
            stored = self._deliver_dispatch(stored, now=now)
        return StartResult(_public(stored.record), created)

    def import_and_start(self, owner: str, request: ImportStartRequest) -> StartResult:
        owner = _require_identity(owner, "job owner")
        if not isinstance(request, ImportStartRequest): raise ValueError("Invalid import request")
        paths=owned_paths(owner, request.id); now=_timestamp(self.clock())
        reservation=JobRecord(RECORD_VERSION, JobManifest(request.id, owner, f"https://{self.store_host}/{paths.input}", "importing", now, now, "", "", 0), request.settings, "linked-lecture.pdf", self.store_host)
        stored, created=self.repository.create(reservation)
        if not created: raise BlobConflictError("Job state changed")
        try:
            imported=self.acquire_remote_pdf(request.remote_url, self.blob_store, paths.input)
            if not isinstance(imported, ImportedBlob) or imported.source_url != reservation.manifest.source_url or not 1 <= imported.source_size <= MAX_INPUT_BYTES:
                raise RuntimeError("Invalid imported PDF")
            queued=replace(stored.record, filename=sanitize_pdf_filename(imported.filename), manifest=transition(stored.record.manifest,"queued",now=_timestamp(self.clock())))
            stored=self.repository.replace(stored,queued)
            stored, should_dispatch=self._claim_dispatch(stored,now=_timestamp(self.clock()))
            if should_dispatch: stored=self._deliver_dispatch(stored,now=_timestamp(self.clock()))
            return StartResult(_public(stored.record), True)
        except Exception:
            try:
                latest=self.repository.load(owner,request.id)
                if latest and latest.record.manifest.state in {"importing", "queued"}:
                    failed=replace(latest.record, manifest=transition(latest.record.manifest,"failed",now=_timestamp(self.clock()),error=True))
                    self.repository.replace(latest,failed); self._retry_source_cleanup(failed)
            except Exception: pass
            raise

    def status(self, owner: str, job_id: str) -> PublicJob:
        owner = _require_identity(owner, "job owner")
        job_id = _require_identity(job_id, "job id")
        stored = self.repository.load(owner, job_id)
        if stored is None:
            raise JobNotFound("Job not found")
        now = _timestamp(self.clock())
        if stored.record.manifest.state == "importing" and now - stored.record.manifest.created_at >= REMOTE_IMPORT_RECOVERY_TTL_SECONDS:
            expired=replace(stored.record, manifest=transition(stored.record.manifest,"expired",now=now))
            stored=self.repository.replace(stored,expired); self._retry_source_cleanup(stored.record)
            retry_source_cleanup = False
        else:
            retry_source_cleanup = stored.record.manifest.state in _SOURCE_CLEANUP_STATES
        stored, should_dispatch = self._claim_dispatch(stored, now=now)
        if should_dispatch:
            stored = self._deliver_dispatch(stored, now=now)
        if retry_source_cleanup:
            self._retry_source_cleanup(stored.record)
        public = _public(stored.record)
        if public.state in {"complete", "failed"}:
            cost = self._load_cost(stored.record)
            if cost is not None:
                public = replace(public, cost=cost)
        return public

    def _load_cost(self, record: JobRecord) -> dict | None:
        """Best-effort read of the job's cost artifact for user display."""
        try:
            path = _cost_path(record.manifest.owner, record.manifest.id)
            raw = self.blob_store.get_bytes(f"https://{self.store_host}/{path}", 4_096)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                return None
            snapshot = {
                "inr": round(float(value.get("inr", 0.0)), 2),
                "input_tokens": int(value.get("input_tokens", 0)),
                "output_tokens": int(value.get("output_tokens", 0)),
                "images": int(value.get("images", 0)),
                "calls": int(value.get("calls", 0)),
            }
            if not 0.0 <= snapshot["inr"] <= 1_000_000.0 or any(
                not 0 <= snapshot[key] <= 10_000_000_000
                for key in ("input_tokens", "output_tokens", "images", "calls")
            ):
                return None
            snapshot["line"] = cost_meter.format_cost_line(snapshot)
            reason = value.get("reason")
            if isinstance(reason, str) and reason:
                cleaned = re.sub(r"[^A-Za-z0-9_.:/·() <>-]", "", reason)[:200].strip()
                if cleaned:
                    snapshot["reason"] = cleaned
            return snapshot
        except Exception:
            return None

    def run(self, owner: str, job_id: str) -> JobManifest:
        owner = _require_identity(owner, "job owner")
        job_id = _require_identity(job_id, "job id")
        stored = self.repository.load(owner, job_id)
        if stored is None:
            raise JobNotFound("Job not found")
        if stored.record.manifest.state != "queued":
            self._retry_source_cleanup(stored.record)
            return stored.record.manifest
        if not stored.record.dispatch_claim_id:
            raise JobNotReady("Job has not been dispatched")

        running_record = replace(
            stored.record,
            manifest=transition(
                stored.record.manifest, "running", now=_timestamp(self.clock())
            ),
        )
        try:
            claimed = self.repository.replace(stored, running_record)
        except BlobConflictError:
            winner = self.repository.load(owner, job_id)
            if winner is None:
                raise RuntimeError("Job storage is unavailable") from None
            return winner.record.manifest

        terminal = run_job(
            claimed.record,
            self.blob_store,
            self.generate_fn,
            self.render_fn,
            now=_timestamp(self.clock()),
        )
        from .output_naming import output_name
        filename = (sanitize_pdf_filename(output_name(claimed.record.filename, max_stem_length=90))
                    if terminal.state == "complete" else claimed.record.filename)
        terminal_record = replace(claimed.record, manifest=terminal, filename=filename)
        try:
            persisted = self.repository.replace(claimed, terminal_record)
        except BlobConflictError:
            self._delete_orphan_output(terminal)
            winner = self.repository.load(owner, job_id)
            if winner is None:
                raise RuntimeError("Job storage is unavailable") from None
            return winner.record.manifest
        except Exception:
            for _attempt in range(TERMINAL_COMMIT_RECOVERY_LOADS):
                try:
                    winner = self.repository.load(owner, job_id)
                except Exception:
                    continue
                if winner is not None and winner.record == terminal_record:
                    return winner.record.manifest
            raise RuntimeError("Job storage is unavailable") from None
        return persisted.record.manifest

    def _delete_orphan_output(self, manifest: JobManifest) -> None:
        if manifest.state != "complete" or not manifest.output_url:
            return
        try:
            self.blob_store.delete([manifest.output_url])
        except Exception:
            pass

    def _retry_source_cleanup(
        self,
        record: JobRecord,
        *,
        required: bool = False,
    ) -> bool:
        """Attempt one idempotent source delete while its manifest is durable."""
        if record.manifest.state not in _SOURCE_CLEANUP_STATES:
            return True
        try:
            self.blob_store.delete([record.manifest.source_url])
        except BlobNotFoundError:
            return True
        except Exception:
            if required:
                raise RuntimeError("Job cleanup is unavailable") from None
            return False
        return True

    def downloaded(self, owner: str, job_id: str, fetched: bool) -> DownloadResult:
        owner = _require_identity(owner, "job owner")
        job_id = _require_identity(job_id, "job id")
        if fetched is not True:
            raise ValueError("A completed fetch acknowledgement is required")
        stored = self.repository.load(owner, job_id)
        if stored is None:
            return DownloadResult(False)
        manifest = stored.record.manifest
        if manifest.state != "complete":
            raise JobNotReady("Job is not complete")
        self._retry_source_cleanup(stored.record, required=True)
        try:
            self.blob_store.delete([manifest.output_url])
        except BlobNotFoundError:
            pass
        self.repository.delete(owner, job_id)
        return DownloadResult(True)


class InternalRunDispatcher:
    """One HTTP delivery attempt to a configuration-derived trusted origin."""

    def __init__(self, origin: str, *, session: object | None = None) -> None:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            raise ValueError("Invalid application origin") from None
        if (
            not isinstance(origin, str)
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid application origin")
        self.origin = f"https://{parsed.hostname}"
        self.session = session if session is not None else requests.Session()

    def __call__(self, owner: str, job_id: str, token: str) -> None:
        owner = _require_identity(owner, "job owner")
        job_id = _require_identity(job_id, "job id")
        if not isinstance(token, str) or not token:
            raise ValueError("Invalid run token")
        try:
            response = self.session.post(
                f"{self.origin}/api/web?action=run",
                json={"owner": owner, "jobId": job_id},
                headers={"X-Shortnotes-Run-Token": token},
                timeout=(3.0, 1.0),
                allow_redirects=False,
            )
        except requests.exceptions.ReadTimeout:
            return
        except Exception:
            raise DispatchUnavailable("Generation dispatch is unavailable") from None
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or status < 200 or status >= 300:
            raise DispatchUnavailable(
                "Generation dispatch is unavailable",
                request_finished=True,
            ) from None
