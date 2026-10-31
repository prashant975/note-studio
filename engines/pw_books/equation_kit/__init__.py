"""equation_kit — portable maths/equation engine.

Drop this folder next to your script and import it:

    from equation_kit import (
        MATH_TAG_RULES,          # prompt text: makes the AI emit [[MATH_...]] tags
        repair_equations,        # safety net: plain-text formulas -> tagged LaTeX
        check_equations,         # QC: what is still untagged
        add_inline_math,         # LaTeX -> real Word equation (OMML) in a paragraph
        add_block_math,          # LaTeX -> centred display equation
        latex_to_unicode,        # LaTeX -> A⃗, î, H₂O, m s⁻²  (no-Word fallback)
    )

Only dependency: python-docx (pip install python-docx). Nothing here calls an
API, reads config, or touches the network.

Pipeline order that the parent app uses, and that you should reuse:

    1. prompt  += MATH_TAG_RULES                  -> AI writes [[MATH_INLINE: ...]]
    2. text, repairs = repair_equations(text)     -> rescue untagged formulas
    3. report = check_equations(text)             -> count what is still wrong
    4. write_reports(run_dir, report, repairs)    -> JSON + Markdown audit trail
    5. add_inline_math / add_block_math           -> native Word equations in DOCX
"""

from .math_renderer import (
    ACCENT_CHARS,
    COMMON_FORMULAS,
    FUNCTIONS,
    LATEX_SYMBOLS,
    MATH_FONT,
    MATH_TAG_RE,
    RENDER_MODES,
    RENDER_OMML,
    RENDER_PLAIN,
    RENDER_UNICODE,
    SUB_MAP,
    SUP_MAP,
    accent,
    add_block_math,
    add_inline_math,
    build_omath,
    flatten_math_tags,
    has_math_tag,
    latex_to_unicode,
    parse_latex,
    split_math_segments,
    strip_math_tags,
)
from .equation_repair import repair_equations
from .equation_quality import check_equations, warnings_from_report, write_reports
from .math_prompt import (
    MATH_NOTATION_SECTION,
    MATH_TAG_RULES,
    MERGE_MATH_RULE,
    PLAIN_MATH_RULES,
    TRANSLATION_MATH_RULE,
    load_prompt_file,
)

__version__ = "1.0.0"

__all__ = [
    # prompt
    "MATH_TAG_RULES", "MATH_NOTATION_SECTION", "MERGE_MATH_RULE",
    "TRANSLATION_MATH_RULE", "PLAIN_MATH_RULES", "load_prompt_file",
    # tags
    "MATH_TAG_RE", "split_math_segments", "has_math_tag", "strip_math_tags",
    "flatten_math_tags",
    # repair + QC
    "repair_equations", "check_equations", "warnings_from_report", "write_reports",
    # rendering
    "RENDER_OMML", "RENDER_UNICODE", "RENDER_PLAIN", "RENDER_MODES", "MATH_FONT",
    "add_inline_math", "add_block_math", "build_omath", "latex_to_unicode",
    "parse_latex", "accent",
    # tables
    "LATEX_SYMBOLS", "ACCENT_CHARS", "COMMON_FORMULAS", "FUNCTIONS",
    "SUP_MAP", "SUB_MAP",
]
