"""Tests for the immutable, one-dispatch job lifecycle."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from notesbot.web_jobs import ALLOWED, JobManifest, JobSettings, transition
from notesbot.web_service import JobRecord, PublicJob, RECORD_VERSION, job_record_from_dict, job_record_to_dict


JOB_ID = "a" * 32
OWNER = "b" * 32
SOURCE = f"https://store.private.blob.vercel-storage.com/shortnotes/v1/inputs/{OWNER}/{JOB_ID}.pdf"
OUTPUT = f"https://store.private.blob.vercel-storage.com/shortnotes/v1/outputs/{OWNER}/{JOB_ID}.pdf"


def queued() -> JobManifest:
    return JobManifest(
        id=JOB_ID, owner=OWNER, source_url=SOURCE, state="queued",
        created_at=1_000, updated_at=1_000, output_url="", error="", dispatch_count=0,
    )


def test_importing_state_has_exact_serialization_transitions_and_public_status_contract():
    importing = JobManifest("a" * 32, "b" * 32, SOURCE, "importing", 1, 1, "", "", 0)
    assert transition(importing, "queued", now=2).state == "queued"
    assert transition(importing, "failed", now=2, error=True).dispatch_count == 0
    assert transition(importing, "expired", now=2).dispatch_count == 0
    with pytest.raises(ValueError):
        transition(importing, "running", now=2)
    store_host = "4mlmgdlkyhhksimu.private.blob.vercel-storage.com"
    canonical_importing = JobManifest("a" * 32, "b" * 32, f"https://{store_host}/shortnotes/v1/inputs/" + "b" * 32 + "/" + "a" * 32 + ".pdf", "importing", 1, 1, "", "", 0)
    record = JobRecord(RECORD_VERSION, canonical_importing, JobSettings("Teacher"), "linked-lecture.pdf", store_host)
    encoded = job_record_to_dict(record)
    assert set(encoded) == {"version", "dispatch_claim_id", "manifest", "settings", "filename"}
    decoded = job_record_from_dict(encoded, store_host=store_host)
    public = PublicJob(decoded.manifest.id, decoded.manifest.state, decoded.filename, 1, 1, "").to_dict()
    assert public == {"jobId": "a" * 32, "state": "importing", "filename": "linked-lecture.pdf", "createdAt": 1, "updatedAt": 1, "error": ""}


def test_settings_are_immutable_and_reject_oversized_instruction():
    settings = JobSettings(teacher_name="Asha", instruction="Keep diagrams", language="hinglish", subject="Physics")
    with pytest.raises(FrozenInstanceError):
        settings.language = "english"
    with pytest.raises(ValueError):
        JobSettings(teacher_name="Asha", instruction="x" * 1001)


def test_settings_keep_teacher_and_logo_watermarks_independent_with_legacy_logo_default():
    legacy = JobSettings(teacher_name="Asha", watermark=True)
    both_off = JobSettings(
        teacher_name="Asha", watermark=False, logo_watermark=False
    )
    both_on = JobSettings(
        teacher_name="Asha", watermark=True, logo_watermark=True
    )

    assert (legacy.watermark, legacy.logo_watermark) == (True, False)
    assert (both_off.watermark, both_off.logo_watermark) == (False, False)
    assert (both_on.watermark, both_on.logo_watermark) == (True, True)
    with pytest.raises(ValueError):
        JobSettings(teacher_name="Asha", logo_watermark=1)


def test_settings_reject_empty_teacher_name_and_store_trimmed_name():
    with pytest.raises(ValueError, match="teacher name"):
        JobSettings(teacher_name="   ")
    assert JobSettings(teacher_name="  Asha  ").teacher_name == "Asha"


def test_queued_can_dispatch_once_then_complete():
    running = transition(queued(), "running", now=1_010)
    assert running.state == "running"
    assert running.dispatch_count == 1
    complete = transition(running, "complete", now=1_020, output_url=OUTPUT)
    assert complete.state == "complete"
    assert complete.output_url == OUTPUT
    assert complete.dispatch_count == 1


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("queued", "complete"), ("queued", "failed"),
        ("running", "queued"),
        ("complete", "running"), ("failed", "running"), ("expired", "running"),
    ],
)
def test_invalid_and_terminal_transitions_are_rejected(source, target):
    manifest = queued()
    if source == "running":
        manifest = transition(manifest, "running", now=1_001)
    elif source == "complete":
        manifest = transition(
            transition(manifest, "running", now=1_001),
            source,
            now=1_002,
            output_url=OUTPUT,
        )
    elif source == "failed":
        manifest = transition(
            transition(manifest, "running", now=1_001),
            source,
            now=1_002,
            error=RuntimeError("model failed"),
        )
    elif source == "expired":
        manifest = transition(manifest, "expired", now=1_001)
    with pytest.raises(ValueError):
        transition(manifest, target, now=1_100)


def test_expiry_is_allowed_before_or_during_dispatch_but_never_after_terminal():
    assert transition(queued(), "expired", now=1_001).state == "expired"
    running = transition(queued(), "running", now=1_001)
    assert transition(running, "expired", now=1_002).state == "expired"


def test_transition_table_cannot_be_mutated_to_reopen_terminal_states():
    original_queued = ALLOWED["queued"]
    try:
        with pytest.raises(TypeError):
            ALLOWED["queued"] = frozenset({"complete"})
    finally:
        if ALLOWED["queued"] is not original_queued:
            ALLOWED["queued"] = original_queued

    try:
        with pytest.raises(AttributeError):
            ALLOWED["complete"].add("expired")
    finally:
        if hasattr(ALLOWED["complete"], "discard"):
            ALLOWED["complete"].discard("expired")


def test_manifest_rejects_bad_identifiers_times_and_dispatch_invariants():
    with pytest.raises(ValueError):
        JobManifest("short", "owner", SOURCE, "queued", 1, 1, "", "", 0)
    with pytest.raises(ValueError):
        JobManifest("a" * 32, "owner", SOURCE, "running", 2, 1, "", "", 1)
    with pytest.raises(ValueError):
        JobManifest("a" * 32, "owner", SOURCE, "running", 1, 1, "", "", 0)
    with pytest.raises(ValueError):
        JobManifest("a" * 32, "owner", SOURCE, "queued", 1, 1, "", "", 1)


def test_second_dispatch_is_impossible_even_if_transition_is_called_again():
    running = transition(queued(), "running", now=1_001)
    with pytest.raises(ValueError):
        transition(running, "running", now=1_002)


@pytest.mark.parametrize(
    ("state", "dispatch_count", "output_url", "error"),
    [
        ("queued", 0, OUTPUT, ""),
        ("queued", 0, "", "Generation failed"),
        ("running", 1, OUTPUT, ""),
        ("running", 1, "", "Generation failed"),
        ("complete", 1, "", ""),
        ("complete", 1, OUTPUT, "Generation failed"),
        ("failed", 1, "", ""),
        ("failed", 1, OUTPUT, "Generation failed"),
        ("expired", 0, OUTPUT, ""),
        ("expired", 0, "", "Generation failed"),
    ],
)
def test_manifest_rejects_result_fields_inconsistent_with_state(
    state, dispatch_count, output_url, error
):
    with pytest.raises(ValueError):
        JobManifest(
            id=JOB_ID,
            owner=OWNER,
            source_url=SOURCE,
            state=state,
            created_at=1_000,
            updated_at=1_100,
            output_url=output_url,
            error=error,
            dispatch_count=dispatch_count,
        )


@pytest.mark.parametrize(
    "source_url",
    [
        "https://evil.example/inputs/a.pdf",
        "https://store.private.blob.vercel-storage.com/inputs/a.pdf",
        "https://store.private.blob.vercel-storage.com/outputs/a.pdf",
        "https://user@store.private.blob.vercel-storage.com/inputs/a.pdf",
    ],
)
def test_manifest_rejects_source_outside_private_input_scope(source_url):
    with pytest.raises(ValueError, match="source URL"):
        JobManifest(
            id=JOB_ID,
            owner=OWNER,
            source_url=source_url,
            state="queued",
            created_at=1_000,
            updated_at=1_000,
            output_url="",
            error="",
            dispatch_count=0,
        )


@pytest.mark.parametrize(
    "output_url",
    [
        "https://evil.example/outputs/a.pdf",
        "https://store.private.blob.vercel-storage.com/outputs/a.pdf",
        "https://otherstore.private.blob.vercel-storage.com/outputs/a.pdf",
        "https://store.private.blob.vercel-storage.com/shortnotes/v1/inputs/a.pdf",
    ],
)
def test_complete_manifest_rejects_output_outside_matching_private_scope(output_url):
    with pytest.raises(ValueError, match="output URL"):
        JobManifest(
            id=JOB_ID,
            owner=OWNER,
            source_url=SOURCE,
            state="complete",
            created_at=1_000,
            updated_at=1_100,
            output_url=output_url,
            error="",
            dispatch_count=1,
        )


@pytest.mark.parametrize(
    ("source_url", "output_url"),
    [
        (
            f"https://store.private.blob.vercel-storage.com/shortnotes/v1/inputs/{'c' * 32}/{JOB_ID}.pdf",
            "",
        ),
        (
            SOURCE,
            f"https://store.private.blob.vercel-storage.com/shortnotes/v1/outputs/{OWNER}/{'c' * 32}.pdf",
        ),
    ],
)
def test_manifest_urls_are_bound_to_the_exact_owner_and_job_id(source_url, output_url):
    state = "complete" if output_url else "queued"
    with pytest.raises(ValueError):
        JobManifest(
            id=JOB_ID,
            owner=OWNER,
            source_url=source_url,
            state=state,
            created_at=1_000,
            updated_at=1_100,
            output_url=output_url,
            error="",
            dispatch_count=1 if state == "complete" else 0,
        )


def test_complete_and_failed_transitions_require_atomic_redacted_results():
    running = transition(queued(), "running", now=1_001)
    with pytest.raises(ValueError):
        transition(running, "complete", now=1_002)
    with pytest.raises(ValueError):
        transition(running, "failed", now=1_002)

    complete = transition(running, "complete", now=1_002, output_url=OUTPUT)
    assert complete.output_url == OUTPUT
    assert complete.error == ""

    failed = transition(
        running,
        "failed",
        now=1_002,
        error=RuntimeError("provider token=super-secret"),
    )
    assert failed.output_url == ""
    assert failed.error == "Generation failed"
    assert "super-secret" not in failed.error
