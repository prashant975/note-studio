"""The progress line, and the two ways it drifts out of step with the run.

app.py maps each stage the pipeline reports onto one of five numbered steps.
The map and the pipeline are edited in different files by different changes, and
nothing connected them — so both halves had drifted:

* the pipeline reported "Checking final formatting" between "Verifying layout"
  (step 4) and "Building error report" (step 5). It was not in the map, so it
  fell through to the default — "Step 3/5: Building notes". The counter ran
  4/5 -> 3/5 -> 5/5, and a step number that goes BACKWARDS reads as a failure
  and a restart;
* the map had an entry for "Extracting lecture (recording only)" that the
  pipeline never sent, so a run with no deck was told the app was "Reading
  lecture file" when there was no file to read.

These tests hold the two halves together: every stage the pipeline sends must be
named in the map, every entry in the map must be a stage the pipeline sends, and
the numbers must never decrease.
"""
from pathlib import Path
import ast
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
PIPELINE_SOURCE = (ROOT / "src" / "pipeline.py").read_text(encoding="utf-8")

#: Stages built by formatting a counter in. The map cannot hold these, so app.py
#: matches them by prefix instead; they are excluded from the exact-match rules.
DYNAMIC_PREFIXES = ("Building notes", "Transcribing the lecture")


def _steps_map() -> dict[str, str]:
    block = re.search(r"STEPS = \{(.*?)\n        \}", APP_SOURCE, re.S)
    assert block, "the STEPS map could not be found in app.py"
    return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block.group(1)))


def _stage_calls(source: str) -> list[tuple[int, str]]:
    """[(line, stage)] for every literal stage a source hands to the callback.

    Parsed, not pattern-matched. A stage name can contain a bracket — the real
    one is "Extracting lecture (recording only)" — and a regex for the call's
    arguments stops at that bracket and silently reports the stage as missing.
    """
    found: list[tuple[int, str]] = []

    def literals(node):
        """Every plain string this expression can evaluate to."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.IfExp):          # "a" if cond else "b"
            yield from literals(node.body)
            yield from literals(node.orelse)
        elif isinstance(node, ast.BoolOp):
            for value in node.values:
                yield from literals(value)

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name not in {"_stage", "stage"}:
            continue
        # _stage(callback, label) or stage(label)
        target = node.args[1] if (name == "_stage" and len(node.args) > 1) else node.args[0]
        for value in literals(target):
            if value and value[0].isupper():
                found.append((node.lineno, value))
    return found


def _stages_sent() -> set[str]:
    """Every literal stage string the run can hand to the progress callback."""
    found: set[str] = set()
    for relative in ("pipeline.py", "transcript.py",
                     "video_sources/google_drive.py",
                     "video_sources/source_router.py"):
        path = ROOT / "src" / relative
        if path.exists():
            found.update(stage for _, stage in
                         _stage_calls(path.read_text(encoding="utf-8")))
    return found


def _step_number(label: str) -> int | None:
    match = re.match(r"Step (\d+)/5", label)
    return int(match.group(1)) if match else None


# --------------------------------------------------------------- the two halves

def test_every_stage_the_run_reports_is_named_in_the_map():
    """An unmapped stage falls through to "Step 3/5: Building notes", which is
    wrong for every stage that is not building notes."""
    unmapped = {
        stage for stage in _stages_sent()
        if stage not in _steps_map()
        and not stage.startswith(DYNAMIC_PREFIXES)
    }
    assert not unmapped, (
        f"these stages reach the progress line with no entry in STEPS, so the "
        f"user is told the app is building notes: {sorted(unmapped)}")


def test_the_map_has_no_entry_for_a_stage_that_is_never_sent():
    """A dead entry is a promise the run does not keep — it was written to give
    a recording-only run its own wording, and the pipeline never sent it."""
    sent = _stages_sent()
    dead = {stage for stage in _steps_map()
            if stage not in sent and not stage.startswith(DYNAMIC_PREFIXES)}
    assert not dead, f"STEPS names stages the pipeline never reports: {sorted(dead)}"


def test_every_mapped_step_is_one_of_the_five():
    for stage, label in _steps_map().items():
        number = _step_number(label)
        assert number is not None, f"{stage!r} -> {label!r} has no step number"
        assert 1 <= number <= 5, f"{stage!r} -> {label!r}"


def test_a_recording_only_run_is_not_told_a_file_is_being_read():
    """There is no deck, so there is no file. The stage exists precisely so the
    wording can be true."""
    assert '"Extracting lecture (recording only)"' in PIPELINE_SOURCE, (
        "the pipeline never sends the recording-only stage")
    assert _steps_map()["Extracting lecture (recording only)"] == (
        "Step 1/5: Preparing the lecture")


# ------------------------------------------------- the numbers only go forwards

def test_the_step_number_never_goes_backwards_during_a_run():
    """The real ordering, taken from where each stage is raised in pipeline.py.

    This is the test that catches the class of bug directly: a stage added
    late in the run but mapped to an early step number.
    """
    steps = _steps_map()
    positions = [(line, stage) for line, stage in _stage_calls(PIPELINE_SOURCE)
                 if stage in steps and not stage.startswith(DYNAMIC_PREFIXES)]
    positions.sort()
    assert positions, "no ordered stages were found in pipeline.py"

    highest = 0
    for _, stage in positions:
        number = _step_number(steps[stage])
        assert number >= highest, (
            f"{stage!r} is raised after a step {highest} stage but is mapped to "
            f"step {number}; the progress counter runs backwards there")
        highest = max(highest, number)


@pytest.mark.parametrize("stage,expected", [
    ("Creating DOCX/PDF", 4),
    ("Verifying layout", 4),
    ("Checking final formatting", 4),
    ("Building error report", 5),
    ("Preparing reports", 5),
])
def test_the_late_stages_are_numbered_late(stage, expected):
    assert _step_number(_steps_map()[stage]) == expected
