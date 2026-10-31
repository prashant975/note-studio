"""Offline smoke test: styled render — banners, icons, emphasis, formula box."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notesbot.latex_repair import repair_text
from notesbot.prompts import Branch, Curve, Graph, MindMap, Section, ShortNotes, Table
from notesbot.notes_pdf import build_notes_pdf


def _daniell_cell_fixture() -> bytes:
    """Load the polished hand-drawn asset used for visual-layout smoke checks."""
    asset = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "diagrams"
        / "daniell-cell-reference.png"
    )
    return asset.read_bytes()

# --- 1) JSON-mangling repair check (the bug seen in production) -------------
mangled = json.loads(
    r'"$NaCl(l) \rightarrow Na(s) + \frac{1}{2}Cl_2(g)$ ; $\triangle G < 0$"'
)
fixed = repair_text(mangled)
assert "\\rightarrow" in fixed and "\\frac" in fixed and "\\triangle" in fixed, fixed
print("repair OK:", fixed)

# --- 2) Full render across every feature ------------------------------------
notes = ShortNotes(
    lecture_no="Lecture No.-10",
    subject="Chemistry",
    title="Electrochemistry: Electrolysis",
    sections=[
        Section(
            heading="Electrolysis of Molten Salts", icon="battery",
            points=[
                fixed,  # repaired line renders as proper maths
                r"Cathode $(-)$: $Na^+ + e^- \rightarrow Na(s)$ (**Reduction**).",
                r"Molten $PbCl_2$: $Pb(s)$ at **cathode**, $Cl_2(g)$ at **anode**.",
                r"$E_{cell} = \frac{l}{L} \times E$",
            ],
            tricks=[
                r"Trick: PANIC — Positive Anode, Negative Is Cathode (electrolytic cell).",
                r"Shortcut: products at electrodes? Pehle SRP table dekho, phir overvoltage.",
            ],
        ),
        Section(
            heading="Electrolytic vs Galvanic Cell", icon="balance",
            table=Table(
                headers=["Property", "Electrolytic", "Galvanic"],
                rows=[
                    ["Spontaneity", r"$\Delta G > 0$", r"$\Delta G < 0$"],
                    [r"$E_{cell}$", r"$< 0$", r"$> 0$"],
                    ["Anode", "$(+)$", "$(-)$"],
                    ["Salt bridge", "No", "Yes"],
                ],
            ),
        ),
        Section(
            heading="Hybridisation & Geometry", icon="atom",
            table=Table(
                headers=["Type", "Geometry", "Angle"],
                rows=[
                    ["sp", "Linear", r"$180^\circ$"],
                    [r"$sp^2$", "Trigonal planar", r"$120^\circ$"],
                    [r"$sp^3$", "Tetrahedral", r"$109.5^\circ$"],
                ],
            ),
        ),
        Section(
            heading="Electrolysis Process", icon="gear",
            flow=[
                "Molten salt",
                "Apply DC current",
                r"Cations $\rightarrow$ cathode",
                "Reduction at cathode",
                r"$Na^+ + e^- \rightarrow Na$",
            ],
        ),
        Section(
            heading="Conductivity (word-form auto-fix)", icon="bolt",
            points=[
                r"Molar conductivity $lambda_m^circ$ rises on **dilution**.",
                r"Kohlrausch: $Lambda_m^circ = lambda^circ_+ + lambda^circ_-$.",
            ],
            tricks=[
                r"Star point: weak electrolyte ka $\Lambda_m^\circ$ extrapolation se NAHI milta — Kohlrausch lagao.",
            ],
            graph=Graph(
                title="Molar conductivity vs dilution",
                x_label=r"$\sqrt{c}$",
                y_label=r"$\Lambda_m$",
                x_min=0.0,
                x_max=1.0,
                curves=[
                    Curve(label="Strong electrolyte (KCl)",
                          expression="150 - 60*x"),
                    Curve(label="Weak electrolyte (CH3COOH)",
                          expression="10 + 140*exp(-14*x)"),
                ],
            ),
        ),
        Section(
            heading="Maxwell-Boltzmann Distribution", icon="fire",
            graph=Graph(
                x_label="Molecular speed",
                y_label="Fraction of molecules",
                x_min=0.0,
                x_max=5.0,
                curves=[
                    Curve(label="T1 (low T)", expression="x**2 * exp(-x**2/1.5)"),
                    Curve(label="T2 (high T)", expression="0.6 * x**2 * exp(-x**2/4)"),
                ],
            ),
        ),
    ],
)

notes.mindmap = MindMap(
    center="Electrolysis",
    branches=[
        Branch(label="Fundamentals", children=[
            "Non-spontaneous", r"$\Delta G > 0$", "Anode $(+)$ oxidation"]),
        Branch(label="Molten Salts", children=[
            r"$Na^+ \rightarrow Na(s)$", r"$Cl_2$ at anode", "PANIC trick"]),
        Branch(label="Cell Types", children=[
            "Electrolytic vs Galvanic", "Salt bridge only galvanic"]),
        Branch(label="Conductivity", children=[
            r"$\Lambda_m^\circ$ Kohlrausch", r"Weak: no extrapolation",
            r"$\Lambda_m$ vs $\sqrt{c}$"]),
        Branch(label="Discharge Order", children=[
            r"$I^- > Br^- > Cl^-$", "Higher SRP wins cathode"]),
        Branch(label="Hybridisation", children=[
            r"sp $180^\circ$", r"$sp^2$ $120^\circ$", r"$sp^3$ $109.5^\circ$"]),
    ],
)

visual_section = Section(
    heading="Daniell Cell Setup",
    icon="battery",
    points=[
        "**Zinc** works as the anode.",
        "Electrons flow from zinc towards **copper**.",
    ],
)
object.__setattr__(visual_section, "_image", _daniell_cell_fixture())
notes.sections.insert(1, visual_section)

pdf_bytes = build_notes_pdf(notes, "by PW Faculty")
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out_test.pdf")
with open(out, "wb") as fh:
    fh.write(pdf_bytes)
assert pdf_bytes[:4] == b"%PDF", "not a valid PDF"
print(f"OK: wrote {out} ({len(pdf_bytes)} bytes)")
