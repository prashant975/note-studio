"""The Error Report contract — copied verbatim from the note-generation project.

``error_report_contract_block()`` and the two constants it reads
(``_STRICTNESS_SCOPE``, ``_ERROR_TYPE_LIST``) are lifted unchanged from
``src/prompt_builder.py`` in the note-generation app. They are what make a row
in this app's XLSX mean the same thing it means there: the same error-type
vocabulary, the same severity ladder, the same strictness scopes, the same
"what NOT to put in the array" list, and the same markers that
``src/error_report_writer.extract_error_report_block()`` parses.

``build_pdf_error_report_prompt()`` at the bottom is the only new text in this
file, and it is the input adaptation and nothing more: where the generation
prompt said "here are the slides and the transcript, write the notes, then
report what you corrected", this one says "here are the finished notes, read out
of a PDF — report what is wrong in them". The contract block it appends is
byte-identical to the generation prompt's.
"""
from __future__ import annotations

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

#: The guard against the single most common false positive in this app.
#:
#: A checking model has a training cutoff; the notes under review usually do
#: not. When it meets a figure, a scheme name or an office-holder it does not
#: recognise, the likeliest explanation by far is that the world moved after its
#: cutoff — not that the writer erred. Without being told this, the model reads
#: its own ignorance as evidence and reports correct, current facts as
#: "outdated". On a clean document that is the only kind of row it can produce,
#: which is why an unguarded run returns a page of confident nonsense.
#:
#: The date is injected live rather than hard-coded so the instruction stays
#: true as the app is used.
_RECENCY_GUARD = """
TODAY'S DATE IS {today}.

CRITICAL — YOUR OWN KNOWLEDGE IS OLDER THAN THESE NOTES.
You have a training cutoff. These notes were very likely written AFTER it.

THE TEST IS NOT WHETHER YOU RECOGNISE SOMETHING. IT IS WHETHER YOU CAN SAY WHAT
IS RIGHT. Apply it to every doubt you have, and let it decide:

- CAN YOU NAME THE CORRECT VALUE? The city, the year, the person, the river, the
  dynasty, the expansion of the acronym — if you can state plainly what it
  should be, REPORT IT. Say what is wrong and what is right. This is the entire
  purpose of the report, and a mistake you could have corrected but kept quiet
  about is the worst outcome of this task. Being asked to guard against
  over-reporting is NOT a reason to withhold a correction you are sure of.
- IS ALL YOU HAVE A DOUBT? The figure looks unfamiliar, you cannot confirm it,
  it sits oddly with something you half-remember, you have not heard of the
  scheme — then SAY NOTHING. Not recognising something is not evidence of an
  error, and is far more likely to mean the notes are newer than you.

Then, whatever the test says, these three stay silent, because they move after
any cutoff and your memory of them is not evidence:
  (a) the current holder of an office;
  (b) the latest value of a statistic, ranking, count or index position;
  (c) whether a scheme, bill or body EXISTS YET — has been notified, launched,
      passed or released.

Read (c) narrowly. It covers existence and nothing else. What a body is CALLED,
what an acronym stands for, which ministry runs a scheme, where an institution
sits, what a law says: all of those are checkable, and a wrong name is one of
the most common errors in notes like these. "The National Tribunals Authority"
where the body is the National Tribunals Commission is an error you should
report — you are not being asked whether it exists, you are being asked what it
is called.
- A revised number, a renamed scheme, a new authority, a merged ministry, an
  updated target: assume the notes are right and you are behind. Say nothing.
- OFFICE-HOLDERS, ABSOLUTELY: never claim a named person does not hold the
  office these notes give them, or holds a different one. Elections, appointments
  and transfers after your cutoff are exactly what you cannot see — if the notes
  call someone Vice President, Governor, Minister or Chief Justice, treat that as
  current fact. Question an office-holder line ONLY when the notes contradict
  themselves about it on the page.
- Report an "Outdated data" or "Current-affairs update" row ONLY when the value
  was ALREADY superseded at the time the notes were written — that is, the
  mistake existed when it was typed, not because the world changed since. If you
  cannot show that, do not raise the row.

What you SHOULD still report with full confidence, because these do not expire:
constitutional articles and their content, the text and year of enacted laws,
settled historical dates and sequences, scientific and mathematical facts,
formulae, units and symbols, definitions, and any place where the notes
CONTRADICT THEMSELVES — a table that disagrees with its own paragraph, a figure
given as two different values on two pages, a diagram whose label does not match
the text. Internal contradiction is always fair game: it needs no outside
knowledge and cannot be explained away by your cutoff.

Equally safe to report, because they are settled and move slowly if at all:
where a monument, temple, institution or headquarters is located (which city,
state or country); which river a monument or city stands on; who built or
founded something, and in which century or under which dynasty; the school,
style or tradition a thing belongs to; what an established acronym stands for
(an acronym expanded as some other body, or an expansion that does not match its
letters, is an error); which body publishes a given report or index; and which
cohort or category precedes or succeeds another in a settled ordering. Where the
notes give a different city, river, founder, century, expansion or publisher
than the settled one, say so plainly.

Two cases you MUST report even though they look like recency questions:
- An IMPOSSIBLE OR WILDLY IMPLAUSIBLE figure: a percentage above 100, a value an
  order of magnitude away from anything plausible, or a figure that cannot sit
  beside a neighbouring figure in these same notes. Do not stay silent just
  because you cannot cite the true value — report the implausibility itself,
  name why it cannot be right, and set the correction to "Needs verification
  from official source before finalisation."
- A FUTURE-DATED SOURCE: check every cited report, survey, census or index
  edition against TODAY'S DATE above — by arithmetic, not by memory of
  publication schedules. An edition covering a period that has not finished, or
  one that could not have been released by today, is an error.

Prefer silence to SPECULATION — not to knowledge. A short report that is
entirely right is worth far more than a long one a reviewer has to fact-check,
and an empty array is a good answer for a clean document. But every error you
can actually correct belongs in it: silence about those is not caution, it is
the failure this check exists to prevent.
"""

_ERROR_TYPE_LIST = (
    "Conceptual | Numerical/outdated | Source-year precision | Conceptual/formula precision | "
    "Factual | Outdated data | Scheme/ministry mismatch | Constitutional/legal mismatch | "
    "Definition error | Overgeneralised claim | Missing qualifier | Unsupported claim | "
    "Terminology issue | Current-affairs update | Diagram/table mismatch | "
    "Unit/formula/symbol issue | Language clarity"
)


def error_report_contract_block(strictness: str = "standard", *, enabled: bool = True) -> str:
    """The ERROR REPORT instruction block, or "" when the report is switched off.

    Returning "" rather than an "omit the report" instruction matters: a prompt
    that mentions the markers at all gets them emitted some of the time, and a
    stray JSON array in the response would be typeset into a student's notes.
    """
    if not enabled:
        return ""
    key = (strictness or "standard").strip().lower()
    scope = _STRICTNESS_SCOPE.get(key, _STRICTNESS_SCOPE["standard"])
    return f"""
ERROR REPORT [SEPARATE FROM THE NOTES — REQUIRED]
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

When you cannot confidently verify the current value of a figure, DO NOT report it. An
unverifiable figure is not a known error, and a row you cannot stand behind costs a reviewer
more time than it saves. This applies however plausible your doubt feels: report the figure
only if you can say what is wrong with it and what the right value is.

The one exception is a figure the notes themselves contradict — the same quantity given as two
different values, or a total that does not match its own parts. That is an error you can prove
from the page alone, so report it, quote both places, and set the correction to whichever value
the rest of the document supports (or "Needs verification from official source before
finalisation." if neither is supported).

For UPSC and current-affairs content: prefer official Indian government or UN sources, and
treat a superseded figure, a renamed scheme, and a wrong constitutional article as High.
For JEE/NEET and science content: report wrong formulae, wrong chemical formulae, wrong units
or symbols, wrong definitions, and diagram-label mismatches. Do NOT report a topic being
covered in less depth than you would like — that is not an error.
"""



# ---------------------------------------------------------------------------
# The standalone wrapper — the ONE piece of new prompt text in this project.
#
# The generation prompt handed the model slides + transcript and asked for
# notes plus the report. Here the notes already exist and are final, so the
# wrapper's whole job is to (a) present the PDF's text as the notes under
# review, (b) say plainly that nothing may be rewritten, and (c) close with the
# unmodified contract block above, which is what the response is parsed by.
#
# Deliberately NOT restated here: the severity ladder, the error-type list, the
# strictness scope, and the "what NOT to report" rules. Every one of those
# lives in error_report_contract_block() and duplicating it in different words
# is exactly how two apps drift apart.
# ---------------------------------------------------------------------------

_PART_LINE = ("This is part {part} of {total} of one lecture's notes. Report only what is "
              "wrong in THIS part. Do not comment on missing context: the other parts "
              "carry it.\n")


#: The second pass over the same pages. A single "find what is wrong" read
#: reliably returns the headline mistakes and misses the quiet ones — a wrong
#: city, a wrong acronym expansion, a wrong tense. Graded trials showed the
#: misses cluster in exactly these classes, so the second pass reads the same
#: pages against this checklist and nothing else. Its rows merge with the first
#: pass's; duplicates are collapsed downstream and every claim still faces the
#: verification pass, so the cost of the extra pass is calls, not noise.
_LENS_ENTITIES = """SECOND PASS — A DIFFERENT LENS ON THE SAME PAGES.
Another pass has already searched these pages for general factual errors. Do not
try to repeat it. Your pass checks ONLY the specific things below.

WORK BY ENUMERATION, NOT BY IMPRESSION. This matters more than anything else in
this prompt. Before you judge anything, go through the pages IN ORDER and make
yourself a list, in your head, of every checkable item on them:

  - every named person, place, institution, organisation, river, monument and
    dynasty, together with whatever the notes say about it;
  - every number, year, date, percentage, count and ratio;
  - every acronym and its expansion.

Then take that list ITEM BY ITEM and ask of each one: is this right? Only after
you have been through the whole list do you write the report, and it contains
only the items that failed.

Do this because a general read reports what stands out, and what stands out is
not the same as what is wrong. Measured on real notes: given pages containing a
wrong city, a wrong weight and a wrong number of arms on a statue, a model
reading for impressions returned six confident findings and none of them were
those three. It had read the page. It simply never asked about them.

The checks to apply to each item on your list:

1. PROPER NOUNS AND THEIR ATTRIBUTES. For every named person, monument, temple,
   institution, organisation, river, city and dynasty: is the attribute the
   notes attach to it right? The city, state or country where a thing is located
   or headquartered, the river something stands on, who built or founded it, the
   century or dynasty, the role a person is famous for. These are settled facts:
   where the notes conflict with what you know, report it.
2. ACRONYMS. Wherever the notes expand an acronym, check the expansion against
   the standard one. A well-known acronym expanded as some other body, or an
   expansion that does not match the acronym's own letters, is an error.
3. DEFINITIONS AND ORDERINGS. Definitional ranges and category boundaries the
   notes assert (year ranges of generations or cohorts, what a term is defined
   as), and settled orderings — which cohort precedes or succeeds another, which
   body publishes which report, which ministry runs which scheme. Report a range
   or ordering that conflicts with the widely accepted one, at Medium, and say
   what the accepted version is.
4. THE DEFINING CHARACTERISTIC. A concept credited to the wrong defining trait —
   a folk tradition tied to the wrong way of life, a generation defined by the
   wrong connector, a style attributed to the wrong school. If the notes name a
   characteristic that contradicts the concept's settled description, report it.
5. GRAMMAR AND TENSE. Subject-verb disagreement, a misspelled word (including a
   proper noun), and the wrong tense — above all a scheme, platform or body that
   operates today described in the past tense ("was" for something that still
   is). File these as Language clarity, severity Low.
6. FIGURES THAT CANNOT BE TRUE. A percentage above 100, a value an order of
   magnitude off anything plausible, a number that contradicts a neighbouring
   number on these pages, and a cited edition dated after today. Where the
   notes give a count AND the items counted (a share and its base, "N
   categories" beside the list of categories), do the arithmetic and check it.
7. ANACHRONISMS. A recent event described as following a commission, plan
   period, guideline cycle or policy whose term ended years before the event
   (a 2026 notification "following the 13th Finance Commission", whose period
   ended in 2015). The settled sequence — which Commission or Plan covered
   which years — does not expire; a reference to one whose term had lapsed
   long before the notes' own dated context is an error.
8. THE ABSURD SENTENCE. A recommendation, aim or finding that no one would
   make in that form: a report that "recommends a fragmented system", a
   measure to "ensure unfair wages", an effect attributed to its opposite
   cause. Read each such sentence as a student would; if it says the reverse
   of what the surrounding text plainly intends, report it.

The recency rules above still bind you: an office-holder, a scheme's latest
figure, whether something has been notified or launched yet, a ranking you
merely do not recognise — silence. These lenses are chosen precisely because
what they check does not expire."""

#: The whole-document pass. The page-window passes above cannot see that page 4
#: and page 16 disagree — no two-page window contains both. This pass is handed
#: the ENTIRE document and asked for exactly one thing: places where the notes
#: contradict themselves. Internal contradiction needs no outside knowledge, so
#: it is immune to the recency trap that makes broad whole-document questions
#: dangerous — and it is the one question that genuinely requires seeing
#: everything at once.
_LENS_CONSISTENCY = """WHOLE-DOCUMENT PASS — INTERNAL CONSISTENCY ONLY.
Unlike the other passes, you have been given the ENTIRE document. Do not audit
individual facts against your own knowledge here — other passes do that. Report
ONLY places where the notes CONTRADICT THEMSELVES, anywhere in the document,
including across distant pages:

- the same quantity, date, count or name given as two different values in two
  places;
- a total that does not match its own parts, or a score, weightage or indicator
  count that cannot be reconciled with the scheme the document itself describes;
- a table or box that disagrees with the paragraph beside it, or a heading that
  promises one thing while the body says another;
- the same entity — a person, scheme, body, place — described in two
  conflicting ways on different pages;
- a grading scale, classification or list used inconsistently: levels or
  categories that appear in one section but not in the document's own
  definition of the scale;
- a chronology that cannot all be true.

For each contradiction, quote BOTH places in "original_statement" with their
page numbers, so a reviewer can put them side by side. Set the correction to
whichever value the rest of the document supports, or to "Needs verification
from official source before finalisation." when neither side is supported.
These are provable from the pages alone: report them with full confidence, and
report nothing else."""

_LENS_BLOCKS = {"entities": _LENS_ENTITIES, "consistency": _LENS_CONSISTENCY}


#: The "here are the notes" half of the prompt, in each of the two input modes.
#: Only this paragraph differs between them: everything around it, and the whole
#: contract block, is the same text in both cases.
_TEXT_SOURCE = """NOTES UNDER REVIEW (read out of the PDF)
--- NOTES START ---
{notes_text}
--- NOTES END ---"""

_IMAGE_SOURCE = """NOTES UNDER REVIEW
The pages are attached as images, in order ({page_range}). Read them.

They are attached rather than pasted as text for a reason you must know about: this PDF embeds its Devanagari font as a subset whose character map returns letters in the order they are DRAWN, not the order they were typed. Copying text out of it produces real-looking but wrong Hindi - "जाति" comes back as "जातत". The words printed on the page are correct; only the copyable layer behind them is scrambled.

So: read every word from the image. Do NOT report a spelling, a matra, a conjunct or a broken word as an error - you cannot tell a typesetting artefact from a typo here, and every such row would be noise. Report what the notes ASSERT: wrong facts, wrong concepts, wrong laws, articles, schemes, bodies, dates, figures, formulae and units.

When you quote a statement in "original_statement", type it out correctly as you read it from the page."""


def build_pdf_error_report_prompt(
    notes_text: str = "",
    *,
    subject: str = "",
    language_code: str = "en",
    lecture_title: str = "",
    file_name: str = "",
    page_range: str = "",
    strictness: str = "standard",
    part: int = 0,
    total_parts: int = 0,
    from_images: bool = False,
    lens: str = "",
) -> str:
    """The error-report pass over one already-generated UPSC notes PDF.

    ``notes_text`` is the text layer read out of the PDF, page by page, with a
    ``[Page n]`` line before each page so the model can cite where a statement
    sits - the original prompt used slide numbers for the same purpose.

    ``from_images=True`` switches to reading the rendered pages instead, which
    is what happens when src/pdf/text_integrity finds the text layer cannot be
    trusted. The caller attaches those page images to the same call.
    """
    language = "Hindi (Devanagari), with English terms" if language_code == "hi" else "English"
    part_line = (_PART_LINE.format(part=part, total=total_parts)
                 if part and total_parts and total_parts > 1 else "")
    header_bits = [f"File: {file_name}" if file_name else "",
                   f"Lecture title: {lecture_title}" if lecture_title else "",
                   f"Subject area: {subject}" if subject else "",
                   f"Pages: {page_range}" if page_range else ""]
    header = "\n".join(b for b in header_bits if b)
    source = (_IMAGE_SOURCE.format(page_range=page_range or "as attached")
              if from_images else _TEXT_SOURCE.format(notes_text=notes_text))
    read_from = ("The attached page images came" if from_images
                 else "The text below came")
    # Injected live: the guard's whole force comes from the model knowing how
    # much of the world may have moved since its own cutoff.
    try:
        from .config import now_ist

        today = now_ist().strftime("%d %B %Y")
    except Exception:
        today = "today"
    recency_guard = _RECENCY_GUARD.format(today=today)

    # The PW OnlyIAS Notes QC Framework is this app's policy, so it travels with
    # every error-report call rather than living only in the review stage. Both
    # blocks are rendered from config/upsc_qc_*.yaml, so the prompt cannot drift
    # away from the rule files; the readable original is
    # reference_documents/notes_qc_framework.md.
    try:
        from .qc.qc_config import framework_checklist, source_guidance

        framework = "\n\n".join(
            block for block in (framework_checklist(), source_guidance(subject))
            if block)
    except Exception:
        framework = ""
    if framework:
        framework = "\n" + framework + "\n"

    # The lens sits AFTER the recency guard so its "these do not expire" claims
    # are read in the guard's light, and BEFORE the contract so its rows arrive
    # in the same array as everyone else's.
    lens_block = _LENS_BLOCKS.get((lens or "").strip().lower(), "")
    if lens_block:
        lens_block = "\n" + lens_block + "\n"

    return f"""You are checking a set of UPSC class notes that have ALREADY been written, typeset and exported as a PDF. {read_from} out of that PDF. Your job is to find what is factually or conceptually wrong in it.

{header}
Language of the notes: {language}
{part_line}
You are NOT writing or rewriting these notes. Do not produce notes, a summary, a rewrite, a table of contents, or any commentary. Produce only the JSON array described at the end of this prompt.

Read every page. A statement is judged on what it asserts to a student, in whatever language it is written - a wrong fact stated in Hindi is exactly as reportable as the same fact stated in English, and your report is written in English either way.
{recency_guard}
{framework}{lens_block}

Quote the wrong statement in "original_statement" as it appears in the notes (shortened if it is long), in its own language, so a reviewer can find it on the page.

{source}
{error_report_contract_block(strictness, enabled=True)}"""
