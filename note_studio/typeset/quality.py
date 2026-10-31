"""The quality report of one typeset run (quality_report.json): what was checked,
what was corrected, and what a person should look at. Its verdict is "passed"
or "needs_attention"; the run then shows Done or Needs attention."""
from __future__ import annotations

import re
from collections import Counter

from . import document
from .prompts import REVISION

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN = re.compile(r"[A-Za-z]")


def language_problem(doc: dict, language: str) -> str:
    """Plain words when the notes are not in the language asked for, else ""."""
    text = " ".join(t for _, t in document.texts(doc))
    text = re.sub(r"\$[^$]*\$", "", text)
    deva, latin = len(_DEVANAGARI.findall(text)), len(_LATIN.findall(text))
    if not deva + latin:
        return ""
    share = deva / (deva + latin)
    if language == "hindi" and share < 0.4:
        return "The notes were asked for in Hindi but are mostly in Latin script."
    if language in ("english", "hinglish") and share > 0.2:
        return "The notes were asked for in Latin script but contain much Devanagari."
    return ""


def report(*, doc: dict, tidy: list[str], review: dict, figures: list[dict], layout: dict, language: str) -> dict:
    checks: list[dict] = []

    def check(name: str, result: str, detail: str = "") -> None:
        checks.append({"check": name, "result": result, "detail": detail})

    kinds = Counter(b["kind"] for b in doc["blocks"])
    styles = Counter(b["style"] for b in doc["blocks"] if b["kind"] == "box")
    check("Notes written", "ok", f"{kinds['section']} sections, {kinds['subsection']} subsections, "
                                 f"{sum(styles.values())} boxes ({styles['definition']} definitions), "
                                 f"{kinds['table']} tables, {kinds['tree'] + kinds['flow']} diagrams")
    check("Key Takeaways", "ok" if styles["summary"] else "attention",
          "" if styles["summary"] else "The notes end without a Key Takeaways box.")
    check("Tidied", "ok" if not tidy else "fixed", "; ".join(tidy[:12]) + (" ..." if len(tidy) > 12 else ""))
    losses = [issue for issue in tidy if "left out" in issue or "trimmed" in issue or "kept the first" in issue]
    if losses:
        check("Content removed during processing", "attention", "; ".join(losses))
    if review.get("done"):
        applied, skipped, missing = review["applied"], review["skipped"], review["missing"]
        check("Checked against the lecture", "ok",
              f"{len(applied)} correction(s) made" + (f", {len(skipped)} not applied (the text had changed)"
                                                      if skipped else ""))
        if missing:
            check("Topics the lecture teaches that the notes leave out", "attention",
                  "; ".join(f"{m['topic']}" + (f" (page {m['page']})" if m.get("page") else "") for m in missing))
    elif review.get("asked"):
        check("Checked against the lecture", "attention", review.get("problem") or "The check could not be made.")
    else:
        check("Checked against the lecture", "skipped", "Switched off for this run.")
    for n, figure in enumerate(figures, start=1):
        check(f"Figure: {figure['caption']}", figure["result"], "; ".join(figure.get("issues") or []))
    overflow = [n + 1 for n, fill in enumerate(layout.get("fills") or []) if fill > 1.002]
    check("Pages", "attention" if overflow else "ok",
          f"{layout.get('pages', 0)} pages" + (f"; page {overflow} runs past its margin" if overflow else ""))
    for issue in layout.get("issues") or []:
        check("Drawing", "attention", issue)
    wrong_language = language_problem(doc, language)
    if wrong_language:
        check("Language", "attention", wrong_language)
    attention = any(c["result"] == "attention" for c in checks)
    return {"verdict": "needs_attention" if attention else "passed", "engine": "typeset", "revision": REVISION,
            "language": language, "checks": checks, "review": review, "figures": figures, "layout": layout}
