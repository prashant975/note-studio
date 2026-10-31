"""End-to-end smoke test: build a fake lecture PDF -> Gemini -> notes PDF.

Uses the real GEMINI_API_KEY from .env. Costs one Gemini call.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpdf import FPDF

from notesbot import config
from notesbot.gemini_client import generate_short_notes
from notesbot.notes_pdf import build_notes_pdf


def make_sample_lecture_pdf() -> bytes:
    pdf = FPDF()
    # Front page (metadata Gemini should extract)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 28)
    pdf.ln(40)
    pdf.cell(0, 16, "YAKEEN NEET 2026", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 18)
    pdf.cell(0, 12, "Lecture 12 : Chemical Bonding", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 12, "Subject: Chemistry", align="C", new_x="LMARGIN", new_y="NEXT")
    # Content page
    pdf.add_page()
    pdf.set_font("Helvetica", "", 12)
    body = (
        "Chemical Bonding - Class Notes\n\n"
        "An ionic bond forms by transfer of electrons from a metal to a non-metal, "
        "for example NaCl. Lattice energy increases with higher ionic charge and "
        "smaller ionic radius. Fajan's rules: a small cation and a large anion "
        "increase covalent character.\n\n"
        "A covalent bond is formed by sharing of electron pairs. Bond order equals "
        "(Nb - Na)/2 where Nb and Na are bonding and antibonding electrons. "
        "For water, 2H2 + O2 gives 2H2O and the reaction is exothermic.\n\n"
        "VSEPR theory predicts geometry. sp hybridisation gives 180 degrees (linear), "
        "sp2 gives 120 degrees (trigonal planar), sp3 gives 109.5 degrees (tetrahedral). "
        "CO2 is linear and non-polar while H2O is bent and polar.\n\n"
        "Dipole moment mu = q x d, measured in Debye. The dipole moment of NH3 is "
        "greater than NF3 because in NH3 the lone pair moment adds to the bond moments, "
        "but in NF3 it opposes them.\n\n"
        "Hydrogen bonding occurs in HF, H2O and NH3 and strongly affects boiling points."
    )
    pdf.multi_cell(0, 7, body)
    return bytes(pdf.output())


def main() -> None:
    missing = config.missing_required()
    if missing:
        sys.exit(f"Missing env vars: {missing}. Put them in .env first.")

    print(f"Model: {config.GEMINI_MODEL} | Language: {config.NOTES_LANGUAGE}")
    print("Building sample lecture PDF ...")
    lecture_pdf = make_sample_lecture_pdf()
    print(f"  sample lecture: {len(lecture_pdf)} bytes")

    print("Calling Gemini (this can take 20-60s) ...")
    notes = generate_short_notes(lecture_pdf)
    print("  lecture_no :", repr(notes.lecture_no))
    print("  subject    :", repr(notes.subject))
    print("  title      :", repr(notes.title))
    print("  sections   :", len(notes.sections))
    for s in notes.sections:
        extras = []
        if s.table:
            extras.append(f"table {len(s.table.headers)}x{len(s.table.rows)}")
        if s.flow:
            extras.append(f"flow {len(s.flow)}")
        extra = (" + " + ", ".join(extras)) if extras else ""
        print(f"    - {s.heading} ({len(s.points)} points{extra})")

    out_pdf = build_notes_pdf(notes, config.TEACHER_SIGNATURE)
    out = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out_e2e.pdf"
    )
    with open(out, "wb") as fh:
        fh.write(out_pdf)
    assert out_pdf[:4] == b"%PDF"
    print(f"\nOK: wrote {out} ({len(out_pdf)} bytes)")


if __name__ == "__main__":
    main()
