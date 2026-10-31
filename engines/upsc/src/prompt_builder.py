from __future__ import annotations

from datetime import date

import re

from .config import (
    DEFAULT_QUALITY_OPTIONS,
    DEFAULT_UPSC_FACT_MODE,
    DEFAULT_UPSC_NOTE_DEPTH,
    DEFAULT_UPSC_NOTE_TYPE,
    QUALITY_OPTIONS,
    ROOT_DIR,
    UPSC_AREAS,
    UPSC_FACT_MODES,
    UPSC_NOTE_DEPTHS,
)
from .models import SlideData
from .transcript_cleaner import glossary_prompt_block
from .upsc_subjects import subject_focus, subject_label

PROMPT_DIR = ROOT_DIR / "prompts"

#: Note Studio (the one allowed change in this copy): requirements the operator
#: set in Note Studio (note type, category, subject, this run), appended after
#: everything else in the notes prompts. Empty = every prompt exactly as before.
EXTRA_INSTRUCTIONS = ""


def _with_extra(prompt: str) -> str:
    extra = (EXTRA_INSTRUCTIONS or "").strip()
    if not extra:
        return prompt
    return (prompt + "\n\nADDITIONAL REQUIREMENTS FOR THIS CLASS (from Note Studio). They decide what the "
            "notes contain; they never change the markers or the output format above.\n" + extra)

# The four slots in prompts/upsc_dcn_en.txt, matched in one pass so that text
# substituted into one slot is never rescanned as another (see
# build_upsc_dcn_prompt).
_DCN_PLACEHOLDER_RE = re.compile(
    r"\{(subject|depth|transcript_block|slides_block|lecture_title|glossary_block"
    r"|length_budget)\}")


from .languages import is_devanagari, language_name, normalise, vocabulary_rule
from .note_budget import budget_block, chunk_budget_line


def base_prompt_file_name(mode: str, language_code: str) -> str:
    return f"base_{mode.lower()}_{language_code.lower()}.txt"


def load_prompt_template(mode: str, language_code: str) -> str:
    """Load the shared UPSC note template for a mode + language.

    The per-subject emphasis is layered on separately (see subject_focus), so
    only four base files exist instead of one per (subject, mode, language).
    """
    path = PROMPT_DIR / base_prompt_file_name(mode, language_code)
    if not path.exists():
        raise FileNotFoundError(f"Base prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def format_slides(slides: list[SlideData]) -> str:
    chunks: list[str] = []
    for s in slides:
        text = (s.prompt_text or "").strip()
        if not text:
            text = "[No extractable text. Use attached slide image if provided.]"
        chunks.append(
            f"--- SLIDE {s.slide_no} ---\n"
            f"Heading detected: {s.heading or '[none]'}\n"
            f"Image available: {'yes' if s.image_path else 'no'}\n"
            f"Slide text / annotations:\n{text}\n"
        )
    return "\n".join(chunks)


def _subject_header(subject: str, language_code: str) -> str:
    label = subject_label(subject)
    focus = subject_focus(subject, language_code)
    header = f"\nUPSC subject for this file: {label}."
    if focus:
        # The focus says what to prioritise, not what to supply. Without this
        # guard, lines like "give conventions with year + full form" read as an
        # instruction to fill those in from the model's own knowledge.
        header += (
            f"\nSubject-specific emphasis: {focus}"
            "\n  (This emphasis applies ONLY to material present in the source. It tells you what to"
            " prioritise and how to present it — it is not permission to supply facts the source"
            " does not contain. If the source lacks a detail named here, leave it out.)"
        )
    return header


def _language_rule(language_code: str) -> str:
    """Lock the OUTPUT language, whatever language the class was taught in.

    This used to open "This is a Hindi lecture", which was true while the output
    language was DETECTED from the lecture. The user now chooses it and the two
    are independent: a Hindi class can be asked for Marathi notes. Telling a
    model "this is a Hindi lecture" while asking it for Marathi invites it to
    drift back to the source language, so the rule names only the target.

    Marathi is named explicitly rather than folded in with Hindi. Both are
    Devanagari, and a model told "write in Devanagari" writes Hindi.
    """
    target = normalise(language_code) or "en"
    if target == "en":
        return (
            "\n- LANGUAGE [NON-NEGOTIABLE]: Write the ENTIRE notes in clear English, whatever "
            "language the class was taught in. Translate non-English content rather than "
            "transliterating it. Keep proper nouns as-is. Do NOT output Hindi or Marathi notes."
        )
    name = language_name(target)
    other = "Marathi" if target == "hi" else "Hindi"
    return (
        f"\n- LANGUAGE [NON-NEGOTIABLE]: Write the ENTIRE notes in {name} (Devanagari), whatever "
        f"language the class was taught in — translate the content, do not transliterate the "
        f"lecture's sentences. Use {name} grammar and {name} exam vocabulary; this must NOT read "
        f"as {other} written in Devanagari.{vocabulary_rule(target)} "
        f"Do NOT output English notes."
    )


def _transcript_block(transcript: str) -> str:
    """Frame the lecture transcript so the model uses each source for what it's good at.

    The slides are authoritative for structure, spellings, Article/scheme names
    and diagrams; the transcript carries what the teacher actually said —
    explanations, examples, and exam emphasis. YouTube auto-captions are noisy
    (no punctuation, mangled terms), so the slides are declared the tie-breaker.
    """
    if not (transcript or "").strip():
        return ""
    return f"""

LECTURE TRANSCRIPT (what the teacher said in class for these slides)
Use BOTH sources, each for its strength:
- SLIDES are authoritative for structure, headings, diagrams/maps, and the correct
  spelling of terms, Article numbers, dates, schemes, and names.
- TRANSCRIPT is authoritative for the teacher's explanation: the reasoning, examples,
  analogies, context, and any emphasis on what matters for the exam (e.g. "this is
  important for Prelims", "this was asked in 2019").
Rules:
- The transcript is usually an AUTOMATIC caption of a code-switched (Hinglish) class,
  so expect two specific defects and read through them:
  (a) English speech transliterated into Devanagari — e.g. "गवर्नेंस" = governance,
      "अकाउंटेबिलिटी" = accountability, "डिस्क्रीशनरी" = discretionary. Recover the
      intended English term; never copy the transliteration into the notes.
  (b) Technical terms corrupted into similar-sounding words — e.g. "प्रोबेबिलिटी"
      (probability) when the topic is clearly "probity". Use the slides and the
      surrounding context to recover the correct term.
- When the two sources disagree, TRUST THE SLIDES for facts, terms, and spellings.
  A term that appears on a slide is correct; the transcript's version of it is not.
- Add substantive explanation from the transcript that the slides do not contain.
- Ignore classroom chatter (greetings, breaks, "can you hear me", promotions).
- Do NOT invent anything that is in neither source.

TRANSCRIPT STARTS
{transcript.strip()}
TRANSCRIPT ENDS
"""


def build_generation_prompt(subject: str, mode: str, language_code: str, slides: list[SlideData], *, chunk_label: str = "", transcript: str = "", error_report: bool = False, error_report_strictness: str = "standard") -> str:
    template = load_prompt_template(mode, language_code)
    label = f"\nYou are processing chunk: {chunk_label}." if chunk_label else ""
    subject_rules = _subject_header(subject, language_code) + _language_rule(language_code)
    if mode.lower() == "summary":
        subject_rules += (
            "\n- REVISION MODE (STRICT): Keep it short. Capture only the highest-yield facts and "
            "essential pointers as brief one-line bullets. Do NOT write long explanations, "
            "background, or elaboration. Prefer the fewest words that preserve every examinable fact."
        )
    return f"""{template}

Important automation instructions:
- Preserve original slide/page numbers in every DTP note.
- If a slide image is attached and text extraction misses handwritten annotations, maps, or tables, read the image and include the instructional content.
- Exclude QR/ads/promotion/thank-you content. Do not reproduce MCQ options; mine questions only for the fact they test.
- Do not mention that you used AI. Output only the final notes content.{subject_rules}
{label}
{_transcript_block(transcript)}
SLIDE DATA STARTS BELOW

{format_slides(slides)}
{error_report_contract_block(error_report_strictness, enabled=error_report)}
""".strip()


def format_slides_reference(slides: list[SlideData]) -> str:
    """Compact slide listing for the transcript-driven prompt.

    Slide *images* are not sent on this path, so "Image available" is what tells
    the model a diagram exists on that slide and a DTP note should be written for
    it. The slide number must survive: it is how the DOCX writer later finds the
    image to insert.
    """
    lines: list[str] = []
    for s in slides:
        text = " ".join((s.prompt_text or "").split())
        lines.append(
            f"[Slide {s.slide_no}] heading: {s.heading or '[none]'} | "
            f"image available: {'yes' if s.image_path else 'no'}\n"
            f"    text: {text or '[no extractable text]'}"
        )
    return "\n".join(lines)


def build_transcript_notes_prompt(
    subject: str,
    mode: str,
    language_code: str,
    slides: list[SlideData],
    transcript: str,
    *,
    error_report: bool = False,
    error_report_strictness: str = "standard",
) -> str:
    """Build UPSC notes from a class recording, in ONE call.

    The transcript is the content and the slides are reference. This is the right
    shape for PW classes: the deck is usually sparse (headings + keywords) while
    the teacher's 2-hour explanation carries the substance. A 2-hour transcript is
    only ~21K tokens, so the whole class fits in one call — which beats chunking,
    because the model sees the full arc of the lecture and nothing has to be
    stitched back together afterwards.
    """
    template = load_prompt_template(mode, language_code)
    rules = _subject_header(subject, language_code) + _language_rule(language_code)
    if mode.lower() == "summary":
        rules += (
            "\n- REVISION MODE (STRICT): Keep it short. Only the highest-yield facts and pointers "
            "as one-line bullets. No long explanations or elaboration."
        )
    return f"""{template}

HOW TO USE YOUR TWO SOURCES [MOST IMPORTANT INSTRUCTION]
These notes are for a recorded UPSC class. You have exactly two sources, with a
fixed weighting:

1. CLASS TRANSCRIPT — the BASE of the notes: aim for 80-90% of the content.
   This is what the teacher actually taught: the concepts, reasoning, examples,
   context, and any exam emphasis ("this is important for Prelims", "this was asked
   in 2019"). Follow the teacher's flow and cover everything substantive they said.
2. SLIDE REFERENCE — the remaining 10-20%, in two roles:
   (a) GAP-FILLING: if a slide carries a point, fact, figure, list item, or example
       that the teacher did NOT cover, ADD it to the notes under the right heading.
       State it as the slide states it — do not expand or elaborate on it.
   (b) CORRECTION/STRUCTURE: heading wording and topic order, the CORRECT spelling of
       terms, Article numbers, dates, Acts, schemes and names, and which slide holds a
       diagram/map/table worth inserting.

[HARD RULE — NO OUTSIDE CONTENT]
Every single line of the notes MUST come from the transcript or the slides.
- Do NOT add background, context, definitions, examples, current affairs, or
  explanation from your own knowledge — even if it is correct, relevant, or would
  make the notes better.
- Do NOT expand a point beyond what the source says. If the teacher stated a fact
  without explaining it, record the fact without explaining it.
- If neither source covers something, OMIT it. Incomplete notes are correct; invented
  notes are not.
- Never guess a number, date, Article, name or scheme that is not in a source.

Method:
- Cover everything substantive the teacher taught, in the order taught.
- Organise it under the slides' topic headings where they line up; if the teacher
  taught something not on any slide, still include it under a sensible heading.
- The transcript is an AUTOMATIC caption of a code-switched (Hinglish) class. Expect
  and silently correct two defects:
  (a) English speech transliterated into Devanagari — "गवर्नेंस" = governance,
      "अकाउंटेबिलिटी" = accountability. Recover the intended English term; never copy
      the transliteration into the notes.
  (b) Technical terms corrupted into similar-sounding words — e.g. "प्रोबेबिलिटी"
      (probability) where the topic is plainly "probity". Use the slides and context
      to recover the correct term.
  Correcting these defects is recovering what the teacher actually said — it is NOT
  adding outside content.
- When transcript and slides disagree on a fact, term, number, or spelling, the
  SLIDES ARE CORRECT.
- Ignore classroom chatter: greetings, "can you hear me", breaks, promotions, revision
  of the previous class unless it carries real content.

Diagram policy
For each slide that has "image available: yes" AND holds a diagram/map/table the notes
need, add this exact note immediately after the relevant bullet:
(Note to DTP: Insert the image with "<label1>" and "<label2>" from slide <Slide#> under the heading "<Heading>".)
The slide number must be the real one from the SLIDE REFERENCE below.

Other automation instructions:
- Do not mention that you used AI, a transcript, or slides. Output only the final notes.{rules}

CLASS TRANSCRIPT STARTS
{(transcript or '').strip()}
CLASS TRANSCRIPT ENDS

SLIDE REFERENCE STARTS
{format_slides_reference(slides)}
SLIDE REFERENCE ENDS
{error_report_contract_block(error_report_strictness, enabled=error_report)}
""".strip()


# ---------------------------------------------------------------------------
# UPSC note types (complete / concise / mains_ready / prelims_ready / revision).
# Everything below is additive. The base_*.txt path above keeps its strict
# "nothing beyond the source" rule; these prompts deliberately allow the model
# to fact-check the faculty and add exam value, with a visible pointer for
# every correction.
# ---------------------------------------------------------------------------

def upsc_prompt_file_name(note_type: str, language_code: str) -> str:
    return f"upsc_{(note_type or '').lower()}_{(language_code or 'en').lower()}.txt"


def load_upsc_prompt_template(note_type: str, language_code: str = "en") -> str:
    """Load prompts/upsc_{note_type}_{lang}.txt, falling back to the English file.

    Only the _en files exist today, so the fallback means a Hindi class still
    gets the right note-type instructions (the language lock below then forces
    Hindi output) instead of crashing on a missing translation.
    """
    note_type = (note_type or DEFAULT_UPSC_NOTE_TYPE).lower()
    path = PROMPT_DIR / upsc_prompt_file_name(note_type, language_code)
    if path.exists():
        return path.read_text(encoding="utf-8")
    fallback = PROMPT_DIR / upsc_prompt_file_name(note_type, "en")
    if fallback.exists():
        return fallback.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"UPSC prompt template not found: {path}. No English fallback either "
        f"({fallback}). Expected '{upsc_prompt_file_name(note_type, 'en')}' in {PROMPT_DIR}."
    )


# Area keys offered in the UI, minus the "auto" sentinel — listed to the model
# so an inferred area is named the same way the UI names it.
_AREA_KEYS: tuple[str, ...] = tuple(k for k in dict.fromkeys(UPSC_AREAS.values()) if k != "auto")


def _upsc_area_header(area: str, language_code: str) -> str:
    """Name the UPSC area (or ask the model to infer it) plus its emphasis."""
    key = (area or "auto").strip().lower()
    if key in {"", "auto"}:
        names = ", ".join(subject_label(k) for k in _AREA_KEYS)
        return (
            "\nUPSC area for this class: NOT SPECIFIED — infer it yourself from the slides and the "
            f"transcript. Choose from: {names}."
            "\n  Name the area you inferred in the first line of the quality report's UPSC Relevance "
            "Check, and apply that area's normal emphasis (exact names, years, bodies, data, and the "
            "debates it is examined on)."
        )
    return _subject_header(key, language_code) + (
        "\n  (Scope note for UPSC note types: the note-type instructions at the top of this prompt "
        "govern what you may add. You MAY add standard definitions, Prelims keywords, Mains linkages, "
        "well-known examples and standard way-forward points, and you MAY correct a wrong faculty "
        "point — provided the addition is accurate and standard, and every correction carries a "
        "visible pointer block.)"
    )


_QUALITY_LEVEL_RULES = {
    "off": (
        "QUALITY CHECK LEVEL: OFF.\n"
        "- Do NOT fact-check the faculty. Assume every faculty point is correct.\n"
        "- Only clean and structure the class content: remove housekeeping, chatter and repetition, "
        "fix obvious transcription noise, and lay the content out under proper headings and bullets.\n"
        "- Emit NO correction pointer blocks of any kind, even if something looks wrong.\n"
        "- The quality report must still be emitted between its markers, but it must be EMPTY of "
        "findings: write 'Quality check was turned off for this run.' and nothing else."
    ),
    "basic": (
        "QUALITY CHECK LEVEL: BASIC.\n"
        "- Flag ONLY obvious problems: a plainly wrong fact, a plainly wrong date/year/Article/number, "
        "and duplicated or repeated points.\n"
        "- Do NOT run the full check list. Do NOT flag debatable framing, ordering, missing context or "
        "stylistic weaknesses.\n"
        "- If you are unsure whether something is wrong, leave it alone rather than emitting a pointer."
    ),
    "strict": (
        "QUALITY CHECK LEVEL: STRICT.\n"
        "- Run the full check list in the instructions above on every substantive faculty point: "
        "factual and conceptual correctness, chronology, cause-effect flow, data/date mismatch, "
        "Article mismatch, scheme-ministry mismatch, institution mismatch, map/location mismatch, "
        "wrong definitions, outdated claims, overgeneralisation, duplicates and missing context.\n"
        "- Still do not over-flag: a correctly taught, well-established point needs no pointer."
    ),
}


def _quality_block(quality_level: str, quality_options: dict | None) -> str:
    """Spell out the quality level and exactly which of the 13 toggles are on.

    Both halves are needed: the level decides how hard to look, the toggles
    decide what the user actually asked for. Disabled items are listed too,
    because a prompt that only lists what to do leaves the model free to do the
    rest anyway.
    """
    level = (quality_level or "strict").strip().lower()
    if level not in _QUALITY_LEVEL_RULES:
        level = "strict"
    options = dict(DEFAULT_QUALITY_OPTIONS)
    options.update({k: bool(v) for k, v in (quality_options or {}).items() if k in QUALITY_OPTIONS})
    if level == "off":
        # OFF must win over any checkbox left ticked in the UI.
        for key in options:
            if key.startswith("check_") or key == "add_correction_pointers":
                options[key] = False

    enabled = [QUALITY_OPTIONS[k] for k in QUALITY_OPTIONS if options.get(k)]
    disabled = [QUALITY_OPTIONS[k] for k in QUALITY_OPTIONS if not options.get(k)]
    lines = [
        "",
        "QUALITY CHECK CONTROL [OVERRIDES ANY CONFLICTING INSTRUCTION ABOVE]",
        _QUALITY_LEVEL_RULES[level],
        "",
        "Enabled for this run — do these:",
    ]
    lines += [f"- {label}" for label in enabled] or ["- (nothing)"]
    lines += ["", "Disabled for this run — do NOT do these:"]
    lines += [f"- {label}" for label in disabled] or ["- (nothing)"]
    if not options.get("add_correction_pointers"):
        lines.append(
            "- Because correction pointers are disabled, do not put any [Faculty Point Corrected] / "
            "[Faculty Point Removed] / [Needs Human Review] block inside the notes. Record findings "
            "in the quality report's Correction Log only."
        )
    if not options.get("add_tables"):
        lines.append("- Do not create markdown tables; write the same content as bullets.")
    if not options.get("add_flowcharts"):
        lines.append("- Do not create flowcharts; write the same content as bullets.")
    return "\n".join(lines)


def _upsc_sources_block(transcript: str) -> str:
    """Weight the two sources: the class recording is the base, slides are reference."""
    if not (transcript or "").strip():
        return """
HOW TO USE YOUR SOURCE [MOST IMPORTANT INSTRUCTION]
No class transcript is available, so the SLIDES are the whole faculty content. Cover every
substantive point on them in slide order, and treat a slide point exactly as you would a
point the faculty spoke aloud: clean it, structure it, and fact-check it under the quality
rules below.
"""
    return """
HOW TO USE YOUR TWO SOURCES [MOST IMPORTANT INSTRUCTION]
Both are "faculty content" and both are fact-checked, but they carry different weight:
1. CLASS TRANSCRIPT — the BASE of the notes: aim for 80-90% of the content. This is what
   the faculty actually taught: concepts, reasoning, examples, context and exam emphasis
   ("this is important for Prelims", "this was asked in 2019"). Follow the teaching flow
   and cover everything substantive that was said.
2. SLIDE REFERENCE — the remaining 10-20%, in two roles:
   (a) GAP-FILLING: a point, figure, list item or example printed on a slide but never
       spoken still belongs in the notes, under the right heading.
   (b) STRUCTURE/SPELLING: heading wording and topic order, and the correct spelling of
       terms, Article numbers, dates, Acts, schemes and names.
The transcript is usually an AUTOMATIC caption of a code-switched (Hinglish) class. Expect
and silently repair two defects — this is recovering what the faculty said, not correcting
them, so it needs NO pointer block:
  (a) English speech transliterated into Devanagari — "गवर्नेंस" = governance,
      "अकाउंटेबिलिटी" = accountability. Recover the intended English term.
  (b) Technical terms corrupted into similar-sounding words — "प्रोबेबिलिटी" (probability)
      where the topic is plainly "probity". Use the slides and context to recover it.
When transcript and slides disagree on a term, number or spelling, the SLIDES are correct.
When both are factually wrong, correct them under the quality rules and emit a pointer.
"""


# Slide images are not sent on this path (one text-only call), so this note is
# the ONLY way a diagram reaches the DOCX: the writer looks up the slide number
# and inserts that slide's image.
_DIAGRAM_POLICY = """
DIAGRAM POLICY
For each slide marked "image available: yes" that holds a diagram, map, chart or table the
notes need, add this exact line immediately after the relevant bullet:
(Note to DTP: Insert the image with "<label1>" and "<label2>" from slide <Slide#> under the heading "<Heading>".)
Use the real slide number from the SLIDE REFERENCE below. These lines are stripped from the
final document, so they are not part of the notes' word count and need no pointer block.
"""

def _student_ready_policy(note_type: str, language_code: str) -> str:
    """Return the strict student-handout contract for the current run."""
    note_type = (note_type or "complete").lower()
    depth = {
        "complete": "DETAILED: retain almost all substantive lecture content, improving its flow.",
        "concise": "CONCISE: compress explanations, but retain every important concept and examinable fact.",
        "revision": "REVISION: keep only high-yield facts, keywords, useful tables and process flowcharts.",
        "prelims_ready": "PRELIMS-READY: prioritise source-based definitions, classifications, facts and keywords.",
        "mains_ready": "MAINS-READY: prioritise source-based arguments, causes, impacts, challenges and solutions.",
    }.get(note_type, "DETAILED: retain almost all substantive lecture content.")
    # The correction template is shown in the note's own script. Hindi and
    # Marathi share it: these are four field labels, and the model fills them
    # in whichever language the LANGUAGE rule above locked.
    hindi = is_devanagari(language_code)
    correction = (
        '''[सुधार नोट]
मूल बिंदु: "<faculty का छोटा original point>"
सही रूप: "<student-ready corrected point>"
कारण: "<संक्षिप्त कारण>"
विश्वास स्तर: High / Medium / Low

[सत्यापन आवश्यक]
बिंदु: "<original point>"
समस्या: "<क्यों verification चाहिए>"'''
        if hindi
        else '''[Correction Note]
Original point: "<short original faculty point>"
Corrected version: "<corrected student-ready point>"
Reason: "<brief reason>"
Confidence: High / Medium / Low

[Needs Verification]
Point: "<original point>"
Issue: "<why it needs verification>"'''
    )
    structure = (
        "मुख्य sections रखें: कवर किए गए मुख्य विषय; मुख्य नोट्स; Prelims Pointers; "
        "Mains Pointers; महत्वपूर्ण शब्दावली; Quick Revision (8-15 बिंदु)।"
        if hindi
        else "Use these main sections: Concepts Covered; Main Notes; Prelims Pointers; "
        "Mains Pointers; Important Keywords; Quick Revision (8-15 bullets)."
    )
    return f"""
STUDENT-READY EDITORIAL POLICY [OVERRIDES CONFLICTING TEMPLATE TEXT]
- NOTE DEPTH: {depth}
- SOURCE FIDELITY: The lecture slides and transcript are the only content base. Do not add
  outside schemes, reports, data, examples, Articles, committees, current affairs,
  definitions, background, case studies or generic way-forward points merely as value-add.
- STUDENT QUERIES: Exclude every student question, doubt, interruption, chat message and
  teacher-student exchange from the notes. If the faculty's answer contains a substantive
  concept that was not otherwise taught, keep only that clean concept as a normal note;
  never reproduce the student's wording, the question-answer format, or dialogue.
- CURRENT EXAMPLES: Do not mistake lecture-taught current affairs, news references, recent
  judgments, schemes, reports, events or contemporary examples for classroom chatter.
  Retain every such example that appears in the slides or transcript, attach it to the
  relevant concept, and preserve its stated date/year/source. Do not invent or update it
  from outside knowledge. If its accuracy or recency is uncertain, use Needs Verification.
- Standard factual knowledge may be used ONLY to correct a clear faculty error. If the
  correction is not confidently verifiable, flag it as Needs Verification.
- Never silently correct or discard a wrong, unclear, outdated, repeated, irrelevant or
  conceptually mismatched faculty point. Put the relevant visible block immediately where
  the point belongs and mirror it in the quality report's correction log.
- Use these exact note blocks:
{correction}
- Do not use the older [Faculty Point Corrected], [Faculty Point Removed], or
  [Needs Human Review] labels.
- {structure} Add a Needs Verification section only when doubtful points exist.
- SUBJECT-SPECIFIC KEYWORDS: Build Important Keywords and Prelims/Mains terminology from
  the selected or inferred UPSC subject, using only terms actually present in the lecture.
  Prefer the subject's standard exam vocabulary (for example constitutional terminology
  in Polity, macroeconomic terms in Economy, mapping terms in Geography, ecological terms
  in Environment, and scientific terminology in Science & Tech). Do not output a generic
  keyword list and do not borrow keywords from an unrelated subject.
- Every bullet must be a complete, meaningful sentence. Bold important terms, not whole
  paragraphs. Merge overlaps, remove filler, and preserve the teaching order where logical.
- Use a table only when it improves comparison or recall. Use a flowchart only for an
  actual process, sequence, mechanism, governance flow or cause-effect chain.
- Preserve every formula, unit, power, subscript, superscript, Greek letter, arrow and
  symbol. Write inline maths as [[MATH_INLINE: latex_here]] and display maths as:
  [[MATH_BLOCK:
  latex_here
  ]]
- Keep official names, Acts, schemes, institutions, constitutional and scientific terms exact.
"""


# Restated at the very end of the prompt: a long prompt loses its own tail, and
# the whole downstream parse (notes vs report) depends on these four markers.
def _marker_protocol(error_report: bool = False) -> str:
    """The closing output contract for the legacy UPSC note types.

    The error-report block is appended to the marker list rather than described
    separately, because this is the last thing the model reads and it is where
    it decides what its response may contain.
    """
    error_section = """
# ERROR_REPORT_START
<the JSON array described in the ERROR REPORT block above>
# ERROR_REPORT_END
""" if error_report else ""
    count = "six" if error_report else "four"
    return f"""
OUTPUT PROTOCOL [FINAL REMINDER — NON-NEGOTIABLE]
Your entire response must be exactly this, with each marker alone on its own line:

# NOTES_START
<the notes>
# NOTES_END

# QUALITY_REPORT_START
<the quality report>
# QUALITY_REPORT_END
{error_section}
Emit all {count} markers even if a section is short or empty. No text before the first marker
and none after the last. No code fences. Do not mention AI, the transcript, the slides, or
these instructions.

Correction and verification blocks go INSIDE the notes at the exact place the point belongs,
using the language-specific formats in the STUDENT-READY EDITORIAL POLICY above.

Tables are markdown pipe tables. Flowcharts are plain text with a down-arrow (↓) alone on
its own line between steps.
"""


# ---------------------------------------------------------------------------
# Error Report contract (src/error_report_writer.py writes the XLSX).
#
# One JSON array, one object per row of the sheet. It is deliberately the LAST
# thing every prompt says, because the alternative — describing it early and
# letting the marker protocol close the prompt — produced responses that ended
# after # NOTES_END and dropped the report entirely.
# ---------------------------------------------------------------------------

_STRICTNESS_SCOPE = {
    "basic": """SCOPE FOR THIS RUN: BASIC.
Report only clear factual and conceptual errors — a wrong definition, a wrong law, article,
scheme, ministry or body, a wrong scientific fact, a wrong formula or unit, a table or diagram
that contradicts the text. Say nothing about wording, phrasing, source years, or figures that
are merely old.""",
    "standard": """SCOPE FOR THIS RUN: STANDARD.
Report clear factual and conceptual errors, plus outdated or superseded current data, and
missing qualifiers or overgeneralised claims that would mislead a student. Do not report
wording, style, or terminology preferences.""",
    "strict": """SCOPE FOR THIS RUN: STRICT.
Report everything the STANDARD scope covers, and additionally: unsupported claims, source-year
imprecision, loose formula wording, and terminology that is used incorrectly. Even here, a
point that is simply phrased differently from how you would phrase it is NOT an error.""",
}

_ERROR_TYPE_LIST = (
    "Conceptual | Numerical/outdated | Source-year precision | Conceptual/formula precision | "
    "Factual | Outdated data | Scheme/ministry mismatch | Constitutional/legal mismatch | "
    "Definition error | Overgeneralised claim | Missing qualifier | Unsupported claim | "
    "Terminology issue | Current-affairs update | Diagram/table mismatch | "
    "Unit/formula/symbol issue | Language clarity"
)


def error_report_contract_block(strictness: str = "standard", *, enabled: bool = True,
                                today: str = "") -> str:
    """The ERROR REPORT instruction block, or "" when the report is switched off.

    Returning "" rather than an "omit the report" instruction matters: a prompt
    that mentions the markers at all gets them emitted some of the time, and a
    stray JSON array in the response would be typeset into a student's notes.

    ``today`` is passed so the model has a reference point for "is this figure
    current?". Without it every currency judgement was made against an unknown
    date, which is how a correct 2024 figure gets filed as outdated.
    """
    if not enabled:
        return ""
    key = (strictness or "standard").strip().lower()
    scope = _STRICTNESS_SCOPE.get(key, _STRICTNESS_SCOPE["standard"])
    today = today or date.today().isoformat()
    return f"""
ERROR REPORT [SEPARATE FROM THE NOTES — REQUIRED]

TODAY'S DATE IS {today}. You have NO live web access and your training data ends before
this date. You therefore cannot know the current value of any figure that changes over
time, and you may be the one who is out of date, not the lecturer.
The notes themselves must already read correctly: put the corrected information into the notes
naturally, as settled fact, with no "the teacher said X" framing. Then list what you corrected,
once, as a JSON array between these two markers:

# ERROR_REPORT_START
[
  {{
    "original_statement": "the wrong statement, as it was made, shortened if long",
    "error_type": "one of the types below",
    "severity": "High",
    "error_concern": "what is wrong with it and why it matters, in one or two sentences",
    "correct_information_correction": "the corrected, student-ready version",
    "source_urls": ["https://official.example.gov.in/page"]
  }}
]
# ERROR_REPORT_END

Rules for the array — these are parsed by a machine, so they are absolute:
- Valid JSON. An array at the top level. No markdown, no tables, no comments, no trailing
  commas, no code fence, and no prose before or after it inside the markers.
- Use [] when there is genuinely nothing to report. Emit both markers either way.
- "severity" is exactly High, Medium or Low. Nothing else.
- "error_type" is one of: {_ERROR_TYPE_LIST}
- "source_urls" is a list of strings, and may be empty. Never invent a URL. Prefer official
  sources (ministry, PIB, RBI, Supreme Court, UN body, NCERT). If you have no source, use [].

How to set severity:
- High   — a wrong concept or fact, a wrong law/article/scheme/body, outdated current data, or
           a misleading statement. The kind of thing that must be fixed before students see it.
- Medium — imprecise formula wording, a missing qualifier, a partially correct point that could
           mislead, or an old figure where a newer one is preferred.
- Low    — source-year or wording precision, a minor update, a terminology or style point that
           does not change what the student understands.

{scope}

What NOT to put in the array:
- Points that were already correct, and anything you are merely restating.
- Praise, summaries, counts, or remarks about the quality of the lecture.
- Notes-generation metadata: the model, the time taken, how you summarised, what you omitted
  for brevity, or any formatting or layout observation.
- Anything invented to make the report look fuller. An empty array is a good answer.

Any "use only the transcript and the slides" rule elsewhere in this prompt governs the NOTES.
It does not govern this array: use your own knowledge here to judge whether what was taught is
correct. Correcting a point in the array is not permission to add new material to the notes.

CLAIMS THAT SOMETHING IS OUTDATED [STRICTEST RULE IN THIS BLOCK]
An "outdated data" or "current-affairs update" row is only an ERROR if you can supply all
three of these in the row itself:
  1. the replacement value, stated exactly;
  2. the specific source that published it — the body and the document, not "official sources";
  3. when that source published it.
Write them into "correct_information_correction" and put the source in "source_urls".

If you cannot supply all three, it is NOT an error and must NOT be reported as one. A figure
you merely suspect has moved is a suspicion, and filing suspicions as errors is how this report
stops being read. Report it instead as severity "Low" with error_type "Numerical/outdated" and
the correction "Needs verification from official source before finalisation." — or leave it out.

Never write "Use the latest official figure available at finalisation." as a correction for a
value you did not check. That sentence reads as a finding and contains none.

The same rule applies to anything you would date-stamp: a scheme you believe was renamed, a
post you believe changed hands, a ranking you believe moved. Name the new value and the source,
or do not file it.

In the notes themselves, write such a point cautiously rather than asserting a number you
cannot stand behind.

For UPSC and current-affairs content: prefer official Indian government or UN sources, and
treat a superseded figure, a renamed scheme, and a wrong constitutional article as High.
For JEE/NEET and science content: report wrong formulae, wrong chemical formulae, wrong units
or symbols, wrong definitions, and diagram-label mismatches. Do NOT report a topic being
covered in less depth than you would like — that is not an error.
"""


def _upsc_transcript_section(transcript: str) -> str:
    """The transcript half of the UPSC source block, or an explicit 'absent' line.

    Saying the transcript is missing beats omitting it: with no line at all the
    model has been seen to hallucinate a lecture around the slide headings.
    """
    transcript_text = (transcript or "").strip()
    if transcript_text:
        return f"CLASS TRANSCRIPT STARTS\n{transcript_text}\nCLASS TRANSCRIPT ENDS\n"
    return "CLASS TRANSCRIPT: [not available for this class]\n"


def _upsc_slide_reference_section(slides: list[SlideData] | None) -> str:
    """The slide half of the UPSC source block, fenced by its start/end lines."""
    return (
        "SLIDE REFERENCE STARTS\n"
        f"{format_slides_reference(slides or [])}\n"
        "SLIDE REFERENCE ENDS"
    )


def build_upsc_prompt(
    *,
    note_type: str,
    area: str,
    language_code: str = "en",
    slides: list[SlideData],
    transcript: str = "",
    quality_level: str = "strict",
    quality_options: dict | None = None,
    error_report: bool = True,
    error_report_strictness: str = "standard",
) -> str:
    """Build the single-call prompt for a UPSC note type.

    Layered in a fixed order — note-type template, area emphasis, language lock,
    quality controls, then the sources — so the later, run-specific blocks can
    override the generic template above them.
    """
    template = load_upsc_prompt_template(note_type, language_code)
    rules = _upsc_area_header(area, language_code) + _language_rule(language_code)
    transcript_text = (transcript or "").strip()
    return _with_extra(f"""{template}

RUN SETTINGS FOR THIS CLASS{rules}
{_quality_block(quality_level, quality_options)}
{_student_ready_policy(note_type, language_code)}
{_upsc_sources_block(transcript_text)}
{_DIAGRAM_POLICY}
{_upsc_transcript_section(transcript_text)}
{_upsc_slide_reference_section(slides)}
{_marker_protocol(error_report)}
{error_report_contract_block(error_report_strictness, enabled=error_report)}""".strip())


# ---------------------------------------------------------------------------
# PW OnlyIAS DCN format (prompts/upsc_dcn_en.txt).
#
# Kept apart from build_upsc_prompt because the output contract is the opposite:
# DCN answers with marker text only (#LECTURE_TITLE:/#H1:/#H2:/#H3:/#TABLE_*/
# #FLOW_*/#SEPARATOR), carries its corrections inline in the running bullets, and
# emits NO quality report — that report is produced afterwards from the notes by
# src/upsc_quality_checker.py. So every block appended below has to express the
# run settings *within* that marker vocabulary; anything that asks for a new
# section, a NOTES_START/QUALITY_REPORT marker, or prose outside the markers
# would contradict the template's final rule and break the DOCX parser.
# ---------------------------------------------------------------------------

DCN_NOTE_TYPE = "dcn"


def _dcn_keyword_links_block() -> str:
    """The same keyword-link policy for whole lectures, chunks and merging."""
    return """
IMPORTANT KEYWORD HYPERLINKS
- Hyperlink the first substantive body mention of important keywords: technical
  terms and concepts, constitutional Articles, Acts, schemes, institutions,
  conventions and reports, when a reliable authoritative source URL is available.
- Put the link on the keyword itself using [**keyword**](https://source/path).
  Keep the label in the notes' language and preserve its bold emphasis. Keep
  headings readable; place the link in the explanatory bullet or table cell.
- Prefer the specific official definition, Act, scheme or report page, rather
  than a generic homepage or search results. Use only a known reliable URL;
  confidence in a domain alone does not establish a page's address. Never invent
  a URL to meet a link quota. If no reliable URL is available, leave the keyword
  bold and unlinked. Links must not introduce unsupported facts into the notes.
- Preserve existing useful keyword hyperlinks and their exact destinations when
  joining or editing parts. Do not strip links to plain text or expand URLs into
  the visible prose. Avoid repeatedly linking the same keyword in body bullets;
  retain source links on revision facts where needed for attribution.
""".strip()


def _dcn_subject_name(subject: str) -> str:
    """Human-readable area name for the template's {subject} slot."""
    key = (subject or "auto").strip().lower()
    if key in {"", "auto"}:
        names = ", ".join(subject_label(k) for k in _AREA_KEYS)
        return f"Not specified — infer it from the lecture. Choose from: {names}."
    return subject_label(key)


_DCN_FACT_MODE_RULES = {
    "source_only": (
        "FACT MODE: SOURCE ONLY.\n"
        "- Do NOT add any fact, figure, year, rank, scheme, report, judgment or example that is "
        "not in the lecture (transcript or slides), even when you are sure of it.\n"
        "- Do NOT update a lecture figure or status to the current one, and do not append a "
        "current-affairs remark of your own.\n"
        "- Still flag internal contradictions: where two lecture points disagree, keep both points "
        "and state the clash on the spot with 'Important distinction:' — or, when the lecture "
        "gives no way to settle it, write [Needs Verification: the lecture states both X and Y]."
    ),
    "verify": (
        "FACT MODE: VERIFY CURRENT FACTS WHERE POSSIBLE.\n"
        "- For any current figure, rank, count, status, target or date, prefer official sources: "
        "Government of India portals (gov.in), PIB, MoEFCC, CPCB, NITI Aayog, UN bodies, IPCC, "
        "IUCN, Ramsar, CBD and CITES.\n"
        "- Where the lecture's figure is outdated, keep the lecture point visible and give the "
        "current official position right after it with 'Important current update:'. Never delete "
        "the original point silently.\n"
        "- If a current figure cannot be confirmed from such a source, do not guess and do not "
        "round: write [Needs Verification: reason] immediately after the claim."
    ),
    "mark_uncertain": (
        "FACT MODE: MARK UNCERTAIN CURRENT DATA.\n"
        "- Do NOT silently update current data. Keep every figure exactly as the lecture taught it.\n"
        "- Mark EVERY current-data claim (latest figure, rank, count, share, status, target, "
        "'as of now', 'currently') with [Needs Verification: reason] immediately after the claim.\n"
        "- The single exception is a claim the lecture itself dates — it states the year, the "
        "report edition, or an 'as of <date>' — which is recorded with that date and left unmarked."
    ),
}


# What each depth actually changes. Substituting the label alone into {depth}
# left the setting inert — the three depths produced identical prompts, so the
# picker did nothing. These directives are what make it real. "Detailed" is
# stated as an anti-compression rule because over-shortening is the failure the
# reference notes exist to avoid.
_DCN_DEPTH_RULES = {
    "detailed": (
        "- DETAILED: this is the default and the reference standard. Keep every concept, "
        "definition, example, date, scheme, institution, Act, report and figure the lecture "
        "gives. Explain each point in a full sentence; do not compress a bullet into a phrase, "
        "and do not drop a sub-point because it seems minor.\n"
        "- Expect roughly one #H1 per major topic with several bullets beneath each, not a "
        "summary list."
    ),
    "concise": (
        "- CONCISE: keep every concept, definition, Act, scheme, institution and figure, but "
        "state each in one tight full sentence. Drop worked examples and repeated illustrations, "
        "keeping the single clearest one where an example is what makes the point land.\n"
        "- Never drop a fact to save space — shorten how it is said, not what is said."
    ),
    "revision": (
        "- REVISION: write for a last-mile recall pass. One short full-sentence bullet per "
        "examinable fact, front-loading the keyword: names, numbers, years, Articles, Sections, "
        "Schedules, conventions, committees, targets.\n"
        "- Prefer a #TABLE_START block wherever several facts share a shape (name/year/purpose), "
        "because a table recalls faster than prose.\n"
        "- Omit narrative build-up and derivations, but keep every correction and caution line: "
        "a wrong fact recalled quickly is worse than one not recalled at all."
    ),
}


def _dcn_depth_block(depth: str) -> str:
    """Turn the depth picker into instructions that change the output."""
    key = (depth or DEFAULT_UPSC_NOTE_DEPTH).strip().lower()
    if key not in _DCN_DEPTH_RULES:
        key = DEFAULT_UPSC_NOTE_DEPTH
    return (
        f"\nNOTE DEPTH — {UPSC_NOTE_DEPTHS[key].upper()}\n"
        f"{_DCN_DEPTH_RULES[key]}\n"
        "- Depth changes how much is written, never the marker vocabulary or the layout."
    )


def _dcn_fact_mode_block(fact_mode: str) -> str:
    """Spell out what this run may do about facts needing current data."""
    mode = (fact_mode or DEFAULT_UPSC_FACT_MODE).strip().lower()
    if mode not in UPSC_FACT_MODES or mode not in _DCN_FACT_MODE_RULES:
        mode = DEFAULT_UPSC_FACT_MODE
    return (
        "\nFACT MODE [OVERRIDES ANY CONFLICTING INSTRUCTION ABOVE, INCLUDING THE QUALITY BLOCK]\n"
        f"{_DCN_FACT_MODE_RULES[mode]}\n"
        "- Whatever this mode requires is written inline in the bullets, in the marker vocabulary. "
        "It never adds a section of its own — the QUICK REVISION and Key Sources closing sections "
        "the format requires are unaffected by this mode."
    )


_DCN_QUALITY_LEVEL_RULES = {
    "off": (
        "QUALITY CHECK LEVEL: OFF.\n"
        "- Do NOT fact-check the faculty. Treat every faculty point as correct.\n"
        "- Only clean and structure the class content: drop housekeeping, chatter and repetition, "
        "repair transcription noise, and lay it out under the heading and bullet markers.\n"
        "- Write no correction wording of any kind, even where something looks wrong.\n"
        "- The FACT MODE block below still applies: it governs current data, not faculty accuracy."
    ),
    "basic": (
        "QUALITY CHECK LEVEL: BASIC.\n"
        "- Correct ONLY plain errors: a plainly wrong fact, date, year, Article, name or number, "
        "and duplicated points.\n"
        "- Do not flag debatable framing, ordering, missing context or style.\n"
        "- When unsure whether something is wrong, leave it as taught."
    ),
    "strict": (
        "QUALITY CHECK LEVEL: STRICT.\n"
        "- Check every substantive faculty point: factual and conceptual correctness, chronology, "
        "cause-effect flow, data/date mismatch, Article mismatch, scheme-ministry mismatch, "
        "institution mismatch, map/location mismatch, wrong definitions, outdated claims, "
        "overgeneralisation, duplicates and missing context.\n"
        "- Do not over-correct: a correctly taught, well-established point is left alone."
    ),
}

# One short directive per toggle, phrased so the model stays inside the DCN
# marker vocabulary. The add_* items in particular must read as "do this inside
# the running notes" — the legacy prompt satisfies them with dedicated sections
# (Prelims Pointers, Mains Pointers...), which DCN forbids.
_DCN_QUALITY_DIRECTIVES = {
    "check_facts": "Fact-check each substantive faculty point and correct the wrong ones in place.",
    "check_concept_flow": "Repair broken concept flow: definition before application, cause before effect.",
    "check_missing_context": "Supply the missing context a point needs to stand on its own.",
    "check_factual_mismatch": (
        "Catch mismatches between a fact and its date, Article, scheme, ministry, institution or location."
    ),
    "check_outdated": "Replace an outdated claim with the current position, stated as settled fact with its year.",
    "check_duplicates": "Merge repeated or duplicated points into a single bullet.",
    "add_correction_pointers": (
        "Corrections stay SILENT: write the correct fact in place as settled knowledge and record what "
        "changed in the ERROR REPORT only. Never announce a fix in the notes ('corrected', 'as stated in "
        "the lecture', 'the earlier figure'). 'Important distinction:' and 'UPSC caution:' remain "
        "available for genuine exam warnings that are not corrections."
    ),
    "add_tables": "Where a comparison or data set reads better as a table, use a #TABLE_START / #TABLE_END block.",
    "add_flowcharts": "For a process, sequence, mechanism or cause-effect chain, use a #FLOW_START / #FLOW_END block.",
    "add_upsc_relevance": (
        "Bring out exam relevance inside the bullet itself (why the point is asked, which angle it serves) "
        "— not as a separate section."
    ),
    "add_prelims_keywords": "Bold the Prelims-facing keywords, exact names, years and thresholds where they occur.",
    "add_mains_value": (
        "Keep the Mains-facing causes, impacts, challenges and way-forward points as normal bullets under "
        "their own heading."
    ),
    "add_examples": "Retain the lecture's examples and case studies with the concept they illustrate.",
}


def _dcn_quality_block(quality_level: str, quality_options: dict | None) -> str:
    """Level + enabled toggles for a DCN run, in the template's own vocabulary.

    Mirrors _quality_block's contract (OFF wins over any ticked checkbox, and
    switched-off items are stated as prohibitions rather than left unsaid) but
    emits DCN-safe directives only: no report section and no extra headings.
    """
    level = (quality_level or "strict").strip().lower()
    if level not in _DCN_QUALITY_LEVEL_RULES:
        level = "strict"
    options = dict(DEFAULT_QUALITY_OPTIONS)
    options.update({k: bool(v) for k, v in (quality_options or {}).items() if k in QUALITY_OPTIONS})
    if level == "off":
        for key in options:
            if key.startswith("check_") or key == "add_correction_pointers":
                options[key] = False

    lines = [
        "",
        "QUALITY CHECK CONTROL FOR THIS RUN",
        _DCN_QUALITY_LEVEL_RULES[level],
        "",
        "Enabled for this run — do these, inside the notes:",
    ]
    enabled = [_DCN_QUALITY_DIRECTIVES[k] for k in QUALITY_OPTIONS if options.get(k) and k in _DCN_QUALITY_DIRECTIVES]
    lines += [f"- {text}" for text in enabled] or ["- (nothing)"]
    off_lines = []
    if not options.get("add_correction_pointers"):
        off_lines.append(
            "- Correction wording is disabled: leave faculty points as taught and write no correction "
            "phrases into the notes. Still do not delete a point."
        )
    if not options.get("add_tables"):
        off_lines.append("- Do not use #TABLE_START / #TABLE_END; write the same content as bullets.")
    if not options.get("add_flowcharts"):
        off_lines.append("- Do not use #FLOW_START / #FLOW_END; write the same content as bullets.")
    for key in ("check_facts", "check_outdated", "check_duplicates", "add_examples"):
        if not options.get(key):
            off_lines.append(f"- Disabled for this run: {_DCN_QUALITY_DIRECTIVES[key].rstrip('.')} — do not do this.")
    if off_lines:
        lines += ["", "Switched off for this run — do NOT do these:"] + off_lines
    return "\n".join(lines)


def dcn_visuals_block(visuals: list[dict] | None) -> str:
    """The LECTURE VISUALS section: metadata only, never image bytes (spec §10).

    Each entry is a visual already extracted, classified and selected from the
    slides. The model's only job is PLACEMENT — a ``#FIGURE: <id>`` line at the
    point in the notes where that visual supports the text. Selection stays in
    code, so the model can neither invent a figure nor resurrect an excluded one
    (unknown ids are stripped before rendering).
    """
    if not visuals:
        return ""
    lines = []
    for v in visuals:
        topic = (v.get("topic") or "").strip()
        description = (v.get("description") or "").strip()
        lines.append(
            f"- {v.get('visual_id')}: slide {v.get('slide')}, "
            f"{(v.get('visual_type') or 'diagram')}"
            + (f" — topic: {topic}" if topic else "")
            + (f". {description}" if description else "")
        )
    listing = "\n".join(lines)
    return f"""
LECTURE VISUALS AVAILABLE FOR INSERTION
These diagrams were extracted from the slides and will be printed in the final
document. Place each one by writing, on its own line, exactly:

#FIGURE: <visual_id>

Rules for placement:
- Put the #FIGURE line inside the section that explains that visual's topic,
  AFTER the bullets that explain the concept — the diagram supplements the text.
- Use each visual_id AT MOST once, and only ids from the list below. Never
  invent an id, and never describe a figure with wording like "see the diagram
  above" — the notes must read complete even without the figure.
- Not every section needs a figure, and you may leave a visual unplaced if its
  topic is genuinely not covered in these notes.
- Never put a #FIGURE line inside a #TABLE or #FLOW block.

AVAILABLE VISUALS
{listing}
"""


# Restated last because the run-specific blocks above sit between the template's
# own marker list and the end of the prompt, and the tail of a long prompt is
# what the model weights most.
def _dcn_corrections_block() -> str:
    """The CORRECTIONS_ENABLED feature's prompt contract. Empty when disabled.

    A deliberate, narrow exception to silent corrections: self-contradictions
    in the source material may be recorded in a structured block the writer
    renders as a red callout. Scope is tight on purpose — outside knowledge
    is still never grounds for a correction record.
    """
    from .config import CORRECTIONS_ENABLED

    if not CORRECTIONS_ENABLED:
        return ""
    return """
CORRECTIONS [NARROW EXCEPTION — SELF-CONTRADICTIONS ONLY]
Record a correction ONLY when the lecture material contradicts ITSELF: a value
that disagrees with a formula stated earlier, a unit that disagrees with the
quantity, a label that disagrees with the diagram it points at, or an
arithmetic slip you can demonstrate from numbers the material itself supplies.
You may NOT correct the material against outside knowledge, textbook values,
or what is "usually" true. If the material is merely unusual, incomplete or
surprising, leave it exactly as written and record nothing.

Write each one as its own block, immediately after the bullets it concerns:
#CORRECTION_START
SOURCE: <what the material actually says, copied verbatim>
CORRECTED: <the corrected version>
REASON: <the material's own contradicting statement that proves the error>
#CORRECTION_END
Every correction must cite the material's own contradicting statement in
REASON. These blocks are the ONE place correction wording may appear; the
silent-correction rule still applies in full everywhere else.
"""


def _dcn_output_reminder(error_report: bool = False, *, figures: bool = False) -> str:
    """The DCN closing contract.

    The DCN notes are marker text with no wrapper, so when the error report is
    on the notes have to be fenced by # NOTES_START / # NOTES_END — otherwise
    there is no boundary between the last bullet and the JSON array, and the
    array ends up typeset as a bullet in the student's document.
    """
    if error_report:
        envelope = """Your response is exactly two blocks, each marker alone on its own line:

# NOTES_START
<the DCN note content, starting with the #LECTURE_TITLE: line>
# NOTES_END

# ERROR_REPORT_START
<the JSON array described in the ERROR REPORT block below>
# ERROR_REPORT_END

Inside # NOTES_START / # NOTES_END, use note content and these markers only:"""
        tail = """- Nothing outside the four markers above: no preamble, no closing remarks, no code
  fences, no quality report, and no mention of AI, the transcript, the slides or these
  instructions."""
    else:
        envelope = "Reply with note content and these markers only:"
        tail = """- Nothing outside the markers and the note content: no preamble, no closing remarks, no code
  fences, no quality report, and no mention of AI, the transcript, the slides or these
  instructions."""
    marker_list = ("#LECTURE_TITLE:  #H1:  #H2:  #H3:  #TABLE_START / #TABLE_END  "
                   "#FLOW_START / #FLOW_END")
    figure_line = ""
    if figures:
        marker_list += "  #FIGURE:"
        figure_line = ("\n- #FIGURE: <visual_id> places one of the listed lecture visuals; "
                       "each id at most once, ids from the LECTURE VISUALS list only.")
    return f"""
OUTPUT PROTOCOL [FINAL REMINDER — NON-NEGOTIABLE]
{envelope}
{marker_list}
- Start with the #LECTURE_TITLE: line. Bullets are "- " (level 1), "  - " (level 2),
  "    - " (level 3). Bold with **keyword**. Table rows separate cells with " | ".
  Flowchart steps are separated by a down-arrow alone on its own line.{figure_line}
- Everything the blocks above ask for — corrections, current updates, verification marks,
  exam relevance — belongs INSIDE the bullets at the point it applies to. Do not open a new
  section for it. The ONLY closing sections are QUICK REVISION and Key Sources, exactly as
  the format section describes; no "Topics Covered", no "Concepts Covered", no "Mains
  Practice Question".
- Corrections are silent: state the correct fact as settled knowledge. Never write
  "corrected", "as stated in the lecture", or any wording that says a fix happened.
{tail}
"""


def build_upsc_dcn_prompt(
    *,
    subject: str,
    depth: str = DEFAULT_UPSC_NOTE_DEPTH,
    fact_mode: str = DEFAULT_UPSC_FACT_MODE,
    slides: list[SlideData] | None = None,
    transcript: str = "",
    quality_level: str = "strict",
    quality_options: dict | None = None,
    error_report: bool = True,
    error_report_strictness: str = "standard",
    language_code: str = "en",
    lecture_title: str = "",
    glossary: list[str] | None = None,
    visuals: list[dict] | None = None,
) -> str:
    """Build the single-call prompt for the PW OnlyIAS DCN format.

    The template already contains the marker contract and the lecture-input
    slots; this only fills those slots and appends the run settings, in the same
    fixed order build_upsc_prompt uses (quality, then the run-specific override,
    then the output protocol) so the later block wins on any conflict.

    ``lecture_title`` is substituted into the template's own #LECTURE_TITLE
    example. Leaving that example as ``Lxx_DCNxx_<Lecture Title>`` is what put
    the placeholder on students' pages: the model had nothing else to copy.
    """
    template = load_upsc_prompt_template(DCN_NOTE_TYPE, language_code)
    depth_key = (depth or DEFAULT_UPSC_NOTE_DEPTH).strip().lower()
    if depth_key not in UPSC_NOTE_DEPTHS:
        depth_key = DEFAULT_UPSC_NOTE_DEPTH
    values = {
        "subject": _dcn_subject_name(subject),
        "depth": _dcn_depth_block(depth_key).strip(),
        "transcript_block": _upsc_transcript_section(transcript),
        "slides_block": _upsc_slide_reference_section(slides),
        "lecture_title": lecture_title.strip() or "Class Notes",
        "glossary_block": glossary_prompt_block(glossary or []),
        # The page band lives in config and reaches the model from there.
        # A number written into the template as well would be a second
        # source of truth that nothing keeps in step.
        "length_budget": budget_block(language_code),
    }
    # One pass over the template, not a chain of .replace() calls. str.format is
    # unusable here (a stray brace in the lecture would raise), and chained
    # replaces are worse than they look: an earlier substitution's text is
    # rescanned by the later ones, so a transcript containing the literal
    # "{slides_block}" would have the slide list injected into it. re.sub walks
    # the original template once and never revisits what it inserted.
    filled = _DCN_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)
    return _with_extra(f"""{filled}
{dcn_visuals_block(visuals)}
{_dcn_keyword_links_block()}
{_dcn_quality_block(quality_level, quality_options)}
{_dcn_corrections_block()}
{_dcn_fact_mode_block(fact_mode)}
{_dcn_output_reminder(error_report, figures=bool(visuals))}
{error_report_contract_block(error_report_strictness, enabled=error_report)}""".strip())


# ---------------------------------------------------------------------------
# Long-lecture pipeline: write each stretch in full, then stitch.
#
# A 15,000-word class in one call does not fail loudly — it produces notes whose
# first half is detailed and whose second half is a summary, because the model
# is budgeting its own output. Splitting the input removes the budget pressure;
# the merge then only has to join, not to compress.
# ---------------------------------------------------------------------------

def build_dcn_chunk_prompt(
    *,
    subject: str,
    depth: str,
    chunk_index: int,
    chunk_total: int,
    transcript_chunk: str,
    slides: list[SlideData] | None = None,
    language_code: str = "en",
    glossary: list[str] | None = None,
    quality_level: str = "strict",
    quality_options: dict | None = None,
    error_report: bool = True,
    error_report_strictness: str = "standard",
    visuals: list[dict] | None = None,
) -> str:
    """One stretch of the lecture, written at full detail.

    No lecture title and no Topics Covered here — those belong to the whole
    class and are written once, at merge time. Asking for them per chunk
    produces N competing titles the merge then has to arbitrate.

    ``visuals`` is the same list every part gets (parts split the transcript,
    not the deck), with one extra rule: place a figure only where THIS part
    actually explains its topic. Duplicates across parts are removed after the
    merge by the placement pass, so an overlap costs nothing.
    """
    depth_key = (depth or DEFAULT_UPSC_NOTE_DEPTH).strip().lower()
    if depth_key not in UPSC_NOTE_DEPTHS:
        depth_key = DEFAULT_UPSC_NOTE_DEPTH
    language = language_name(language_code)
    figure_marker_rule = (",\n#FIGURE: <visual_id> to place a listed lecture visual"
                          if visuals else "")
    visuals_section = ""
    if visuals:
        visuals_section = (
            dcn_visuals_block(visuals)
            + "\n- FOR THIS PART: place a #FIGURE line only where THIS part of the "
              "transcript actually explains that visual's topic. Leave the rest unplaced."
        )
    chunk_budget = chunk_budget_line(chunk_index, chunk_total, language_code)
    return _with_extra(f"""You are an expert UPSC Daily Class Notes writer and fact-review editor.

This is PART {chunk_index} OF {chunk_total} of one recorded UPSC class. Write the notes
for THIS PART ONLY. Another pass will join the parts, so do not write an introduction or
a conclusion, and do not try to cover the whole class.

SOURCE PRIORITY
- SLIDES are the source of truth for facts, figures, dates, names, acronyms, Acts,
  Articles, schemes, institutions, headings and spellings.
- The TRANSCRIPT is an automatic caption used only for explanation, examples and the
  order topics were taught in. It transliterates English into Devanagari and mis-hears
  technical terms; never let it override a slide spelling or a slide fact.

{_dcn_depth_block(depth_key).strip()}

WHAT TO WRITE FOR THIS PART
- Cover every substantive point made in this stretch of the class, in the order made.
- Full-sentence bullets. Bold keywords with **keyword**.
- Keep definitions, examples, comparisons, Acts, Articles, schemes, institutions,
  committees, reports, dates and data.
- Remove only chatter, ads, QR/coupon text, greetings and filler.
- Do not summarise this part into an overview. Write it out — within the budget below.

{chunk_budget}

{_dcn_keyword_links_block()}

MARKERS — use these and nothing else:
#H1: / #H2: / #H3: for headings, "- " / "  - " / "    - " for the three bullet levels,
#FLOW_START ... #FLOW_END for a genuine sequence, #SEPARATOR between major parts{figure_marker_rule}.

Do NOT write #LECTURE_TITLE: and do NOT write #TOPICS_COVERED_START in this part.

TABLES
Only for short structured data — 2 to 4 columns, cells under 60 characters, rows under
90. A comparison that needs explanation must be a #H3: heading plus bullets, never a
table: the notes are typeset in a 5.5 inch column and a wide table is clipped off the page.

Language: {language}
UPSC area: {_dcn_subject_name(subject)}
{glossary_prompt_block(glossary or [])}
{visuals_section}
{_upsc_slide_reference_section(slides)}

CLASS TRANSCRIPT — PART {chunk_index} OF {chunk_total} STARTS
{(transcript_chunk or '').strip()}
CLASS TRANSCRIPT — PART {chunk_index} OF {chunk_total} ENDS
{_dcn_quality_block(quality_level, quality_options)}
{error_report_contract_block(error_report_strictness, enabled=error_report)}""".strip())


def build_dcn_merge_prompt(
    *,
    subject: str,
    depth: str,
    parts: list[str],
    lecture_title: str,
    language_code: str = "en",
    glossary: list[str] | None = None,
    figures: bool = False,
) -> str:
    """Join the parts into one set of notes, without shortening them.

    The instruction that matters is the negative one. A merge prompt that does
    not forbid summarising gets a summary — the model reads "merge N documents"
    as "produce something shorter than N documents".
    """
    depth_key = (depth or DEFAULT_UPSC_NOTE_DEPTH).strip().lower()
    if depth_key not in UPSC_NOTE_DEPTHS:
        depth_key = DEFAULT_UPSC_NOTE_DEPTH
    language = language_name(language_code)
    joined = "\n\n".join(
        f"--- PART {i} OF {len(parts)} ---\n{note}" for i, note in enumerate(parts, start=1))
    length_budget = budget_block(language_code)
    figure_rule = ""
    if figures:
        figure_rule = ("\n- Keep every \"#FIGURE: <id>\" line exactly where it sits relative "
                       "to its surrounding bullets. Never duplicate one, never invent one; "
                       "when two overlapping parts placed the same id, keep only the first.")
    return _with_extra(f"""You are an expert UPSC Daily Class Notes writer.

Below are DETAILED notes for consecutive parts of ONE recorded UPSC class, already
written. Join them into one final set of PW OnlyIAS Daily Class Notes.

THIS IS A JOIN, NOT A SUMMARY [MOST IMPORTANT]
- Keep every FACT you are given. Do NOT drop examples, definitions, figures, Acts,
  Articles, schemes, committees, institutions, dates or data.
- Do NOT collapse a section into a one-line overview.
- You MAY tighten prose to meet the budget below: the parts were written separately and
  repeat each other's framing. Cut the repetition, never the content.

{length_budget}

{_dcn_keyword_links_block()}

WHAT TO CHANGE
- Keep the original order of the class.
- Where the same heading appears at the end of one part and the start of the next, merge
  those two sections into one and keep both sets of bullets.
- Remove only genuine duplicates — the same point stated twice because the parts overlap.
- Fix the heading ladder so it reads as one document (#H1 for major topics, #H2/#H3 beneath).{figure_rule}

WHAT TO ADD
Start the output with exactly this line:

#LECTURE_TITLE: {lecture_title}

Then, immediately after it:

#TOPICS_COVERED_START
- one short phrase per major topic
#TOPICS_COVERED_END

- 5 to 15 entries, matching the #H1 sections in the merged notes, in the same order.
- This section is mandatory.

MARKERS
#LECTURE_TITLE: / #TOPICS_COVERED_START ... #TOPICS_COVERED_END / #H1: / #H2: / #H3: /
"- " bullets at three indent levels / #FLOW_START ... #FLOW_END / #SEPARATOR.
Nothing outside these markers and the note content. No commentary about merging.

TABLES
Keep tables only where they are short structured data (2-4 columns, cells under 60
characters). Turn any wide comparison table into a #H3: heading plus bullets — the notes
are typeset in a 5.5 inch column and a wide table is clipped off the page.

Language: {language}
UPSC area: {_dcn_subject_name(subject)}
Note depth: {UPSC_NOTE_DEPTHS[depth_key]}
{glossary_prompt_block(glossary or [])}
------------------------------------------------------------------
PARTS TO JOIN
------------------------------------------------------------------

{joined}
""".strip())


def build_merge_prompt(subject: str, mode: str, language_code: str, partial_notes: list[str]) -> str:
    template = load_prompt_template(mode, language_code)
    joined = "\n\n".join(f"--- PART {i+1} ---\n{note}" for i, note in enumerate(partial_notes))
    brevity = "\n- REVISION MODE (STRICT): Keep the merged notes short and concise; one-line bullets, no long explanations." if mode.lower() == "summary" else ""
    return f"""{template}
{_subject_header(subject, language_code)}{_language_rule(language_code)}

You are merging partial notes from consecutive slide chunks into one final set of UPSC notes.
Rules:
- Preserve original topic order.
- Remove repeated "Topics Covered" items.
- Merge duplicate headings only when they are the same heading from continuation slides.
- Keep a single coherent "Prelims Pointers" / "Mains Angle" block per topic where they occur; merge duplicates.
- Keep DTP notes with their slide numbers exactly.
- Output only the final notes, no extra commentary.{brevity}

PARTIAL NOTES START BELOW

{joined}
""".strip()
