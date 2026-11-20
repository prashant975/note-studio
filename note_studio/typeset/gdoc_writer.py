"""Write locked typeset notes into an editable Google Doc: one tab per topic, B.Sc. look.

Google converts an uploaded Word file into a single-tab Doc, so tabs need the
Docs API itself. Each tab is built by inserting its blocks in reverse order at
the tab's first position (index 1): every insert's range is then known exactly,
with no index arithmetic over earlier content, and one batch builds one tab.

The B.Sc. template's colours, box styles and branding come from render.py, so
the Doc and the PDF stay one design (reference: the B.Sc. Wallah notes):

- the tab's first heading is the centred navy title; a short line before it
  (the course line) is a small grey kicker;
- boxes: a source label just before a box ("Definition:", "Important Points")
  moves into the box's title bar; a box without a source label has no bar (the
  template adds no words); an "Important Points"/"Key Points"/"Definition"
  section becomes a box with its heading in the bar;
- bold words inside a sentence are key terms (navy bold); a bold lead label
  ("Epimysium:") stays black; headings drop the source's underline;
- simple $...$ notation in the source ($Ca^{2+}$, $T_3$) is shown as text with
  real superscripts/subscripts;
- booktabs tables (header shading, top/mid/bottom rules, short columns centred,
  header repeated on each page); split header and footer bars.

Content is the source's, only the formatting is new: the read-back check (verify)
proves every source paragraph, cell and picture arrived in order with the same
words, and that nothing was added except a list item's own number or bullet.
"""
from __future__ import annotations

import re
from typing import Callable

from . import render, source
from .gdoc_source import BULLET, NUMBERED, TEXT_WIDTH_PT, u16

FONT = "Lato"
SIZE = {"title": 20, "kicker": 9.5, "section": 14, "subsection": 12, "paragraph": 11, "table": 10,
        "box_title": 10.5, "header": 9, "footer": 8}
NAMED = {"title": "TITLE", "kicker": "SUBTITLE", "section": "HEADING_1", "subsection": "HEADING_2",
         "paragraph": "NORMAL_TEXT"}
SPACING = {"title": (2, 12), "kicker": (0, 2), "section": (16, 6), "subsection": (12, 4), "paragraph": (3, 3)}
PAGE = {"width": render.PAGE_W, "height": render.PAGE_H, "left": render.LEFT, "right": render.PAGE_W - render.RIGHT,
        "top": 46.8, "bottom": 43.2, "header": 18.0, "footer": 18.0}
MAX_IMAGE_HEIGHT = 680.0
CHUNK = 400                       # requests per batchUpdate
# A section with one of these headings is boxed in that style, with the heading in the title bar.
KEY_SECTIONS = {"important points": "note", "important point": "note", "points to remember": "note",
                "remember": "note", "note": "note", "notes": "note", "key points": "summary", "key point": "summary",
                "summary": "summary", "quick recap": "summary", "recap": "summary", "key takeaways": "summary",
                "definition": "definition", "definitions": "definition"}
SECTION_BOX_LIMIT = (12, 1600)    # blocks, UTF-16 characters: a longer section stays as running text
CENTRE_CELL = 40                  # a table column whose cells are all this short is centred


def pt(value: float) -> dict:
    return {"magnitude": round(float(value), 2), "unit": "PT"}


def rgb(colour) -> dict:
    r, g, b = colour
    return {"color": {"rgbColor": {"red": r / 255, "green": g / 255, "blue": b / 255}}}


def border(colour, width: float) -> dict:
    return {"color": rgb(colour), "width": pt(width), "dashStyle": "SOLID"}


def _range(start: int, end: int, tab: str, segment: str = "") -> dict:
    found = {"startIndex": start, "endIndex": end, "tabId": tab}
    if segment:
        found["segmentId"] = segment
    return found


def _at(index: int, tab: str, segment: str = "") -> dict:
    found = {"index": index, "tabId": tab}
    if segment:
        found["segmentId"] = segment
    return found


TEXT_FIELDS = ("bold,italic,underline,strikethrough,smallCaps,baselineOffset,fontSize,foregroundColor,"
               "backgroundColor,weightedFontFamily,link")


def text_style(start: int, end: int, tab: str, *, size: float, bold: bool = False, italic: bool = False,
               colour=render.BLACK, segment: str = "") -> dict:
    return {"updateTextStyle": {"range": _range(start, end, tab, segment), "fields": TEXT_FIELDS, "textStyle": {
        "bold": bold, "italic": italic, "underline": False, "fontSize": pt(size), "foregroundColor": rgb(colour),
        "weightedFontFamily": {"fontFamily": FONT, "weight": 400}}}}


def paragraph_style(start: int, end: int, tab: str, *, named: str = "NORMAL_TEXT", align: str = "JUSTIFIED",
                    above: float = 3, below: float = 3, line: float = 115, indent: bool = True,
                    keep: bool = False, segment: str = "", extra: dict | None = None) -> dict:
    # Every property a split paragraph could inherit from its neighbour is set explicitly.
    # Sent before the text style: setting a named style resets the text style of its runs.
    style = {"namedStyleType": named, "alignment": align, "spaceAbove": pt(above), "spaceBelow": pt(below),
             "lineSpacing": line, "keepWithNext": keep}
    fields = ["namedStyleType", "alignment", "spaceAbove", "spaceBelow", "lineSpacing", "keepWithNext"]
    if indent:
        style.update(indentStart=pt(0), indentFirstLine=pt(0))
        fields += ["indentStart", "indentFirstLine"]
    for key, value in (extra or {}).items():
        style[key] = value
        fields.append(key)
    return {"updateParagraphStyle": {"range": _range(start, end, tab, segment), "paragraphStyle": style,
                                     "fields": ",".join(fields)}}


def no_bullets(start: int, end: int, tab: str) -> dict:
    return {"deleteParagraphBullets": {"range": _range(start, end, tab)}}


# --------------------------------------------------------------------------- inline notation
_MATH = re.compile(r"\$([^$\n]{1,60})\$")
_SYMBOLS = {"times": "×", "cdot": "·", "pm": "±", "rightarrow": "→", "to": "→", "leftarrow": "←",
            "leftrightarrow": "↔", "rightleftharpoons": "⇌", "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
            "Delta": "Δ", "mu": "μ", "lambda": "λ", "pi": "π", "sigma": "σ", "theta": "θ", "omega": "ω",
            "Omega": "Ω", "circ": "°", "degree": "°", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "approx": "≈",
            "neq": "≠", "%": "%", ",": " ", ";": " ", " ": " ", "uparrow": "↑", "downarrow": "↓"}


def _render_math(src: str):
    """Simple TeX notation -> (text, [(start, end, SUPERSCRIPT|SUBSCRIPT)]), or None when not simple."""
    out, scripts, i = "", [], 0
    while i < len(src):
        ch = src[i]
        if ch in "^_":
            i += 1
            if i < len(src) and src[i] == "{":
                close = src.find("}", i)
                if close < 0:
                    return None
                inner, i = src[i + 1:close], close + 1
            elif i < len(src):
                inner, i = src[i], i + 1
            else:
                return None
            made = _render_math(inner)
            if made is None or made[1]:
                return None
            start = u16(out)
            out += made[0]
            scripts.append((start, u16(out), "SUPERSCRIPT" if ch == "^" else "SUBSCRIPT"))
        elif ch == "\\":
            found = re.match(r"\\([A-Za-z]+|.)", src[i:])
            if not found:
                return None
            name, i = found.group(1), i + len(found.group(0))
            if name in ("text", "mathrm", "mathbf", "rm") and i < len(src) and src[i] == "{":
                close = src.find("}", i)
                if close < 0:
                    return None
                out, i = out + src[i + 1:close], close + 1
                continue
            symbol = _SYMBOLS.get(name)
            if symbol is None:
                return None
            out += symbol
        elif ch in "{}":
            i += 1
        else:
            out, i = out + ch, i + 1
    return out, scripts


def inline_math(text: str):
    """(new text, scripts, old->new UTF-16 offset map) with each simple $...$ shown as text."""
    if "$" not in text:
        return text, [], None
    out, scripts, mapping, last = "", [], [], 0

    def keep(chunk: str) -> None:
        nonlocal out
        for ch in chunk:
            for _ in range(u16(ch)):
                mapping.append(u16(out))
            out += ch
    for match in _MATH.finditer(text):
        inner = match.group(1)
        # TeX-like only: no space at its edges, and a script, a command or a formula word ("HCl").
        # Prices such as "$5 and $10" stay as written.
        if inner != inner.strip() or not (re.search(r"[\^_\\]", inner) or re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", inner)):
            continue
        made = _render_math(inner)
        if made is None:
            continue
        keep(text[last:match.start()])
        base = u16(out)
        for _ in range(u16(match.group(0))):
            mapping.append(base)
        out += made[0]
        scripts += [(base + s, base + e, kind) for s, e, kind in made[1]]
        last = match.end()
    if not scripts and out == "":
        return text, [], None
    keep(text[last:])
    mapping.append(u16(out))
    return out, scripts, mapping


def math_plain(text: str) -> str:
    return inline_math(text)[0]


def _with_math(block: dict) -> dict:
    """A copy of a block with its $...$ notation shown as text; spans follow the new offsets."""
    block = dict(block)
    if block["kind"] == "source_text" and "$" in block.get("text", ""):
        new, scripts, mapping = inline_math(block["text"])
        if mapping:
            block["spans"] = [[mapping[min(s, len(mapping) - 1)], mapping[min(e, len(mapping) - 1)], marks]
                              for s, e, marks in block.get("spans") or []]
            block.update(text=new, scripts=scripts)
    elif block["kind"] == "source_box" and any("$" in p for p in block.get("parts") or []):
        made = [inline_math(p) for p in block["parts"]]
        block["parts"] = [m[0] for m in made]
        block["part_scripts"] = [m[1] for m in made]
        if len(made) == 1 and made[0][2]:
            mapping = made[0][2]
            block["spans"] = [[mapping[min(s, len(mapping) - 1)], mapping[min(e, len(mapping) - 1)], marks]
                              for s, e, marks in block.get("spans") or []]
    elif block["kind"] == "source_table":
        grid = [block["columns"], *block["rows"]]
        if any("$" in cell for row in grid for cell in row):
            made = [[inline_math(cell) for cell in row] for row in grid]
            block["columns"] = [m[0] for m in made[0]]
            block["rows"] = [[m[0] for m in row] for row in made[1:]]
            block["cell_scripts"] = [[m[1] for m in row] for row in made]
    return block


def span_styles(start: int, spans: list, tab: str, text: str = "", *, key_terms: bool = True) -> list[dict]:
    """The source's own bold/italic/underline/links, at their exact places. A bold word inside a
    sentence is a key term (navy, as the B.Sc. notes mark them); a bold lead label stays black."""
    out = []
    for s, e, marks in spans or []:
        if e <= s:
            continue
        style, fields = {}, []
        for key in ("bold", "italic", "underline"):
            if marks.get(key):
                style[key] = True
                fields.append(key)
        if marks.get("link"):
            style["link"] = {"url": marks["link"]}
            fields.append("link")
        if key_terms and marks.get("bold") and not _lead_label(text, s, e):
            style["foregroundColor"] = rgb(render.NAVY)
            fields.append("foregroundColor")
        if fields:
            out.append({"updateTextStyle": {"range": _range(start + s, start + e, tab), "fields": ",".join(fields),
                                            "textStyle": style}})
    return out


def _lead_label(text: str, s: int, e: int) -> bool:
    """A bold run at the start of a paragraph that ends in a colon ("Epimysium:", "Definition -")."""
    if not text or s > 1:
        return False
    tail = text.encode("utf-16-le")[2 * e:].decode("utf-16-le", "ignore").lstrip()
    head = text.encode("utf-16-le")[2 * s:2 * e].decode("utf-16-le", "ignore").rstrip()
    return head.endswith((":", "-", "–")) or tail.startswith((":", "-", "–"))


FALLBACK_FONT = "Noto Sans"      # for characters Lato has no glyph for (ₙ, ₘ ...): same characters, drawn


def _lato_has() -> set:
    import functools

    @functools.lru_cache(maxsize=1)
    def load() -> frozenset:
        from fontTools.ttLib import TTFont

        return frozenset(TTFont(str(render.FONT_DIR / "Lato-Regular.ttf")).getBestCmap())
    return load()


def glyph_styles(start: int, text: str, tab: str) -> list[dict]:
    """Runs of characters Lato cannot draw are set in Noto Sans, so they print instead of "?"."""
    have, out, run_start, offset = _lato_has(), [], None, 0
    for ch in text + "\n":
        missing = not ch.isspace() and ord(ch) not in have
        if missing and run_start is None:
            run_start = offset
        elif not missing and run_start is not None:
            out.append({"updateTextStyle": {"range": _range(start + run_start, start + offset, tab),
                                            "fields": "weightedFontFamily",
                                            "textStyle": {"weightedFontFamily": {"fontFamily": FALLBACK_FONT,
                                                                                 "weight": 400}}}})
            run_start = None
        offset += u16(ch)
    return out


def script_styles(start: int, scripts: list, tab: str) -> list[dict]:
    return [{"updateTextStyle": {"range": _range(start + s, start + e, tab), "fields": "baselineOffset",
                                 "textStyle": {"baselineOffset": kind}}} for s, e, kind in scripts or [] if e > s]


# --------------------------------------------------------------------------- list numbers
_ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
          (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
_DEFAULT_KINDS = ("DECIMAL", "ALPHA", "ROMAN")


def _number(n: int, kind: str) -> str:
    if kind == "ZERO_DECIMAL":
        return f"{n:02d}"
    if kind in ("ALPHA", "UPPER_ALPHA") and 1 <= n <= 26:
        letter = chr(96 + n)
        return letter.upper() if kind == "UPPER_ALPHA" else letter
    if kind in ("ROMAN", "UPPER_ROMAN") and n >= 1:
        out, rest = "", n
        for value, numeral in _ROMAN:
            while rest >= value:
                out, rest = out + numeral, rest - value
        return out.upper() if kind == "UPPER_ROMAN" else out
    return str(n)


def list_labels(blocks: list[dict], lists: dict | None = None) -> dict[str, str]:
    """Each list item's label as the source shows it ("2.", "b)", "•"), counting every
    item of a list in reading order, also across the paragraphs that interrupt it."""
    counters: dict[str, list[int]] = {}
    labels: dict[str, str] = {}
    for block in blocks:
        info = block.get("list")
        if not info:
            continue
        levels = (lists or {}).get(info["id"]) or []
        level = min(int(info.get("level") or 0), 8)
        count = counters.setdefault(info["id"], [0] * 9)
        count[level] += 1
        for deeper in range(level + 1, 9):
            count[deeper] = 0

        def glyph(k: int) -> dict:
            return levels[k] if k < len(levels) else {}

        kind = glyph(level).get("glyphType") or (_DEFAULT_KINDS[level % 3] if info.get("numbered") and not levels else "")
        if kind not in NUMBERED:
            labels[block["source_id"]] = BULLET.strip()
            continue

        def value(k: int) -> str:
            start = int(glyph(k).get("startNumber") or 1)
            return _number(start + max(count[k], 1) - 1, glyph(k).get("glyphType") or _DEFAULT_KINDS[k % 3])
        pattern = glyph(level).get("glyphFormat") or f"%{level}."
        labels[block["source_id"]] = re.sub(r"%(\d)", lambda m: value(int(m.group(1))), pattern)
    return labels


def _prefix(block: dict, labels: dict[str, str], text: str) -> str:
    """The marker written before a list item's text when Google's own list is not used."""
    if not block.get("list"):
        return ""
    label = labels.get(block["source_id"]) or BULLET.strip()
    return "" if text.startswith(label + " ") else label + " "


# --------------------------------------------------------------------------- layout plan
def _group(blocks: list[dict]) -> list[dict]:
    """Consecutive items of one list (one native list); consecutive boxes of one style (one frame).
    A numbered list interrupted by other content is "literal": Google would restart the numbers
    of each part, so its source numbers are written as text instead."""
    out: list[dict] = []
    for block in blocks:
        kind = block["kind"]
        last = out[-1] if out else None
        if kind == "source_text" and block.get("list"):
            if last and last["kind"] == "list" and last["list_id"] == block["list"]["id"]:
                last["blocks"].append(block)
            else:
                out.append({"kind": "list", "list_id": block["list"]["id"], "blocks": [block]})
        elif kind == "source_box":
            if last and last["kind"] == "box" and last["style"] == block["style"] and not last.get("title_block"):
                last["blocks"].append(block)
            else:
                out.append({"kind": "box", "style": block["style"], "blocks": [block]})
        else:
            out.append({"kind": kind, "blocks": [block]})
    parts: dict[str, int] = {}
    for group in out:
        if group["kind"] == "list":
            parts[group["list_id"]] = parts.get(group["list_id"], 0) + 1
    for group in out:
        if group["kind"] == "list":
            group["literal"] = parts[group["list_id"]] > 1 and any(b["list"].get("numbered") for b in group["blocks"])
    return out


def _bare(text: str) -> str:
    return re.sub(r"[\s:–\-]+$", "", source.normal(text)).lower()


def _key_section(block: dict) -> str:
    """The box style for an "Important Points"/"Key Points"/"Definition" heading or label, else ''."""
    if block["kind"] != "source_text" or block.get("list") or block.get("role") in ("title", "kicker"):
        return ""
    return KEY_SECTIONS.get(_bare(block["text"]), "")


def _is_label(block: dict) -> bool:
    """A short source line that introduces what follows ("Definition:", "Musculoskeletal:")."""
    return (block["kind"] == "source_text" and not block.get("list") and block.get("role") in ("paragraph", "subsection")
            and u16(block["text"]) <= 60 and block["text"].rstrip().endswith(":")) or bool(_key_section(block))


def _is_body(block: dict) -> bool:
    return block["kind"] == "source_text" and block.get("role", "paragraph") == "paragraph"


def plan(blocks: list[dict], lists: dict | None = None) -> tuple[list[dict], dict[str, str]]:
    """The tab's groups in reading order with their B.Sc. presentation, and the list labels.
    Only appearance and grouping are decided here; every word stays in its place."""
    labels = list_labels(blocks, lists)
    work = [_with_math(b) for b in blocks]
    for i, block in enumerate(work[:3]):
        if block["kind"] == "source_text" and not block.get("list") and \
                block.get("role") in ("title", "section", "subsection") and _bare(block["text"]) not in KEY_SECTIONS:
            work[i] = dict(block, role="title")
            for k in range(i):
                before = work[k]
                if before["kind"] == "source_text" and not before.get("list") and \
                        before.get("role") == "paragraph" and u16(before["text"]) <= 90:
                    work[k] = dict(before, role="kicker")
            break
    out = _group(work)
    final: list[dict] = []
    i = 0
    while i < len(out):
        group = out[i]
        head = group["blocks"][0] if group["kind"] == "source_text" else None
        # A label, or a short subheading, that leads straight into a box becomes the box's title bar:
        # the same words in the same place, and Google never keeps a paragraph with a table after it,
        # so a heading before a box could end a page alone (seen in UNIT-1 "Clinical Importance").
        leads_in = _is_label(head) or (head.get("role") == "subsection" and u16(head["text"]) <= 60) \
            if head is not None else False
        if head is not None and i + 1 < len(out) and out[i + 1]["kind"] == "box" and \
                not out[i + 1].get("title_block") and leads_in:
            out[i + 1]["title_block"] = head
            i += 1
            continue
        style = _key_section(head) if head is not None else ""
        if style:
            j, members = i + 1, []
            while j < len(out) and out[j]["kind"] in ("source_text", "list") and \
                    all(_is_body(b) for b in out[j]["blocks"]):
                members += out[j]["blocks"]
                j += 1
                if style == "definition":       # a definition is the one paragraph (or list) after its label
                    break
            size = sum(u16(b["text"]) for b in members)
            if members and len(members) <= SECTION_BOX_LIMIT[0] and size <= SECTION_BOX_LIMIT[1]:
                final.append({"kind": "box", "style": style, "blocks": members, "title_block": head})
                i = j
                continue
        final.append(group)
        i += 1
    return final, labels


def box_title(group: dict) -> str:
    """The box's title: the source label moved into the bar, or '' (the template adds no words)."""
    return group["title_block"]["text"] if group.get("title_block") else ""


def box_entries(group: dict, labels: dict[str, str]) -> list[dict]:
    """Each paragraph of a box: its text (with a written number if any), bullet level, spans, scripts."""
    entries = []
    for block in group["blocks"]:
        parts = block.get("parts") if block["kind"] == "source_box" else [block["text"]]
        part_scripts = block.get("part_scripts") or [block.get("scripts") or []] * len(parts)
        for n, part in enumerate(parts):
            info = block.get("list")
            prefix, bullet = "", None
            if info:
                label = labels.get(block["source_id"]) or BULLET.strip()
                if label == BULLET.strip():
                    bullet = int(info.get("level") or 0)
                elif not part.startswith(label + " "):
                    prefix = label + " "
            one = len(parts) == 1
            entries.append({"text": prefix + part, "prefix": prefix, "bullet": bullet,
                            "spans": block.get("spans") if one else [],
                            "scripts": part_scripts[n] if n < len(part_scripts) else []})
    return entries


# Compatibility for callers that only need the grouping.
def groups(blocks: list[dict]) -> list[dict]:
    return plan(blocks)[0]


def expected(blocks: list[dict], lists: dict | None = None) -> list[tuple[str, str]]:
    """Every non-empty paragraph the Doc must show, in order: (text, what the writer added).
    The only addition is a list item's own source number or bullet, written as text."""
    found, labels = plan(blocks, lists)
    out: list[tuple[str, str]] = []
    for group in found:
        kind = group["kind"]
        if kind == "source_text":
            out += [(b["text"], "") for b in group["blocks"]]
        elif kind == "list":
            for b in group["blocks"]:
                prefix = _prefix(b, labels, b["text"]) if group["literal"] else ""
                out.append((prefix + b["text"], prefix))
        elif kind == "box":
            title = box_title(group)
            if title:
                out.append((title, ""))
            out += [(e["text"], e["prefix"]) for e in box_entries(group, labels)]
        elif kind == "source_table":
            block = group["blocks"][0]
            for row in [block["columns"], *block["rows"]]:
                for cell in row:
                    out += [(line, "") for line in cell.split("\n") if line.strip()]
    return [(t, prefix) for t, prefix in out if source.normal(t)]


def picture_count(blocks: list[dict]) -> int:
    return sum(1 for b in blocks if b["kind"] == "source_panel")


# --------------------------------------------------------------------------- one tab
def _text_block(block: dict, tab: str) -> list[dict]:
    role = block.get("role", "paragraph")
    text = block["text"]
    n = u16(text)
    above, below = SPACING.get(role, SPACING["paragraph"])
    align = "CENTER" if role in ("title", "kicker") else "START" if role != "paragraph" else "JUSTIFIED"
    colour = render.NAVY if role == "title" else render.GREY if role == "kicker" else render.BLACK
    heading = role in ("title", "section", "subsection")
    spans = [[s, e, {k: v for k, v in marks.items() if k in ("italic", "link")}] for s, e, marks in
             block.get("spans") or []] if heading or role == "kicker" else block.get("spans")
    return [{"insertText": {"location": _at(1, tab), "text": text + "\n"}},
            no_bullets(1, 1 + n + 1, tab),
            paragraph_style(1, 1 + n + 1, tab, named=NAMED.get(role, "NORMAL_TEXT"), align=align, above=above,
                            below=below, line=100 if role != "paragraph" else 115, keep=heading,
                            extra={"borderBottom": {**border(render.WHITE, 0), "padding": pt(0)}}
                            if role in ("title", "kicker") else None),
            text_style(1, 1 + n, tab, size=SIZE.get(role, SIZE["paragraph"]), bold=heading,
                       italic=role == "kicker", colour=colour),
            *span_styles(1, spans, tab, text, key_terms=not heading),
            *script_styles(1, block.get("scripts"), tab),
            *glyph_styles(1, text, tab)]


def _literal_list_block(group: dict, tab: str, labels: dict[str, str]) -> list[dict]:
    """A numbered list interrupted by other content: its source numbers as text, hanging like a list."""
    items = group["blocks"]
    lines = [_prefix(b, labels, b["text"]) + b["text"] for b in items]
    joined = "\n".join(lines) + "\n"
    total = u16(joined)
    requests = [{"insertText": {"location": _at(1, tab), "text": joined}}, no_bullets(1, 1 + total, tab)]
    cursor = 1
    for block, line in zip(items, lines):
        level = int(block["list"].get("level") or 0)
        end = cursor + u16(line) + 1
        requests.append(paragraph_style(cursor, end, tab, align="JUSTIFIED", above=1.5, below=1.5, indent=False,
                                        extra={"indentStart": pt(36 + 36 * level),
                                               "indentFirstLine": pt(18 + 36 * level)}))
        cursor = end
    requests.append(text_style(1, 1 + total - 1, tab, size=SIZE["paragraph"]))
    cursor = 1
    for block, line in zip(items, lines):
        at = cursor + u16(line) - u16(block["text"])
        requests += span_styles(at, block.get("spans"), tab, block["text"])
        requests += script_styles(at, block.get("scripts"), tab)
        requests += glyph_styles(at, block["text"], tab)
        cursor += u16(line) + 1
    return requests


def _list_block(group: dict, tab: str, labels: dict[str, str] | None = None) -> list[dict]:
    if group.get("literal"):
        return _literal_list_block(group, tab, labels or {})
    items = group["blocks"]
    lines = ["\t" * b["list"]["level"] + b["text"] for b in items]
    joined = "\n".join(lines) + "\n"
    total = u16(joined)
    requests = [{"insertText": {"location": _at(1, tab), "text": joined}},
                no_bullets(1, 1 + total, tab),
                paragraph_style(1, 1 + total, tab, align="JUSTIFIED", above=1.5, below=1.5, indent=False),
                text_style(1, 1 + total - 1, tab, size=SIZE["paragraph"])]
    cursor = 1
    for block, line in zip(items, lines):
        start = cursor + block["list"]["level"]
        requests += span_styles(start, block.get("spans"), tab, block["text"])
        requests += script_styles(start, block.get("scripts"), tab)
        requests += glyph_styles(start, block["text"], tab)
        cursor += u16(line) + 1
    preset = "NUMBERED_DECIMAL_ALPHA_ROMAN" if items[0]["list"].get("numbered") else "BULLET_DISC_CIRCLE_SQUARE"
    # Last: creating the bullets removes the nesting tabs, after every range above is applied.
    requests.append({"createParagraphBullets": {"range": _range(1, 1 + total, tab), "bulletPreset": preset}})
    return requests


def _image_block(block: dict, tab: str, link: str) -> list[dict]:
    width = min(TEXT_WIDTH_PT, float(block["width_pt"]))
    height = width * float(block["height_pt"]) / float(block["width_pt"])
    if height > MAX_IMAGE_HEIGHT:
        width, height = width * MAX_IMAGE_HEIGHT / height, MAX_IMAGE_HEIGHT
    return [{"insertText": {"location": _at(1, tab), "text": "\n"}},
            {"insertInlineImage": {"location": _at(1, tab), "uri": link,
                                   "objectSize": {"width": pt(width), "height": pt(height)}}},
            no_bullets(1, 3, tab),
            paragraph_style(1, 3, tab, align="CENTER", above=6, below=6, line=100)]


def cell_index(table_start: int, row: int, column: int, columns: int) -> int:
    """Where an empty new cell's paragraph starts: the table, then per row its
    start plus (cell start + one empty paragraph) per column."""
    return table_start + 3 + row * (2 * columns + 1) + 2 * column


def _table_frame(tab: str, rows: int, columns: int) -> list[dict]:
    """A table at index 1, inside a fresh, plain, tiny paragraph: Google indents a new table like
    the paragraph it goes into (a following list item would push a box off the margin). Google adds
    an empty paragraph before the table (kept tiny by _spacer); the fresh one stays after it."""
    return [{"insertText": {"location": _at(1, tab), "text": "\n"}},
            no_bullets(1, 2, tab),
            paragraph_style(1, 2, tab, align="START", above=0, below=0, line=100),
            text_style(1, 2, tab, size=4),
            {"insertTable": {"location": _at(1, tab), "rows": rows, "columns": columns}}]


def _spacer(tab: str) -> list[dict]:
    return [no_bullets(1, 2, tab),
            paragraph_style(1, 2, tab, align="START", above=0, below=0, line=100),
            text_style(1, 2, tab, size=4)]


CELL_FIELDS = ("backgroundColor,borderTop,borderBottom,borderLeft,borderRight,"
               "paddingTop,paddingBottom,paddingLeft,paddingRight,contentAlignment")


def _cells(tab: str, table_start: int, row: int, rows: int, columns: int, style: dict, segment: str = "") -> dict:
    """Cell style for whole rows. A field left out of `style` (e.g. backgroundColor) is reset: no fill."""
    base = {"paddingTop": pt(3), "paddingBottom": pt(3), "paddingLeft": pt(5), "paddingRight": pt(5),
            "contentAlignment": "TOP"}
    base.update(style)
    return {"updateTableCellStyle": {"tableRange": {"tableCellLocation": {
        "tableStartLocation": _at(table_start, tab, segment), "rowIndex": row, "columnIndex": 0},
        "rowSpan": rows, "columnSpan": columns}, "tableCellStyle": base, "fields": CELL_FIELDS}}


def _table_block(block: dict, tab: str) -> list[dict]:
    grid = [block["columns"], *block["rows"]]
    scripts = block.get("cell_scripts") or [[[] for _ in row] for row in grid]
    rows, columns = len(grid), len(block["columns"])
    centred = [c > 0 and all(u16(row[c]) <= CENTRE_CELL for row in grid) for c in range(columns)]
    start = 2
    requests = _table_frame(tab, rows, columns)
    for r in reversed(range(rows)):
        for c in reversed(range(columns)):
            text = grid[r][c]
            at = cell_index(start, r, c, columns)
            if text:
                head, first = r == 0, c == 0
                requests.append({"insertText": {"location": _at(at, tab), "text": text}})
                requests.append(paragraph_style(at, at + u16(text) + 1, tab, align="CENTER" if centred[c] else "START",
                                                above=1, below=1, line=100))
                requests.append(text_style(at, at + u16(text), tab, size=SIZE["table"], bold=head or first,
                                           colour=render.NAVY if first else render.BLACK))
                requests += script_styles(at, scripts[r][c] if r < len(scripts) and c < len(scripts[r]) else [], tab)
                requests += glyph_styles(at, text, tab)
    hidden = border(render.WHITE, 0)
    rules = {"borderLeft": hidden, "borderRight": hidden}
    requests.append(_cells(tab, start, 0, rows, columns, {**rules, "borderTop": hidden, "borderBottom": hidden}))
    requests.append(_cells(tab, start, 0, 1, columns, {**rules, "borderTop": border(render.BLACK, 0.9),
                                                       "borderBottom": border(render.BLACK, 0.6),
                                                       "backgroundColor": rgb(render.HEAD_ROW)}))
    if rows > 1:
        requests.append(_cells(tab, start, rows - 1, 1, columns, {**rules, "borderTop": hidden,
                                                                  "borderBottom": border(render.BLACK, 0.9)}))
    for r, c, rs, cs in block.get("merges") or []:
        if r + rs <= rows and c + cs <= columns:
            requests.append({"mergeTableCells": {"tableRange": {"tableCellLocation": {
                "tableStartLocation": _at(start, tab), "rowIndex": r, "columnIndex": c},
                "rowSpan": rs, "columnSpan": cs}}})
    if rows > 2:
        requests.append({"pinTableHeaderRows": {"tableStartLocation": _at(start, tab), "pinnedHeaderRowsCount": 1}})
    return requests + _spacer(tab)


BOX_PAD = 9.0


def _box_block(group: dict, tab: str, labels: dict[str, str] | None = None) -> list[dict]:
    """The B.Sc. box: a frame and a tinted body in one cell.

    When the source gives the box its label ("Definition:", "Important Points"), that label is the
    cell's first paragraph, shaded in the bar colour and kept with the text below it, so a page
    never ends on a lone title (checked on scratch Docs; a two-row box could). Without a source
    label there is no bar: the template adds no words."""
    frame, bar, ink, body, _indent = render.BOXES.get(group["style"], render.BOXES["note"])
    entries = box_entries(group, labels or {})
    title = box_title(group)
    title_scripts = (group.get("title_block") or {}).get("scripts") or []
    start, title_at = 2, cell_index(2, 0, 0, 1)
    body_at = title_at + (u16(title) + 1 if title else 0)
    requests = _table_frame(tab, 1, 1)
    if title:
        requests.append({"insertText": {"location": _at(title_at, tab), "text": title + "\n"}})
    lines = [("\t" * e["bullet"] if e["bullet"] is not None else "") + e["text"] for e in entries]
    text = "\n".join(lines)
    if text:
        requests.append({"insertText": {"location": _at(body_at, tab), "text": text}})
        cursor, places = body_at, []
        for n, (entry, line) in enumerate(zip(entries, lines)):
            end = cursor + u16(line) + 1
            extra = {"indentEnd": pt(BOX_PAD)}
            if entry["bullet"] is None:
                extra.update(indentStart=pt(BOX_PAD), indentFirstLine=pt(BOX_PAD))
            requests.append(paragraph_style(cursor, end, tab, align="JUSTIFIED", above=4 if n == 0 else 1.5,
                                            below=6 if n == len(entries) - 1 else 1.5, indent=False, extra=extra))
            places.append((cursor, end, entry, line))
            cursor = end
        requests.append(text_style(body_at, body_at + u16(text), tab, size=SIZE["paragraph"]))
        for begin, _end, entry, line in places:
            at = begin + (u16(line) - u16(entry["text"])) + u16(entry["prefix"])
            plain = entry["text"][len(entry["prefix"]):]
            requests += span_styles(at, entry["spans"], tab, plain)
            requests += script_styles(at, entry["scripts"], tab)
            requests += glyph_styles(at, plain, tab)
        runs, current = [], []
        for begin, end, entry, _line in places:
            if entry["bullet"] is not None:
                current.append((begin, end))
            elif current:
                runs.append(current)
                current = []
        if current:
            runs.append(current)
        for run in reversed(runs):          # last run first: removing nesting tabs never moves an earlier run
            requests.append({"createParagraphBullets": {"range": _range(run[0][0], run[-1][1], tab),
                                                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
    if title:
        # The bar: the title paragraph, shaded. Google shades only between the indents, so side borders in
        # the bar colour (with padding) carry the shade out to the frame while the text stays inset.
        side = {**border(bar, 0.75), "padding": pt(BOX_PAD)}
        requests += [paragraph_style(title_at, title_at + u16(title) + 1, tab, align="START", above=0, below=0,
                                     line=100, keep=True, indent=False,
                                     extra={"indentStart": pt(BOX_PAD), "indentFirstLine": pt(BOX_PAD),
                                            "indentEnd": pt(BOX_PAD), "shading": {"backgroundColor": rgb(bar)},
                                            "borderTop": {**border(bar, 3), "padding": pt(0)},
                                            "borderBottom": {**border(bar, 3), "padding": pt(0)},
                                            "borderLeft": side, "borderRight": side}),
                     text_style(title_at, title_at + u16(title), tab, size=SIZE["box_title"], bold=True, colour=ink),
                     *script_styles(title_at, title_scripts, tab),
                     *glyph_styles(title_at, title, tab)]
    edge = border(frame, 0.75)
    requests.append(_cells(tab, start, 0, 1, 1, {"backgroundColor": rgb(body), "borderTop": edge, "borderLeft": edge,
                                                 "borderRight": edge, "borderBottom": edge, "paddingTop": pt(0),
                                                 "paddingBottom": pt(0), "paddingLeft": pt(0), "paddingRight": pt(0)}))
    return requests + _spacer(tab)


def tab_requests(blocks: list[dict], tab: str, picture_link: Callable[[dict], str],
                 lists: dict | None = None) -> list[dict]:
    """Every request that builds one tab's body, to be sent in this order.
    `lists` is the source tab's {list id: nesting levels}, for its own numbering."""
    found, labels = plan(blocks, lists)
    requests: list[dict] = []
    for group in reversed(found):
        kind = group["kind"]
        if kind == "source_text":
            requests += _text_block(group["blocks"][0], tab)
        elif kind == "list":
            requests += _list_block(group, tab, labels)
        elif kind == "box":
            requests += _box_block(group, tab, labels)
        elif kind == "source_table":
            requests += _table_block(group["blocks"][0], tab)
        elif kind == "source_panel":
            requests += _image_block(group["blocks"][0], tab, picture_link(group["blocks"][0]))
        else:
            raise source.SourceError(f"Unknown block kind {kind!r}; nothing was written.")
    return requests


def page_requests(tab: str) -> list[dict]:
    """A4, the B.Sc. margins and its faint blue page."""
    return [{"updateDocumentStyle": {"tabId": tab, "fields": "pageSize,marginLeft,marginRight,marginTop,"
                                     "marginBottom,marginHeader,marginFooter,background", "documentStyle": {
        "pageSize": {"width": pt(PAGE["width"]), "height": pt(PAGE["height"])},
        "marginLeft": pt(PAGE["left"]), "marginRight": pt(PAGE["right"]), "marginTop": pt(PAGE["top"]),
        "marginBottom": pt(PAGE["bottom"]), "marginHeader": pt(PAGE["header"]), "marginFooter": pt(PAGE["footer"]),
        "background": {"color": rgb(render.BG)}}}}]


# --------------------------------------------------------------------------- the document
def _execute(call, pause: float = 8.0, tries: int = 12):
    """Google's per-minute read/write limits (429) and brief outages (5xx) wait and retry (up to about
    ten minutes, for several notes made at once). A batchUpdate applies all its requests or none, so a
    retry never doubles content."""
    import time

    for attempt in range(tries):
        try:
            return call.execute()
        except Exception as exc:                       # noqa: BLE001 - only rate limits and outages retry
            status = getattr(getattr(exc, "resp", None), "status", 0)
            if status not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
            time.sleep(min(90.0, pause * (attempt + 1)))


def _get(docs, document_id: str, **fields):
    """documents.get with the same waiting on Google's per-minute limits."""
    return _execute(docs.documents().get(documentId=document_id, **fields))


def batch(docs, document_id: str, requests: list[dict]) -> list[dict]:
    replies: list[dict] = []
    for i in range(0, len(requests), CHUNK):
        answer = _execute(docs.documents().batchUpdate(documentId=document_id,
                                                       body={"requests": requests[i:i + CHUNK]}))
        replies += answer.get("replies") or []
    return replies


def create(docs, title: str, tabs: list[dict]) -> tuple[str, list[str]]:
    """A new Doc with one tab per entry ({title, depth}), in order: (document id, tab ids)."""
    made = docs.documents().create(body={"title": title}).execute()
    document_id = made["documentId"]
    first = docs.documents().get(documentId=document_id, includeTabsContent=True,
                                 fields="tabs(tabProperties)").execute()["tabs"][0]["tabProperties"]["tabId"]
    batch(docs, document_id, [{"updateDocumentTabProperties": {"tabProperties": {"tabId": first, "title": tabs[0]["title"]},
                                                              "fields": "title"}}])
    ids = [first]
    parents: list[str] = [first]
    for position, tab in enumerate(tabs[1:], start=1):
        props = {"title": tab["title"]}
        depth = int(tab.get("depth") or 0)
        if depth and len(parents) >= depth:
            props["parentTabId"] = parents[depth - 1]
        else:
            props["index"] = position
        reply = batch(docs, document_id, [{"addDocumentTab": {"tabProperties": props}}])[0]
        new_id = reply["addDocumentTab"]["tabProperties"]["tabId"]
        ids.append(new_id)
        parents = parents[:depth] + [new_id]
    return document_id, ids


# --------------------------------------------------------------------------- PW watermark (template)
# Google's Docs API cannot add a watermark or copy a tab (checked against the live API description,
# revision 20261109). A Word file whose header holds the faded PW logo does convert into a native
# Docs watermark on its one tab, and that watermark survives page setup, our header bar, content,
# copying the Doc and deleting other tabs (checked on scratch Docs). So every note is a copy of a
# template whose tabs all carry it: the template's first tab comes from that Word file and the
# person duplicates it once in Google Docs (tab menu > Duplicate), which the API cannot do.
TEMPLATE_MIME = "application/vnd.google-apps.document"


def _hex(colour) -> str:
    return "%02X%02X%02X" % tuple(colour)


def _word_bar(part, cells: list[tuple[str, dict, str]], *, rule: bool, logo: bytes | None = None) -> None:
    """A borderless one-row Word table across the text width (a header or footer bar). Each cell:
    (text, style, alignment); style keys size, bold, italic, colour; text "{PAGE}" is a page number.
    `logo` goes after the last cell's text. The bar's bottom rule is navy when `rule`."""
    import io

    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls
    from docx.shared import Pt, RGBColor

    table = part.add_table(rows=1, cols=len(cells), width=Pt(render.TEXT_W))
    bottom = f'<w:bottom w:val="single" w:sz="5" w:space="0" w:color="{_hex(render.NAVY)}"/>' if rule \
        else '<w:bottom w:val="nil"/>'
    table._tbl.tblPr.append(parse_xml(
        f'<w:tblBorders {nsdecls("w")}><w:top w:val="nil"/><w:left w:val="nil"/>{bottom}<w:right w:val="nil"/>'
        '<w:insideH w:val="nil"/><w:insideV w:val="nil"/></w:tblBorders>'))
    table._tbl.tblPr.append(parse_xml(
        f'<w:tblCellMar {nsdecls("w")}><w:left w:w="0" w:type="dxa"/><w:right w:w="0" w:type="dxa"/></w:tblCellMar>'))
    aligns = {"START": WD_ALIGN_PARAGRAPH.LEFT, "CENTER": WD_ALIGN_PARAGRAPH.CENTER, "END": WD_ALIGN_PARAGRAPH.RIGHT}
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT

    for n, (text, style, align) in enumerate(cells):
        table.rows[0].cells[n].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.BOTTOM
        paragraph = table.rows[0].cells[n].paragraphs[0]
        paragraph.alignment = aligns[align]
        paragraph.paragraph_format.space_after = Pt(1)

        def styled(run):
            run.font.name = FONT
            run.font.size = Pt(style.get("size", 9))
            run.bold = style.get("bold", False)
            run.italic = style.get("italic", False)
            run.font.color.rgb = RGBColor(*style.get("colour", render.BLACK))
            return run
        if text == "{PAGE}":
            look = (f'<w:rPr><w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}"/>'
                    f'<w:color w:val="{_hex(style.get("colour", render.BLACK))}"/>'
                    f'<w:sz w:val="{int(style.get("size", 9) * 2)}"/></w:rPr>')
            paragraph._p.append(parse_xml(f'<w:fldSimple {nsdecls("w")} w:instr="PAGE"><w:r>{look}'
                                          '<w:t>1</w:t></w:r></w:fldSimple>'))
        else:
            styled(paragraph.add_run(text))
        if logo is not None and n == len(cells) - 1:
            paragraph.add_run("  ")
            paragraph.add_run().add_picture(io.BytesIO(logo), height=Pt(20))


def watermark_word_file(branding: render.Branding | None = None) -> bytes:
    """An empty A4 Word file with the NEET/JEE PW watermark: the hand-drawn engine's own faded logo
    (its watermark strength setting), 124 mm and centred, as the NEET/JEE PDFs draw it.

    With `branding`, the header and footer bars are built into the file too: header "left | right"
    with the PW logo and the navy rule, footer "left | page number | right". The Docs API cannot put
    a picture in a header without dropping the tab's watermark, but a converted Word header keeps both."""
    import io

    from .. import engine_catalog

    engine_catalog.put_shortnotes_on_path()
    from docx import Document
    from docx.oxml import parse_xml
    from docx.shared import Pt
    from sheets_bridge import notes_doc, pdf_branding

    word = Document()
    section = word.sections[0]
    section.page_width, section.page_height = Pt(render.PAGE_W), Pt(render.PAGE_H)
    section.left_margin, section.right_margin = Pt(PAGE["left"]), Pt(PAGE["right"])
    section.header_distance, section.footer_distance = Pt(PAGE["header"]), Pt(PAGE["footer"])
    header = section.header
    if branding is not None:
        header._element.remove(header.paragraphs[0]._p)
        _word_bar(header, [(branding.header_left, {"size": SIZE["header"], "bold": True, "colour": render.NAVY}, "START"),
                           (branding.header_right, {"size": SIZE["header"], "italic": True, "colour": render.GREY}, "END")],
                  rule=True, logo=render.pw_logo_path().read_bytes() if branding.pw_logo else None)
        footer = section.footer
        footer._element.remove(footer.paragraphs[0]._p)
        _word_bar(footer, [(branding.footer_left, {"size": SIZE["footer"], "colour": render.GREY}, "START"),
                           ("{PAGE}", {"size": SIZE["footer"], "colour": render.GREY}, "CENTER"),
                           (branding.footer_right, {"size": SIZE["footer"], "italic": True, "colour": render.GREY}, "END")],
                  rule=False)
        footer.add_paragraph().paragraph_format.space_after = Pt(0)
    rel_id, _ = header.part.get_or_add_image(notes_doc.faded_logo(render.pw_logo_path(),
                                                                  pdf_branding.watermark_opacity()))
    header._element.append(parse_xml(notes_doc._watermark_xml(rel_id, render.WATERMARK, render.WATERMARK)))
    buffer = io.BytesIO()
    word.save(buffer)
    return buffer.getvalue()


def make_template(drive, docs, name: str, branding: render.Branding | None = None) -> str:
    """A new template Doc: one tab with the PW watermark and the B.Sc. page (and, with `branding`,
    its header and footer bars and PW logo). Returns its id."""
    import io

    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(io.BytesIO(watermark_word_file(branding)), resumable=False,
                              mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    made = drive.files().create(body={"name": name, "mimeType": TEMPLATE_MIME}, media_body=media, fields="id",
                                supportsAllDrives=True).execute()
    tab = docs.documents().get(documentId=made["id"], includeTabsContent=True,
                               fields="tabs(tabProperties)").execute()["tabs"][0]["tabProperties"]["tabId"]
    batch(docs, made["id"], page_requests(tab))
    return made["id"]


def template_tab_ids(docs, template_id: str) -> list[str]:
    from .gdoc_source import all_tabs

    document = _get(docs, template_id, includeTabsContent=True,
                    fields="tabs(tabProperties,childTabs(tabProperties))")
    return [t["tab_id"] for t in all_tabs(document)]


def has_watermark(pdf: bytes) -> bool:
    """Every page of an exported tab shows the watermark: one large picture, centred across the
    page, at the same place on every page. (A page's own big pictures do not count: they differ
    from page to page; a size test alone passed a UNIT-1 tab that had no watermark.)"""
    import pymupdf

    with pymupdf.open(stream=pdf, filetype="pdf") as found:
        if found.page_count == 0:
            return False
        common = None
        for page in found:
            width = page.rect.width
            places = {tuple(round(v) for v in info["bbox"]) for info in page.get_image_info()
                      if (info["bbox"][2] - info["bbox"][0]) > 0.4 * width
                      and abs((info["bbox"][0] + info["bbox"][2]) / 2 - width / 2) < 0.03 * width}
            common = places if common is None else common & places
            if not common:
                return False
        return True


def has_header_logo(pdf: bytes) -> bool:
    """Every page shows the small PW logo in the header's right-hand corner."""
    import pymupdf

    with pymupdf.open(stream=pdf, filetype="pdf") as found:
        return found.page_count > 0 and all(
            any((i["bbox"][2] - i["bbox"][0]) < 40 and i["bbox"][3] < 70 and i["bbox"][0] > page.rect.width * 0.8
                for i in page.get_image_info())
            for page in found)


def _last_page_body(pdf: bytes, index: int = -1) -> tuple[list[tuple], float] | None:
    """The boxes of the words and pictures on an exported tab's last page (or page `index`) that are not
    what every page shows (header and footer words, the watermark and the logo, at the same places as on
    page 1, and page numbers), with the page height. None for a one-page tab."""
    import pymupdf

    def marks(page):
        words = {(w[4], round(w[0]), round(w[1])): tuple(w[:4]) for w in page.get_text("words")
                 if not w[4].isdigit()}
        pictures = {tuple(round(v) for v in i["bbox"]): tuple(i["bbox"]) for i in page.get_image_info()}
        return words, pictures
    with pymupdf.open(stream=pdf, filetype="pdf") as found:
        if found.page_count < 2:
            return None
        first_words, first_pictures = marks(found[0])
        last_words, last_pictures = marks(found[index])
        body = [box for key, box in last_words.items() if key not in first_words]
        body += [box for key, box in last_pictures.items() if key not in first_pictures]
        return body, found[index].rect.height


def ends_on_blank_page(pdf: bytes) -> bool:
    """The exported tab's last page shows nothing but what every page shows."""
    found = _last_page_body(pdf)
    return found is not None and not found[0]


def last_page_nearly_empty(pdf: bytes, share: float = 0.05) -> bool:
    """The last page holds only a sliver of content: a table's last row, a box's last line or two."""
    found = _last_page_body(pdf)
    if not found or not found[0]:
        return False
    body, height = found
    return max(b[3] for b in body) - min(b[1] for b in body) < share * height


def page_fill(pdf: bytes, index: int) -> float:
    """How far down page `index` its own content reaches, as a share of the page height."""
    found = _last_page_body(pdf, index)
    if not found or not found[0]:
        return 0.0
    return max(b[3] for b in found[0]) / found[1]


def page_count(pdf: bytes) -> int:
    import pymupdf

    with pymupdf.open(stream=pdf, filetype="pdf") as found:
        return found.page_count


def create_from_template(drive, docs, template_id: str, title: str, tabs: list[dict]) -> tuple[str, list[str]]:
    """A copy of the watermark template with exactly one tab per entry, renamed, in order.
    Child tabs are written as top-level tabs (the template's tabs are flat)."""
    available = template_tab_ids(docs, template_id)
    if len(available) < len(tabs):
        raise source.SourceError(
            f"The Google Doc template has {len(available)} tabs with the PW watermark but this note needs "
            f"{len(tabs)}. Open the template and duplicate its first tab {len(tabs) - len(available)} more "
            "times, then try again.")
    made = drive.files().copy(fileId=template_id, body={"name": title}, fields="id",
                              supportsAllDrives=True).execute()
    document_id = made["id"]
    ids = template_tab_ids(docs, document_id)
    used, extra = ids[:len(tabs)], ids[len(tabs):]
    requests = [{"deleteTab": {"tabId": tab}} for tab in reversed(extra)]
    requests += [{"updateDocumentTabProperties": {"tabProperties": {"tabId": tab, "title": entry["title"]},
                                                  "fields": "title"}} for tab, entry in zip(used, tabs)]
    batch(docs, document_id, requests)
    return document_id, used


def _bar(tab: str, segment: str, left: str, right: str, *, left_style: dict, right_style: dict, rule: bool,
         logo_link: str = "", logo_ratio: float = 1.0) -> list[dict]:
    """A header/footer bar: a borderless 1x2 table, left label and right label (and logo)."""
    start = 1                                   # the table goes in at 0; Google puts a newline before it
    left_at, right_at = cell_index(start, 0, 0, 2), cell_index(start, 0, 1, 2)
    requests = [{"insertTable": {"location": _at(0, tab, segment), "rows": 1, "columns": 2}},
                {"insertText": {"location": _at(right_at, tab, segment), "text": right}},
                paragraph_style(right_at, right_at + u16(right) + 1, tab, align="END", above=0, below=0, line=100,
                                segment=segment),
                text_style(right_at, right_at + u16(right), tab, segment=segment, **right_style)]
    if logo_link:
        requests += [{"insertText": {"location": _at(right_at + u16(right), tab, segment), "text": "  "}},
                     {"insertInlineImage": {"location": _at(right_at + u16(right) + 2, tab, segment), "uri": logo_link,
                                            "objectSize": {"height": pt(20), "width": pt(20 * logo_ratio)}}}]
    requests += [{"insertText": {"location": _at(left_at, tab, segment), "text": left}},
                 paragraph_style(left_at, left_at + u16(left) + 1, tab, align="START", above=0, below=0, line=100,
                                 segment=segment),
                 text_style(left_at, left_at + u16(left), tab, segment=segment, **left_style)]
    hidden = border(render.WHITE, 0)
    requests.append(_cells(tab, start, 0, 1, 2, {"borderTop": hidden, "borderLeft": hidden, "borderRight": hidden,
                                                 "borderBottom": border(render.NAVY, 0.6) if rule else hidden,
                                                 "paddingTop": pt(0), "paddingBottom": pt(2), "paddingLeft": pt(0),
                                                 "paddingRight": pt(0), "contentAlignment": "BOTTOM"}, segment))
    return requests


def _shrink_empty(docs, document_id: str, tab: str, segment: str, kind: str) -> None:
    """The empty paragraphs around a header/footer bar take no room."""
    from .gdoc_source import all_tabs

    document = _get(docs, document_id, includeTabsContent=True)
    found = next(t for t in all_tabs(document) if t["tab_id"] == tab)
    content = ((found["document_tab"].get(kind) or {}).get(segment) or {}).get("content") or []
    requests = []
    for element in content:
        paragraph = element.get("paragraph")
        if paragraph is None:
            continue
        text = "".join((e.get("textRun") or {}).get("content", "") for e in paragraph.get("elements") or [])
        if not text.strip("\n"):
            start, end = element.get("startIndex", 0), element["endIndex"]      # Google omits a 0 start
            requests += [paragraph_style(start, end, tab, align="START", above=0, below=0, line=100, segment=segment),
                         text_style(start, end, tab, size=2, segment=segment)]
    if requests:
        batch(docs, document_id, requests)


def header_footer(docs, document_id: str, tab: str, branding: render.Branding, logo_link: str = "",
                  logo_ratio: float = 1.0, watermarked: bool = False) -> None:
    """The B.Sc. header (left label, right label and PW logo, navy rule) and footer, on one tab.

    A watermarked (template) tab, as seen on scratch copies of the template:
    - its header and footer are emptied and reused, never deleted (the watermark goes with the header);
    - the header is written BEFORE a footer is created: a footer created while the header is
      empty drops the watermark;
    - the PW logo goes into the bar's right cell in its OWN step, after the bar exists: inserted in
      the same step as the bar it drops the watermark; afterwards it keeps it (checked on the
      template's original and duplicated tabs: watermark and corner logo on every page)."""
    header = _segment(docs, document_id, tab, "Header")
    batch(docs, document_id, _bar(
        tab, header, branding.header_left, branding.header_right, rule=True,
        left_style={"size": SIZE["header"], "bold": True, "colour": render.NAVY},
        right_style={"size": SIZE["header"], "italic": True, "colour": render.GREY}))
    if logo_link:
        _logo_into_bar(docs, document_id, tab, header, logo_link, logo_ratio)
    footer = _segment(docs, document_id, tab, "Footer")
    batch(docs, document_id, _bar(
        tab, footer, branding.footer_left, branding.footer_right, rule=False,
        left_style={"size": SIZE["footer"], "colour": render.GREY},
        right_style={"size": SIZE["footer"], "italic": True, "colour": render.GREY}))
    _shrink_empty(docs, document_id, tab, header, "headers")
    _shrink_empty(docs, document_id, tab, footer, "footers")


def _logo_into_bar(docs, document_id: str, tab: str, header: str, logo_link: str, logo_ratio: float) -> None:
    """The PW logo at the end of the header bar's right cell (its own step; see header_footer)."""
    from .gdoc_source import all_tabs

    document = _get(docs, document_id, includeTabsContent=True)
    found = next(t for t in all_tabs(document) if t["tab_id"] == tab)
    content = ((found["document_tab"].get("headers") or {}).get(header) or {}).get("content") or []
    table = next(e for e in content if "table" in e)
    cell = table["table"]["tableRows"][0]["tableCells"][-1]["content"][-1]
    at = cell["endIndex"] - 1
    batch(docs, document_id, [{"insertText": {"location": _at(at, tab, header), "text": "  "}}])
    batch(docs, document_id, [{"insertInlineImage": {"location": _at(at + 2, tab, header), "uri": logo_link,
                                                     "objectSize": {"height": pt(20), "width": pt(20 * logo_ratio)}}}])


def _segment(docs, document_id: str, tab: str, kind: str) -> str:
    """The tab's default header or footer ("Header"/"Footer") id, without an earlier bar; made when missing."""
    from .gdoc_source import all_tabs

    key, plural = f"default{kind}Id", f"{kind.lower()}s"
    document = _get(docs, document_id, includeTabsContent=True)
    found = next(t for t in all_tabs(document) if t["tab_id"] == tab)
    segment = (found["document_tab"].get("documentStyle") or {}).get(key)
    if segment:
        # Only an earlier bar (a table) is removed. Its paragraphs stay: a template tab's watermark is
        # anchored in its header's first paragraph, and deleting that paragraph drops the watermark
        # (seen on the user's duplicated template tabs, whose headers hold two empty paragraphs).
        content = ((found["document_tab"].get(plural) or {}).get(segment) or {}).get("content") or []
        tables = [e for e in content if "table" in e]
        for element in reversed(tables):
            batch(docs, document_id, [{"deleteContentRange": {"range": _range(element["startIndex"], element["endIndex"],
                                                                              tab, segment)}}])
        return segment
    reply = batch(docs, document_id, [{f"create{kind}": {"type": "DEFAULT", "sectionBreakLocation": _at(0, tab)}}])
    return reply[0][f"create{kind}"][f"{kind.lower()}Id"]


def remove_header_footer(docs, document_id: str, tab: str) -> None:
    """Delete a tab's header and footer (to make them again). Not for a watermarked tab: its
    watermark goes with its header (header_footer empties and reuses them instead)."""
    from .gdoc_source import all_tabs

    document = _get(docs, document_id, includeTabsContent=True)
    found = next(t for t in all_tabs(document) if t["tab_id"] == tab)
    style = found["document_tab"].get("documentStyle") or {}
    requests = []
    if style.get("defaultHeaderId"):
        requests.append({"deleteHeader": {"headerId": style["defaultHeaderId"], "tabId": tab}})
    if style.get("defaultFooterId"):
        requests.append({"deleteFooter": {"footerId": style["defaultFooterId"], "tabId": tab}})
    if requests:
        batch(docs, document_id, requests)


def local_picture_links(drive, docs, pictures: list[bytes], name: str = "NoteCraft pictures (temporary)"):
    """Google inserts pictures only from a link it can fetch. Local pictures (the PW
    logo, a Word or PDF source's images) go into a temporary converted Doc, whose
    own short-lived picture links are read back, so nothing is shared publicly.
    Returns (links in order, the temporary Doc's id, to be put in the bin after use)."""
    import io

    from docx import Document
    from docx.shared import Pt
    from googleapiclient.http import MediaIoBaseUpload

    word = Document()
    for data in pictures:
        word.add_paragraph().add_run().add_picture(io.BytesIO(data), height=Pt(40))
    buffer = io.BytesIO()
    word.save(buffer)
    buffer.seek(0)
    media = MediaIoBaseUpload(buffer, mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                              resumable=False)
    made = drive.files().create(body={"name": name, "mimeType": "application/vnd.google-apps.document"},
                                media_body=media, fields="id", supportsAllDrives=True).execute()
    temp_id = made["id"]
    document = _get(docs, temp_id)
    objects = document.get("inlineObjects") or {}
    links = []
    for element in (document.get("body") or {}).get("content") or []:
        for part in (element.get("paragraph") or {}).get("elements") or []:
            object_id = (part.get("inlineObjectElement") or {}).get("inlineObjectId")
            if object_id:
                links.append(((((objects.get(object_id) or {}).get("inlineObjectProperties") or {})
                               .get("embeddedObject") or {}).get("imageProperties") or {}).get("contentUri", ""))
    if len(links) != len(pictures) or not all(links):
        raise source.SourceError("Google did not keep every picture of the temporary upload; nothing was written.")
    return links, temp_id


# --------------------------------------------------------------------------- read back
def _paragraph_text(paragraph: dict) -> tuple[str, int]:
    text, pictures = "", 0
    for element in paragraph.get("elements") or []:
        if "textRun" in element:
            text += element["textRun"].get("content") or ""
        elif "inlineObjectElement" in element:
            pictures += 1
    return text.replace("\n", ""), pictures


def read_back(document: dict) -> dict[str, dict]:
    """{tab id: {"title", "paragraphs": [...], "pictures": n, "tables": n}} of a written Doc."""
    from .gdoc_source import all_tabs

    found = {}
    for tab in all_tabs(document):
        paragraphs: list[str] = []
        counts = {"pictures": 0, "tables": 0}

        def walk(content):
            for element in content or []:
                if "paragraph" in element:
                    text, pictures = _paragraph_text(element["paragraph"])
                    counts["pictures"] += pictures
                    if source.normal(text):
                        paragraphs.append(text)
                elif "table" in element:
                    counts["tables"] += 1
                    for row in element["table"].get("tableRows") or []:
                        for cell in row.get("tableCells") or []:
                            walk(cell.get("content"))

        walk(((tab["document_tab"].get("body") or {}).get("content")))
        found[tab["tab_id"]] = {"title": tab["title"], "paragraphs": paragraphs, **counts}
    return found


def _empty_paragraph(element: dict) -> bool:
    paragraph = element.get("paragraph")
    if paragraph is None:
        return False
    elements = paragraph.get("elements") or []
    text = "".join((e.get("textRun") or {}).get("content", "") for e in elements)
    return not text.strip("\n") and not any("inlineObjectElement" in e for e in elements)


def end_requests(content: list[dict], tab: str) -> list[dict]:
    """Google keeps one empty paragraph at the end of every tab; on a page filled to the footer even a
    1 pt one starts a blank last page. After text, our last paragraph is joined with it (Google keeps
    that paragraph's own style, bullet and spacing), so nothing is left over. After a table a paragraph
    must stay: the extra empty ones are removed and the last is made 1 pt with no spacing."""
    trailing = []
    for element in reversed(content):
        if not _empty_paragraph(element) or "sectionBreak" in element:
            break
        trailing.insert(0, element)
    if not trailing:
        return []
    first, last = trailing[0], trailing[-1]
    start = first.get("startIndex", 0)
    before = content[len(content) - len(trailing) - 1] if len(content) > len(trailing) else {}
    if "paragraph" in before:
        final = last.get("startIndex", 0)
        return [text_style(final, last["endIndex"], tab, size=1),
                {"deleteContentRange": {"range": _range(before["endIndex"] - 1, final, tab)}}]
    requests = []
    if len(trailing) > 1:
        requests.append({"deleteContentRange": {"range": _range(start, last.get("startIndex", 0), tab)}})
    end = start + (last["endIndex"] - last.get("startIndex", 0))
    return requests + [paragraph_style(start, end, tab, align="START", above=0, below=0, line=100),
                       text_style(start, end, tab, size=1)]


def full_page_requests(content: list[dict], tab: str) -> list[dict]:
    """For a tab whose last table fills its page to the footer, so Google's required paragraph after
    it starts a blank page (seen in a PDF export): the spacing just above that table is reduced so
    the paragraph fits. Of a doubled spacer the first is removed (Google refuses to delete the one
    touching the table); a single spacer is made 1 pt. [] when the tab does not end in a table."""
    trailing = 0
    for element in reversed(content):
        if not _empty_paragraph(element) or "sectionBreak" in element:
            break
        trailing += 1
    at = len(content) - trailing - 1
    if trailing == 0 or at < 2 or "table" not in content[at]:
        return []
    spacer, earlier = content[at - 1], content[at - 2]
    if not _empty_paragraph(spacer):
        return []
    if _empty_paragraph(earlier) and "sectionBreak" not in earlier:
        return [{"deleteContentRange": {"range": _range(earlier.get("startIndex", 0), earlier["endIndex"], tab)}}]
    start, end = spacer.get("startIndex", 0), spacer["endIndex"]
    return [paragraph_style(start, end, tab, align="START", above=0, below=0, line=100),
            text_style(start, end, tab, size=1)]


def tidy_end(docs, document_id: str, tab: str) -> None:
    from .gdoc_source import all_tabs

    document = _get(docs, document_id, includeTabsContent=True)
    found = next(t for t in all_tabs(document) if t["tab_id"] == tab)
    requests = end_requests((found["document_tab"].get("body") or {}).get("content") or [], tab)
    if requests:
        batch(docs, document_id, requests)


def last_table_break_requests(content: list[dict], tab: str, on: bool = True) -> list[dict]:
    """For a tab that ends with a table or box whose last row or line spilled onto a page of its own:
    the table starts a new page, together with its heading when one stands just above it (otherwise
    the spacer paragraph above it takes the page break). [] when the tab does not end in a table."""
    at = len(content) - 1
    while at > 0 and _empty_paragraph(content[at]):
        at -= 1
    if at < 2 or "table" not in content[at]:
        return []
    above = at - 1
    while above > 0 and _empty_paragraph(content[above]):
        above -= 1
    style = ((content[above].get("paragraph") or {}).get("paragraphStyle") or {}).get("namedStyleType", "")
    if style.startswith("HEADING") or style in ("TITLE", "SUBTITLE"):
        target = content[above]
    elif _empty_paragraph(content[at - 1]):
        target = content[at - 1]
    else:
        return []
    return [{"updateParagraphStyle": {"range": _range(target.get("startIndex", 0), target["endIndex"], tab),
                                      "paragraphStyle": {"pageBreakBefore": on}, "fields": "pageBreakBefore"}}]


def break_before_last_table(docs, document_id: str, tab: str, on: bool = True) -> bool:
    from .gdoc_source import all_tabs

    document = _get(docs, document_id, includeTabsContent=True)
    found = next(t for t in all_tabs(document) if t["tab_id"] == tab)
    requests = last_table_break_requests((found["document_tab"].get("body") or {}).get("content") or [], tab, on)
    if requests:
        batch(docs, document_id, requests)
    return bool(requests)


def make_room_at_end(docs, document_id: str, tab: str) -> None:
    """For a tab whose export ends on a blank page: the end is tidied (a text ending is joined to
    Google's last paragraph) and, after a last table, the spacing above that table is reduced."""
    from .gdoc_source import all_tabs

    tidy_end(docs, document_id, tab)
    document = _get(docs, document_id, includeTabsContent=True)
    found = next(t for t in all_tabs(document) if t["tab_id"] == tab)
    requests = full_page_requests((found["document_tab"].get("body") or {}).get("content") or [], tab)
    if requests:
        batch(docs, document_id, requests)


def clear_tab(docs, document_id: str, tab: str) -> None:
    """Remove a tab's body content (its page style stays), to write it again."""
    from .gdoc_source import all_tabs

    document = _get(docs, document_id, includeTabsContent=True)
    found = next(t for t in all_tabs(document) if t["tab_id"] == tab)
    content = (found["document_tab"].get("body") or {}).get("content") or []
    end = content[-1]["endIndex"] if content else 1
    if end - 1 > 1:
        batch(docs, document_id, [{"deleteContentRange": {"range": _range(1, end - 1, tab)}}])


# A list item's own label: a bullet, or a number / letter / roman numeral with its brackets or dot.
_LIST_LABEL = re.compile(r"(?:•|[(\[]?(?:\d{1,3}|[A-Za-z]|[ivxlcdmIVXLCDM]{1,6})(?:[.)\]]|\.\)|\)\.)?) ")


def verify(blocks: list[dict], source_units: list[dict], written: dict, lists: dict | None = None) -> list[str]:
    """Problems, or [] when the tab shows every source paragraph, cell and picture, in order, unchanged."""
    problems = []
    lines = expected(blocks, lists)
    want = [source.normal(t) for t, _ in lines]
    got = [source.normal(t) for t in written["paragraphs"]]
    if want != got:
        for i, (a, b) in enumerate(zip(want, got)):
            if a != b:
                problems.append(f"Paragraph {i + 1} differs: expected “{a[:80]}”, found “{b[:80]}”.")
                break
        else:
            problems.append(f"Expected {len(want)} paragraphs, found {len(got)}.")
    # Nothing may be added except a list item's own source number or bullet ("2. ", "(a) ", "• ").
    added = [prefix for _t, prefix in lines if prefix and not _LIST_LABEL.fullmatch(prefix)]
    if added:
        problems.append(f"Words were added that are not in the source: {added[:3]}.")
    # The words themselves, against the source as it was read (before any formatting choice).
    # $...$ notation is compared as it is shown (Ca2+ for $Ca^{2+}$).
    plain = [source.normal(t[len(prefix):] if prefix and t.startswith(prefix) else t) for t, prefix in lines]
    source_text = [math_plain(t) for unit in source_units for t in unit["texts"]]
    if source.normal(" ".join(plain)) != source.normal(" ".join(source_text)):
        problems.append("The written words differ from the source words.")
    pictures = picture_count(blocks)
    if written["pictures"] != pictures:
        problems.append(f"Expected {pictures} pictures, found {written['pictures']}.")
    return problems
