"""The DMP prompt library, adapted from the faculty's PROMPT sheet.

Two changes were forced by the fact that this is an application rather than a
person pasting into a chat window:

1. **The level prompts are split in two.** Each sheet cell asks for five
   questions AND a model answer for every one of them, in a single reply. The
   app cannot use that: the user picks which questions to answer, and answering
   all fifteen would cost five times what the questions did. So each cell became
   a question-generator and an answer-generator, with the wording of both halves
   adapted to the current question counts and model-answer requirements.

2. **The output format and the language directive are appended by code**, not
   baked into each of the twenty-five templates. A parser change would otherwise
   mean twenty-five edits, and twenty-four-of-twenty-five is how a format
   contract quietly rots.

The sheet's `DMP MODEL ANSWER` cell is NOT the answer generator — see
``DMP_CONTENT_REFINE_PROMPT`` at the bottom.
"""
from __future__ import annotations

from ..config import (
    DMP_ANSWER_TARGET_WORDS,
    DMP_ANSWER_WORD_TOLERANCE,
    dmp_question_count,
)
from ..languages import language_name, script_name, normalise, vocabulary_rule

# ---------------------------------------------------------------------------
# Level -> QUESTION generation
# ---------------------------------------------------------------------------

DMP_LEVEL_QUESTION_PROMPTS: dict[str, str] = {
    "easy": """Act as a UPSC CSE Mains expert and question setter. Analyze the provided daily class notes.
Generate exactly 5 Easy and Direct questions based STRICTLY on the notes. The questions should test foundational conceptual clarity, core features, provisions, or the historical evolution of the topic. The language must be straightforward and unambiguous, letting the candidate structure the answer using standard textbook knowledge. Avoid complex current-affairs link-ups or multi-layered statements. Include a mix of 10-mark (150 words) and 15-mark (250 words) questions.""",

    "moderate": """Act as a UPSC CSE Mains expert and question setter. Analyze the provided daily class notes.
Generate exactly 5 Moderate-difficulty questions based STRICTLY on the notes. The questions must be multi-layered, requiring the candidate to connect static core concepts with contemporary developments or recent structural challenges. Frame each so it has at least two distinct parts to address (e.g., assessing current status while proposing solutions, or analyzing causes alongside implications). The wording should mimic the authentic subtle complexity of the actual UPSC Mains exam. Include a mix of 10-mark (150 words) and 15-mark (250 words) questions.""",

    "applied": """Act as a senior and expert UPSC CSE Mains examiner. Analyze the provided daily class notes.
Generate exactly 5 highly Applied and Analytical questions based STRICTLY on the notes. Focus on real-world application, policy dilemmas, implementation bottlenecks, and systemic contradictions. Instead of asking 'what' or 'why' directly, frame questions around critical statements, institutional friction, or socio-economic dynamics that force the candidate to think like an administrator. Avoid any direct or bookish questions. Include a mix of 10-mark (150 words) and 15-mark (250 words) questions.""",
}

# ---------------------------------------------------------------------------
# Level -> MODEL ANSWER generation, for ONE selected question
# ---------------------------------------------------------------------------
#
# "Using ONLY the provided daily class notes as the knowledge base" is load
# bearing and is why the answer is worth having: an answer written from the
# model's general knowledge would teach the student things the class did not
# cover, and they would carry it into the exam as if it had.

DMP_LEVEL_ANSWER_PROMPTS: dict[str, str] = {
    "easy": """Act as a UPSC CSE Mains expert. Using ONLY the provided daily class notes as the knowledge base, write a creative, complete model answer for the SINGLE question below.
Emphasize foundational conceptual clarity and explain the relevant features, provisions or historical evolution to meet the question's demands.
Do not invent facts beyond the notes.

QUESTION ({marks} marks): {question}""",

    "moderate": """Act as a UPSC CSE Mains expert. Using ONLY the provided daily class notes as the knowledge base, write a creative, complete model answer for the SINGLE question below.
Address every layer of the multi-layered question, connecting concepts, implications and solutions wherever the question demands them and the notes support them.
Do not invent facts beyond the notes.

QUESTION ({marks} marks): {question}""",

    "applied": """Act as a senior UPSC CSE Mains examiner. Using ONLY the provided daily class notes as the knowledge base, write a creative, complete model answer for the SINGLE question below.
Think like an administrator: examine applications, policy dilemmas, implementation bottlenecks and counter-perspectives wherever the question demands them and the notes support them.
Do not invent facts beyond the notes.

QUESTION ({marks} marks): {question}""",
}


# ---------------------------------------------------------------------------
# Appended by code, never baked into a template
# ---------------------------------------------------------------------------

#: The parser's contract with the model. Written as one block so there is
#: exactly one place to change when the shape changes.
_QUESTION_FORMAT = """

Return ONLY valid JSON, no prose, no markdown fences, no commentary before or after:
{{"questions":[{{"marks":10,"text":"..."}},{{"marks":15,"text":"..."}}]}}
Exactly {count} items. "marks" is the integer 10 or 15 and nothing else.
"text" is the complete question, in {language}, written out in full — not a topic,
not a heading, not an abbreviation of the question."""


def question_format_block(language_code: str = "en", *, count: int) -> str:
    """The strict JSON contract, naming the language the text must be in.

    The language is repeated here as well as in the language directive because
    this is the line the model is looking at when it writes the field, and a
    directive twenty lines earlier loses to the nearer instruction more often
    than it should. ``count`` is per level now (4/3/3), so the caller says
    which level's contract this is.
    """
    return _QUESTION_FORMAT.format(
        count=count, language=language_name(language_code))


def language_directive(language_code: str = "en") -> str:
    """Lock the output language of a DMP call.

    Deliberately parallel to prompt_builder._language_rule, and deliberately not
    imported from it: that one is about a whole set of notes and carries rules
    about translating a lecture. This one is about a question and an answer. The
    shared part is the sentence that names the target language and refuses the
    other Devanagari one.
    """
    target = normalise(language_code) or "en"
    name = language_name(target)
    if target == "en":
        return (
            "\n\nWrite ALL output in English. Keep established technical terms, "
            "proper nouns, Act and scheme names readable as they are; do not "
            "force-translate them."
        )
    other = "Marathi" if target == "hi" else "Hindi"
    return (
        f"\n\nWrite ALL output in {name} ({script_name(target)}), using {name} grammar "
        f"and {name} exam vocabulary — it must NOT read as {other} written in "
        f"{script_name(target)}.{vocabulary_rule(target)}"
    )


def grounding_block(notes_text: str) -> str:
    """The finished note, presented as the "daily class notes" every template
    refers to. Fenced so the model can tell the notes apart from the instructions
    even when the notes themselves contain instruction-shaped bullet text."""
    return (
        "\n\nDAILY CLASS NOTES (the only knowledge base for this task):\n"
        "-----BEGIN NOTES-----\n"
        f"{notes_text.strip()}\n"
        "-----END NOTES-----\n"
    )


def build_question_prompt(level: str, notes_text: str, *, language_code: str = "en") -> str:
    """Full prompt for one level's questions.

    The sheet's cells all say "exactly 5"; the faculty later asked for
    4 easy / 3 moderate / 3 applied, so the count is substituted into the
    template's own wording rather than left to fight the JSON contract below
    it — two different numbers in one prompt and the model picks either.
    """
    count = dmp_question_count(level)
    template = DMP_LEVEL_QUESTION_PROMPTS[level].replace(
        "exactly 5", f"exactly {count}", 1)
    return (template + language_directive(language_code)
            + question_format_block(language_code, count=count)
            + grounding_block(notes_text))


def answer_plan(marks: int) -> dict:
    """The word budget for one answer, from the exam's limit for its marks.

    150 words (10 marks) -> 25 / 100 / 25 with four to five points;
    250 words (15 marks) -> 40 / 170 / 40 with six to seven. Stated to the model
    as numbers because "keep it short" is read as a style note, and a section
    budget is what stops a model spending the whole limit on its introduction.
    """
    words = DMP_ANSWER_TARGET_WORDS.get(int(marks), DMP_ANSWER_TARGET_WORDS[10])
    edge = 5 * round(words / 6 / 5)
    body = words - 2 * edge
    fewest = max(3, body // 25)
    return {
        "marks": int(marks),
        "words": words,
        "low": round(words * (1 - DMP_ANSWER_WORD_TOLERANCE)),
        "high": round(words * (1 + DMP_ANSWER_WORD_TOLERANCE)),
        "intro": edge,
        "body": body,
        "conclusion": edge,
        "points": f"{fewest} to {fewest + 1}",
        "point_words": 5 * round(body / (fewest + 0.5) / 5),
    }


def build_answer_prompt(level: str, question: str, marks: int, notes_text: str,
                        *, language_code: str = "en") -> str:
    """One grounded exam-length answer, with a common structure for every level."""
    plan = answer_plan(marks)
    template = DMP_LEVEL_ANSWER_PROMPTS[level]
    body = template.format(marks=int(marks), question=question.strip())
    return (body
            + f"\n\nWrite the COMPLETE model answer in about {plan['words']} "
              f"words — the UPSC word limit for a {plan['marks']}-mark "
              f"question — and keep it between {plan['low']} and "
              f"{plan['high']} words. Write it as a candidate would in the "
              f"exam: a finished answer, not an outline of what the answer "
              f"would say, and not longer than the limit. Count only # ANSWER, "
              f"excluding the approach box and formatting markers."
            + answer_quality_block(marks)
            + answer_format_block()
            + language_directive(language_code)
            + grounding_block(notes_text))


#: The printed sheet opens with an approach box — how to attack the question —
#: before the answer itself, exactly as the faculty's sheets do. It is asked for
#: as its own labelled section rather than left to be inferred from the answer's
#: headings, because a box built by guessing at which paragraph was the
#: introduction is a box that is sometimes wrong about it.
#:
#: The markers are ASCII and in English even for a Hindi answer: they are
#: parsed, never printed, and a model asked to translate its own section markers
#: is a model that eventually translates one of them and not the other.
#:
#: The answer is the UPSC shape — introduction, body, conclusion — and nothing
#: else. It used to ask for a heading per demand and a "-----**Point:**" label
#: on every bullet; the content team (2026-10) rejected both, because a Mains
#: answer has neither: invented sub-headings ("Conceptual Basis of the
#: Distinction") and "Point:" before every bullet are not how a candidate
#: writes. The three ## lines are markers the parser removes, so the sheet
#: shows a paragraph, plain bullets and a paragraph.
ANSWER_FORMAT_BLOCK = """

Return the answer in exactly two labelled sections, in this order:

# APPROACH
- Introduction: one line on how to open the answer.
- Body:
  - one line for each part of the body, in the order the answer takes them
    (one per demand of the question; name any limitation, nuance or
    counter-view the answer must include).
- Conclusion: one line on how to close.

# ANSWER
## INTRODUCTION
One short prose paragraph.
## BODY
- **Bold key phrase:** explanation of the point. For example, specific Indian application.
- **Bold key phrase:** the next point, on its own line, in the same pattern.
## CONCLUSION
One short prose paragraph.

The three ## lines are section markers and are never printed. Write NO other
heading or sub-heading anywhere in the answer: not one per demand of the
question, and not a bold line standing on its own. Every body point is a plain
"- " bullet that starts with a short **bold key phrase**; never start a point
with a label such as "Point:". The body follows the approach, taking its parts
in the same order. Keep the explanation and its example together in the same
point. Use "For example," or "For instance," (or the natural equivalent in the
answer's language) before each example. Use **bold** for the key phrase and
important keywords, institutions and schemes. Do not use nested sub-points or a
separate examples section.

Write both sections in the answer's language, but keep the "# APPROACH",
"# ANSWER", "## INTRODUCTION", "## BODY" and "## CONCLUSION" marker lines
exactly as written above."""


def answer_format_block() -> str:
    return ANSWER_FORMAT_BLOCK


#: Common faculty requirements for every selected question's model answer.
#:
#: The section lengths and point counts are filled from :func:`answer_plan`.
#: They were fixed numbers (60-word introduction, five points per demand,
#: "exceeding the overall target" allowed) that added up to ~500 words for a
#: 150-word question — the overrun the content team reported.
ANSWER_QUALITY_BLOCK = """

Depth and quality requirements for the # ANSWER section:
- Write a creative, FULL model answer, not an outline. Creativity means clear
  framing and connections within the notes, never invented evidence.
- Word limit: this is a {marks}-mark question, so the # ANSWER section is
  {words} words, never fewer than {low} or more than {high}. Spend them as about
  {intro} words of introduction, {body} of body and {conclusion} of conclusion.
- Introduction: one short paragraph of about {intro} words. Open with a relevant
  data point, scheme, phrase or fact present in the notes, and connect it
  directly to the question.
- Be comprehensive: identify and address every demand of the question and all
  relevant dimensions, questions and ideas in the provided content. Cover them
  all in the one body, as {points} distinct, concise points in total — no
  heading per demand.
- Each point: a short bold key phrase, then an explanation that directly answers
  that demand with an India-relevant, application-based, specific example —
  about {point_words} words for the whole point.
- Substantiate every point with a concrete example from the notes: a named
  scheme, place, institution, provision, judgment or documented application
  that illustrates that very point. Avoid vague or generic examples.
- Conclusion: one short paragraph of about {conclusion} words, synthesizing the
  answer with a positive, optimistic, futuristic and action-oriented ending
  grounded in the notes.
- The word limit is the exam's, not a suggestion. For multipart questions,
  cover every demand with fewer, tighter points rather than by exceeding the
  limit. Avoid padding.
- Stay strictly within the provided notes; detail comes from developing their
  content, never from inventing beyond it. Do not add external facts, data,
  schemes or examples. If the notes lack a suitable opening statistic, use a
  supplied phrase or fact. If they cannot support {points} distinct points or a
  specific Indian example, briefly state that limitation in the relevant section
  instead of fabricating or repeating material to satisfy a count.
- Before returning, count the words of the # ANSWER section and check the word
  limit, point count and grounding. Return only the approach and answer, not
  this check."""


def answer_quality_block(marks: int = 10) -> str:
    """The quality contract, with the word budget for ``marks`` filled in."""
    return ANSWER_QUALITY_BLOCK.format(**answer_plan(marks))


# ---------------------------------------------------------------------------
# Directive style library (secondary; OFF in the default flow)
# ---------------------------------------------------------------------------
#
# The sheet's second column: twenty-two ways of ASKING, independent of
# difficulty. Kept complete and verbatim so a later "directive mode" is a UI
# change rather than another transcription of the sheet, but not wired into the
# panel — the default flow is level-based, and offering twenty-two more buttons
# would bury the two that matter.
#
# `[Topic Name]` is filled from the note's derived topic. COMPARE and
# DIFFERENTIATE take two placeholders instead of one, which is exactly why they
# are not in the v1 flow: there is no second topic to fill them with.

DMP_DIRECTIVE_QUESTION_PROMPTS: dict[str, str] = {
    "DISCUSS": """Act as an experienced UPSC CSE Mains question setter. Generate 5 UPSC Mains questions on [Topic Name] using the directive "Discuss" (चर्चा कीजिए). The questions should require the candidate to discuss multiple dimensions of the issue, including its political, social, economic, environmental, ethical, technological, and institutional aspects wherever relevant. Avoid purely factual phrasing and ensure the questions test analytical depth suitable for UPSC. Include a mix of 10-mark (150 words) and 15-mark (250 words) questions with authentic UPSC wording.""",

    "EXAMINE": """Act as a UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the directive "Examine" (परीक्षण कीजिए). Each question must contain a proposition, claim, or statement that forces the candidate to examine its validity using logical arguments, evidence, real-world examples, and valid counterarguments. The questions should not be answerable through simple description; ensure that the candidate must assess both supporting and opposing dimensions before reaching a reasoned conclusion. Maintain authentic UPSC style.""",

    "CRITICALLY EXAMINE": """Act as an expert UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the directive "Critically Examine" (समालोचनात्मक परीक्षण कीजिए). The questions must require candidates to systematically examine the merits, limitations, underlying contradictions, challenges, and long-term implications of the given statement or proposition. The phrasing should force a balanced and critical assessment rather than a one-sided descriptive answer. Provide scope for empirical evidence, case studies, and a reasoned judgement.""",

    "ANALYZE": """Act as an experienced UPSC CSE Mains examiner. Generate 5 questions on [Topic Name] using the directive "Analyze" (विश्लेषण कीजिए). The questions should require the candidate to break the core issue down into its constituent factors and clearly establish relationships such as cause-effect, interdependence, continuity-change, or short-term and long-term consequences. Avoid questions that can be answered by merely listing bullet points; they must test analytical depth and conceptual clarity using authentic UPSC-style language.""",

    "CRITICALLY ANALYZE": """Act as a UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the directive "Critically Analyze" (समालोचनात्मक विश्लेषण कीजिए). The questions should require both an analytical decomposition of the issue into its components and a critical evaluation of its core assumptions, outcomes, limitations, and practical contradictions. The candidate should be pushed to establish causal relationships and also critically challenge them. Avoid descriptive setups and maintain high conceptual depth.""",

    "EVALUATE": """Act as an expert UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the directive "Evaluate" (मूल्यांकन कीजिए). The questions should require candidates to rigorously assess the effectiveness, significance, structural achievements, core limitations, and overall impact of the given policy, institution, development, or phenomenon. The question must guide the candidate toward formulating a reasoned overall judgement based on objective criteria. Avoid descriptive prompts.""",

    "CRITICALLY EVALUATE": """Act as a senior UPSC CSE Mains examiner. Generate 5 questions on [Topic Name] using the directive "Critically Evaluate" (समालोचनात्मक मूल्यांकन कीजिए). The questions should compel candidates to weigh both the achievements and shortcomings, the intended and unintended consequences, and the operational effectiveness versus structural limitations of the subject. The response must encourage critical thinking leading to a balanced, mature overall judgement. Include contemporary relevance wherever appropriate.""",

    "COMMENT": """Act as an expert UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the directive "Comment" (टिप्पणी कीजिए). Frame each question around a thought-provoking statement, assertion, or expert observation. The question must require the candidate to explain the underlying context, present relevant arguments and counterarguments, and provide a reasoned, independent assessment of the statement while remaining firmly grounded in constitutional, historical, economic, or social realities.""",

    "EXPLAIN": """Act as a UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the directive "Explain" (व्याख्या कीजिए). The questions should test precise conceptual clarity, requiring the candidate to unpack the meaning, functional mechanism, underlying causes, systemic significance, or operational functioning of the given phenomenon. Avoid questions that require merely listing standard facts; keep them conceptually robust and at the UPSC difficulty level.""",

    "ELUCIDATE": """Act as an expert UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the directive "Elucidate" (स्पष्ट कीजिए). Frame the questions around an abstract concept, complex statement, or nuanced idea. The questions must require the candidate to clarify, deconstruct, and unpack its deeper meaning using suitable logical arguments, real-world examples, and clear illustrations. It should strictly test conceptual understanding rather than simple factual recall.""",

    "DESCRIBE": """Act as a UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the directive "Describe" (वर्णन कीजिए). The questions should require a systematic, structured description of the major features, core characteristics, sequential stages, historical developments, or main components of the topic. Keep the questions factual but intellectually rigorous, ensuring tight alignment with the UPSC CSE syllabus without being overly analytical.""",

    "JUSTIFY": """Act as a senior UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the directive "Justify" (औचित्य सिद्ध कीजिए). Frame each question around a specific proposition, policy decision, or assertion whose validity needs to be established. The question must require the candidate to convince the examiner of the statement's validity through robust logical arguments, empirical evidence, case laws, and relevant constitutional, economic, or social reasoning, while acknowledging minor limitations where necessary.""",

    "SUBSTANTIATE": """Act as an expert UPSC CSE Mains examiner. Generate 5 questions on [Topic Name] using the directive "Substantiate" (पुष्ट कीजिए). Each question should present a strong assertion or claim that candidates must support and validate using specific facts, data, concrete evidence, real-world examples, reliable case studies, constitutional provisions, or historical developments. The questions must explicitly test the ability to connect arguments with hard evidence rather than merely explaining the topic vaguely.""",

    "ILLUSTRATE": """Act as a UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the directive "Illustrate" (उदाहरण सहित स्पष्ट कीजिए). The questions must require candidates to explain a complex concept or theoretical argument and explicitly substantiate it through relevant, real-world examples, landmark case studies, historical instances, active government schemes, or contemporary developments. Ensure that the examples are treated as integral to the core answer, not as optional additions.""",

    "ASSESS": """Act as an experienced UPSC CSE Mains examiner. Generate 5 questions on [Topic Name] using the directive "Assess" (आकलन कीजिए). The questions should require candidates to determine the exact extent, magnitude, structural effectiveness, or overall significance of the given issue after systematically considering the available evidence, notable achievements, critical limitations, and contextual factors. Each question must demand a balanced, reasoned assessment.""",

    "COMPARE": """Act as a UPSC CSE Mains question setter. Generate 5 questions on [Topic A] and [Topic B] using the directive "Compare" (तुलना कीजिए). The questions should require a systematic, parameter-driven comparison across relevant indicators such as core objectives, key features, underlying causes, operational functioning, institutional impact, and final outcomes. Avoid superficial list-making; ensure the questions highlight both deep-seated similarities and differences in authentic UPSC style.""",

    "DIFFERENTIATE": """Act as an expert UPSC CSE Mains examiner. Generate 5 questions on [Concept A] and [Concept B] using the directive "Differentiate" (अंतर स्पष्ट कीजिए). The questions must test precise conceptual clarity by requiring the candidate to distinguish between two closely related or overlapping concepts based on clearly identifiable parameters, functional mechanics, or legal/constitutional bases. Avoid purely factual or obscure distinctions; keep them highly relevant to the syllabus.""",

    "ENUMERATE": """Act as a UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the directive "Enumerate" (गिनाइए). The questions should require candidates to identify, extract, and systematically list the major factors, features, legal provisions, core causes, consequences, or structural components relevant to the topic. Keep the questions concise, structured, and factual, but ensure they require syllabus-based comprehensive mapping rather than trivial memory recall.""",

    "DEFINE": """Act as a UPSC CSE Mains question setter. Generate 5 questions on [Topic/Concept] using the directive "Define" (परिभाषित कीजिए). The questions must test precise technical or conceptual understanding. Frame the questions such that candidates must provide a textbook-clear definition and, wherever appropriate, briefly distinguish the core concept from closely related terms or mention its most essential operational characteristics. Avoid obscure terminology.""",

    "TO WHAT EXTENT": """Act as a senior UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] using the high-value directive "To what extent" (किस सीमा तक). Frame each question around a debatable, nuanced proposition where the statement is deliberately designed to be neither completely true nor completely false. The questions must require candidates to examine supporting arguments, structural limitations, contextual variables, and counterarguments before arriving at a highly qualified and balanced conclusion.""",

    "WHY": """Act as an expert UPSC CSE Mains question setter. Generate 5 questions on [Topic Name] beginning with or centered on the word "Why" (क्यों). The questions must focus strictly on identifying root causes, historical/structural reasons, underlying socio-economic factors, institutional conditions, and contextual explanations. Avoid designs that can be answered through a simple one-line reason; encourage multidimensional causal analysis.""",

    "HOW": """Act as an experienced UPSC CSE Mains examiner. Generate 5 questions on [Topic Name] using the directive "How" (कैसे). The questions should systematically test the underlying mechanisms, administrative processes, implementation pathways, structural functioning, methods, or strategic policies through which a particular outcome or transition occurs. Avoid merely asking for a list of causes; ensure the focus remains on the 'process' backed by real-world examples.""",
}

#: The placeholders each directive expects. COMPARE and DIFFERENTIATE need two
#: subjects, which a single note does not supply — recorded so directive mode
#: can skip or specially handle them rather than sending "[Topic B]" to a model.
DIRECTIVE_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "COMPARE": ("[Topic A]", "[Topic B]"),
    "DIFFERENTIATE": ("[Concept A]", "[Concept B]"),
    "DEFINE": ("[Topic/Concept]",),
}
DEFAULT_DIRECTIVE_PLACEHOLDER = "[Topic Name]"


def build_directive_prompt(directive: str, topic: str, *, language_code: str = "en",
                           topic_b: str = "") -> str:
    """Fill a directive template's topic placeholder(s) and add the app's blocks.

    Raises KeyError for an unknown directive rather than falling back to a
    default one: a silently substituted directive would produce five perfectly
    plausible questions of the wrong kind.
    """
    key = directive.strip().upper()
    template = DMP_DIRECTIVE_QUESTION_PROMPTS[key]
    placeholders = DIRECTIVE_PLACEHOLDERS.get(key, (DEFAULT_DIRECTIVE_PLACEHOLDER,))
    filled = template
    for index, placeholder in enumerate(placeholders):
        value = topic_b if (index == 1 and topic_b) else topic
        filled = filled.replace(placeholder, value)
    # Directive cells still say five, and directive mode is not level-based,
    # so their contract keeps the count their own wording asks for.
    return (filled + language_directive(language_code)
            + question_format_block(language_code, count=5))


# ---------------------------------------------------------------------------
# The mislabelled cell
# ---------------------------------------------------------------------------

#: The sheet's cell B6 is headed "DMP MODEL ANSWER", but its text is a
#: NOTES-REFINEMENT prompt — "You are an expert UPSC notes maker", asking for the
#: content to be reflowed into bullets with bolded keywords. It does not write an
#: answer to anything.
#:
#: Kept verbatim and available, and deliberately NOT wired into the answer path:
#: using it there would return a tidied copy of the notes under the heading
#: "Model answer", which looks like an answer and is not one. Its only sensible
#: use here is an optional cleanup pass over the grounding content BEFORE
#: answering, and even that is off by default — the grounding note has already
#: been through the pipeline's own QC and repair stages.
DMP_CONTENT_REFINE_PROMPT = """You are an expert UPSC notes maker. Process the content given below using the following instructions:
- Stick strictly to the content given below and dont add anything extra.
- Prepare a better flow of the content given below for UPSC preparation.
- Do not overly shorten the content; maintain a detailed and comprehensive explanation of all concepts.
- Include all important points, concepts, and explanations without losing meaning. Dont miss any important point in the content given below.
- Present the content in bullet points with full sentences (avoid fragments).
- Bold all important keywords, terms, and concepts.
- Do not divide points into unnecessary sub-points; keep the structure simple but well-organized.
- Use headings and subheadings wherever required for clarity, but avoid excessive or forced structuring.
- Remove irrelevant or off-topic discussion, but retain conceptual clarity and flow.
- Keep and explain relevant examples wherever needed to improve understanding.
- Try to make the content crisp by merging overlapping points but dont miss any important point of the lecture.
Ensure the final output is well-structured, detailed, and suitable for building strong conceptual understanding for UPSC preparation."""
