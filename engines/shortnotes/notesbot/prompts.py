"""Gemini prompt + structured-output schema for short-notes generation."""
from __future__ import annotations

import json
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from . import categories, generation_profile
from .visual_manifest import VisualManifest
from .concise_policy import CONCISE_POLICY
from .chemistry_repair import CHEMISTRY_QUALITY_RULES


# --------------------------------------------------------------------------- #
# Structured output schema. Gemini is forced to return exactly this shape so
# the PDF renderer can lay it out cleanly (no fragile free-text parsing).
# --------------------------------------------------------------------------- #
class Table(BaseModel):
    headers: List[str] = Field(
        description="Every PDF-taught column header; prefer 2-4 short columns."
    )
    rows: List[List[str]] = Field(
        description=(
            "Every taught row and cell, source-only text with its $LaTeX$."
        )
    )


class Curve(BaseModel):
    label: str = Field(
        default="", description="Curve label copied from the PDF; empty if not stated."
    )
    expression: str = Field(
        default="",
        description=(
            "Internal y(x) needed only to reproduce a curve explicitly taught "
            "in the PDF; never create a related curve. Allowed: x, numbers, "
            "+ - * / ** ( ), and sin cos tan exp log log10 sqrt abs pi e."
        ),
    )
    points: List[List[float]] = Field(
        default_factory=list,
        description=(
            "Alternative to expression: source-supported [x, y] points sufficient "
            "to reproduce the taught curve; never invent points to meet a count."
        ),
    )


class Graph(BaseModel):
    title: str = Field(default="", description="PDF's graph title; empty if absent.")
    x_label: str = Field(default="", description="PDF's x-axis label, may contain $LaTeX$.")
    y_label: str = Field(default="", description="PDF's y-axis label, may contain $LaTeX$.")
    x_min: float = Field(default=0.0)
    x_max: float = Field(default=10.0)
    qualitative: bool = Field(default=False, description="True for a taught shape without numerical data. Hide numeric ticks; coordinates specify shape only, not measured values.")
    curves: List[Curve] = Field(
        default_factory=list,
        description="Every PDF-taught curve with its label.",
    )


class DiagramPoint(BaseModel):
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)


class DiagramStroke(BaseModel):
    kind: Literal["line", "arrow", "ellipse"] = "line"
    points: List[DiagramPoint] = Field(min_length=2, max_length=30, description="Polyline vertices; arrowhead at last vertex. For ellipse use exactly two opposite bounding corners.")
    dashed: bool = False


class DiagramLabel(BaseModel):
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)
    text: str = Field(min_length=1, max_length=100)


class Schematic(BaseModel):
    caption: str = Field(default="", max_length=160)
    strokes: List[DiagramStroke] = Field(min_length=1, max_length=60)
    labels: List[DiagramLabel] = Field(default_factory=list, max_length=30)


class WorkedExample(BaseModel):
    kind: Literal["example", "proof"] = "example"
    statement: str = Field(description="Complete source question or theorem, including given matrices, assumptions and what to find/prove. In the requested language.")
    steps: List[str] = Field(default_factory=list, description="Ordered source solution/proof steps, one meaningful step per entry. Keep equations and reasons together; never invent missing work.")
    result: str = Field(default="", description="Source-stated answer or proof conclusion; empty if absent. In the requested language.")


class Section(BaseModel):
    heading: str = Field(
        description="Non-empty source-taught topic heading; prefer 2-5 words."
    )
    source_local_page: int = Field(
        default=0,
        exclude=True,
        description=(
            "One-based page inside the attached PDF chunk where the teacher "
            "first teaches this section. Active sequence generation must set "
            "a value of one or higher."
        ),
    )
    source_order: int = Field(
        default=0,
        exclude=True,
        description=(
            "Zero-based teaching order among sections first taught on the same "
            "source page."
        ),
    )
    visual_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Ordered IDs copied only from the supplied immutable source-visual "
            "manifest. Never invent, duplicate, reorder, or rewrite an ID."
        ),
    )
    points: List[str] = Field(
        default_factory=list,
        description=(
            "Crisp source-only bullets (~12 words each) for every explicitly "
            "taught definition, formula, condition, or exception. A Where "
            "legend, visual, or mind-map branch never replaces theory here. "
            "Use one main-body representation per fact; do not duplicate a "
            "definition already given in key_terms or a complete table/flow. "
            "Never add a textbook fact."
        ),
    )
    tricks: List[str] = Field(
        default_factory=list,
        description=(
            "STAR points: every trick, shortcut, jugaad, mnemonic, 'yaad "
            "rakhna', warning or exam hack explicitly taught in this section "
            "— never invent or omit one; keep source detail and $LaTeX$."
        ),
    )
    worked_examples: List[WorkedExample] = Field(
        default_factory=list,
        description="Legacy field: always return an empty list for concise theory notes. Extract unique general teaching into theory fields; omit all questions and solutions.",
    )
    table: Optional[Table] = Field(
        default=None,
        description=(
            "Use only for a comparison, trend, series or property list explicitly "
            "present in the PDF, instead of duplicating it in bullets."
        ),
    )
    flow: List[str] = Field(
        default_factory=list,
        description=(
            "Every source-taught process/sequence step (1-5 words each); "
            "preserve taught $LaTeX$. Do not repeat the same process in bullets "
            "or turn a worked solution into a second flow."
        ),
    )
    graph: Optional[Graph] = Field(
        default=None,
        description=(
            "Use when the source explicitly describes or plots a curve/trend: "
            "copy its axes, curves, and labels faithfully from the PDF. Never "
            "invent a plot the source does not teach."
        ),
    )
    schematic: Optional[Schematic] = Field(default=None, description="Essential source-taught diagram (rays, geometry, circuit, labelled structure). Coordinates 0-100, origin TOP LEFT. Preserve taught geometry, arrows and labels; null if unnecessary. Rendered locally in the PDF, without an image service.")
    icon: str = Field(
        default="",
        description=(
            "Small margin doodle for this section — pick the MOST topic-related "
            "one of: flask, test_tube, atom, bulb, battery, bolt, drop, fire, "
            "magnet, book, gear, balance, star, heart_ecg, scientist, doctor, "
            "microscope, dna, rocket, globe. Empty if none fits."
        ),
    )
    where: List[WhereItem] = Field(
        default_factory=list,
        description=(
            "Symbol meanings explicitly defined in the PDF for this section's "
            "formula. Never infer meanings or units; empty if not stated."
        ),
    )
    notes: List[str] = Field(
        default_factory=list,
        description=(
            "Every explicitly source-taught context/exception/caution callout. "
            "Prefer at most 2 per visual group as a layout preference, but add "
            "overflow callouts rather than dropping them. Never add helpful "
            "context from general knowledge."
        ),
    )
    compare: Optional[Compare] = Field(
        default=None,
        description=(
            "Use only when the PDF explicitly compares exactly two things. "
            "Rendered as two pastel columns; never construct an adjacent comparison."
        ),
    )
    key_terms: List[str] = Field(
        default_factory=list,
        description=(
            "Every 'term : meaning' one-liner explicitly defined in this PDF "
            "section, rendered in one or more KEY TERMS boxes. Prefer 5 per box "
            "as a layout preference; overflow rather than omit. Never supply a "
            "textbook definition or inferred meaning; empty if none is taught. "
            "Choose this box OR a defining bullet, never both for the same meaning."
        ),
    )
    diagram: str = Field(
        default="",
        description=(
            "Legacy read-compatibility field only. New generation must leave "
            "this empty and link immutable source visuals through visual_ids."
        ),
    )
    visual: Optional[Visual] = Field(
        default=None,
        description=(
            "The ONE spatial core diagram this section explicitly draws or "
            "teaches in the PDF, if any: set kind='core_diagram', its exact "
            "source page, ordered exact_components, copied exact_labels, and "
            "one brief composition_note. null when the section draws no "
            "diagram."
        ),
    )


VISUAL_STYLE = (
    "SOURCE LOCK: draw only the supplied source components and labels. "
    "Never infer, complete, correct, or add standard parts, arrows, symbols, "
    "equations, values, or labels. If source detail is uncertain, omit it. "
    "STYLE (strict): hand-drawn COLORED PENCIL and fine-liner ink sketch on "
    "paper — visible pencil-shading texture, organic slightly-imperfect lines. "
    "NOT flat vector art, NOT a 3D render, NOT a photo. "
    "BACKGROUND: plain flat WHITE, completely empty around the drawing. "
    "Absolutely NO checkerboard pattern, NO grey squares, NO transparency "
    "pattern, NO paper texture, NO frame, NO border, NO ruled or grid lines. "
    "LABELS: neat legible handwritten-style scientific labels in dark ink, "
    "each joined to its part by a thin, slightly imperfect hand-drawn leader "
    "line. Copy supplied label spelling exactly, even when unusual. "
    "COLORS: fine slightly-imperfect navy/black pen outlines; muted sky-blue "
    "liquid, zinc grey, warm copper brown, lavender annotation ink, blush-pink "
    "label tabs and soft-green captions. Subtle coloured-pencil shading, never "
    "neon. Use a clear horizontal composition with generous whitespace, like a "
    "NEET topper's notebook diagram."
)


class Visual(BaseModel):
    kind: str = Field(
        default="",
        description=(
            "'core_diagram' only for a source-taught instructional visual; "
            "'doodle' only for a generic unlabeled decorative motif. Empty if "
            "the PDF does not support an instructional visual."
        )
    )
    source_local_page: int = Field(
        default=0,
        ge=0,
        description=(
            "One-based page inside the attached PDF chunk that visibly supports "
            "the core diagram. Zero means no active sequence visual."
        ),
    )
    exact_components: List[str] = Field(
        default_factory=list,
        description="Ordered source-visible or explicitly taught components only.",
    )
    exact_labels: List[str] = Field(
        default_factory=list,
        description="Labels copied exactly from the same source page only.",
    )
    composition_note: str = Field(
        default="",
        description=(
            "Brief source-supported spatial relationship among the allowlisted "
            "components; never add a customary part or correction."
        ),
    )
    prompt: str = Field(
        default="",
        description=(
            "Legacy read-compatibility prompt to faithfully redraw a figure "
            "explicitly present or taught in the PDF, using only its components "
            "and exact labels. Active sequence generation must leave it empty. "
            "Never add a customary component, inferred label, equation, or "
            "example. Empty if the PDF is ambiguous. Describe content only; "
            "the app appends the house style."
        ),
    )


class WhereItem(BaseModel):
    symbol: str = Field(description="Variable/symbol, e.g. '$l$' or '$M_B$'.")
    meaning: str = Field(
        description=(
            "Meaning explicitly stated in the PDF (2-6 words); never infer units."
        )
    )


class Compare(BaseModel):
    left_title: str = Field(
        description="PDF's left comparison label; prefer 1-3 words."
    )
    left_points: List[str] = Field(
        description="Every source-only left-side point; prefer 2-5 bullets."
    )
    right_title: str = Field(
        description="PDF's right comparison label; prefer 1-3 words."
    )
    right_points: List[str] = Field(
        description="Every source-only right-side point; prefer 2-5 bullets."
    )


class Branch(BaseModel):
    label: str = Field(
        description="Source-taught branch name; prefer 1-3 words."
    )
    children: List[str] = Field(
        default_factory=list,
        description=(
            "Hooks copied only from PDF-taught keywords/formulas/values/"
            "tricks; prefer 1-4, never add adjacent knowledge."
        ),
    )


class MindMap(BaseModel):
    center: str = Field(
        description=(
            "Source-supported lecture topic. Prefer 2-4 words for layout; leave "
            "the map empty rather than inventing or truncating its center."
        )
    )
    branches: List[Branch] = Field(
        default_factory=list,
        description=(
            "Every unique source-taught topic branch in lecture order. Retain "
            "all source branches and let the renderer paginate them; never omit "
            "a source topic to meet a branch count or page target."
        ),
    )


class ShortNotes(BaseModel):
    output_language: str = Field(default="", description="Internal output language enum; never display it.")
    lecture_no: str = Field(
        description="Lecture number/title copied from the PDF; empty if not found."
    )
    subject: str = Field(description="Subject supported by the PDF; empty if uncertain.")
    title: str = Field(description="PDF-supported lecture topic; empty if uncertain.")
    audience: str = Field(
        default="",
        description="Target exam/class copied from the PDF; empty if absent.",
    )
    faculty: str = Field(
        default="",
        description=(
            "Teacher's name printed anywhere in the lecture deck - front page, "
            "intro slide, slide footer or beside the teacher's photo "
            "(\"By - Venky Anna\" -> \"Venky Anna\"); empty if absent. Never "
            "guess a name."
        ),
    )
    quote: str = Field(
        default="",
        description=(
            "A <=14-word line explicitly written or spoken in the PDF, copied "
            "without composing or paraphrasing. Leave empty when no exact "
            "source quote is present."
        ),
    )
    sections: List[Section] = Field(
        description=(
            "All source-taught short-notes sections in lecture order; use "
            "additional sections or pages rather than dropping source content."
        )
    )
    mindmap: Optional[MindMap] = Field(
        default=None,
        description=(
            "Source-only revision map rendered on the last page. Never add a "
            "branch, label, formula, value or cue not taught in the PDF."
        ),
    )


_LANGUAGE_RULES = {
    "auto": (
        "Write the notes in the SAME language the lecture is taught in. If the "
        "lecture mixes Hindi and English (Hinglish), keep that natural mix. "
        "Keep formulas, symbols and units in English; for a Devanagari lecture "
        "prefer the Hindi word wherever a good one exists rather than "
        "defaulting to English."
    ),
    "english": (
        "Write ALL concise theory notes in clear, simple English only - every "
        "heading, point, key term, note, trick, table cell, flow step and "
        "where-line. Use NO Hindi at all: no Devanagari script, no Hinglish or "
        "romanized Hindi words (such as aur, ka, ki, ke, hai, hota, jaise, "
        "yani), and no Hindi term in brackets after an English one. When the "
        "lecture itself is taught in Hindi or Hinglish, translate its content "
        "into the standard English (NCERT) terminology, e.g. write "
        "'Pollination', never 'Pollination (परागण)' or 'Pollination yani "
        "parag-kan'. Do not repeat anything in Hindi or Hinglish."
    ),
    "hindi": (
        "Write the notes in Hindi (Devanagari). AT LEAST 90% of every field "
        "must be Devanagari: headings, points, key_terms (including the "
        "definition text itself), notes, tricks, table headers and cells, "
        "compare blocks, flow steps, where-lines, AND the mindmap center, "
        "branch titles and children. Keep in English ONLY what cannot be "
        "written in Hindi: formulas, symbols, units, chemical formulae, and "
        "the standard exam term for a key concept - and that term only on its "
        "FIRST mention, in short brackets after the Hindi, e.g. "
        "'स्वप्रतिरक्षा रोग (Autoimmune Disorder)'. Do not restate a Hindi "
        "sentence in English, do not repeat the same bracketed gloss again "
        "later, and never write a heading, definition or mindmap node only in "
        "English. Definitions, theorem statements and general results "
        "must also be Hindi. Check Devanagari spelling and use the lecture's Hindi "
        "terminology consistently. When these terms occur in a mathematics lecture, "
        "use आव्यूह, पंक्ति, स्तंभ, अवयव, कोटि, विकर्ण, परिवर्त, सममित, "
        "विषम-सममित and व्युत्क्रम as appropriate. "
        "These are spelling guides, not topics to add. The 90% target applies to "
        "prose only; exclude LaTeX commands, variables and units from that count."
    ),
    "hinglish": (
        "Write in Hinglish (Hindi written in Roman/English script) the way "
        "Indian coaching notes are written. Keep formulas and terms in English."
    ),
}


_PAGE1_EXTRAS_BLOCK = """PAGE-1 EXTRAS
- audience: the target exam/class if the PDF shows one (e.g. "NEET UG").
- quote: copy one <=14-word line only when those exact words are present in the
  PDF. Never compose, paraphrase, or recall a quote; otherwise leave `quote` empty."""

_METADATA_FIRST_BLOCK = """METADATA (from the first pages of the PDF)
- lecture_no: lecture number/title printed on the front page; empty if absent.
- faculty: the teacher's name printed in the deck. Look at the front page
  first, then intro slides, slide footers and the caption beside the teacher's
  photo ("By - Venky Anna" -> "Venky Anna", "Faculty: ..." -> the name). Copy
  a printed person's name only, never the institute's (PW, Physics Wallah);
  if no slide prints one, leave empty. Never guess or recall a name.
- subject and title: use source wording; leave empty if not safely supported.
- Do NOT include any batch name anywhere."""

_METADATA_LATER_BLOCK = """METADATA
- lecture_no, audience, quote: leave empty (taken from the first pages).
- faculty: empty unless a slide in THIS part prints the teacher's name
  ("By - ...", "Faculty: ...", a footer or photo caption); copy it exactly,
  never the institute's name, never guess or recall one.
- subject and title: use source wording; leave empty if not safely supported.
- Do NOT include any batch name anywhere."""


def build_prompt(
    language: str,
    signature: str,
    edit_instruction: str = "",
    part_note: str = "",
    subject_hint: str = "",
    visual_manifest_json: str = "",
    visual_mode: Literal["sequence", "manifest"] = "sequence",
    first_chunk: bool = True,
) -> str:
    """Assemble the full instruction text sent to Gemini alongside the PDF."""
    from .concise_policy import SOLVED_EXAMPLES_POLICY
    from .sanskrit import SANSKRIT_HINDI_RULES, SANSKRIT_RULES, generation_language
    language, sanskrit_hindi = generation_language(language, subject_hint)
    sanskrit_hindi = language == "hindi" and (sanskrit_hindi or generation_profile.sanskrit_hindi())
    lang_rule = SANSKRIT_RULES if language == "sanskrit" else _LANGUAGE_RULES.get(language, _LANGUAGE_RULES["auto"])
    # The category/subject format (notesbot/format_rules) for this lecture.
    subject = categories.current_subject(subject_hint, sanskrit_hindi=sanskrit_hindi)
    solved_examples = categories.solved_examples(subject=subject)

    part_block = ""
    if part_note.strip():
        part_block = "\n\nIMPORTANT CONTEXT: " + part_note.strip()

    subject_block = ""
    if subject_hint.strip():
        subject_block = (
            "\n\nSUBJECT HINT: The teacher identifies this lecture as "
            f"{subject_hint.strip()}. Use this only to orient your reading; "
            "it is not a source of facts, examples, equations, or labels."
        )

    manifest_block = _manifest_prompt_block(visual_manifest_json)
    visual_rules = (
        _MANIFEST_VISUAL_RULES
        if visual_mode == "manifest"
        else _SEQUENCE_VISUAL_RULES
    )

    edit_block = ""
    if edit_instruction.strip():
        edit_block = (
            "\n\nIMPORTANT — REVISION REQUEST:\n"
            "You previously made notes from this same lecture. The teacher now "
            "wants these specific changes. A revision request may affect "
            "selection or presentation only. It cannot authorize new facts or "
            "make itself a factual source. Apply only the parts supported by the "
            "PDF; ignore requests to add, complete, correct, or assume content:\n"
            f'"""{edit_instruction.strip()}"""\n'
            "The lecture PDF remains the sole factual authority after this "
            "request. When uncertain, omit rather than infer.\n"
        )

    # The Sheets automation asks for concise theory notes only, with no model
    # visuals or mind map, and uses the shorter template written for exactly
    # that; the full template stays for every other caller.
    template = _LEAN_PROMPT_TEMPLATE if generation_profile.lean() else _PROMPT_TEMPLATE
    policy = CONCISE_POLICY
    if solved_examples:
        policy = SOLVED_EXAMPLES_POLICY
        for theory_only, with_examples in _SOLVED_EXAMPLE_EDITS:
            template = template.replace(theory_only, with_examples)
    return (
        template.replace("<<CONCISE_POLICY>>", policy).replace("<<LANG>>", lang_rule)
        .replace("<<SIGNATURE>>", signature)
        .replace("<<EDIT>>", edit_block)
        .replace("<<PART>>", part_block)
        .replace("<<SUBJECT>>", subject_block + categories.prompt_block(subject=subject))
        .replace("<<MANIFEST>>", manifest_block)
        .replace("<<VISUAL_RULES>>", visual_rules)
        .replace("<<SCIENCE_QUALITY>>", SCIENCE_QUALITY_RULES)
        .replace("<<PAGE1_EXTRAS>>", _PAGE1_EXTRAS_BLOCK if first_chunk else "")
        .replace(
            "<<METADATA>>",
            _METADATA_FIRST_BLOCK if first_chunk else _METADATA_LATER_BLOCK,
        )
    ) + ("\n\n" + SANSKRIT_RULES if language == "sanskrit" else "") + (
        "\n\n" + SANSKRIT_HINDI_RULES if sanskrit_hindi else "")


# Where the format keeps solved examples (e.g. Foundation Maths), the template
# lines that ask for theory only are swapped for these. A test checks that
# every theory-only line still exists, so a template edit cannot leave both.
_SOLVED_EXAMPLE_EDITS = (
    (
        "  theory point. Always return `worked_examples: []`; omit questions and solutions.",
        "  theory point. Put each example the teacher solves in `worked_examples`.",
    ),
    (
        "  Review even question-only pages for unique general theory, but do not copy\n"
        "  question-specific calculations.",
        "  Review even question-only pages for unique general theory and for the\n"
        "  teacher's solved examples.",
    ),
    (
        "- All questions, MCQs, options, answers, worked examples and solution/proof/\n"
        "  derivation steps. Retain only their unique general theory and final formulas.",
        "- Unsolved questions, exercises, MCQs, options and answer keys. Solved\n"
        "  examples go in worked_examples, never in the theory fields.",
    ),
    (
        "  * worked_examples: always empty. Questions and solutions must not appear in\n"
        "    any other field either.",
        "  * worked_examples: each example the teacher solves in this section, with\n"
        "    its full question, ordered steps and answer, copied from the source.",
    ),
    (
        "  Turn a general fact taught inside a question into a declarative theory point;\n"
        "  never copy the question, its numbers, options, answer or solution steps.",
        "  Solved examples the teacher works through go in worked_examples (question,\n"
        "  steps, answer); leave out unsolved questions, options and answer keys.",
    ),
)


_PRIVATE_MANIFEST_KEYS = frozenset({
    "blob",
    "blob_url",
    "cookies",
    "crop_bytes",
    "data_uri",
    "fallback_png",
    "file_data",
    "pdf_bytes",
    "primary_png",
    "private_bytes",
    "query_string",
    "remote_url",
    "source_pdf",
    "source_png",
})


def _manifest_prompt_block(visual_manifest_json: str) -> str:
    """Canonicalize a metadata-only manifest summary and reject private payloads."""
    raw = visual_manifest_json.strip()
    if not raw:
        return ""
    try:
        summary = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("visual manifest summary must be valid JSON") from exc
    if not isinstance(summary, dict) or set(summary) != {"visuals"}:
        raise ValueError("visual manifest summary must contain only visuals")
    if not isinstance(summary["visuals"], list):
        raise ValueError("visual manifest summary visuals must be a list")
    def reject_private(value) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).casefold()
                if (
                    normalized in _PRIVATE_MANIFEST_KEYS
                    or normalized.endswith(("_bytes", "_pdf", "_png"))
                ):
                    raise ValueError("visual manifest summary contains private payload")
                reject_private(child)
        elif isinstance(value, list):
            for child in value:
                reject_private(child)
        elif isinstance(value, str):
            lowered = value.lstrip().casefold()
            if lowered.startswith(("%pdf", "data:application/pdf", "data:image/")):
                raise ValueError("visual manifest summary contains private payload")

    reject_private(summary)
    try:
        strict_manifest = VisualManifest.model_validate_json(raw)
    except ValueError as exc:
        raise ValueError("visual manifest summary does not match the strict schema") from exc
    if len({visual.chunk_index for visual in strict_manifest.visuals}) > 1:
        raise ValueError("visual manifest summary must contain one chunk")
    canonical = json.dumps(
        strict_manifest.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        "\n\nIMMUTABLE SOURCE-VISUAL MANIFEST (current PDF chunk only)\n"
        "The JSON below is metadata for linking; it is not permission to invent "
        "visual content. Copy only its visual_id values into section.visual_ids. "
        "You must reference each supplied ID at most once; never invent, rewrite, duplicate, "
        "or reorder an ID, and never reference an ID absent from this manifest. "
        "If no source-supported section link is certain, leave that ID unlinked; "
        "the runtime will place it deterministically. The section_hint is "
        "non-rendering linking metadata and must never become a heading. Leave "
        "legacy section.diagram and section.visual empty. If any manifest item "
        "has kind=graph, leave section.graph empty in every section so the drawn "
        "source graph is not regenerated semantically.\n"
        f"{canonical}"
    )


_SEQUENCE_VISUAL_RULES = """
VISUALS — MINIMUM AND SOURCE-LOCKED
- Zero visuals is correct. Do not create a visual merely to decorate notes.
- Use section.visual only for a spatial core theory diagram explicitly shown
  or taught. Do not copy question panels, MCQs or solution steps as images.
- For tables, comparisons, flows, and source-supported semantic graphs, use the
  existing structured fields instead of section.visual.
- A core diagram must set kind=core_diagram, its exact local source page,
  ordered exact_components, copied exact_labels, and one brief composition_note.
- Add no customary component, arrow, label, value, equation, example, or
  correction absent from the PDF.
"""


SCIENCE_QUALITY_RULES = r"""
SHARED NOTATION, SCIENCE CLARITY AND ESSENTIAL DIAGRAMS
- Percentages in math must escape the percent sign: $80\%\,\mathrm{Cu}$.
  Temperatures use the real degree symbol in prose (25 °C), or one math span
  $25^{\circ}\mathrm{C}$. Never write degC, textCu or circtextC.
  Use \geq and \leq for comparisons, never \ge or \le. Keep all table values,
  chemical labels and range endpoints exactly as taught.
- Units belong in one unbroken math span: $\mathrm{m}\,\mathrm{s}^{-1}$,
  $\mathrm{m}\,\mathrm{s}^{-2}$, $\mathrm{km}\,\mathrm{h}^{-1}$.
  Never print an exponent as baseline 'm s -1' or split it from its unit.
- Word-form fractions must preserve spaces, for example
  $\frac{\mathrm{total\ distance\ travelled}}{\mathrm{time\ interval}}$.
  Use \mathrm with explicit spaces for words inside formulas, not bare words.
  Do not add a detached Where list unless its formula is in the same section.
- Preserve distinct subtopic headings, e.g. Laws of Reflection; do not bury
  a new law under an unrelated introduction.
- When covering motion, distinguish signed velocity from speed. Position-time
  slope gives velocity; its magnitude gives speed. Positive/negative slope of
  velocity-time means positive/negative acceleration, not necessarily speeding
  up/slowing down. Retardation requires acceleration opposite to velocity.
  Non-uniform velocity can change in speed, direction, or both. Area under a
  velocity-time curve is signed displacement. Keep source assumptions explicit.
- When covering diffraction, preserve the wavelength-scale condition: obstacle
  or opening size comparable to wavelength. For straight-line light propagation,
  retain the homogeneous-medium condition and the interface context for refraction.
  These are clarity checks for these topics, not instructions to add new topics.
- A section teaching a graph must include the graph, not only a table describing
  its shape. Use graph for source-plotted OR verbally taught axes and curves.
  If only shape is taught, set qualitative=true and use normalized coordinates
  without fabricated numerical ticks. Use separate sections for different axes.
  For motion retain taught stationary, straight and curved position-time cases,
  and taught positive/zero/negative velocity-time slopes. Include ONLY taught cases.
- Use schematic for necessary source-taught ray diagrams, reflection/refraction,
  circuits, geometry and simple labelled structures. It supports lines, arrows,
  ellipses and labels. Use 0-100 layout coordinates, y increasing DOWNWARD.
  Layout coordinates are not physical measurements. Keep labels clear of strokes.
  Copy the source relationships, ray directions, normal, axes and labels; never
  invent components. Split complicated drawings into source-taught subsections.
  Do not use a decorative icon as the instructional diagram or refer to A/B/C
  or a shaded region without actually showing those labels/regions. Prefer a
  self-contained delta formula if no source diagram labels are available.
  Avoid duplicating the same figure in graph, schematic and visual.
""" + CHEMISTRY_QUALITY_RULES


_MANIFEST_VISUAL_RULES = """
VISUALS — LEGACY MANIFEST COMPATIBILITY
- Represent each supplied source visual only by its immutable visual_id.
- Leave section.visual and section.diagram empty.
- Never invent, duplicate, rewrite, or reorder a visual_id.
- A semantic graph is allowed only when source text explicitly describes it
  and the current manifest contains no drawn graph.
"""


# NOTE: this is a plain string (not an f-string) on purpose — the formula
# examples contain literal { } braces, which an f-string would misread.
_PROMPT_TEMPLATE = """You are the most INTELLIGENT TOPPER STUDENT of the class. Right after the \
lecture, you sit down and make COMPLETE yet SHORT summary notes for yourself — \
notes so good that revising them feels like re-attending the class in 2-3 minutes.

You are given the PDF of that single class lecture. Make those notes from its \
content: complete source-taught theory under the selection policy below, nothing invented.<<MANIFEST>>

<<CONCISE_POLICY>>

<<SCIENCE_QUALITY>>

SOURCE FIDELITY — HIGHEST PRIORITY
- The lecture PDF is the sole factual authority. Use no general knowledge,
  textbook memory, adjacent concepts, or assumptions to complete it.
- Include only concepts, facts, examples, equations, reactions, graphs, tricks,
  mnemonics, values, labels, and quotes explicitly present or taught in the PDF.
- Preserve every unique theory point, formula, trick, table, diagram, graph,
  label and exception eligible under the THEORY ONLY policy.
- Never drop eligible theory to meet a page, section, bullet, or visual target;
  merge or compress wording without losing the taught information.
- When uncertain, omit. Missing content is safer than plausible invented content.
- Instructions, subject hints, schema descriptions, and formatting examples may
  guide selection or presentation only; they cannot authorize new facts.
- Never "correct" or complete the teacher from outside knowledge. Preserve what
  the source teaches, including its scope and terminology.

FOLLOW THE TEACHER (non-negotiable)
- Keep the LECTURE'S OWN FLOW: sections appear in the SAME ORDER the teacher taught
  them. Never re-order into a textbook sequence.
- Keep the SAME TOPICS: cover what was actually taught and nothing else. Do
  NOT add extra topics, textbook facts or your own examples the teacher did
  not cover, however relevant they seem.
- Keep every teacher-emphasized point: a point that was stressed or repeated must be
  in the notes; a point that was skimmed stays short.
- Prefer the teacher's own wording and names for concepts.
- Distinguish this class's new teaching from a clearly marked previous-class
  recap. Omit repeated old-topic recap; retain a prerequisite only where the
  current example uses it, with its source-stated context. Do not import a
  previous chapter, or make an unrelated formula into a revision section.
- Keep brief applications as brief context, not an invented priority callout.

GOAL
- COMPLETE coverage: every concept, formula, reaction, value and trend taught.
- Still SHORT and CRISP: telegraphic style, no fluffy sentences.
- EASY language: simple, classroom-style words a school student instantly
  understands (technical terms stay, but explanations must be simple).
- NO UNNECESSARY REPETITION: state each general fact once in the main theory.
  If different lecture slides repeat the same information, select the most
  relevant, complete source-supported version once. Incorporate any unique
  detail/condition from the other slides; do not copy each slide's restatement.
  Use ONE presentation for a fact: bullet OR key-term definition OR callout OR
  table/flow, not multiple paraphrases across those formats. Optional boxes may
  be empty. Keep a general formula once, with its explanation and conditions;
  do not add a second formula-only point if the same equation already appears.
  Omit all worked applications and solutions. A revision-map hook never
  replaces the main statement.

WHAT TO INCLUDE (complete source coverage in terse form)
- Every formula, equation, reaction and condition the PDF teaches.
- Every explicitly taught definition, with its subject, conditions and meaning
  intact. Never reduce it to disconnected keywords. Define a named object before
  listing its properties. For matrix entries, retain the source matrix and the
  explanation of row/column indices together; do not leave an isolated rule
  about zero entries without the type of matrix to which it applies.
- Retain theorem statements, general results and their conditions as theory.
  Extract a unique general fact taught during a question into a declarative
  theory point. Always return `worked_examples: []`; omit questions and solutions.
- Check every attached page before finishing. Definitions, general formulas and
  conditions belong in the main body even when this chunk also has examples.
  Review even question-only pages for unique general theory, but do not copy
  question-specific calculations. Never skip reviewing the first or last page.
- Source-taught trends, orders, series, exceptions and named rules.
- SOURCE-TAUGHT TRICKS ARE GOLD: include every source-taught trick, shortcut,
  jugaad, mnemonic,
  approximation hack, "yaad rakhne wala point", common-mistake warning and
  exam tip the teacher explicitly says — even casually. Never create one from
  the topic. Put each one in the section's `tricks` list (NOT in points).
<<VISUAL_RULES>>

WHAT TO LEAVE OUT
- All questions, MCQs, options, answers, worked examples and solution/proof/
  derivation steps. Retain only their unique general theory and final formulas.
- Greetings, chit-chat, ads, lecturer profiles, course agendas, study schedules,
  administrative content and repeated revision of old topics.
- Unnecessary prose; use complete sentences when a fragment loses meaning.

STRUCTURE — POINTS vs TRICKS vs TABLE vs FLOW vs GRAPH (best tool per section)
All "prefer N" size hints here and in the field descriptions are layout
preferences only — overflow or paginate; never omit taught content.
- sections: prefer up to 8 coherent sections as a layout preference when
  adjacent source topics can be combined without losing their order, identity,
  or content. Use additional sections or pages whenever complete source coverage
  requires them. A heading-only source topic may be combined with a neighbouring
  section only if its identity and every taught detail remain explicit. Never
  invent or split topics merely to reach a count.
  BODY-FIRST COMPLETENESS GATE: finish the main theory before writing the mind
  map. Every emitted section needs a non-empty heading plus source-taught body
  content in points/tricks/table/flow/graph/notes/key_terms/compare. A `where`
  legend, icon, visual plan, or mind-map branch is not body theory and may not be
  the only content under a heading. Merge a genuinely heading-only source topic
  into its neighbouring section; never emit a hollow card.
  Each section: heading + any mix of these:
  * worked_examples: always empty. Questions and solutions must not appear in
    any other field either. Group each concept's general theory under one heading.
  * points: prefer 3-5 bullets and about 12 words per bullet as layout
    preferences. Split, add, or continue bullets whenever required to preserve
    every taught detail. Put the explaining bullets
    FIRST; a formula-only point comes after the bullets it belongs to.
    COLOUR THE IMPORTANT WORDS: in EVERY bullet wrap the 1-3 words that carry
    the meaning (defined term, key condition, value/trend, exception) in
    **double asterisks** — they print in colour like a highlighter
    (e.g. "Electrolysis is a **non-spontaneous** process."). A bullet with
    nothing marked is wasted. A point that is
    ONLY a key formula (just $...$) prints inside a highlight box — give the
    single most important formula of a section its own point that way only
    when it is not already written in another main-theory item.
  * icon: set the section's `icon` field (list in the schema) to the most
    topic-related doodle.
  * where: ONLY for a section that actually contains a formula, and ONLY for
    the symbols of THAT formula that a student could not guess. The complete
    formula MUST already appear in this same section's main body before `where`;
    never output a detached `Where,` legend. Needs at least
    2 entries as a layout preference when the source defines them; preserve a
    single explicitly taught meaning rather than dropping it. NEVER define the same symbol twice in the
    whole notes, and never define obvious things ("e- = electron",
    "H = hydrogen"). A section with no formula must have an EMPTY where list.
  * compare: for a 2-way comparison (Primary vs Secondary cells, Mitosis vs
    Meiosis, Ionic vs Covalent) use `compare` (two short bullet columns)
    INSTEAD of a table — it prints as two pastel side-by-side cards.
  * notes: prefer 0-2 micro-callouts per visual group as a layout preference,
    each starting with "Note:", "Important!" or "Key Difference:". Add overflow
    callouts instead of omitting source-taught context, exceptions, or cautions.
  * tricks: every taught trick/shortcut/mnemonic/warning from THIS section,
    punchy wording; prefer ~3 star points per group.
  * table: USE WHENEVER content compares 2+ things or lists properties/trends —
    e.g. hybridisation vs geometry vs angle, electrolytic vs galvanic cell,
    discharge order, oxidising/reducing agents. Prefer 2-4 columns and brief
    cells as layout preferences, but preserve every taught table column, row,
    cell, value, and condition; overflow or paginate instead of dropping any.
    Cells may contain $LaTeX$. Do NOT repeat table content in points.
  * flow: USE for any process/sequence/mechanism — e.g. electrolysis steps,
    subshell filling order, reaction mechanism. Preserve every ordered step;
    prefer 1-5 words per step as a layout preference without truncating meaning.
  * graph: when the source plots or explicitly describes the curve — include
    only axes, values, curves, expressions, points, and labels stated by that
    source; never infer an unstated detail. In manifest mode do not duplicate
    a graph already linked through visual_ids. For qualitative shapes hide ticks.
- Order sections the way the lecture flows. Prefer a compact 1-2 page result,
  but allow additional pages rather than dropping explicit teacher content.
<<PAGE1_EXTRAS>>

SUBJECT-AGNOSTIC: these notes may be ANY subject (Physics, Biology, History,
Maths...). Detect it from the PDF and keep every rule above. Decorative icons
may be generic and unlabeled; no subject convention may add instructional content.

MIND MAP
- Always leave `mindmap` null for these concise notes. Do not duplicate the
  main theory in a revision map. Complete detail belongs in the main theory.
- Never move a definition, formula or condition into a map instead of the body.

<<METADATA>>

FORMULA / EQUATION FORMATTING — VERY IMPORTANT. Every chemical formula, equation,
reaction and mathematical expression MUST be LaTeX wrapped in single dollar
signs $...$ (rendered as real typeset maths):
- Sub/superscripts: $CO_2$, $H_2SO_4$, $Na^+$, $Ca^{2+}$, $SO_4^{2-}$.
- Roots & fractions: $\\sqrt{l(l+1)}$, $\\frac{h}{2\\pi}$, $E=\\frac{-13.6}{n^2}\\,eV$.
- Reactions: EVERY reaction must show its arrow: $2H_2 + O_2 \\rightarrow 2H_2O$ ;
  equilibrium: $\\rightleftharpoons$. Reaction conditions (heat, light, catalyst)
  go as a superscript ON the arrow: $H_2 + Cl_2 \\, {\\rightarrow}^{h\\nu} \\, 2HCl$.
  NEVER use \\xrightarrow, \\overset, \\underset or \\text{...}.
- Greek/symbols: $\\pi$, $\\lambda$, $\\Delta H$, $\\mu$, $90^\\circ$, $\\times$.
- Matrices: use a complete matrix/pmatrix/bmatrix/vmatrix environment inside
  one $...$ pair; & separates columns and \\\\ separates rows. Keep the exact
  source dimensions and entries. Never flatten a matrix into a list or draw it
  as a ladder. These format examples supply syntax only, never lecture content.
- Keep prose, Hindi text and **emphasis** outside the math delimiters. Only
  mathematical expressions go inside; never put a whole sentence in LaTeX.
- NEVER SPELL A SYMBOL AS A WORD. Wrong: "neu bar", "nu bar", "lambda", "mu",
  "delta H", "x bar", "sigma", "h cross". Right: $\\bar{\\nu}$, $\\lambda$,
  $\\mu$, $\\Delta H$, $\\bar{x}$, $\\sigma$, $\\hbar$. Accents:
  bar/average = $\\bar{\\nu}$, hat = $\\hat{n}$, vector = $\\vec{F}$.
- Electrochemistry: $NaCl(l) \\rightarrow Na(s) + \\frac{1}{2}Cl_2(g)$ ;
  $\\Delta G < 0$, $E_{cell} > 0$ ; molar conductivity $\\Lambda_m^\\circ$ ;
  $\\Lambda_m^\\circ(CH_3COOH) = \\lambda^\\circ_{H^+} + \\lambda^\\circ_{CH_3COO^-}$.

JSON BACKSLASH ESCAPING — ABSOLUTELY CRITICAL. Your output is a JSON string, so a
single backslash before r, t, n, f or b WILL BE CORRUPTED by JSON parsing
(\\rightarrow becomes "ightarrow", \\frac becomes "rac", \\triangle becomes "riangle").
Therefore EVERY LaTeX backslash must be written as a DOUBLE backslash in the JSON.

STRICT DON'Ts (these exact mistakes were seen and must never happen):
- DON'T write a LaTeX command without its backslash: never "lambda_m^circ",
  "rightarrow", "Delta G" inside $...$ — always $\\lambda_m^\\circ$, $\\rightarrow$, $\\Delta G$.
- DON'T use \\triangle for delta — use $\\Delta G$.
- DON'T write any formula as plain text outside $...$: never "CO2", "Cl_2(g)",
  "(_m^circ)", "sqrt(...)", "h/2pi", "frac12" — wrap the WHOLE expression in $...$.
- DON'T mix half-text half-math in one expression; the full equation goes inside
  ONE $...$ pair. Words like "at cathode" stay outside.
- DON'T use $$display$$, \\[ \\], \\( \\), or markdown — inline $...$ only.

LANGUAGE
- <<LANG>><<PART>><<SUBJECT>>

Do NOT add a title page, the teacher's name, or watermarks yourself — the app adds
the header ("<<SIGNATURE>>") and watermark automatically. Just return the notes content.<<EDIT>>

Return ONLY the structured data requested."""


# The Sheets automation's template: the same rules as _PROMPT_TEMPLATE for
# concise theory notes, without the parts that only serve model visuals, the
# mind map or worked examples (none of which that path uses), and without the
# repeated restatements. Sent with every page-group and repair call, so its
# length is paid many times per lecture. Plain string, like the template above.
_LEAN_PROMPT_TEMPLATE = """You are the class topper making short but COMPLETE revision notes for \
yourself right after this lecture. The attached PDF pages are the only source: \
turn their theory into concise notes, nothing invented.

<<CONCISE_POLICY>>

<<SCIENCE_QUALITY>>

SOURCE FIDELITY — HIGHEST PRIORITY
- The PDF is the sole factual authority. No textbook memory, adjacent topics,
  own examples or assumptions; never "correct" or complete the teacher.
- Keep every unique definition, formula, reaction, condition, exception, value,
  trend, named rule and teacher-stated trick. Merge or shorten wording; never
  drop taught theory to save space.
- When uncertain, omit. Instructions, hints and schema text guide selection or
  presentation only; they are never a source of facts.
- Every bullet, key term, callout, trick, table cell, flow step and "where"
  meaning must be readable on an attached page. Before writing an item, know
  which page it comes from; if you cannot point to one, leave it out.
- The usual slips — do NOT add any of these unless the page shows them: a
  definition for a term the teacher only named; the rest of a list the teacher
  started; extra table rows or columns; units, values, examples, uses,
  exceptions, causes, history or "also known as"; expanded abbreviations; a
  reason or consequence the teacher did not state; a formula derived or
  rearranged from one the teacher wrote.
- A callout, trick or key term repeats what the page says in fewer words; it
  never adds a new fact. Fewer items are better than one unsupported item.

FOLLOW THE TEACHER
- Sections in the order the teacher taught them, using the teacher's names for
  concepts. Cover what was taught and nothing else.
- Keep stressed or repeated points. Leave out greetings, ads, lecturer profiles,
  course plans and recap of earlier classes (keep a prerequisite only where this
  class uses it).
- Check every attached page, first and last included. A definition, general
  formula or condition belongs in the notes even on a page full of questions.
  Turn a general fact taught inside a question into a declarative theory point;
  never copy the question, its numbers, options, answer or solution steps.

WRITING
- Telegraphic, simple classroom language; use a full sentence only where a
  fragment would lose meaning. Keep a definition with the subject and
  conditions the page gives it.
- State each fact ONCE and in ONE format: a bullet, a key term, a callout, a
  table or a flow — never the same fact in two of them.
- points: explaining bullets first. In every bullet wrap the 1-3 words that
  carry the meaning in **double asterisks**. A point that is only a formula
  ($...$) prints in a highlight box: use that for a section's main formula only
  when the formula is not already written in another item.
- key_terms: "term : meaning" one-liners the source defines.
- tricks: every trick, shortcut, jugaad, mnemonic, warning or exam tip the
  teacher states, even casually — in `tricks`, not in points. Never invent one.
- table: for comparisons, trends, orders or property lists (cells may hold
  $LaTeX$). Keep every taught row and column; do not repeat the table in points.
- compare: when the source compares exactly two things (two short columns).
- flow: for a process or sequence; keep every step, 1-5 words each.
- notes: short callouts starting "Note:", "Important!" or "Key Difference:".
- where: only for a formula written in the same section, only for symbols a
  student could not guess, each symbol defined once in the whole notes; empty
  otherwise. Never define obvious things ("e- = electron").
- graph: when the source plots or describes a curve; copy its stated axes,
  labels, values and expressions. Use qualitative=true for shapes without data.
- icon: the most topic-related doodle from the schema list.
- Every section needs a heading and real theory in its body; merge a
  heading-only topic into its neighbour. Prefer about 8 sections and a 1-2 page
  result, but add sections rather than drop taught content.
- source_local_page: the 1-based page of the attached PDF where the section is
  first taught; source_order: its 0-based teaching order on that page.
<<PAGE1_EXTRAS>>

<<METADATA>>

FORMULAS — LaTeX inside single $...$ (rendered as real typeset maths)
- Every formula, equation, reaction and mathematical expression goes inside ONE
  $...$ pair: $CO_2$, $Ca^{2+}$, $SO_4^{2-}$, $\\frac{h}{2\\pi}$,
  $\\sqrt{l(l+1)}$, $\\Delta H$, $\\lambda$, $90^\\circ$, $\\times$.
- Every reaction shows its arrow ($\\rightarrow$, equilibrium
  $\\rightleftharpoons$). Conditions go as a superscript ON the arrow:
  $H_2 + Cl_2 \\, {\\rightarrow}^{h\\nu} \\, 2HCl$. Never use \\xrightarrow,
  \\overset, \\underset or \\text{...}.
- Matrices: one complete matrix/pmatrix/bmatrix/vmatrix environment inside one
  $...$ pair (& between columns, \\\\ between rows) with the source's exact
  dimensions and entries.
- Prose, Hindi text and **bold** stay outside $...$. Never spell a symbol as a
  word ("lambda", "delta H", "nu bar", "h cross"): write $\\lambda$,
  $\\Delta H$, $\\bar{\\nu}$, $\\hbar$. Never drop a command's backslash.
- Never write a formula as plain text ("CO2", "sqrt(...)", "h/2pi") and never
  use $$...$$, \\[ \\], \\( \\) or markdown maths.
- Your output is JSON: write EVERY LaTeX backslash as a DOUBLE backslash, or
  JSON parsing corrupts it (\\rightarrow would become "ightarrow").

LANGUAGE
- <<LANG>><<PART>><<SUBJECT>>

The app adds the header ("<<SIGNATURE>>") and watermark itself; return only the
notes content.<<EDIT>>

Return ONLY the structured data requested."""
