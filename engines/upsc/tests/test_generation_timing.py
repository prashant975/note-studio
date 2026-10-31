"""How long a thing took, said in words, and said on the page.

The page never reported this at all: a run that took nine minutes and one that
took ninety seconds finished with the same sentence. These cover the formatter
and the three places the page now reports a measurement from.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dmp.labels import UI, text
from src.utils import format_duration

APP = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


# ------------------------------------------------------------- the formatter

@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0.0 s"),
        (0.04, "0.0 s"),
        (3.44, "3.4 s"),
        (9.94, "9.9 s"),
        (10, "10 s"),
        (42.4, "42 s"),
        (59.4, "59 s"),
        (60, "1 m 00 s"),
        (72, "1 m 12 s"),
        (612, "10 m 12 s"),
        (3599, "59 m 59 s"),
        (3600, "1 h 00 m"),
        (7845, "2 h 10 m"),
    ],
)
def test_a_duration_reads_the_way_a_person_says_it(seconds, expected) -> None:
    assert format_duration(seconds) == expected


@pytest.mark.parametrize("seconds,expected", [(9.96, "10 s"), (59.6, "1 m 00 s")])
def test_rounding_does_not_strand_a_value_between_two_bands(seconds, expected) -> None:
    """9.96 s must not print as "10.0 s", nor 59.6 s stall at "60 s"."""
    assert format_duration(seconds) == expected


@pytest.mark.parametrize("value", [None, "", "abc", -1, float("-inf")])
def test_an_unmeasured_run_says_so_rather_than_inventing_a_zero(value) -> None:
    """A missing or negative measurement — a clock moved mid-run — is not 0 s."""
    assert format_duration(value) == "—"


# --------------------------------------------------- the page reports it back

def test_the_notes_run_is_timed_and_reported_per_file() -> None:
    """Both the batch heading and each file's own card carry the number."""
    assert "batch_started = time.monotonic()" in APP
    assert "job_started = time.monotonic()" in APP
    assert 'result_dict["elapsed_seconds"] = time.monotonic() - job_started' in APP
    assert "Notes generated successfully in {batch_elapsed}." in APP
    assert "Notes generation time: {format_duration(r['elapsed_seconds'])}" in APP


def test_a_failed_run_still_reports_how_long_it_took() -> None:
    """"It failed after nine minutes" is the more useful of the two facts."""
    failures = re.findall(
        r'"tracking": None,\s*\n\s*"elapsed_seconds": time\.monotonic\(\) - job_started,',
        APP,
    )
    assert len(failures) == 2, "both failure paths must record their elapsed time"


def test_the_dmp_measurements_survive_the_rerun_that_follows_them() -> None:
    """Both generate paths end in st.rerun(), which discards every local."""
    assert 'st.session_state[f"dmp_questions_seconds_{note_id}"]' in APP
    assert 'st.session_state[f"dmp_answers_seconds_{note_id}"]' in APP
    assert 'text(label_key, lang, duration=format_duration(measured))' in APP


def test_a_new_dmp_set_does_not_inherit_the_last_one_s_timings() -> None:
    assert 'st.session_state.pop(f"dmp_questions_seconds_{note_id}", None)' in APP
    assert 'st.session_state.pop(f"dmp_answers_seconds_{note_id}", None)' in APP


@pytest.mark.parametrize("language", sorted(UI))
def test_the_dmp_timings_are_worded_in_every_language(language) -> None:
    for key in ("questions_time", "answers_time"):
        rendered = text(key, language, duration="1 m 12 s")
        assert "1 m 12 s" in rendered, f"{language}.{key} dropped the duration"
        assert "{duration}" not in rendered


def test_each_dpp_step_reports_its_own_time() -> None:
    """Reading the bank, matching, building the set, writing the files."""
    assert "Question bank read in {format_duration(st.session_state[bank_key])} " in APP
    assert "Notes matched to the taxonomy in {format_duration(matched_in)}." in APP
    assert 'suffix = f" · built in {format_duration(built_in)}"' in APP
    assert "Practice set files written in {format_duration(built['seconds'])}." in APP


def test_the_bank_read_keeps_the_real_parse_not_the_cache_hit() -> None:
    """Every widget click re-enters the loader, which then returns instantly."""
    assert "st.session_state.setdefault(bank_key, time.monotonic() - bank_clock)" in APP
    assert 'bank_key = f"dpp_bank_seconds_{bool(verified_only)}"' in APP


def test_a_rebuilt_dpp_set_drops_the_previous_build_time() -> None:
    assert '"slots", "flash", "build_seconds"' in APP


# --------------------------------------------------------------- the tab strip

def test_the_tabs_are_boxed_rather_than_a_row_of_running_text() -> None:
    """Three long labels side by side read as one sentence without this."""
    tabs = APP[APP.index("/* Tabs."):APP.index("hr { border-color: var(--border); }")]
    assert "padding: 0.55rem 1.05rem !important;" in tabs
    assert "border-radius: var(--radius-sm);" in tabs
    assert "gap: 0.5rem;" in tabs
    assert "margin-bottom: 1.1rem;" in tabs
    # The sliding underline fights the boxes, so it is turned off.
    assert '[data-baseweb="tab-highlight"]' in tabs
    assert "display: none !important;" in tabs


def test_the_selected_tab_is_distinguishable_from_the_others() -> None:
    tabs = APP[APP.index("/* Tabs."):APP.index("hr { border-color: var(--border); }")]
    selected = tabs[tabs.index('[data-baseweb="tab"][aria-selected="true"]'):]
    assert "background: var(--brand-soft) !important;" in selected
    assert "border-color: var(--accent) !important;" in selected
