"""Build the benchmark PDF: a realistic notes document with known errors in it.

The document is laid out like a real Daily Class Note — title, headings,
bulleted body, footer with page numbers — because a checker that only works on
a bare wall of text has not been tested on what it is actually given.

    python tests/benchmark/build.py

Writes ``tests/benchmark/benchmark_notes.pdf`` and prints what went into it.
The answers live in ground_truth.py; nothing about them is written into the PDF.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

from make_fixtures import _closing, _h2, _page  # noqa: E402
from ground_truth import CLEAN_SENTENCES, SEEDED  # noqa: E402

OUT = Path(__file__).resolve().parent / "benchmark_notes.pdf"
LONG_OUT = Path(__file__).resolve().parent / "benchmark_notes_long.pdf"

#: Words the long build must exceed so the pipeline is forced to CHUNK. The
#: short document fits in one call, which means it never tested the path where
#: the real losses happened: parts that fail, get cut off, or fall between
#: chunk boundaries. Set from MAX_SINGLE_CALL_TRANSCRIPT_WORDS at build time.
LONG_TARGET_WORDS = 8000

#: Bullets per page. Enough that the document spans several pages and the
#: chunker has to split it, which is where coverage bugs used to hide.
PER_PAGE = 5


#: Neutral, correct filler. Varied so the duplicate-paragraph checks do not
#: fire on the padding, and factually bland so it cannot create false positives.
_FILLER_TOPICS = (
    "parliamentary procedure", "fiscal federalism", "administrative reform",
    "electoral administration", "public service delivery", "budget scrutiny",
    "committee oversight", "regulatory practice", "local government finance",
    "audit and accountability", "policy evaluation", "grievance redress",
)


def _filler(count: int) -> list[str]:
    """``count`` distinct, correct, unremarkable sentences.

    Padding exists to push the document past the single-call ceiling. It must
    be clean: any error in here would be scored against the checker as a false
    positive it did not deserve.
    """
    out: list[str] = []
    for i in range(count):
        topic = _FILLER_TOPICS[i % len(_FILLER_TOPICS)]
        out.append(
            f"Point {i + 1}: sound practice in {topic} depends on records that "
            f"can be inspected afterwards, on responsibilities that are stated "
            f"in advance, and on a route by which a decision can be questioned "
            f"by the people it affects."
        )
    return out


def build(path: Path = OUT, seed: int = 20260822, *, long: bool = False) -> Path:
    """Write the benchmark PDF. Deterministic: the same seed, the same document.

    Wrong and correct sentences are interleaved rather than grouped, so a
    checker cannot do well by noticing that "everything after page 2 is wrong".
    """
    rng = random.Random(seed)

    lines: list[str] = [e.sentence for e in SEEDED] + list(CLEAN_SENTENCES)
    rng.shuffle(lines)

    if long:
        # Spread the seeded errors evenly through a much longer document, so
        # some land in the first chunk, some in the middle, and some in the
        # last — the position where a tail-drop or a cut-off reply loses them.
        seeded = [e.sentence for e in SEEDED]
        rng.shuffle(seeded)
        words_needed = LONG_TARGET_WORDS
        filler = _filler(max(1, words_needed // 34))
        lines = []
        step = max(1, len(filler) // len(seeded))
        for index, sentence in enumerate(filler):
            lines.append(sentence)
            if index % step == 0 and seeded:
                lines.append(seeded.pop())
        lines.extend(seeded)          # anything left goes at the very end
        lines.extend(CLEAN_SENTENCES)

    doc = fitz.open()
    headings = ["Constitutional Framework", "Historical Background",
                "Geography and Culture", "Governance and Accountability",
                "Key Institutions", "Summary Points"]

    page_no = 1
    for start in range(0, len(lines), PER_PAGE):
        chunk = lines[start:start + PER_PAGE]
        heading = headings[(page_no - 1) % len(headings)]
        if page_no == 1:
            page = _page(doc, title="L07_DCN07_Polity_and_Culture",
                         heading=heading, body=chunk, page_no=page_no, logo=True)
            _h2(page, "Definitions", 520)
        else:
            _page(doc, heading=heading, body=chunk, page_no=page_no)
        page_no += 1

    _closing(doc, page_no)
    doc.save(str(path))
    doc.close()
    return path


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--long", action="store_true",
                    help="force multi-chunk: pad past the single-call ceiling")
    args = ap.parse_args()
    written = build(LONG_OUT if args.long else OUT, long=args.long)
    with fitz.open(str(written)) as d:
        pages, words = d.page_count, sum(len(p.get_text().split()) for p in d)

    print(f"wrote {written}")
    print(f"  {pages} pages, ~{words} words")
    print(f"  {len(SEEDED)} seeded errors, {len(CLEAN_SENTENCES)} correct sentences")
    for error in SEEDED:
        print(f"    [{error.category:10}] {error.key:24} {error.note}")
