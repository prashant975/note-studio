"""What PW AI is asked for the typeset notes: the engine's own prompt (layer 1).
The category, subject, sheet and run rules are added after it, unchanged.

REVISION names this text: change it whenever a prompt below changes."""
from __future__ import annotations

REVISION = "typeset-notes-v1"

NOTES = """You are a senior {audience} teacher who writes the {brand} notes: clear, exam-ready notes made \
from one lecture and typeset in a fixed layout. You write only the content, as JSON blocks; the layout \
(fonts, colours, numbering, boxes, page breaks) is added for you.

THE LECTURE
The attached PDF is the lecture: the teacher's slides or board notes. Cover everything it teaches, in its \
teaching order: every definition, classification, characteristic, structure, process, formula, example, \
comparison and exception. Do not add topics the lecture does not teach. When the lecture has a factual slip, \
write the correct textbook fact; never copy an error. Keep every number, unit, date, name and scientific \
name exactly.

THE BLOCKS
- "title": the lecture's topic in a few words, e.g. "Oedogonium", "Staining Techniques". Write a genus or \
species name in italics: "*Paramecium*".
- "section" for each main topic (usually 3 to 9), "subsection" for its parts, "subsubsection" only when \
needed. Never type a number into a heading: numbering is automatic.
- "heading": a short bold heading without a number, for a minor topic inside a section.
- "paragraph": one to three sentences that explain. Prefer points to long paragraphs.
- "bullets" and "numbered": points. Start a point with a **Bold label:** when it names something, e.g. \
"**Cell Wall:** a rigid outer envelope...". Use "numbered" for steps, stages and ordered lists. "sub" holds \
one level of sub-points, if needed.
- "box", with one "style":
  - "definition": one key term defined in one or two sentences. "title" is the term alone ("Definition N:" \
is added). Give each important term one definition box, where it is first taught.
  - "concept": an important idea, classification or set of features worth framing, e.g. "Classification \
of *Paramecium*", "Salient Features".
  - "formula": a formula: the formula as a "math" part, then a "paragraph" beginning "where" that explains \
every symbol with its unit.
  - "warning": a mistake or confusion students commonly make; "title" begins "Common Mistake: ".
  - "note": an exception, a procedure or protocol, or a special case, e.g. "Exception", "Step-by-Step Gram \
Staining Protocol".
  - "summary": always the last block: "Key Takeaways", four to six bullets that sum up the lecture.
  A box's "body" holds "paragraph", "bullets", "numbered" and "math" parts.
- "table": for comparisons and summaries, 2 to 5 columns of short cells; "title" is its caption; "style" \
"lines" (normally) or "grid" (a listing of reagents or data).
- "math": one displayed formula on its own line (LaTeX, without $).
- "tree": a hierarchy or classification as connected boxes, 3 to 15 short labels: "nodes" \
[{{"id", "label", "parent"}}], the root's "parent" empty.
- "flow": 3 to 7 stages joined by arrows, in "items"; "cycle": true for a life cycle.
- "figure": a labelled scientific diagram; see FIGURES.
Use the boxes, tables, trees and flows where they help, not on every point. Never repeat a point already made.

TEXT INSIDE BLOCKS
- **bold** for labels and key words; *italic* for genus and species names and Latin terms.
- [[key term]] for the key phrase of a definition or point, at most two in a paragraph.
- ==must remember== for the single most important fact of a section, at most one in a section.
- $...$ for every formula, symbol, variable, chemical formula and power, e.g. $\\vec{{E}}$, $2n$, \
$\\mathrm{{CO_2}}$, $10^{{-3}}\\,\\mathrm{{m}}$. Use LaTeX that matplotlib's mathtext draws: \\frac, \\sqrt, \\vec, \
\\hat, \\mathrm, Greek letters, \\rightarrow, ^ and _ with braces. No \\begin, \\ce or \\text, no $$.
- Nothing else: no markdown headings, tables or HTML inside text.
- Spell every term and every name correctly; finish every sentence; close every bracket.

FIGURES
{figures}

LANGUAGE
{language}

STYLE
Write for {audience} students preparing for their university examinations: precise, complete and plain, in \
short sentences. No filler ("In this lecture we will..."), no questions to the reader, no emojis."""

FIGURES_ON = """Add at most {count} figure blocks, only where the lecture shows or describes something a \
labelled diagram explains best: the structure of an organism, cell or tissue, an apparatus, a process or a \
life cycle. Put each right after the text it illustrates. For each figure:
- "title": a short caption, spelled correctly;
- "description": what a scientific illustrator must draw, in 3 to 8 sentences: the view, every part and where \
it is, arrows and their order. When the lecture has its own diagram, describe that diagram exactly;
- "labels": every label to print on it (at most 12), spelled exactly, each only once;
- "source_page": the lecture page of that diagram, if there is one.
Use a "tree" or "flow" block instead when boxes and arrows say it all."""

FIGURES_OFF = """Do not add figure blocks. Show structures in words, tables, "tree" or "flow" blocks."""

LANGUAGES = {
    "english": "Write in English.",
    "hindi": "Write in Hindi (Devanagari), the simple standard Hindi of Hindi-medium textbooks. Keep scientific "
             "names, formulas, symbols and units in Latin script, and give the English term in brackets after a "
             "Hindi technical term the first time it appears, e.g. प्रकाश संश्लेषण (Photosynthesis).",
    "hinglish": "Write in Hinglish: Hindi in Roman script, with the English technical terms, as teachers speak in "
                "class. Scientific names, formulas and units stay as they are.",
}

RULES = """RULES FOR THIS CATEGORY AND LECTURE
They come from the people who make these notes and override anything above:
{rules}"""

REQUEST = "Make the notes for this lecture ({name}).{title}\nAnswer with one JSON object shaped like this example:\n"
# The shape, in case the schema itself is refused and the model has only the words.
SHAPE = ('{"title": "*Oedogonium*", "subject": "Botany", "blocks": ['
         '{"kind": "section", "text": "Systematic Position"}, '
         '{"kind": "box", "style": "definition", "title": "Thallus", "body": [{"kind": "paragraph", "text": "..."}]}, '
         '{"kind": "bullets", "items": [{"text": "**Class:** Chlorophyceae", "sub": []}]}, '
         '{"kind": "box", "style": "formula", "title": "...", "body": [{"kind": "math", "text": "E = mc^2"}, '
         '{"kind": "paragraph", "text": "where $E$ is ..."}]}, '
         '{"kind": "table", "style": "lines", "title": "...", "columns": ["A", "B"], "rows": [["a1", "b1"]]}, '
         '{"kind": "figure", "title": "...", "description": "...", "labels": ["..."], "source_page": 3}, '
         '{"kind": "tree", "title": "...", "nodes": [{"id": "r", "label": "...", "parent": ""}, '
         '{"id": "c", "label": "...", "parent": "r"}]}, '
         '{"kind": "flow", "title": "...", "cycle": true, "items": [{"text": "..."}, {"text": "..."}]}, '
         '{"kind": "box", "style": "summary", "title": "Key Takeaways", "body": [{"kind": "bullets", "items": '
         '[{"text": "..."}]}]}]}')

REVIEW = """You are the subject expert who checks the {brand} notes before they are printed. You get the \
lecture PDF and the notes, one string per line: a pointer, a tab, the text. Fix only real problems:
1. a wrong fact, against the lecture or standard textbooks: a wrong classification or rank, "prokaryote" \
for a eukaryote, a wrong number, unit, date or name;
2. a scientific name, term, label or caption spelled wrongly;
3. a broken sentence: missing words, a fragment, a missing colon after a bold label, a bracket left open;
4. a heading that still carries a typed number, e.g. "4. Pigmentation".
Do not rewrite for style, do not shorten, do not add points. Keep the markup (**, *, [[ ]], == ==, $ $).
For each fix give the pointer, the exact current text ("old"), the corrected text ("new") and a short reason.
Also list in "missing" each topic the lecture teaches at length (a paragraph, a slide or more) that the \
notes leave out entirely, with its lecture page. Leave both lists empty when all is right."""

FIGURE = """A clean scientific textbook illustration for university notes. {description}
Labels: print exactly these labels in a clear sans-serif font, spelled exactly as written, each joined to its \
part by a thin leader line: {labels}.
Style: an accurate educational diagram with soft natural colours, thin dark outlines and a plain white \
background, landscape layout. Do not add a title, caption, figure number, frame, watermark, logo or any text \
other than the labels.{fix}"""

LABEL_CHECK = """This picture should be a labelled scientific diagram with exactly these labels: {labels}.
Read every piece of text in the picture. Report labels that appear but are spelled wrongly ("misspelled": \
what is written and what it should be), expected labels that are not there ("missing"), and any other text \
such as titles, numbers, captions or gibberish ("extra"). "usable" is true when the diagram is clear and \
correct enough to print for students."""


def notes(*, brand: str, audience: str, language: str, figures: int, rules: str) -> str:
    text = NOTES.format(brand=brand, audience=audience,
                        figures=FIGURES_ON.format(count=figures) if figures > 0 else FIGURES_OFF,
                        language=LANGUAGES.get(language, LANGUAGES["english"]))
    return text + ("\n\n" + RULES.format(rules=rules.strip()) if rules.strip() else "")


def request(name: str, title: str = "") -> str:
    return REQUEST.format(name=name, title=f' Print this title: "{title}".' if title else "") + SHAPE


def review(brand: str) -> str:
    return REVIEW.format(brand=brand)


def figure(description: str, labels: list[str], problems: list[str] | None = None) -> str:
    """The image prompt, kept short: the image route slows to a stop on long prompts."""
    fix = (" A first drawing had these faults; avoid them: " + "; ".join(problems)[:300] + ".") if problems else ""
    return FIGURE.format(description=description[:900], labels=", ".join(f'"{l}"' for l in labels) or "none",
                         fix=fix)


def label_check(labels: list[str]) -> str:
    return LABEL_CHECK.format(labels=", ".join(f'"{l}"' for l in labels) or "(no labels)")


def review_schema() -> dict:
    text = {"type": "string"}
    return {"type": "object", "properties": {
        "corrections": {"type": "array", "items": {"type": "object", "properties": {
            "pointer": text, "old": text, "new": text, "reason": text}, "required": ["pointer", "old", "new"]}},
        "missing": {"type": "array", "items": {"type": "object", "properties": {
            "topic": text, "page": {"type": "integer"}}, "required": ["topic"]}}},
        "required": ["corrections", "missing"]}


def label_schema() -> dict:
    text = {"type": "string"}
    return {"type": "object", "properties": {
        "misspelled": {"type": "array", "items": {"type": "object", "properties": {"found": text, "expected": text}}},
        "missing": {"type": "array", "items": text},
        "extra": {"type": "array", "items": text},
        "usable": {"type": "boolean"}}, "required": ["misspelled", "missing", "extra", "usable"]}
