"""Recover a PDF's real text when its own text layer cannot be read.

The problem this solves is described in ``src/pdf/text_integrity.py``: a DCN PDF
whose Devanagari font is an ``Identity-H`` subset gives back its letters in the
order they are *drawn*, so ``जाति`` extracts as ``जातत``. The words on the page
are correct; the copyable layer behind them is not.

Five of the nine QC stages read that layer. Left alone, all five have to be
discounted on every Hindi notes PDF — which is most of them. So instead the
rendered pages are read back by the same model the visual check already uses,
and the result is handed to those stages as if it had come out of the file.

Two things make that safe rather than a leap of faith:

* The model is asked to **transcribe, not interpret** — verbatim text, at
  temperature 0, with the page structure marked up in the DCN marker vocabulary
  the checks already speak (``#H1:``, ``#TABLE_START``, ``- `` bullets). Nothing
  downstream has to be adapted to it.
* The result is **validated before it is trusted** (:func:`validate`): it must
  be free of the impossible letter sequences that gave the original away, and it
  must be about as long as the page said it was. A transcription that fails is
  discarded and the caller falls back to reading pages as images.

Recovering the text also recovers two things the geometry alone could not: the
``#FLOW_START``/``#FLOW_END`` zones (a flowchart is arrows on the page, and only
a reader can tell them from prose) and the notes' true word count.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .extractor import PdfDocument
from .text_integrity import check_text_integrity

#: Pages per transcription call. The same batch the visual check uses: images
#: travel inline through the proxy and small batches are what that path is
#: tuned for.
PAGES_PER_CALL = 6

#: A recovered page shorter than this share of what the (scrambled) text layer
#: reported means the model summarised or skipped instead of transcribing.
#: Devanagari extraction can lose or duplicate characters in either direction,
#: so the window is generous — it is there to catch "it wrote a paragraph about
#: the page", not to police wording.
MIN_LENGTH_RATIO = 0.45
MAX_LENGTH_RATIO = 2.5

_PAGE_MARK_RE = re.compile(r"^\s*#PAGE\s+(\d+)\s*$", re.I | re.M)
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$|^\s*```\s*$", re.M)


@dataclass
class Recovery:
    """What the transcription produced, and whether it can be used."""

    marker_text: str = ""
    #: 1-based page number -> that page's recovered text.
    pages: dict[int, str] = field(default_factory=dict)
    ok: bool = False
    reason: str = ""
    calls: int = 0
    warnings: list[str] = field(default_factory=list)
    #: marker_text line index (0-based) -> page number, so a finding reported
    #: as "Line 118" can still be given to a reviewer as "Page 7".
    line_pages: dict[int, int] = field(default_factory=dict)

    def page_of_line(self, line_no: int) -> int | None:
        """Page for a 1-based line number, matching Reconstruction's API."""
        return self.line_pages.get(line_no - 1)

    @property
    def plain_text(self) -> str:
        """The recovered notes with the structural markers stripped back out."""
        return strip_markers(self.marker_text)

    @property
    def paged_text(self) -> str:
        """``[Page n]``-prefixed text, the shape the error-report prompt wants."""
        return "\n\n".join(f"[Page {n}]\n{self.pages[n]}".rstrip()
                           for n in sorted(self.pages))


_MARKER_LINE_RE = re.compile(
    r"^\s*#(?:LECTURE_TITLE|H[1-6]|TABLE_START|TABLE_END|FLOW_START|FLOW_END|"
    r"TOPICS_COVERED_START|TOPICS_COVERED_END|SEPARATOR|PAGE)\b:?\s*", re.I)


def strip_markers(marker_text: str) -> str:
    """Marker text -> the prose a text check should see."""
    out = []
    for line in (marker_text or "").splitlines():
        if re.match(r"^\s*#(?:TABLE|FLOW|TOPICS_COVERED|SEPARATOR|PAGE)\b", line, re.I):
            continue
        out.append(_MARKER_LINE_RE.sub("", line))
    return "\n".join(out)


PROMPT = """These are consecutive pages ({page_range}) of a printed UPSC class-notes PDF. Transcribe them.

This is a TRANSCRIPTION task, not a summary and not a review. Write out what is printed, word for word, in the language it is printed in. Do not translate. Do not correct anything, however wrong it looks — a wrong fact must be transcribed as the wrong fact. Do not add, explain, comment, or skip anything. If a word is genuinely illegible, write [?] in its place rather than guessing.

Mark up the structure with these markers, each alone on its own line, so the notes keep their shape:

#PAGE <printed page number>      before each page's content, always
#LECTURE_TITLE: <text>           the big coloured title on the first page, once
#H1: <text>                      a top-level section heading
#H2: <text>                      a sub-heading
#H3: <text>                      a sub-sub-heading
- <text>                         a bullet; indent nested bullets by two spaces per level
#TABLE_START / #TABLE_END        around a table; write each row as "cell | cell | cell"
#FLOW_START / #FLOW_END          around a flowchart; one step per line, with a lone down-arrow between steps

Rules:
- A heading that is broken across two printed lines is ONE heading on ONE line.
- A sentence broken across printed lines is ONE line. Do not keep the page's line breaks.
- Keep every number, unit, date, article number, scheme name and proper noun exactly as printed.
- Keep text that is clearly not meant for students (draft notes, verification markers, system text) — it is what the check is looking for.
- SKIP the page furniture that repeats on every page: the running header, the footer strip, the page number, and the "Space for Notes" caption down the right-hand side. They are the template, not the notes.
- Output the markers and the transcribed text only. No preamble, no code fence, no closing remark."""


def _clean_response(text: str) -> str:
    return _FENCE_RE.sub("", text or "").strip()


def _split_pages(text: str) -> dict[int, str]:
    """The response, cut into pages on its own ``#PAGE n`` marks."""
    pages: dict[int, str] = {}
    marks = list(_PAGE_MARK_RE.finditer(text))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[mark.end():end].strip()
        try:
            pages[int(mark.group(1))] = body
        except ValueError:
            continue
    return pages


def validate(recovered: dict[int, str], document: PdfDocument) -> tuple[bool, str]:
    """Is this transcription usable? Returns (ok, reason when not).

    Three ways it can fail, in the order they are worth reporting:
    nothing came back; what came back is still malformed Devanagari (the model
    read the broken text layer instead of the picture, or hallucinated); or a
    page came back far shorter or far longer than the page actually is.
    """
    if not recovered:
        return False, "the transcription returned no pages"

    joined = "\n".join(recovered.values())
    integrity = check_text_integrity(joined)
    if not integrity.reliable:
        return False, ("the transcription still contains impossible Devanagari "
                       f"({integrity.corruption_ratio:.0%} of words) — it was not "
                       "read from the page")

    expected = {p.page_no: len(p.text.strip()) for p in document.pages}
    bad: list[str] = []
    for page_no, body in recovered.items():
        want = expected.get(page_no, 0)
        if want < 200:
            continue                       # a near-empty page has no ratio to hold
        ratio = len(body.strip()) / want
        if not (MIN_LENGTH_RATIO <= ratio <= MAX_LENGTH_RATIO):
            bad.append(f"page {page_no} ({ratio:.0%} of its printed length)")
    if len(bad) > max(1, len(recovered) // 4):
        return False, ("the transcription is the wrong length on " +
                       ", ".join(bad[:6]) +
                       (" and others" if len(bad) > 6 else ""))

    missing = [p.page_no for p in document.pages
               if p.page_no not in recovered and len((p.text or "").strip()) >= 200]
    if missing:
        return False, (f"{len(missing)} page(s) were not transcribed at all "
                       f"(first: page {missing[0]})")
    return True, ""


def recover_text(
    client,
    document: PdfDocument,
    run_dir: Path,
    *,
    pages_per_call: int = PAGES_PER_CALL,
    max_workers: int = 4,
    stage_callback=None,
) -> Recovery:
    """Read the rendered pages back as text. Never raises.

    A failed or unusable transcription comes back with ``ok=False`` and a reason;
    the caller then falls back to sending pages as images, which is what this app
    did before this stage existed.
    """
    result = Recovery()
    pages = [p for p in document.pages if p.image_path and Path(p.image_path).exists()]
    if not pages:
        result.reason = "no page was rendered, so there was nothing to read"
        return result

    batches = [pages[i:i + pages_per_call] for i in range(0, len(pages), pages_per_call)]
    result.calls = len(batches)

    def one(index: int, batch: list) -> tuple[int, str, str]:
        page_range = f"{batch[0].page_no}-{batch[-1].page_no}"
        prompt = PROMPT.format(page_range=page_range)
        try:
            resp = client.generate(
                prompt, [p.image_path for p in batch],
                max_images=len(batch), max_output_tokens=32000, temperature=0.0)
        except Exception as e:
            return index, "", f"pages {page_range}: {str(e)[:200]}"
        return index, _clean_response(resp.text), ""

    if len(batches) == 1:
        outcomes = [one(0, batches[0])]
    else:
        from concurrent.futures import ThreadPoolExecutor

        if stage_callback:
            try:
                stage_callback(f"Reading the pages (0 of {len(batches)})")
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = [pool.submit(one, i, b) for i, b in enumerate(batches)]
            outcomes = []
            for done, future in enumerate(futures, start=1):
                outcomes.append(future.result())
                if stage_callback:
                    try:
                        stage_callback(f"Reading the pages ({done} of {len(batches)})")
                    except Exception:
                        pass

    parts: list[str] = []
    for _i, text, error in sorted(outcomes, key=lambda o: o[0]):
        if error:
            result.warnings.append(error)
        if text:
            parts.append(text)
    if not parts:
        result.reason = (
            "the transcription call failed: " + "; ".join(result.warnings[:2])
            if result.warnings else "the transcription returned nothing")
        return result

    combined = "\n".join(parts)
    try:
        (Path(run_dir) / "recovered_notes.txt").write_text(combined, encoding="utf-8")
    except Exception:
        pass

    result.pages = _split_pages(combined)
    ok, reason = validate(result.pages, document)
    if not ok:
        result.reason = reason
        return result

    # The #PAGE marks are this module's own scaffolding; check_dcn_notes has
    # never heard of them and would read one as an unknown marker line. They are
    # dropped, and the page each surviving line came from is recorded on the way
    # out so a "Line 118" finding can still be reported as "Page 7".
    kept: list[str] = []
    current_page = pages[0].page_no
    for line in combined.splitlines():
        mark = _PAGE_MARK_RE.match(line)
        if mark:
            try:
                current_page = int(mark.group(1))
            except ValueError:
                pass
            continue
        result.line_pages[len(kept)] = current_page
        kept.append(line)
    result.marker_text = "\n".join(kept).strip()
    result.ok = True
    return result
