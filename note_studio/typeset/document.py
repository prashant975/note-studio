"""The typeset notes as data: the JSON PW AI writes (schema()), tidied into the
document the PDF is drawn from, and the inline markup of its text. No model here.

A document is {"title", "subject", "blocks": [...]}; blocks in reading order:

    section | subsection | subsubsection   text (numbered when drawn: 1, 1.1, 1.1.1)
    heading                              text (a bold heading without a number)
    paragraph                            text
    bullets | numbered                   items: [{text, sub: [text]}]
    math                                 text: one display formula (LaTeX, no $)
    box                                  style, title, body: [paragraph | bullets | numbered | math]
    table                                style (lines | grid), title (the caption), columns, rows
    figure                               title (the caption), description, labels, source_page; image once drawn
    tree                                 title, nodes: [{id, label, parent}]
    flow                                 title, items (the steps), cycle

Inline markup: **bold**, *italic*, [[key term]], ==must remember==, $math$.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HEADINGS = ("section", "subsection", "subsubsection", "heading")
LISTS = ("bullets", "numbered")
INNER = ("paragraph", "bullets", "numbered", "math")
BLOCKS = (*HEADINGS, "paragraph", *LISTS, "math", "box", "table", "figure", "tree", "flow")
BOX_STYLES = ("definition", "concept", "formula", "warning", "note", "summary")
TABLE_STYLES = ("lines", "grid")
MAX_LABELS, MAX_NODES, MAX_DEPTH, MAX_STEPS, MAX_COLUMNS, MAX_ROWS = 14, 18, 4, 8, 6, 40
BOX_TITLES = {"summary": "Key Takeaways", "warning": "Common Mistake", "note": "Note"}


def schema() -> dict:
    """The response schema (Gemini's responseSchema subset: no $ref, no anyOf)."""
    text = {"type": "string"}
    strings = {"type": "array", "items": text}
    item = {"type": "object", "properties": {
        "text": {"type": "string", "description": "One point; may start with a **Bold label:**"},
        "sub": {"type": "array", "items": text, "description": "Sub-points, if any"}}, "required": ["text"]}
    inner = {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(INNER)},
        "text": {"type": "string", "description": "paragraph text, or the LaTeX of one display formula"},
        "items": {"type": "array", "items": item}}, "required": ["kind"]}
    node = {"type": "object", "properties": {"id": text, "label": text,
                                             "parent": {"type": "string", "description": "id of the parent; empty for the root"}},
            "required": ["id", "label"]}
    block = {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(BLOCKS)},
        "text": {"type": "string", "description": "heading or paragraph text; LaTeX for math"},
        "style": {"type": "string", "enum": [*BOX_STYLES, *TABLE_STYLES], "description": "box style, or table style"},
        "title": {"type": "string", "description": "box title, table/figure/tree/flow caption"},
        "body": {"type": "array", "items": inner, "description": "box content"},
        "items": {"type": "array", "items": item, "description": "list items, or flow steps"},
        "columns": {**strings, "description": "table header"},
        "rows": {"type": "array", "items": strings, "description": "table rows, one string per column"},
        "description": {"type": "string", "description": "figure: exactly what to draw, for the illustrator"},
        "labels": {**strings, "description": "figure: every label to print on it, spelled exactly"},
        "source_page": {"type": "integer", "description": "figure: the lecture page of the diagram it redraws"},
        "nodes": {"type": "array", "items": node, "description": "tree nodes"},
        "cycle": {"type": "boolean", "description": "flow: the last step leads back to the first"}},
        "required": ["kind"]}
    return {"type": "object", "properties": {
        "title": {"type": "string", "description": "the lecture's topic, printed at the top"},
        "subject": text,
        "blocks": {"type": "array", "items": block}}, "required": ["title", "blocks"]}


# --------------------------------------------------------------------- tidy
# "4. Pigmentation", "3.4 Cell Wall", "II. Types", "(a)"... but never "E. coli" or "V. cholerae".
_NUMBERED = re.compile(r"^\s*(?:(?:section|chapter|unit|topic|part)\s+)?(?:\d+(?:\.\d+)*[.):]|\d+\.\d+(?:\.\d+)*"
                       r"|(?:ii|iii|iv|vi|vii|viii|ix|xi|xii)[.)]|[ivx]\)|\(?[a-h]\))\s+", re.I)
_BULLET = re.compile(r"^\s*(?:[-–—•●▪◦]|\d{1,2}[.)])\s+")
_CAPTION = re.compile(r"^\s*(?:figure|fig\.?|table|definition)\s*\d*\s*[:.\-–]\s*", re.I)
_SPACES = re.compile(r"[ \t\u00a0\u2009\u202f]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(value) -> str:
    """One line of text: control characters out, runs of spaces made one."""
    text = _CONTROL.sub("", str(value or "").replace("\r", " ").replace("\n", " "))
    return _SPACES.sub(" ", text).strip()


def _balance(text: str, issues: list, where: str) -> str:
    """Close a bracket left open ("The Frustule (Cell Wall") and drop stray markup."""
    for opener, closer in (("(", ")"), ("[", "]")):
        plain = re.sub(r"\$[^$]*\$|\[\[|\]\]", "", text)
        if plain.count(opener) == plain.count(closer) + 1 and not text.rstrip().endswith(closer):
            text = text.rstrip(" .,;:") + closer
            issues.append(f"{where}: closed a bracket left open")
    for marker in ("**", "==", "$"):
        if _unescaped(text, marker) % 2:
            cut = text.rfind(marker)
            text = (text[:cut] + text[cut + len(marker):]).strip()
            issues.append(f"{where}: removed an unmatched {marker}")
    if text.count("[[") != text.count("]]"):
        text = text.replace("[[", "").replace("]]", "")
        issues.append(f"{where}: removed unmatched [[ ]]")
    return text


def _unescaped(text: str, marker: str) -> int:
    return len(re.findall(r"(?<!\\)" + re.escape(marker), text))


def _text(value, issues: list, where: str) -> str:
    return _balance(clean(value), issues, where) if clean(value) else ""


def _heading(value, issues: list, where: str) -> str:
    text = _text(value, issues, where)
    stripped = _NUMBERED.sub("", text, count=1)
    if stripped != text and stripped:
        issues.append(f"{where}: removed the number typed into the heading ({text[:40]})")
        text = stripped
    return text.rstrip(":").strip()


def _math(value) -> str:
    text = clean(value)
    for left, right in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")):
        if text.startswith(left) and text.endswith(right) and len(text) > len(left) + len(right):
            return text[len(left):-len(right)].strip()
    return text


def _items(values, issues: list, where: str) -> list[dict]:
    items = []
    for n, value in enumerate(values if isinstance(values, list) else []):
        if isinstance(value, str):
            value = {"text": value}
        if not isinstance(value, dict):
            continue
        text = _BULLET.sub("", _text(value.get("text"), issues, f"{where} item {n + 1}"), count=1)
        sub = [_BULLET.sub("", _text(s, issues, f"{where} item {n + 1}"), count=1)
               for s in (value.get("sub") or []) if isinstance(s, str)]
        sub = [s for s in sub if s]
        if text or sub:
            items.append({"text": text or sub.pop(0), "sub": sub})
    return items


def _inner(values, issues: list, where: str) -> list[dict]:
    body = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        kind = value.get("kind")
        if kind == "math" and _math(value.get("text")):
            body.append({"kind": "math", "text": _math(value.get("text"))})
        elif kind in LISTS:
            items = _items(value.get("items"), issues, where)
            if items:
                body.append({"kind": kind, "items": items})
        elif _text(value.get("text"), issues, where):
            body.append({"kind": "paragraph", "text": _text(value.get("text"), issues, where)})
    return body


def _table(block: dict, issues: list, where: str) -> dict | None:
    columns = [_text(c, issues, where) for c in (block.get("columns") or []) if isinstance(c, (str, int, float))]
    rows = [[_text(c, issues, where) for c in row] for row in (block.get("rows") or []) if isinstance(row, list)]
    rows = [row for row in rows if any(row)]
    if not columns and rows:
        columns, rows = rows[0], rows[1:]
    width = min(MAX_COLUMNS, max([len(columns), *map(len, rows)] or [0]))
    if width < 2 or not rows:
        return None
    if len(rows) > MAX_ROWS:
        issues.append(f"{where}: kept the first {MAX_ROWS} of {len(rows)} rows")
    fit = lambda row: (row + [""] * width)[:width]                      # noqa: E731
    style = block.get("style") if block.get("style") in TABLE_STYLES else "lines"
    return {"kind": "table", "style": style, "title": _CAPTION.sub("", _text(block.get("title"), issues, where)),
            "columns": fit(columns), "rows": [fit(r) for r in rows[:MAX_ROWS]]}


def _figure(block: dict, issues: list, where: str) -> dict | None:
    description = _text(block.get("description"), issues, where)
    if not description:
        return None
    labels, seen = [], set()
    for label in block.get("labels") or []:
        label = clean(label)
        if label and label.casefold() not in seen and len(labels) < MAX_LABELS:
            seen.add(label.casefold())
            labels.append(label)
    title = _CAPTION.sub("", _text(block.get("title"), issues, where)) or description.split(".")[0][:90]
    page = block.get("source_page")
    return {"kind": "figure", "title": title, "description": description, "labels": labels,
            "source_page": page if isinstance(page, int) and not isinstance(page, bool) and page > 0 else None}


def _tree(block: dict, issues: list, where: str) -> dict | None:
    nodes, ids = [], set()
    for value in block.get("nodes") or []:
        if not isinstance(value, dict):
            continue
        node_id, label = clean(value.get("id")), _text(value.get("label"), issues, where)
        if node_id and label and node_id not in ids:
            ids.add(node_id)
            nodes.append({"id": node_id, "label": label, "parent": clean(value.get("parent"))})
    if len(nodes) < 2:
        return None
    for node in nodes:
        if node["parent"] not in ids or node["parent"] == node["id"]:
            node["parent"] = ""
    roots = [n for n in nodes if not n["parent"]] or [nodes[0]]
    roots[0]["parent"] = ""
    if len(roots) > 1:
        root = {"id": "__root", "label": _text(block.get("title"), issues, where) or "Overview", "parent": ""}
        nodes.insert(0, root)
        for node in roots:
            node["parent"] = root["id"]
    root = next(n for n in nodes if not n["parent"])
    # Breadth first from the root: parents always come before their children,
    # and a box caught in a loop of parents hangs from the root instead.
    order, depth, waiting = [root], {root["id"]: 0}, [n for n in nodes if n is not root]
    while waiting:
        reached = [n for n in waiting if n["parent"] in depth]
        if not reached:
            waiting[0]["parent"] = root["id"]
            continue
        for node in reached:
            depth[node["id"]] = depth[node["parent"]] + 1
            order.append(node)
            waiting.remove(node)
    kept = [n for n in order if depth[n["id"]] < MAX_DEPTH][:MAX_NODES]
    if len(kept) < len(order):
        issues.append(f"{where}: the diagram was trimmed to {len(kept)} boxes")
    return {"kind": "tree", "title": _CAPTION.sub("", _text(block.get("title"), issues, where)), "nodes": kept}


def _block(block: dict, issues: list, where: str) -> dict | None:
    kind = block.get("kind")
    if kind in HEADINGS:
        text = _heading(block.get("text") or block.get("title"), issues, where)
        return {"kind": kind, "text": text} if text else None
    if kind == "paragraph":
        text = _text(block.get("text"), issues, where)
        return {"kind": kind, "text": text} if text else None
    if kind in LISTS:
        items = _items(block.get("items"), issues, where)
        return {"kind": kind, "items": items} if items else None
    if kind == "math":
        text = _math(block.get("text"))
        return {"kind": kind, "text": text} if text else None
    if kind == "box":
        style = block.get("style") if block.get("style") in BOX_STYLES else "concept"
        body = _inner(block.get("body"), issues, where)
        if not body and _text(block.get("text"), issues, where):
            body = [{"kind": "paragraph", "text": _text(block.get("text"), issues, where)}]
        if not body and block.get("items"):
            items = _items(block.get("items"), issues, where)
            body = [{"kind": "bullets", "items": items}] if items else []
        title = _text(block.get("title"), issues, where)
        if style == "definition":
            title = _CAPTION.sub("", title)
        title = title or BOX_TITLES.get(style, "")
        return {"kind": "box", "style": style, "title": title, "body": body} if body else None
    if kind == "table":
        return _table(block, issues, where)
    if kind == "figure":
        return _figure(block, issues, where)
    if kind == "tree":
        return _tree(block, issues, where)
    if kind == "flow":
        steps = [i["text"] for i in _items(block.get("items"), issues, where)][:MAX_STEPS]
        return {"kind": "flow", "title": _CAPTION.sub("", _text(block.get("title"), issues, where)),
                "items": steps, "cycle": bool(block.get("cycle"))} if len(steps) >= 2 else None
    return None


def tidy(raw: dict, *, title: str = "") -> tuple[dict, list[str]]:
    """The model's JSON, made safe to draw: unknown or empty blocks dropped,
    numbers typed into headings removed, brackets and markup balanced.
    Returns (document, what was changed, in plain words)."""
    issues: list[str] = []
    raw = raw if isinstance(raw, dict) else {}
    blocks = []
    for n, block in enumerate(raw.get("blocks") or []):
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        made = _block(block, issues, f"block {n + 1} ({kind})")
        if made is None:
            issues.append(f"block {n + 1} ({kind}): left out (empty or not a known kind)")
        else:
            blocks.append(made)
    doc_title = clean(title) or _text(raw.get("title"), issues, "title") or "Lecture notes"
    return {"title": doc_title, "subject": clean(raw.get("subject")), "blocks": blocks}, issues


def figures(doc: dict) -> list[dict]:
    return [b for b in doc.get("blocks") or [] if b.get("kind") == "figure"]


def texts(doc: dict):
    """(JSON pointer, text) for every string a reviewer may correct."""
    for b, block in enumerate(doc.get("blocks") or []):
        base = f"/blocks/{b}"
        for key in ("text", "title", "description"):
            if isinstance(block.get(key), str) and block[key]:
                yield f"{base}/{key}", block[key]
        for i, item in enumerate(block.get("items") or []):
            if isinstance(item, str):
                yield f"{base}/items/{i}", item
                continue
            yield f"{base}/items/{i}/text", item["text"]
            for s, sub in enumerate(item.get("sub") or []):
                yield f"{base}/items/{i}/sub/{s}", sub
        for p, part in enumerate(block.get("body") or []):
            if part.get("text"):
                yield f"{base}/body/{p}/text", part["text"]
            for i, item in enumerate(part.get("items") or []):
                yield f"{base}/body/{p}/items/{i}/text", item["text"]
                for s, sub in enumerate(item.get("sub") or []):
                    yield f"{base}/body/{p}/items/{i}/sub/{s}", sub
        for c, column in enumerate(block.get("columns") or []):
            yield f"{base}/columns/{c}", column
        for r, row in enumerate(block.get("rows") or []):
            for c, cell in enumerate(row):
                if cell:
                    yield f"{base}/rows/{r}/{c}", cell
        for l, label in enumerate(block.get("labels") or []):
            yield f"{base}/labels/{l}", label
        for n, node in enumerate(block.get("nodes") or []):
            yield f"{base}/nodes/{n}/label", node["label"]


def replace(doc: dict, pointer: str, old: str, new: str) -> bool:
    """Set one string by JSON pointer, only if it still reads ``old``."""
    parts = [p for p in str(pointer or "").split("/") if p != ""]
    if not parts or parts[0] != "blocks":
        return False
    target = doc
    try:
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        last = int(parts[-1]) if isinstance(target, list) else parts[-1]
        if target[last] != old or not isinstance(target[last], str):
            return False
    except (KeyError, IndexError, ValueError, TypeError):
        return False
    target[last] = clean(new)
    return True


# ----------------------------------------------------------- inline markup
@dataclass
class Span:
    text: str
    math: bool = False
    bold: bool = False
    italic: bool = False
    key: bool = False           # [[key term]]: bold, in the brand colour
    mark: bool = False          # ==must remember==: bold on a highlight

    def style(self) -> tuple:
        return self.math, self.bold, self.italic, self.key, self.mark


_TOKEN = re.compile(r"\\[*$=\[\]\\]|\$|\*\*|\*|\[\[|\]\]|==")


def _tokens(text: str) -> list[list]:
    out, pos = [], 0
    while pos < len(text):
        found = _TOKEN.search(text, pos)
        if not found:
            out.append(["text", text[pos:]])
            break
        if found.start() > pos:
            out.append(["text", text[pos:found.start()]])
        token = found.group()
        if token.startswith("\\"):
            out.append(["text", token[1:]])
            pos = found.end()
        elif token == "$":
            end = text.find("$", found.end())
            while end != -1 and text[end - 1] == "\\":
                end = text.find("$", end + 1)
            if end == -1 or end == found.end():
                out.append(["text", "$"])
                pos = found.end()
            else:
                out.append(["math", text[found.end():end]])
                pos = end + 1
        else:
            out.append(["mk", token])
            pos = found.end()
    return out


def parse_inline(text: str) -> list[Span]:
    """Spans of one text, in order; markup without a partner stays as text."""
    tokens = _tokens(str(text or ""))
    for marker in ("**", "*", "=="):
        where = [i for i, t in enumerate(tokens) if t == ["mk", marker]]
        if len(where) % 2:
            tokens[where[-1]] = ["text", marker]
    depth = []
    for i, token in enumerate(tokens):
        if token == ["mk", "[["]:
            depth.append(i)
        elif token == ["mk", "]]"]:
            if depth:
                depth.pop()
            else:
                tokens[i] = ["text", "]]"]
    for i in depth:
        tokens[i] = ["text", "[["]
    spans: list[Span] = []
    style = {"**": False, "*": False, "==": False, "[[": False}
    for kind, value in tokens:
        if kind == "mk":
            if value == "]]":
                style["[["] = False
            else:
                style[value] = not style[value] if value != "[[" else True
            continue
        span = Span(value, math=kind == "math", bold=style["**"], italic=style["*"], key=style["[["], mark=style["=="])
        if spans and not span.math and not spans[-1].math and spans[-1].style() == span.style():
            spans[-1].text += value
        elif value:
            spans.append(span)
    return spans


def plain(text: str) -> str:
    """The text without markup (math kept as written), for checks and search."""
    return "".join(s.text if not s.math else f"${s.text}$" for s in parse_inline(text))
