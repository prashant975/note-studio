"""The short guide above each page (_page_guide.html): 2-4 plain steps.

Every release that adds or changes a page updates its guide here.
"""
from __future__ import annotations
from . import engine_catalog
from .brand import APP_NAME as APP

GUIDES = {
    "/": {
        "title": "How this page works",
        "steps": [
            "Start: pick what you want to make. Each tile opens its own page.",
            f"Running now shows what {APP} is working on; open one to watch it, or to stop a class or a sheet run.",
            "Needs you lists anything waiting for a decision or a fix.",
        ],
    },
    "/new": {
        "title": "How New notes works",
        "steps": [
            "Pick the category, note style, subject and language. Choose Gujarati (ગુજરાતી) for Gujarati hand-drawn notes, including readable word labels in fractions and equations.",
            "Hand-drawn, PW Books or B.Sc. typeset notes: add one or more lecture PDFs (PW Books also takes "
            "PowerPoints); each is made in turn. B.Sc. style also accepts Word references for Nursing and other categories. "
            "AI reads and checks PDF pages by default; Word content stays locked while AI chooses formatting. "
            "All content and its order are retained in the fixed B.Sc. template. NEET/JEE quality checks review every final PDF page; inspect any issues in its quality report. Uses PW AI credit.",
            "UPSC notes: paste the class recording link and, if there is one, add the slides. "
            "If Drive's transcript cannot be read, paste its full text under More options to skip audio extraction.",
            "Press Make notes. It waits in the Smart Queue and uses PW AI credit. Hand-drawn notes use automation quality checks by default. Gujarati checks include source chapter order, repetitions and terminology; inspect the PDF and quality report in its finished run.",
        ],
    },
    "/automation": {
        "title": "How Sheet automation works",
        "steps": [
            "Pick the sheet, category and subject. Gujarati is detected from Gujarat/Gujarati batch names "
            "or the PDF's readable text. Catalog PDF cells can contain a link or a Drive file chip. "
            "Press Try it first to see which rows would be made.",
            "Run now adds the run to the Smart Queue. It starts when the Sheets lane is free, makes notes row by "
            "row and files them in Drive. Gujarati checks source chapter order, repetitions and terminology; inspect any issues in the run's quality report.",
            "Stop after this row stops cleanly and holds the queue; the rows not reached stay ready for next time.",
            "Sheets are shared with the old Short Notes app. Every change keeps a backup of the list.",
        ],
    },
    "/queue": {
        "title": "How the Smart Queue works",
        "steps": [
            "Everything you start waits here: lectures in the Notes lane, sheet runs in the Sheets lane.",
            "Each lane makes one item at a time, in order; the two lanes work side by side.",
            "Remove takes out an item that has not started. Nothing was made or paid for it.",
            "After a Stop, a lost sign-in or a restart the queue holds: press Continue when you are ready.",
        ],
    },
    "/runs": {
        "title": "How Runs works",
        "steps": [
            "Every set of notes made here is listed, newest first. Open one to download its files.",
            "Use the buttons above the list to see what is waiting, done, or did not finish.",
            "A run that did not finish says why in plain words; Try again makes it with the same choices.",
            "From older apps lists the UPSC Notes app's own runs, read where they are.",
        ],
    },
    "/practice": {
        "title": "How Practice works",
        "steps": [
            "Pick a finished UPSC lecture (made here or in the UPSC Notes app) and press Mains practice or DPP.",
            "Mains practice: make ten questions, tick the ones you want, make their model answers, then the sheets. "
            "Questions and answers use PW AI credit and wait in the Smart Queue.",
            "DPP: tick the topics, choose how many Easy, Medium and Hard, replace or remove any, then build the files. "
            "No AI and no cost.",
            "The lecture is only read: everything is made in its own practice set here.",
        ],
    },
    "/check": {
        "title": "How Check notes works",
        "steps": [
            "Drop a finished notes file, or press Check these notes on any finished run.",
            "UPSC notes get the full check (PW AI reviews the facts, with web lookups). Other notes get the basic "
            "checks: spelling, names, leftover markers and repeats, with no AI and no cost.",
            "The check waits in the Smart Queue; its run page has the error report (Excel) to download.",
            "The full check needs your account in the QC tool's column of the PW whitelist (upsc_file_QC).",
        ],
    },
    "/settings": {
        "title": "How Settings works",
        "steps": [
            f"Sign-in: choose once whether {APP} uses the old Short Notes app's sheets and sign-in, then sign in.",
            "AI models: the first model makes the notes. A gateway budget failure stops hand-drawn notes; ask the administrator to renew it.",
            "Categories: add a new exam, write its rules in plain sentences, check it, or switch it off.",
            "Values: the numbers the notes engine starts with. System: engines, Check setup and where your work is kept.",
            f"System → Updates: your version, Check for updates and the team's release folder. A newer {APP} shows "
            "the download progress beside Smart Queue. After it finishes and is checked, a pop-up offers Install update.",
        ],
    },
}


def for_path(path: str) -> dict | None:
    guide = GUIDES.get(path)
    if not guide or path not in ("/new", "/practice"):
        return guide
    steps = list(guide["steps"])
    if not engine_catalog.available("upsc"):
        steps = [s for s in steps if not s.startswith("UPSC notes:")] if path == "/new" else []
        steps.append("UPSC notes, Mains practice and DPP are not in this installation.")
    if path == "/new" and not engine_catalog.available("pw_books"):
        steps = [s.replace("Hand-drawn, PW Books or B.Sc. typeset notes", "Hand-drawn or B.Sc. typeset notes")
                  .replace(" (PW Books also takes PowerPoints)", "") for s in steps]
    return {**guide, "steps": steps}
