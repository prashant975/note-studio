"""A short "What's new" tour after an update: QBG One's whats_new.py
(QBG_Studio_Vibecraft/backend/studio/qbg_studio/whats_new.py, 4 Oct 2026; the same rules).

A tour is shown on the first page NoteCraft opens after it starts, at most
SHOWINGS times in all (one start each): "Got it" ends it for good; closing it any
other way lets it come back once more on the next start. "What's new" (under the
version, in the account menu) opens it again any time. Each tour has its own id,
so the next release's tour is shown even to someone who finished this one.

Every release with a user-facing feature adds a tour here, newest first (CLAUDE.md).
"""
from __future__ import annotations

import json
import time
import uuid

from . import db
from .version import EDITION

SHOWINGS = 2
STATE = "whats_new_state"            # {tour id: {shown, boots, done, at}}
BOOT = uuid.uuid4().hex              # this start: a tour is shown once per start

# The newest tour first. Each step: title, a short text, and where "Show me" goes.
OMITTED = (["UPSC notes, Mains practice and DPP"] if "noupsc" in EDITION else []) + (
    ["PW Books"] if "nopwbooks" in EDITION else [])
TOURS = [
    dict(id="2026-10-30-word-reference", title="Full Word references in the B.Sc. template", steps=[
        dict(title="Keep the complete reference", href="/new", icon="file-text",
             text="B.Sc. style now accepts .docx references, including Nursing notes. AI chooses formatting while every source paragraph, table and embedded image stays locked. No shortening or invented content."),
        dict(title="One file, one run", href="/queue", icon="shield",
             text="Add several reference files; each becomes its own queued run. NEET/JEE quality checks review every final PDF page, including notation, terminology, tables, figures and layout. Reports retain any concerns; source words stay unchanged. Uses PW AI credit."),
    ]),
    dict(id="2026-10-30-bsc-ai-default", title="B.Sc.: AI notes in the reference template", steps=[
        dict(title="Upload the lecture and make notes", href="/new?category=bsc", icon="file-text",
             text="AI reads and checks every lecture page by default. The B.Sc. template is fixed; wording, examples and order follow the uploaded lecture. No transcription checkbox is needed. Uses PW AI credit and Google sign-in."),
        dict(title="Review the lecture content", href="/runs", icon="shield",
             text="Formulas and diagrams retain their original source material. Review handwriting and symbols against the lecture. Retrying a failed run reuses its checked AI pages when the source and prompt match."),
    ]),
    dict(id="2026-10-30-bsc-reference-repair", title="B.Sc.: rebuild the reference layout", steps=[
        dict(title="Original lecture to reference-style notes", href="/new?category=bsc", icon="file-text",
             text="Text highlights and inline formulas no longer turn a whole page into an old-layout screenshot. Definitions and feature lists are framed in the reference style while keeping the source wording."),
        dict(title="Scanned lecture transcription", href="/new?category=bsc", icon="shield",
             text="Enable PW AI transcription to convert scanned or complex lecture pages into typed content. It uses credit, checks each page separately and requires source review. Upload the original lecture, not an incomplete earlier output."),
    ]),
    dict(id="2026-10-30-bsc-source", title="B.Sc.: preserve the lecture content", steps=[
        dict(title="Original wording and order", href="/new?category=bsc", icon="file-text",
             text="B.Sc. notes now preserve the lecture's wording and order by default. Original formulas, tables and diagrams are retained, with no AI rewriting or AI cost."),
        dict(title="Content checked before delivery", href="/new?category=bsc", icon="shield",
             text="Every source item is checked for changes, loss and order. Scanned or complex pages stay as original excerpts and need review. Upload compiled lecture PDFs, not LaTeX code printed as a PDF."),
    ]),
    *([dict(id="2026-10-28-" + EDITION, title="A smaller NoteCraft", steps=[
        dict(title="A smaller download", href="/new", icon="file-text",
             text="This edition includes hand-drawn and B.Sc. typeset notes, Sheet automation and Notes check."),
        dict(title="Engines in this edition", href="/settings/system", icon="info",
             text=" and ".join(OMITTED) + " are not in this installation. Updates stay with this edition."),
    ])] if OMITTED else []),
    dict(id="2026-10-26-bsc-typeset", title="B.Sc. typeset notes", steps=[
        dict(title="A new category: B.Sc.", href="/new?category=bsc", icon="plus",
             text="Choose B.Sc. on New notes, pick the subject and add lecture PDFs. The notes come in the B.Sc. "
                  "Wallah layout: numbered sections, coloured boxes, tables and Key Takeaways."),
        dict(title="Checked and illustrated", href="/new?category=bsc", icon="shield",
             text="PW AI checks the notes against the lecture before the PDF is drawn, and draws up to six labelled "
                  "figures with their spelling checked. Each run keeps a quality report."),
        dict(title="Rules in plain sentences", href="/settings/categories", icon="book",
             text="Edit the B.Sc. rules for every subject or for one subject in Settings → Categories, as for any "
                  "other category."),
    ]),
    dict(id="2026-10-23-gujarati-review", title="Cleaner Gujarati notes", steps=[
        dict(title="Chapters follow the source", href="/new", icon="book",
             text="Gujarati notes now check chapter order, repeated sections and terminology against the lecture. Unique source details stay together."),
        dict(title="Clearer numbers and page breaks", href="/runs", icon="layers",
             text="Prose numbers match the text size, minus signs are centred, ordinary table rows stay together and continued glossary boxes have one heading."),
    ]),
    dict(id="2026-10-14-gujarati-formulas", title="Readable Gujarati formulas", steps=[
        dict(title="Words inside fractions and equations", href="/automation", icon="sheet",
             text="Gujarati word labels in formulas now render as complete, readable words, with fraction bars and operators preserved."),
    ]),
    dict(id="2026-10-14-gujarati-sheets", title="Gujarati in Sheet automation", steps=[
        dict(title="Gujarati output for sheet rows", href="/automation", icon="sheet",
             text="Gujarati is detected automatically from Gujarat/Gujarati batch names or readable Gujarati text in the lecture PDF."),
        dict(title="Gujarati fonts and quality checks", href="/automation", icon="shield",
             text="Sheet runs use the same Gujarati prompts, fonts and quality checks as New notes."),
    ]),
    dict(id="2026-10-14-pdf-chips", title="PDF attachments in sheet automation", steps=[
        dict(title="Use Drive PDF chips", href="/automation", icon="sheet",
             text="Catalog sheets now read the Drive link behind an attached PDF's filename. Plain PDF links and linked text also work."),
        dict(title="Check before running", href="/automation", icon="shield",
             text="Use Try it first to inspect the ready rows before starting a run."),
    ]),
    dict(id="2026-10-12-engine-sync", title="Current Short Notes and UPSC updates", steps=[
        dict(title="Chemistry and notation repairs", href="/new", icon="plus",
             text="Hand-drawn notes use the current chemistry, percentage and temperature repairs and quality checks."),
        dict(title="Use an existing Drive transcript", href="/new?category=upsc", icon="video",
             text="Open More options and paste the full Drive transcript when it cannot be read automatically. The notes use it directly and skip audio extraction."),
    ]),
    dict(id="2026-10-09-gujarati", title="Gujarati Short Notes and automation quality", steps=[
        dict(title="Notes in Gujarati", href="/new?category=foundation&type=concise_handdrawn", icon="plus",
             text="Choose Gujarati (ગુજરાતી) to make concise notes in Gujarati using the Short Notes layout."),
        dict(title="Gujarati letters render correctly", href="/new", icon="book",
             text="Gujarati notes include a Gujarati font and text shaping for vowel signs and combined letters."),
        dict(title="The automation checks", href="/new", icon="shield",
             text="Manual hand-drawn notes now use the sheet automation's style, source reviews and final PDF checks by default."),
        dict(title="Inspect the result", href="/runs", icon="layers",
             text="Open a finished run to download its notes and quality report. Checks that need attention are shown clearly."),
    ]),
    dict(id="2026-10-08-engine-sync", title="Current engines and automatic updates", steps=[
        dict(title="Current Short Notes engine", href="/new", icon="plus",
             text="Hand-drawn notes now use the current Short Notes prompts, quality checks and PDF renderer."),
        dict(title="Current UPSC engine", href="/new?category=upsc", icon="video",
             text="UPSC notes use the current lecture processing, Word layout, PDF export and practice code."),
        dict(title="Keep working during updates", href="/settings/system#updates", icon="download",
             text="Automatic updates are on. New versions download and are checked in the background."),
        dict(title="Install when ready", href="/settings/system#updates", icon="layers",
             text="The install pop-up appears after the download finishes. Install now or ask for a reminder in ten minutes."),
        dict(title="Your queue waits during installation", href="/queue", icon="shield",
             text="Installation waits for active work to finish. Your notes and saved settings stay in place."),
        dict(title="Your existing sign-in", href="/settings", icon="sparkles",
             text="NoteCraft keeps using your PW Google sign-in and the team's update access. A gateway budget failure is shown clearly."),
    ]),
    dict(id="2026-10-04-notecraft", title="Welcome to NoteCraft", steps=[
        dict(title="All your note apps in one place", href="/", icon="sparkles",
             text="Hand-drawn notes, UPSC notes, PW Books notes, practice sets and notes checks: one app, "
                  "the same notes your old apps make."),
        dict(title="New notes", href="/new", icon="plus",
             text="Pick the category and the kind of notes, add the lecture (PDFs, a PowerPoint or a class "
                  "recording link) and press Make notes."),
        dict(title="UPSC notes", href="/new?category=upsc", icon="video",
             text="Paste a class recording link and add the slides if there are any: daily class notes, simple, "
                  "Mains or Prelims notes, with the error report."),
        dict(title="Practice", href="/practice", icon="target",
             text="Mains practice questions and model answers, and DPP sets from the question bank, made from "
                  "finished UPSC notes (also the old app's)."),
        dict(title="PW Books handwritten notes", href="/new?category=neet_jee&type=pw_books_handwritten", icon="book",
             text="Under NEET-JEE: a lecture PDF or PowerPoint becomes PW Books handwritten-style notes."),
        dict(title="Check notes", href="/check", icon="shield",
             text="An error report for any finished notes: press Check these notes on a run, or drop a file."),
        dict(title="Smart Queue and updates", href="/queue", icon="layers",
             text="Everything waits its turn in the Smart Queue. When a new version is released, a blue Update "
                  "button appears beside Smart Queue."),
    ]),
]


def _load() -> dict:
    try:
        value = json.loads(db.get_setting(STATE, "") or "{}")
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _save(value: dict) -> None:
    db.set_setting(STATE, json.dumps(value))


def current() -> dict | None:
    return TOURS[0] if TOURS else None


def state(mark: bool = True) -> dict:
    """What the page needs on load: the tour to show now, or none.

    mark: count this as one showing (once per start)."""
    tour = current()
    if not tour:
        return {"show": False}
    saved = _load()
    entry = saved.get(tour["id"]) or {}
    shown = int(entry.get("shown") or 0)
    boots = list(entry.get("boots") or [])
    show = not entry.get("done") and (BOOT in boots or shown < SHOWINGS)
    if show and mark and BOOT not in boots:
        entry.update(shown=shown + 1, boots=(boots + [BOOT])[-SHOWINGS:], at=time.time())
        saved[tour["id"]] = entry
        _save(saved)
    return {"show": show, "id": tour["id"], "title": tour["title"], "steps": tour["steps"],
            "last": bool(show and int(entry.get("shown") or 0) >= SHOWINGS)}


def finish(tour_id: str, done: bool = True) -> None:
    """'Got it' (done) ends the tour for good; closing it lets it come back once more."""
    saved = _load()
    entry = saved.get(tour_id) or {}
    if done:
        entry.update(done=True, at=time.time())
    else:
        entry["closed"] = time.time()
    saved[tour_id] = entry
    _save(saved)
