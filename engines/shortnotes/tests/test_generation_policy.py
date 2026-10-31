from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from notesbot.errors import VisualFidelityError
from notesbot.generation_policy import GenerationRun


def test_note_dispatch_is_exactly_once_per_chunk() -> None:
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)

    run.take_note(0)

    with pytest.raises(VisualFidelityError) as caught:
        run.take_note(0)
    assert caught.value.code == "work_limit_exceeded"


def test_note_dispatch_supports_distinct_chunks_concurrently() -> None:
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run.take_note, range(8)))

    assert run.snapshot(8)["notes_model_calls"] == 8


def test_image_dispatch_is_hard_capped_at_two() -> None:
    run = GenerationRun(deadline_at=10.0, max_images=99, clock=lambda: 1.0)

    assert run.try_take_image("c0-p1-s0") is True
    assert run.try_take_image("c0-p2-s0") is True
    assert run.try_take_image("c0-p3-s0") is False


def test_duplicate_image_dispatch_is_rejected() -> None:
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)
    assert run.try_take_image("c0-p1-s0") is True

    with pytest.raises(VisualFidelityError) as caught:
        run.try_take_image("c0-p1-s0")

    assert caught.value.code == "work_limit_exceeded"


def test_deadline_is_checked_before_provider_work() -> None:
    run = GenerationRun(deadline_at=1.0, max_images=2, clock=lambda: 1.0)

    with pytest.raises(VisualFidelityError) as caught:
        run.take_note(0)

    assert caught.value.code == "deadline_exceeded"


def test_snapshot_is_redacted_numeric_and_omissions_are_deduplicated() -> None:
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)
    run.take_note(0)
    run.mark_visual_eligible("c0-p1-s0")
    run.mark_visual_eligible("c0-p2-s0")
    assert run.try_take_image("c0-p1-s0") is True
    run.mark_image_omitted("c0-p2-s0")
    run.mark_image_omitted("c0-p2-s0")

    snapshot = run.snapshot(1)

    assert snapshot == {
        "chunk_count": 1,
        "notes_model_calls": 1,
        "eligible_visuals": 2,
        "image_model_calls": 1,
        "images_omitted": 1,
        "mathpix_calls": 0,
        "inventory_calls": 0,
        "audit_calls": 0,
        "verifier_calls": 0,
    }
    assert all(isinstance(value, int) for value in snapshot.values())


@pytest.mark.parametrize("job_key", ["", None])
def test_empty_ledger_keys_are_rejected(job_key) -> None:
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)

    with pytest.raises(VisualFidelityError) as caught:
        run.mark_visual_eligible(job_key)

    assert caught.value.code == "work_limit_exceeded"
