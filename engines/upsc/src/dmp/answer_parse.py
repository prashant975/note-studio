"""Turn one model answer into the blocks the house-format writer draws.

The model returns Markdown-ish text. The writer wants structure: headings,
bullets with their nesting level, and paragraphs whose bold spans are marked.
This module is the whole of the translation, and it is deliberately forgiving —
a model that ignores the requested syntax should still produce a usable sheet,
not a wall of asterisks.

Two shapes are recognised as a heading, because models produce both: a real
Markdown heading (`## Why ...`), and a line that is nothing but a bold label
(`**Why ...:**`). The second is the common one, and reading it as body text is
what turns a structured answer into an undifferentiated block.

Recognised, but no longer printed. A Mains answer is an introduction, a body
and a conclusion; the content team (2026-10) rejected the per-demand
sub-headings ("Conceptual Basis of the Distinction") and the "Point:" label on
every bullet that the older prompt asked for. :func:`fold_answer_blocks` turns
any answer — new or saved under the old prompt — into that shape: the
INTRODUCTION / BODY / CONCLUSION markers are dropped, a stray sub-heading
becomes the bold lead-in of the point under it, and "Point:" is removed.
"""
from __future__ import annotations

import re

#: `# APPROACH` ... `# ANSWER`. Tolerates the heading markers being absent,
#: extra hashes, and the localised words the language directive may produce.
_APPROACH_HEAD_RE = re.compile(
    r"^\s*#{0,6}\s*(APPROACH|दृष्टिकोण|दृष्टिकोन)\s*:?\s*$", re.I)
_ANSWER_HEAD_RE = re.compile(
    r"^\s*#{0,6}\s*(ANSWER|उत्तर)\s*:?\s*$", re.I)

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

#: A bullet marker. The negative lookahead on `*` is what separates a list
#: item (`* text`) from emphasis (`**text**`) — without it every bolded label
#: reads as a bullet.
_BULLET_RE = re.compile(
    r"^(\s*)(?:([-•▪◦–○●]|\*(?!\*)|\d+[.)])\s+|-{5}\s*)(\S.*)$")

#: A line that is ONLY a bold label, with nothing after it: a section heading.
_BOLD_ONLY_RE = re.compile(r"^\*\*(?P<label>[^*]{2,80}?)\s*:?\*\*\s*:?\s*$")

#: A bold lead-in followed by text — kept as one paragraph/bullet, not split.
_BOLD_SPAN_RE = re.compile(r"\*\*(.+?)\*\*", re.S)

#: Approach lines: "Introduction: ...", "भूमिका: ...", "- Body: ..."
_APPROACH_KEYS = {
    "introduction": ("introduction", "intro", "भूमिका", "प्रस्तावना"),
    "body": ("body", "main body", "मुख्य भाग", "मुख्यभाग"),
    "conclusion": ("conclusion", "निष्कर्ष", "समापन"),
}

#: Headings that name a part of the answer rather than its content: the
#: prompt's own ## INTRODUCTION / ## BODY / ## CONCLUSION markers, and the
#: translated or older spellings of them. Matched whole, so "Introduction to
#: Federalism" is content and is folded like any other sub-heading.
_SECTION_MARKERS = frozenset(
    alias for aliases in _APPROACH_KEYS.values() for alias in aliases
) | {"परिचय", "answer", "model answer", "उत्तर", "मॉडल उत्तर", "नमुना उत्तर"}

#: The "Point:" label the older prompt put before every bullet ("-----**Point:**"),
#: with its Hindi and Marathi renderings. Needs its separator, so a sentence that
#: merely starts with the word ("Point of order ...") is left alone.
_POINT_LABEL_RE = re.compile(
    r"^\s*(?:point|बिंदु|बिन्दु|मुद्दा)\s*(?:\d+\s*)?[:：–-]\s*", re.I)

#: A bold-only line that ends like a sentence is a bold sentence, not a heading.
_SENTENCE_END_RE = re.compile(r"[.!?।]\s*$")


def split_bold(text: str) -> list[tuple[str, bool]]:
    """`a **b** c` -> [("a ", False), ("b", True), (" c", False)].

    Returns the whole line as one unbolded segment when there is no markup, so
    the caller never has to special-case the common shape.
    """
    text = (text or "").replace("\r", "")
    if "**" not in text:
        return [(text, False)] if text else []
    segments: list[tuple[str, bool]] = []
    position = 0
    for match in _BOLD_SPAN_RE.finditer(text):
        if match.start() > position:
            segments.append((text[position:match.start()], False))
        inner = match.group(1).strip()
        if inner:
            segments.append((inner, True))
        position = match.end()
    if position < len(text):
        segments.append((text[position:], False))
    return [(chunk, bold) for chunk, bold in segments if chunk]


def _indent_level(spaces: str) -> int:
    """Two spaces (or one tab) per level, capped at the three glyphs we draw."""
    width = len(spaces.replace("\t", "  "))
    return min(2, width // 2)


def split_approach(answer: str) -> tuple[str, str]:
    """(approach text, answer text).

    An answer with no APPROACH section returns ("", the whole thing) — which is
    every answer generated before the approach was asked for, and the reason
    the box is drawn only when it has content.
    """
    lines = (answer or "").replace("\r\n", "\n").split("\n")
    approach: list[str] = []
    body: list[str] = []
    target = None
    for line in lines:
        if _APPROACH_HEAD_RE.match(line):
            target = approach
            continue
        if _ANSWER_HEAD_RE.match(line):
            target = body
            continue
        (target if target is not None else body).append(line)
    return "\n".join(approach).strip(), "\n".join(body).strip()


def parse_approach(approach_text: str) -> dict:
    """The approach block as {introduction, body, body_subpoints, conclusion}.

    Sub-points are the lines indented under Body. They are collected wherever
    they appear after it, because a model that indents inconsistently should
    still get its nuance line into the box.
    """
    result = {"introduction": "", "body": "", "conclusion": "",
              "body_subpoints": []}
    current = None
    for raw in (approach_text or "").split("\n"):
        if not raw.strip():
            continue
        bullet = _BULLET_RE.match(raw)
        indent, content = (bullet.group(1), bullet.group(3)) if bullet else ("", raw)
        content = content.strip()
        plain = re.sub(r"\*\*", "", content).strip()
        matched = None
        for key, aliases in _APPROACH_KEYS.items():
            for alias in aliases:
                # Content starts where the matched separator ends. Finding an
                # ASCII ":" instead used to discard the whole line for the
                # hyphen and full-width-colon separators this regex accepts,
                # and cut at any incidental colon later in the sentence.
                separator = re.match(rf"^{re.escape(alias)}\s*[:：-]\s*", plain, re.I)
                if separator:
                    matched = key
                    plain = plain[separator.end():].strip()
                    break
            if matched:
                break
        if matched:
            result[matched] = plain
            current = matched
            continue
        # Not a labelled line: a sub-point under whatever came last.
        if current == "body" and (_indent_level(indent) > 0 or bullet):
            result["body_subpoints"].append(plain)
        elif current:
            result[current] = f"{result[current]} {plain}".strip()
    return result


def parse_answer_blocks(answer: str) -> list:
    """The answer body as the writer's block list.

    ("heading", text) | ("bullet", level, segments) | ("paragraph", segments)
    """
    blocks: list = []
    for raw in (answer or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue

        heading = _MD_HEADING_RE.match(line.strip())
        if heading:
            text = heading.group(2).strip().strip("*").strip()
            if text:
                blocks.append(("heading", text))
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            level = _indent_level(bullet.group(1))
            segments = split_bold(bullet.group(3).strip())
            if segments:
                blocks.append(("bullet", level, segments))
            continue

        bold_only = _BOLD_ONLY_RE.match(line.strip())
        if bold_only and not _SENTENCE_END_RE.search(bold_only.group("label")):
            blocks.append(("heading", bold_only.group("label").strip()))
            continue

        segments = split_bold(line.strip())
        if segments:
            blocks.append(("paragraph", segments))
    return blocks


def is_section_marker(heading: str) -> bool:
    """Is this heading INTRODUCTION / BODY / CONCLUSION (in any spelling)?"""
    key = re.sub(r"[*:：#]", "", heading or "").strip().lower()
    return " ".join(key.split()) in _SECTION_MARKERS


def strip_point_label(segments: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Segments with a leading "Point:" label removed, bold kept where it was.

    Worked on the joined text and then cut back out of the segments, because
    the label arrives as `**Point:** x`, `**Point: x**` and `**Point**: x`, and
    each splits into segments differently.
    """
    plain = "".join(chunk for chunk, _bold in segments)
    match = _POINT_LABEL_RE.match(plain)
    if not match:
        return segments
    drop = match.end()
    out: list[tuple[str, bool]] = []
    for chunk, bold in segments:
        if drop >= len(chunk):
            drop -= len(chunk)
            continue
        chunk, drop = chunk[drop:], 0
        if not out:
            chunk = chunk.lstrip()
        if chunk:
            out.append((chunk, bold))
    return out


def _lead_in(heading: str) -> tuple[str, bool]:
    """A folded sub-heading as the bold opening of the block under it."""
    text = heading.strip().rstrip(":：").strip()
    return (f"{text} " if _SENTENCE_END_RE.search(text) else f"{text}: ", True)


def fold_answer_blocks(blocks: list) -> list:
    """The answer as introduction, body and conclusion — and no headings.

    * The INTRODUCTION / BODY / CONCLUSION markers are dropped: the paragraph,
      bullets and paragraph under them already read as those three parts.
    * Any other heading is a sub-heading the answer should not have had. It
      becomes the bold lead-in of the point (or paragraph) under it, so a
      for/against split in an older answer keeps its words without printing
      as a heading. One with nothing under it is dropped.
    * A leading "Point:" is removed, and a paragraph that carried one becomes
      the bullet it was meant to be.
    """
    out: list = []
    pending: str = ""
    for block in blocks:
        kind = block[0]
        if kind == "heading":
            if is_section_marker(block[1]):
                pending = ""
            else:
                pending = f"{pending} — {block[1]}" if pending else block[1]
            continue
        if kind == "bullet":
            level, segments = block[1], strip_point_label(block[2])
        else:
            segments = block[1]
            stripped = strip_point_label(segments)
            kind, level = (("bullet", 0) if stripped is not segments
                           else ("paragraph", None))
            segments = stripped
        if pending:
            segments = [_lead_in(pending)] + list(segments)
            pending = ""
        if not segments:
            continue
        out.append(("bullet", level, segments) if kind == "bullet"
                   else ("paragraph", segments))
    return out


def answer_word_count(answer: str) -> int:
    """Words in the answer as it prints: the # ANSWER section only.

    The approach box, the markers and the Markdown are not counted, and
    neither is the app's own "> NOTE:" about a cut-off generation. A word is
    any whitespace-separated run with a letter or digit in it, which counts
    Devanagari the same way and leaves out a stray "—" or "।".
    """
    _approach, body = split_approach(answer or "")
    kept = "\n".join(line for line in body.split("\n")
                     if not line.lstrip().startswith(">"))
    words = 0
    for block in fold_answer_blocks(parse_answer_blocks(kept)):
        segments = block[2] if block[0] == "bullet" else block[1]
        text = "".join(chunk for chunk, _bold in segments)
        words += sum(1 for token in text.split()
                     if any(ch.isalnum() for ch in token))
    return words


def preview_markdown(answer: str, *, approach_title: str = "Approach",
                     answer_title: str = "Answer") -> str:
    """The stored answer as the panel shows it: what the sheet prints, in Markdown.

    The raw text carries the ## INTRODUCTION / ## BODY / ## CONCLUSION markers
    the sheet never prints; shown raw, the preview put them back as headings.
    """
    approach_text, body = split_approach(answer or "")

    def line(segments) -> str:
        return "".join(f"**{chunk}**" if bold else chunk for chunk, bold in segments).strip()

    out: list[str] = []
    if approach_text:
        out += [f"**{approach_title}**", "", approach_text, "", f"**{answer_title}**", ""]
    for block in fold_answer_blocks(parse_answer_blocks(body)):
        if block[0] == "bullet":
            out.append(f"{'  ' * block[1]}- {line(block[2])}")
        else:
            out += ["", line(block[1]), ""]
    return "\n".join(out).strip()


def parse(answer: str) -> tuple[dict, list]:
    """(approach dict, answer blocks) for one stored model answer.

    The blocks are folded (see :func:`fold_answer_blocks`): what the sheet
    prints is paragraphs and bullets, never a heading.
    """
    approach_text, body = split_approach(answer)
    return (parse_approach(approach_text),
            fold_answer_blocks(parse_answer_blocks(body)))
