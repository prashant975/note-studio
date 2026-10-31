"""Gujarati formula labels must never reach mathtext's missing-glyph fallback."""
import io
import inspect

import pytest
from PIL import Image

from note_studio import gujarati_math, shortnotes_extensions


@pytest.mark.parametrize("latex", [
    r"\frac{\text{ભાગીદારના નફાનો ભાગ}}{\text{કુલ નફાના ભાગ}}",
    r"\text{કમિશન}=\text{નફો}\times\frac{\text{કમિશનનો દર}}{100+\text{કમિશનનો દર}}",
    r"\frac{\text{શરૂની મૂડી}\times\text{વ્યાજ દર}\times\text{સમયગાળો}}{100}",
    r"\frac{\text{નફો}}{\frac{\text{દર}}{100}}",
])
def test_gujarati_labels_are_shaped_separately_from_math_symbols(client, latex, caplog):
    from notesbot import math_render

    original = inspect.getclosurevars(math_render.render_latex).nonlocals["original"]
    calls = []

    def symbols(text, size):
        assert not gujarati_math._GUJARATI.search(text)
        calls.append(text)
        return original(text, size)

    png, width, height = gujarati_math.render(latex, 13.37, shortnotes_extensions.FONT_DIR / "BalooBhai2-Regular.ttf",
                                             symbols, math_render.normalize_latex)
    image = Image.open(io.BytesIO(png))
    assert width > 0 and height > 0 and image.mode == "RGBA"
    assert image.getextrema()[-1][0] == 0 and image.getextrema()[-1][1] > 0
    assert not any("dummy symbol" in record.message.lower() or "does not have a glyph" in record.message.lower()
                   for record in caplog.records)


@pytest.mark.parametrize("latex", [r"I=\frac{P\times R\times N}{100}", r"\frac{\text{total distance}}{\text{time interval}}"])
def test_existing_formula_pixels_and_metrics_are_unchanged(client, latex):
    from notesbot import math_render

    original = inspect.getclosurevars(math_render.render_latex).nonlocals["original"]
    assert math_render.render_latex(latex, 14) == original(latex, 14)


def test_bad_gujarati_math_is_rejected_instead_of_rendering_dummy_symbols(client):
    from notesbot.math_render import render_latex

    with pytest.raises(ValueError):
        render_latex(r"\frac{\text{નફો}}{\text{દર}", 14)


def test_gujarati_fraction_labels_pass_the_real_pdf_and_local_formula_checks(client):
    from notesbot.prompts import ShortNotes, Section
    from notesbot.final_quality import local_findings
    from sheets_bridge.generator import render_artifacts

    notes = ShortNotes(lecture_no="", output_language="gujarati", title="ભાગીદારી", subject="હિસાબશાસ્ત્ર",
        sections=[Section(heading="નફાની વહેંચણી", points=[
            r"ભાગીદારનો નફો = કુલ નફો $\times\frac{\text{ભાગીદારના નફાનો ભાગ}}{\text{કુલ નફાના ભાગ}}$.",
            r"કમિશનનું સૂત્ર: $\text{કમિશન}=\text{નફો}\times\frac{\text{કમિશનનો દર}}{100}$.",
        ])])
    pdf = render_artifacts(notes, signature="by Teacher", language="gujarati")
    count, issues = local_findings(notes, pdf, "gujarati")
    assert count > 0 and not issues, issues
