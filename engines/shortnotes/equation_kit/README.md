# equation_kit — the maths/equation engine, packaged to move

Everything this project uses to turn a lecture slide's maths into a real,
editable Word equation. Copy this whole folder into any other project.

**Only dependency:** `python-docx`. No API keys, no config, no network.

```
pip install python-docx
python demo.py          # writes demo_equations.docx — open it in Word
```

---

## 1. What is used to make equations (the full list)

| # | Piece | File | What it does |
|---|-------|------|--------------|
| 1 | **Protected maths tags** | `math_renderer.py` | The contract: `[[MATH_INLINE: latex]]` and `[[MATH_BLOCK: latex]]`. Everything else keys off these. |
| 2 | **The prompt** | `MATH_PROMPT.txt`, `math_prompt.py` | Text that forces the AI to emit those tags with real LaTeX instead of `H2O` / `A x B = C`. |
| 3 | **Tag flattener** | `flatten_math_tags()` | Rebalances nested/unclosed tags. Without it a nested tag leaks `[[MATH_INLINE:` and stray braces into the page, and one dropped `]]` swallows the rest of the document. |
| 4 | **LaTeX parser** | `parse_latex()` | Hand-written tokenizer → node tree. No `latex`, no MathJax, no image rendering — pure Python. |
| 5 | **OMML builder** | `build_omath()` | Node tree → `<m:oMath>`, i.e. a **native Word equation**. Fractions `m:f`, roots `m:rad`, accents `m:acc`, scripts `m:sSub`/`m:sSup`/`m:sSubSup`. Stays editable in Word and survives Save-as-PDF. |
| 6 | **Unicode fallback** | `latex_to_unicode()` | Same LaTeX → `A⃗ × B⃗ = C⃗`, `î → ĵ`, `H₂O`, `SO₄²⁻`, `m s⁻²`. Used when OMML is off or a formula fails to build. |
| 7 | **Symbol tables** | `LATEX_SYMBOLS`, `ACCENT_CHARS`, `SUP_MAP`, `SUB_MAP`, `FUNCTIONS`, `COMMON_FORMULAS` | ~150 LaTeX commands → Unicode, accents (vec/hat/bar/dot), super/subscript glyph maps, upright function names, ~80 whitelisted chemical formulas. |
| 8 | **Equation repair** | `equation_repair.py` | Safety net for when the model ignores the prompt: `A x B = C` → `\vec{A} \times \vec{B} = \vec{C}`, `H2O` → `H_2O`, `m/s2` → `m\,s^{-2}`, `SO4^2-` → `SO_4^{2-}`, `v0` → `v_0`. **Context-gated** — vector rules only fire when the surrounding text says "vector"/"cross product". Every repair is recorded. |
| 9 | **Equation QC** | `equation_quality.py` | Scans the repaired text for maths still written as plain text; emits warnings + `equation_quality_report.json` / `.md`. |
| 10 | **Maths font pin** | `MATH_FONT = "Cambria Math"` | Every maths run is pinned to it. A handwriting font (Kalam) has no combining arrow/hat glyphs — forcing it on an equation is exactly what makes vectors and hats vanish. |

Not included (they belong to this app, not to maths): Mathpix OCR (`src/math_ocr.py`)
sends a slide image to the PW proxy for a high-accuracy transcript, and is only
triggered lazily when a chunk's equations come out weak.

## 2. The prompt

`MATH_PROMPT.txt` holds five ready-to-paste sections:

- **A — long form**: a full `MATH NOTATION [ABSOLUTELY NON-NEGOTIABLE]` block for a prompt template file.
- **B — short form**: one paragraph to append to an existing prompt (`MATH_TAG_RULES`).
- **C — merge rule**: for a second AI pass that must not unwrap existing tags.
- **D — translation rule**: keep LaTeX untranslated in non-English output.
- **E — legacy mode**: plain-text formulas (`^` / `_`) when you do not want tags.

The same text is importable:

```python
from equation_kit import MATH_TAG_RULES, MATH_NOTATION_SECTION, MERGE_MATH_RULE

prompt = my_template + MATH_TAG_RULES + "\n\nSOURCE TEXT:\n" + source_text
```

The core of it:

> Wrap EVERY formula, scripted symbol, chemical formula and unit-with-a-power in a
> protected maths tag. Inline: `[[MATH_INLINE: latex]]`. Displayed: `[[MATH_BLOCK: latex]]`.
> NEVER write a formula as bare text (`A x B = C`, `i -> j -> k`, `H2O`, `CO2`, `ms-2`).
> Never nest one tag inside another; always close every tag with `]]`.
> Outside maths tags use plain prose only — no `$...$`, no code fences, no `*` emphasis.

## 3. Use it in another folder

Copy `equation_kit/` next to your script, then:

```python
from equation_kit import (
    MATH_TAG_RULES, repair_equations, check_equations, write_reports,
    split_math_segments, add_inline_math, add_block_math, latex_to_unicode,
    RENDER_OMML,
)

# 1. prompt the model
prompt = my_prompt + MATH_TAG_RULES
notes  = call_my_ai(prompt)          # any provider — the kit does not care

# 2. rescue formulas the model wrote as plain text
notes, repairs = repair_equations(notes)

# 3. check what is still wrong, and keep an audit trail
report = check_equations(notes)
write_reports(Path("out"), report, repairs)

# 4. render into a DOCX as native Word equations
from docx import Document
from docx.shared import Pt
doc = Document()
for line in notes.splitlines():
    p = doc.add_paragraph()
    for kind, payload in split_math_segments(line):
        if kind == "text":
            p.add_run(payload)
        elif kind == "inline":
            add_inline_math(p, payload, mode=RENDER_OMML, size=Pt(12))
        else:
            add_block_math(doc, payload, mode=RENDER_OMML, size=Pt(12))
doc.save("notes.docx")
```

Not writing a DOCX? `latex_to_unicode()` gives you plain readable text for
HTML, Markdown, terminal or plain-text output.

### Render modes

| Mode | Constant | Result |
|------|----------|--------|
| Native Word equation | `RENDER_OMML` (default) | `<m:oMath>` — editable in Word, correct in PDF |
| Unicode text | `RENDER_UNICODE` | `A⃗ × B⃗ = C⃗`, `H₂O`, `m s⁻²` |
| Raw LaTeX | `RENDER_PLAIN` | `[[MATH_INLINE: ...]]` kept verbatim, for debugging |

`add_inline_math()` returns the mode it actually used, so you can report when a
formula had to fall back.

## 4. Files

```
equation_kit/
├── __init__.py            public API (import from here)
├── math_renderer.py       tags, LaTeX parser, OMML builder, Unicode fallback, symbol tables
├── equation_repair.py     plain-text formulas -> tagged LaTeX (context-gated, logged)
├── equation_quality.py    QC scan + JSON/Markdown reports
├── math_prompt.py         the prompt as importable constants
├── MATH_PROMPT.txt        the prompt as pasteable text
├── demo.py                runnable end-to-end example
├── requirements.txt       python-docx
└── README.md              this file
```

## 5. Changing it

- **Different maths font**: `MATH_FONT` at the top of `math_renderer.py`.
- **More symbols**: add to `LATEX_SYMBOLS` in `math_renderer.py`. Unknown commands
  already degrade gracefully to an upright name rather than breaking.
- **More chemical formulas**: add to `COMMON_FORMULAS` — a whitelist, deliberately,
  so `A4`, `MP3` and `Class 11` are never mangled into subscripts.
- **New repair rule**: add a regex + a `note(...)` call in `equation_repair.py`.
  Keep it context-gated; a rule that fires everywhere will corrupt prose.
