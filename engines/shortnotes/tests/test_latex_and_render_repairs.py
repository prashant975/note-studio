"""Regression tests for LaTeX repair / render text-cleanup edge cases.

Each case here reproduced a real defect seen in a generated notes PDF.
"""
from notesbot.latex_repair import autowrap_math, repair_text
from notesbot.math_render import normalize_latex
from notesbot.notes_pdf import _emph_runs


def _fix(text: str) -> str:
    return autowrap_math(repair_text(text))


# --- "pm" is the picometre unit, never plus-minus (Bohr radius bug) ---------

def test_pm_unit_survives_math_normalization():
    assert normalize_latex(r"r = 52.9 \frac{n^2}{Z} \, pm").endswith("pm")
    assert normalize_latex("1 pm = 10^{-12} m").startswith("1 pm")
    assert normalize_latex("100 pm") == "100 pm"


def test_real_plus_minus_forms_still_render():
    assert r"\pm" in normalize_latex("5 ± 2")
    assert normalize_latex(r"5 \pm 2") == r"5 \pm 2"


# --- spelled-out arrows outside $...$ become real symbols -------------------

def test_spelled_arrow_words_in_prose_become_math():
    fixed = _fix("T uparrow implies lambda downarrow, $ν ↑$")
    assert "$\\uparrow$" in fixed
    assert "$\\lambda$" in fixed
    assert "$\\downarrow$" in fixed
    assert "uparrow$" not in fixed.replace("$\\uparrow$", "")


def test_wrap_of_bare_commands_never_nests_dollar_spans():
    # Model wrote LaTeX commands without $...$: the auto-wrap swallows the
    # prose into one span; spelled-out words inside it must NOT be wrapped
    # again (nested $ broke the renderer and printed literal words).
    fixed = _fix("T uparrow implies lambda downarrow, \\nu \\uparrow")
    assert fixed.count("$") == 2
    assert fixed.startswith("$") and fixed.endswith("$")
    normalized = normalize_latex(fixed.strip("$"))
    assert "uparrow" not in normalized.replace("\\uparrow", "")
    assert "\\lambda" in normalized and "\\Rightarrow" in normalized


def test_unicode_nu_and_arrows_map_inside_math():
    normalized = normalize_latex("ν ↑ ↓ ⇒")
    assert "\\nu" in normalized
    assert "\\uparrow" in normalized
    assert "\\downarrow" in normalized
    assert "\\Rightarrow" in normalized


# --- unpaired ** markers must never print as literal asterisks --------------

def test_emph_runs_keep_paired_highlights():
    runs = _emph_runs("colour changes to **blue** finally.")
    assert ("blue", True) in runs
    assert all("**" not in segment for segment, _emph in runs)


def test_emph_runs_strip_unpaired_marker():
    runs = _emph_runs("colour changes to blue**.")
    assert runs == [("colour changes to blue.", False)]
    assert _emph_runs("**broken start here") == [("broken start here", False)]
