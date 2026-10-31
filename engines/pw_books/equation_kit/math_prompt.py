"""The prompt text that makes an AI emit protected maths tags.

Import these constants and append them to whatever prompt you already send.
The same rules live in MATH_PROMPT.txt next to this file, for prompts kept as
plain text templates.

    from equation_kit import MATH_TAG_RULES
    prompt = my_template + MATH_TAG_RULES + "\n\nSOURCE TEXT:\n" + source
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# SHORT FORM — append to an existing prompt (this is what the notes app uses).
# ---------------------------------------------------------------------------
MATH_TAG_RULES = "\n" + r"""- MATHS NOTATION (ABSOLUTELY NON-NEGOTIABLE): Wrap EVERY formula, scripted symbol,
  chemical formula and unit-with-a-power in a protected maths tag. Inline:
  [[MATH_INLINE: latex]]. A displayed equation on its own line: [[MATH_BLOCK: latex]].
  Write real LaTeX inside the tag.
- Vectors keep their arrows: [[MATH_INLINE: \vec{A} \times \vec{B} = \vec{C}]].
  Unit vectors keep their hats: [[MATH_INLINE: \hat{i} \rightarrow \hat{j} \rightarrow \hat{k}]].
  Chemistry keeps subscripts: [[MATH_INLINE: H_2O]], [[MATH_INLINE: CO_2]], [[MATH_INLINE: SO_4^{2-}]].
  Units keep negative powers: [[MATH_INLINE: m\,s^{-2}]], [[MATH_INLINE: mol\,L^{-1}]].
  Scripts are preserved: [[MATH_INLINE: x^2]], [[MATH_INLINE: v_0]], [[MATH_INLINE: a_n = \frac{v^2}{r}]].
- NEVER write a formula as bare text (A x B = C, i -> j -> k, H2O, CO2, ms-2).
  NEVER drop a vector arrow, unit-vector hat, power or subscript. NEVER replace
  \times with a plain 'x' when it means a cross product. NEVER turn a formula
  into words only.
- If the source shows an arrow, hat, bar, dot, subscript, superscript, Greek letter,
  angle, degree, ±, root or fraction, reproduce it EXACTLY as LaTeX inside a maths tag.
- Never nest one maths tag inside another, and always close every tag with ]].
- Outside maths tags use plain prose only — no LaTeX, no $...$, no code fences,
  and no markdown emphasis (* or **).
"""

# ---------------------------------------------------------------------------
# LONG FORM — a standalone section for a prompt template file.
# ---------------------------------------------------------------------------
MATH_NOTATION_SECTION = r"""MATH NOTATION [ABSOLUTELY NON-NEGOTIABLE]
Every formula, symbol, variable with a script, unit with a power, and chemical
formula MUST be wrapped in a protected maths tag. Never write these as plain text.

Inline maths (inside a sentence or bullet):
[[MATH_INLINE: latex_here]]

Displayed maths (a standalone equation on its own line):
[[MATH_BLOCK:
latex_here
]]

Write correct LaTeX inside the tags:
- Vectors: [[MATH_INLINE: \vec{A}]], [[MATH_INLINE: \vec{B}]], [[MATH_INLINE: \vec{C}]]
- Cross product: [[MATH_INLINE: \vec{A} \times \vec{B} = \vec{C}]]
- Unit vectors: [[MATH_INLINE: \hat{i}, \hat{j}, \hat{k}]]
- Cyclic order: [[MATH_INLINE: \hat{i} \rightarrow \hat{j} \rightarrow \hat{k}]]
- Dot product: [[MATH_INLINE: \vec{A} \cdot \vec{B} = AB\cos\theta]]
- Magnitude: [[MATH_INLINE: |\vec{A} \times \vec{B}| = AB\sin\theta]]
- Superscript: [[MATH_INLINE: x^2]]
- Subscript: [[MATH_INLINE: v_0]]
- Combined: [[MATH_INLINE: a_n = \frac{v^2}{r}]]
- Fraction / root: [[MATH_BLOCK: x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}]]
- Chemistry: [[MATH_INLINE: H_2O]], [[MATH_INLINE: CO_2]], [[MATH_INLINE: SO_4^{2-}]]
- Units: [[MATH_INLINE: m\,s^{-2}]], [[MATH_INLINE: mol\,L^{-1}]]

NEVER output these:
A x B = C
i -> j -> k
H2O
CO2
ms-2

ALWAYS output these instead:
[[MATH_INLINE: \vec{A} \times \vec{B} = \vec{C}]]
[[MATH_INLINE: \hat{i} \rightarrow \hat{j} \rightarrow \hat{k}]]
[[MATH_INLINE: H_2O]]
[[MATH_INLINE: CO_2]]
[[MATH_INLINE: m\,s^{-2}]]

Preserve the source notation
If the slide/page shows a vector arrow, cap/hat, bar, dot, subscript, superscript,
Greek symbol, angle, degree, plus/minus, root or fraction - reproduce it EXACTLY
using LaTeX inside a maths tag.

Do not simplify equations
- Do not turn a formula into words only.
- Do not drop vector arrows or unit-vector hats.
- Do not drop powers or subscripts.
- Do not replace \times with a plain "x" when it means a cross product.

Tag hygiene (this is what breaks documents when ignored)
- NEVER nest one maths tag inside another. One formula = exactly ONE tag.
  WRONG: [[MATH_BLOCK: x = \frac{-b \pm \sqrt{[[MATH_INLINE: b^2-4ac]]}}{2a}]]
  RIGHT: [[MATH_BLOCK: x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}]]
- ALWAYS close a tag with ]] on the same line (INLINE) or before the next blank
  line (BLOCK). An unclosed tag swallows the rest of the document.
- Outside maths tags use plain prose only - no LaTeX, no $...$, no \( \),
  no code fences, and no markdown emphasis (* or **).
"""

# ---------------------------------------------------------------------------
# Second-pass rules.
# ---------------------------------------------------------------------------
MERGE_MATH_RULE = (
    "\n- Keep every [[MATH_INLINE: ...]] and [[MATH_BLOCK: ...]] tag EXACTLY as written, "
    "including the LaTeX inside it. Never unwrap a maths tag into plain text, and never drop "
    "a vector arrow, hat, power or subscript while merging."
)

TRANSLATION_MATH_RULE = (
    "\n- Keep the contents of every [[MATH_INLINE: ...]] / [[MATH_BLOCK: ...]] tag as LaTeX, "
    "never translated. Chemical formulas, numbers, units and symbols stay in their original form."
)

# Legacy mode: no tags, plain-text formulas normalised at write time.
PLAIN_MATH_RULES = "\n" + r"""- FORMULAS: Write formulas in plain text. Use '_' for subscripts and '^' for
  superscripts (e.g. v_AB = v_A - v_B, v_A^2, x_{net}). Write symbols directly:
  √ × ÷ ≤ ≥ ± θ α β Δ π. Never use $...$, code fences, or markdown emphasis
  (* or **) inside a formula — an asterisk is only ever multiplication.
"""

PROMPT_FILE = Path(__file__).with_name("MATH_PROMPT.txt")


def load_prompt_file() -> str:
    """The full MATH_PROMPT.txt as text (all sections)."""
    return PROMPT_FILE.read_text(encoding="utf-8")


__all__ = [
    "MATH_TAG_RULES",
    "MATH_NOTATION_SECTION",
    "MERGE_MATH_RULE",
    "TRANSLATION_MATH_RULE",
    "PLAIN_MATH_RULES",
    "PROMPT_FILE",
    "load_prompt_file",
]
