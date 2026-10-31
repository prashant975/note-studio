"""Immutable web-job records and the intentionally tiny job state machine."""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
import secrets
import time
from types import MappingProxyType
from urllib.parse import urlsplit

from notesbot.blob_store import validate_blob_url
from notesbot.categories import DEFAULT as DEFAULT_CATEGORY, LEGACY as LEGACY_CATEGORIES, CATEGORIES


ALLOWED = MappingProxyType(
    {
        "importing": frozenset({"queued", "failed", "expired"}),
        "queued": frozenset({"running", "failed", "expired"}),
        "running": frozenset({"complete", "failed", "expired"}),
        "complete": frozenset(),
        "failed": frozenset(),
        "expired": frozenset(),
    }
)
_JOB_ID = re.compile(r"[a-f0-9]{32}")
_OWNER_ID = re.compile(r"[a-f0-9]{32}")
_PRIVATE_BLOB_HOST = re.compile(r"[a-z0-9]{1,64}\.private\.blob\.vercel-storage\.com")
_SAFE_JOB_ERROR = "Generation failed"
_INPUT_PREFIX = "shortnotes/v1/inputs/"
_OUTPUT_PREFIX = "shortnotes/v1/outputs/"


def _nonnegative_int(value: int, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"Invalid {field}")


def _private_blob_url(
    value: str,
    prefix: str,
    field: str,
    expected_host: str | None = None,
    expected_path: str | None = None,
) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if not _PRIVATE_BLOB_HOST.fullmatch(host):
            raise ValueError
        if expected_host is not None and host != expected_host:
            raise ValueError
        if validate_blob_url(value, host, prefix) != value:
            raise ValueError
        if expected_path is not None and parsed.path != f"/{expected_path}":
            raise ValueError
        return host
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {field} URL") from None


@dataclass(frozen=True)
class JobSettings:
    """Per-request generation options; never mutate process-wide config."""

    teacher_name: str
    instruction: str = ""
    language: str = "auto"
    subject: str = "auto"
    watermark: bool = True
    logo_watermark: bool = False
    category: str = DEFAULT_CATEGORY

    def __post_init__(self) -> None:
        if not isinstance(self.teacher_name, str):
            raise ValueError("Invalid teacher name")
        teacher_name = self.teacher_name.strip()
        if not teacher_name or len(teacher_name) > 120:
            raise ValueError("Invalid teacher name")
        object.__setattr__(self, "teacher_name", teacher_name)
        if not isinstance(self.instruction, str) or len(self.instruction) > 1000:
            raise ValueError("Invalid instruction")
        if not isinstance(self.language, str) or not self.language.strip() or len(self.language) > 32:
            raise ValueError("Invalid language")
        if not isinstance(self.subject, str) or not self.subject.strip() or len(self.subject) > 80:
            raise ValueError("Invalid subject")
        if not isinstance(self.watermark, bool):
            raise ValueError("Invalid watermark setting")
        if not isinstance(self.logo_watermark, bool):
            raise ValueError("Invalid logo watermark setting")
        category = LEGACY_CATEGORIES.get(self.category, self.category)
        if category not in CATEGORIES:
            raise ValueError("Invalid category")
        object.__setattr__(self, "category", category)

    @property
    def instructions(self) -> str:
        """Plural alias used by the generation entry point."""
        return self.instruction


@dataclass(frozen=True)
class JobManifest:
    id: str
    owner: str
    source_url: str
    state: str
    created_at: int
    updated_at: int
    output_url: str
    error: str
    dispatch_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _JOB_ID.fullmatch(self.id):
            raise ValueError("Invalid job id")
        if not isinstance(self.owner, str) or not _OWNER_ID.fullmatch(self.owner):
            raise ValueError("Invalid job owner")
        source_host = _private_blob_url(
            self.source_url,
            _INPUT_PREFIX,
            "source",
            expected_path=f"{_INPUT_PREFIX}{self.owner}/{self.id}.pdf",
        )
        if self.state not in ALLOWED:
            raise ValueError("Invalid job state")
        _nonnegative_int(self.created_at, "created time")
        _nonnegative_int(self.updated_at, "updated time")
        if self.updated_at < self.created_at:
            raise ValueError("Invalid job timestamps")
        _nonnegative_int(self.dispatch_count, "dispatch count")
        if self.dispatch_count > 1:
            raise ValueError("Job may be dispatched once")
        if self.state in {"importing", "queued"} and self.dispatch_count != 0:
            raise ValueError("Queued job cannot be dispatched")
        if self.state == "running" and self.dispatch_count != 1:
            raise ValueError("Running job must be dispatched")
        if self.state in {"complete"} and self.dispatch_count != 1:
            raise ValueError("Finished job must be dispatched")
        if self.state == "failed" and self.dispatch_count not in {0, 1}:
            raise ValueError("Failed job dispatch count is invalid")
        if not isinstance(self.output_url, str) or not isinstance(self.error, str):
            raise ValueError("Invalid job result")
        if self.state in {"importing", "queued", "running", "expired"}:
            if self.output_url or self.error:
                raise ValueError("Job state cannot carry a result")
        elif self.state == "complete":
            if not self.output_url or self.error:
                raise ValueError("Complete job requires only an output URL")
            _private_blob_url(
                self.output_url,
                _OUTPUT_PREFIX,
                "output",
                source_host,
                f"{_OUTPUT_PREFIX}{self.owner}/{self.id}.pdf",
            )
        elif self.state == "failed":
            if self.output_url or self.error != _SAFE_JOB_ERROR:
                raise ValueError("Failed job requires a redacted error")


def new_job_id() -> str:
    """Return the 128-bit, URL-safe identifier expected by ``JobManifest``."""
    return secrets.token_hex(16)


def transition(
    manifest: JobManifest,
    target: str,
    *,
    now: int | None = None,
    output_url: str = "",
    error: object | None = None,
) -> JobManifest:
    """Create the sole permitted next immutable state for a job."""
    if not isinstance(manifest, JobManifest) or target not in ALLOWED.get(manifest.state, set()):
        raise ValueError("Invalid job state transition")
    timestamp = int(time.time()) if now is None else now
    _nonnegative_int(timestamp, "transition time")
    if timestamp < manifest.updated_at:
        raise ValueError("Transition time cannot move backwards")
    dispatch_count = manifest.dispatch_count
    if target == "running":
        if dispatch_count != 0:
            raise ValueError("Job has already been dispatched")
        dispatch_count = 1
    next_output = ""
    next_error = ""
    if target == "complete":
        if not isinstance(output_url, str) or not output_url or error not in {None, ""}:
            raise ValueError("Complete transition requires only an output URL")
        next_output = output_url
    elif target == "failed":
        if output_url or error is None or (isinstance(error, str) and not error.strip()):
            raise ValueError("Failed transition requires an error")
        next_error = _SAFE_JOB_ERROR
    elif output_url or error not in {None, ""}:
        raise ValueError("This transition cannot carry a result")
    return replace(
        manifest,
        state=target,
        updated_at=timestamp,
        dispatch_count=dispatch_count,
        output_url=next_output,
        error=next_error,
    )
