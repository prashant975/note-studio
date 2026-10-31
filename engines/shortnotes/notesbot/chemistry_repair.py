"""Narrow, deterministic repairs for user-reported chemistry mistakes.

These are explicit corrections to existing content, not a general chemistry
engine. In particular, PbCl2(aq) is valid in dissolution contexts and must not
be changed globally.
"""
from __future__ import annotations

import re


CHEMISTRY_QUALITY_RULES = r"""
CHEMICAL FORMULAS, BOND SYMBOLS AND REACTION STATES
- Keep atom symbols, subscripts, charges, coefficients and reaction arrows
  intact. Check each reaction's own state labels against its source; never
  carry (aq) forward from the preceding reaction or assume every salt is aqueous.
- A standalone single/double/triple covalent bond symbol is -, =, or \equiv.
  Keep a bond legend separate from a molecular structure: a double-bond symbol
  is $=$, while a complete oxygen structure is $O=O$. Never write $=O$ as the
  standalone double-covalent-bond symbol after an $O_2$ molecule description.
- For the ordinary lead/copper chloride displacement already taught in the
  notes, use $Pb(s)+CuCl_2(aq)\rightarrow PbCl_2(s)+Cu(s)$: the lead chloride
  product is solid, not aqueous. Do not globally change PbCl2(aq); retain
  dissolution/equilibrium equations and explicitly taught solution conditions.
- These two user-reported corrections (the extra O in a standalone bond legend
  and PbCl2's state in this displacement) are explicit exceptions to source-only
  copying. Apply them to matching existing teaching even if a slide contains
  the typo. Do not add these topics or reactions when absent from the lecture.
"""

_DOUBLE_BOND_CONTEXT = re.compile(
    r"\bdouble\s+covalent\s+bond\b|\bdouble\s+bond\s+(?:symbol|notation|represented)\b"
    r"|\b(?:two|2)\s+pairs\s+of\s+electrons\b"
    r"|(?:द्वि|दोहरा|डबल)\s*(?:सहसंयोजक\s*)?(?:बंध|आबंध|बन्ध)"
    r"|दो\s*(?:जोड़े|युग्म)\s*(?:इलेक्ट्रॉनों|इलेक्ट्रॉन)",
    re.I,
)
_MATH = re.compile(r"\$([^$]*)\$")
_LATEX_TOKENS = re.compile(
    r"\\(?:mathrm|mathbf|mathit|text(?:rm|bf|it)?|ce|left|right)\b"
    r"|\\(?:longrightarrow|rightarrow|to)\b|->|[→⟶]"
    r"|\\[,;!: ]|[{}_$*]"
)
_SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")


def _canonical(text: str) -> tuple[str, list[tuple[int, int]]]:
    """A matching view with offsets back to the original formula's characters.

    Ignore only presentation wrappers. Preserve charge, condition and
    equilibrium symbols so a different reaction cannot match this repair.
    """
    chars: list[str] = []
    offsets: list[tuple[int, int]] = []
    end = 0
    for token in _LATEX_TOKENS.finditer(text):
        for index in range(end, token.start()):
            chars.append(text[index].translate(_SUBSCRIPTS))
            offsets.append((index, index + 1))
        if token[0] in (r"\rightarrow", r"\longrightarrow", r"\to", "->", "→", "⟶"):
            chars.append("→")
            offsets.append((token.start(), token.end()))
        end = token.end()
    for index in range(end, len(text)):
        chars.append(text[index].translate(_SUBSCRIPTS))
        offsets.append((index, index + 1))
    return "".join(chars), offsets


_LEAD = r"Pb\s*\(\s*s\s*\)"
_COPPER_CHLORIDE = r"Cu\s*Cl\s*2\s*\(\s*aq\s*\)"
_COPPER = r"Cu\s*\(\s*s\s*\)"
_LEAD_CHLORIDE = r"Pb\s*Cl\s*2\s*\(\s*(?P<{phase}>aq)\s*\)"
_LEAD_DISPLACEMENT = re.compile(
    rf"(?<![A-Za-z0-9])(?:{_LEAD}\s*\+\s*{_COPPER_CHLORIDE}"
    rf"|{_COPPER_CHLORIDE}\s*\+\s*{_LEAD})\s*→\s*"
    rf"(?:{_LEAD_CHLORIDE.format(phase='first')}\s*\+\s*{_COPPER}"
    rf"|{_COPPER}\s*\+\s*{_LEAD_CHLORIDE.format(phase='last')})"
    r"(?![A-Za-z0-9]|\s*\+)",
)


def _repair_bond_legend(text: str) -> str:
    if not _DOUBLE_BOND_CONTEXT.search(text):
        return text

    def bond(match):
        expression, _ = _canonical(match[1])
        compact = re.sub(r"\s+", "", expression)
        if compact == "=O":
            return "$=$"
        if compact == "(=O)":
            return "$(=)$"
        return match[0]

    # Handle legacy prose notation too, without changing complete O=O / C=O
    # structures, formula variables, or atom labels in a schematic.
    parts = re.split(r"(\$[^$]*\$)", _MATH.sub(bond, text))
    return "".join(
        part if part.startswith("$") else re.sub(r"\(\s*=\s*O\s*\)", "(=)", part)
        for part in parts
    )


def _repair_lead_displacement(text: str) -> str:
    if "Pb" not in text or "Cu" not in text:
        return text
    canonical, offsets = _canonical(text)
    replacements = []
    for reaction in _LEAD_DISPLACEMENT.finditer(canonical):
        phase = "first" if reaction.group("first") is not None else "last"
        start, end = reaction.span(phase)
        replacements.append((offsets[start][0], offsets[end - 1][1]))
    # Replace only the product's state letters, preserving every formula,
    # wrapper, coefficient, arrow and neighbouring equation byte for byte.
    for start, end in reversed(replacements):
        text = text[:start] + "s" + text[end:]
    return text


def normalize_chemistry_notation(text: str) -> str:
    return _repair_lead_displacement(_repair_bond_legend(text))


def chemistry_findings(text: str) -> list[str]:
    """Report known defects even when their LaTeX renders successfully."""
    findings = []
    if _repair_bond_legend(text) != text:
        findings.append("Standalone double-covalent-bond symbol contains an extra O.")
    if _repair_lead_displacement(text) != text:
        findings.append("Lead/copper chloride displacement has PbCl2(aq); the product must be PbCl2(s).")
    return findings
