"""Runnable end-to-end demo: messy AI text -> repaired -> QC'd -> DOCX equations.

    pip install python-docx
    python demo.py

Writes demo_equations.docx next to this file. Open it in Word: every formula is
a real, editable Word equation (click one and the equation ribbon appears), and
it survives Save-as-PDF.
"""

import sys
from pathlib import Path

# so `python demo.py` works from anywhere, not just the parent folder
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docx import Document
from docx.shared import Pt, RGBColor

from equation_kit import (
    RENDER_OMML,
    add_block_math,
    add_inline_math,
    check_equations,
    latex_to_unicode,
    repair_equations,
    split_math_segments,
    write_reports,
)

# What an AI typically returns: some formulas correctly tagged, some written as
# bare text (H2O, A x B = C, ms-2) that the repair step must rescue.
RAW_NOTES = r"""Vector Cross Product
- The cross product of two vectors A x B = C gives a vector perpendicular to both.
- Magnitude: [[MATH_INLINE: |\vec{A} \times \vec{B}| = AB\sin\theta]]
- For unit vectors the cyclic order is i -> j -> k.
- Centripetal acceleration: [[MATH_BLOCK: a_n = \frac{v^2}{r}]]
- Free-fall acceleration is 9.8 m/s2 near the surface.

Quadratic Formula
[[MATH_BLOCK: x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}]]

Chemistry
- Water is H2O and the gas released is CO2.
- The sulfate ion is SO4^2-.
- Concentration is measured in mol/L.
"""

BODY = Pt(12)
INK = RGBColor(0x2B, 0x2B, 0x2B)


def main() -> None:
    # the Windows console defaults to cp1252 and cannot print A-with-arrow
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    here = Path(__file__).resolve().parent

    # 1. repair — formulas the model wrote as plain text become tagged LaTeX
    notes, repairs = repair_equations(RAW_NOTES)
    print(f"repairs applied: {len(repairs)}")
    for r in repairs:
        print(f"  line {r['line']:>2}  {r['rule']:<20} {r['before']!r} -> {r['after']!r}")

    # 2. quality check — what is still untagged
    report = check_equations(notes)
    print(f"\ntagged formulas: {report['tagged_formula_count']}   "
          f"remaining issues: {report['issue_count']}   passed: {report['passed']}")
    write_reports(here, report, repairs)   # equation_quality_report.json / .md

    # 3. render to DOCX as native Word equations
    doc = Document()
    stats: dict[str, int] = {}
    for line in notes.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # a line that is nothing but one block tag -> centred display equation
        segments = split_math_segments(stripped)
        if len(segments) == 1 and segments[0][0] == "block":
            _, used = add_block_math(doc, segments[0][1], mode=RENDER_OMML,
                                     size=BODY, color=INK)
            stats[used] = stats.get(used, 0) + 1
            continue
        p = doc.add_paragraph()
        for kind, payload in segments:
            if kind == "text":
                run = p.add_run(payload)
                run.font.size = BODY
                run.font.color.rgb = INK
            else:
                used = add_inline_math(p, payload, mode=RENDER_OMML,
                                       size=BODY, color=INK)
                stats[used] = stats.get(used, 0) + 1

    out = here / "demo_equations.docx"
    doc.save(out)
    print(f"\nwrote {out}")
    print(f"render modes used: {stats}")

    # 4. the no-Word fallback: same LaTeX as readable Unicode
    print("\nUnicode fallback samples:")
    for latex in [r"\vec{A} \times \vec{B} = \vec{C}", r"\hat{i} \rightarrow \hat{j}",
                  "H_2O", "SO_4^{2-}", r"m\,s^{-2}", r"a_n = \frac{v^2}{r}",
                  r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}"]:
        print(f"  {latex:<40} -> {latex_to_unicode(latex)}")


if __name__ == "__main__":
    main()
