"""Source-locked B.Sc. layout for native text or reviewed literal AI transcripts.

Prose is reflowed; formulas, tables and pictures use lossless source excerpts.
Complex pages require visual transcription instead of old-layout screenshots.
The ordered manifest and the rendered PDF are checked before delivery.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

REVISION = "typeset-reference-ai-v3"
_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})
_MATH_FONT = re.compile(r"^(?:CMR|CMM|CMSY|CMEX|MSAM|MSBM|Symbol|STIX)", re.I)
_TEX = re.compile(r"\\documentclass\s*(?:\[|\{)|\\begin\s*\{document\}")
_COLORS = {"definition": (32, 74, 135), "concept": (235, 135, 45),
           "formula": (110, 80, 160), "warning": (190, 70, 70),
           "summary": (62, 139, 210), "note": (165, 95, 32)}


class SourceError(ValueError):
    """An input that cannot safely be turned into source-locked notes."""


class LayoutRequired(SourceError):
    def __init__(self, page: int):
        self.page = page
        super().__init__("This page needs AI transcription to rebuild its layout. Upload the original lecture PDF using the default B.Sc. notes settings.")


def scanned_pages(pdf_bytes: bytes) -> list[int]:
    import fitz

    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if pdf.needs_pass:
            raise SourceError("Upload an unlocked lecture PDF.")
        if any(_TEX.search(pdf[n].get_text()) for n in range(min(3, len(pdf)))):
            raise SourceError("This PDF contains LaTeX source code. Compile it first, then upload the finished lecture PDF.")
        return [i for i, p in enumerate(pdf) if not p.get_text().strip() or any(
            (fitz.Rect(im["bbox"]) & p.rect).get_area() > p.rect.get_area()*.6 for im in p.get_image_info())]


def normal(text: str) -> str:
    """Whitespace and typographic ligatures only; no correction or paraphrase."""
    return " ".join(text.translate(_LIGATURES).split())


def _text(block: dict) -> str:
    return normal(" ".join("".join(s["text"] for s in line["spans"])
                           for line in block.get("lines", [])))


def _parts(block: dict) -> list[str]:
    """Keep real list-item boundaries while reflowing physical line wraps."""
    parts = []
    for line in block.get("lines", []):
        text = normal("".join(s["text"] for s in line["spans"]))
        if not text:
            continue
        if not parts or re.match(r"^(?:[•●▪◦]|\d+[.)])\s", text):
            parts.append(text)
        else:
            parts[-1] += " " + text
    return parts


def _combined_parts(children: list[dict]) -> list[str]:
    return _parts({"lines": [line for child in children for line in child.get("lines", [])]})


def _spans(block: dict) -> list[dict]:
    return [s for line in block.get("lines", []) for s in line["spans"] if s["text"].strip()]


def _decoration(block: dict, height: float) -> bool:
    # Only the known reference's running furniture, never arbitrary margin text.
    text, box = _text(block), block["bbox"]
    return ((box[3] < 30 and text == "B.Sc. Wallah B.Sc. Notes") or
            (box[1] > height - 40 and text.startswith("B.Sc. Wallah") and
             text.endswith("Concept • Understand • Apply")))


def _math(block: dict) -> bool:
    return any(_MATH_FONT.match(s["font"].split("+")[-1]) or
               any(0x300 <= ord(c) <= 0x36F or c == "�" for c in s["text"])
               for s in _spans(block))


def _role(block: dict, page: int) -> str:
    spans = _spans(block)
    size = max((s["size"] for s in spans), default=10)
    bold = spans and sum(len(s["text"]) for s in spans if s["flags"] & 16) >= len(_text(block)) * .8
    if bold and size >= 18 and page == 1:
        return "title"
    if bold and size >= 11.5 and len(_text(block)) < 180:
        return "section" if size >= 13.5 else "subsection"
    return "paragraph"


def _frames(page, drawings: list, fitz) -> list[tuple]:
    frames = []
    for drawing in drawings:
        color, rect = drawing.get("fill"), drawing["rect"]
        if not color or rect.width < page.rect.width * .5 or rect.height < 25:
            continue
        rgb = tuple(round(c * 255) for c in color)
        style = next((s for s, value in _COLORS.items() if max(abs(a-b) for a, b in zip(rgb, value)) < 4), "")
        if style and not any(fitz.Rect(r).contains(rect) for r, _ in frames):
            frames.append((tuple(rect), style))
    return frames


def _booktabs(drawings: list, fitz) -> list:
    """Reference tables have grey header cells and horizontal rules, no vertical grid."""
    headers = []
    for d in drawings:
        color, rect = d.get("fill"), d["rect"]
        if color and 8 < rect.height < 35 and rect.width > 15:
            rgb = tuple(round(c*255) for c in color)
            if max(abs(a-b) for a,b in zip(rgb, (233, 237, 243))) < 4:
                match = next((r for r in headers if abs(r.y0-rect.y0) < 2), None)
                if match is not None:
                    match.include_rect(rect)
                else:
                    headers.append(fitz.Rect(rect))
    tables = []
    for head in headers:
        rules = [d["rect"] for d in drawings if d["rect"].height < 1.5 and d["rect"].width >= head.width*.9
                 and abs(d["rect"].x0-head.x0) < 3]
        top = [r.y0 for r in rules if head.y0-15 < r.y0 <= head.y0]
        bottom = [r.y0 for r in rules if r.y0 > head.y1+15]
        if top and bottom:
            tables.append(fitz.Rect(head.x0, max(top), head.x1, min(bottom)))
    return tables


def _highlight(drawing: dict, body: list, fitz) -> bool:
    """Our pale inline emphasis rectangles are text decoration, not diagrams."""
    fill, rect = drawing.get("fill"), drawing["rect"]
    if not fill or rect.height > 22:
        return False
    rgb = tuple(round(c*255) for c in fill)
    return max(abs(a-b) for a,b in zip(rgb, (252, 237, 223))) < 4 and any(
        rect.intersects(fitz.Rect(b["bbox"])) for b in body)


def _definition(text: str) -> tuple[str, str] | None:
    # Source labels and source wording only. No generated definition or summary.
    match = re.match(r"^((?:[•●▪◦]\s*)?[^:]{2,55}:)\s+(.+)$", text)
    if match and re.search(r"\b(?:defined|definition|means|refers|is|are)\b", match[2], re.I):
        return match[1], match[2]
    match = re.match(r"^(.{2,55}?)\s*((?:\$\\(?:long)?rightarrow\$|→|⇒)\s*.+)$", text)
    if match and re.search(r"\b(?:event|observer|frame|defined|definition|means)\b", match[1]+" "+match[2], re.I):
        return match[1], match[2]
    match = re.match(r"^((?:An?|The) .{2,60}?)\s+((?:is|are|refers to|means)\b.+)$", text)
    return (match[1], match[2]) if match else None


def _restyle(doc: dict, units: list[dict]) -> None:
    """Add the reference's visual hierarchy without asking a model to rewrite text."""
    source_units = {u["id"]: u for u in units}
    for block in doc["blocks"]:
        if block["kind"] == "source_box" and not block.get("title") and block.get("parts"):
            defined = _definition(block["parts"][0])
            if defined:
                block.update(title=defined[0], parts=[defined[1], *block["parts"][1:]])
                source_units[block["source_id"]]["render"] = block.copy()
        if block["kind"] != "source_text" or block.get("role") != "paragraph":
            continue
        parts = block.get("parts") or [block["text"]]
        defined = _definition(parts[0])
        if defined:
            label, body = defined
            block.update(kind="source_box", style="definition", title=label, parts=[body, *parts[1:]])
            block.pop("role", None)
            block.pop("text", None)
        elif len(parts) >= 2 and all(re.match(r"^(?:[•●▪◦]|\d+[.)])\s", p) for p in parts):
            # A feature list can be framed without adding a heading that the source did not supply.
            block.update(kind="source_box", style="concept", title="", parts=parts)
            block.pop("role", None)
            block.pop("text", None)
        source_units[block["source_id"]]["render"] = block.copy()


def _merge_entries(entries: list, fitz) -> list:
    """Inline formula images often share a paragraph; they are not separate columns."""
    merged = []
    for rect, children, style, visual in entries:
        rect = fitz.Rect(rect)
        changed = True
        while changed:
            changed = False
            for index, (other, more, other_style, other_visual) in enumerate(merged):
                if rect.intersects(other):
                    rect |= other
                    children = list({id(b): b for b in [*children, *more]}.values())
                    style = style or other_style
                    visual = visual or other_visual
                    merged.pop(index)
                    changed = True
                    break
        children = sorted(children, key=lambda b: (round(b["bbox"][1], 1), b["bbox"][0]))
        merged.append((rect, children, style, visual))
    ordered = sorted(merged, key=lambda e: (round(e[0].y0, 1), e[0].x0))
    # PDF writers can split a wrapped list item at each inline formula. Join its
    # adjacent fragments before assigning reference styles, retaining source order.
    out = []
    for entry in ordered:
        if out:
            a, ac, ast, av = out[-1]
            b, bc, bst, bv = entry
            if not ast and not bst and ac and bc and 0 <= b.y0-a.y1 < 3 and \
                    _role(ac[-1], 2) == _role(bc[0], 2) == "paragraph":
                out[-1] = (a | b, [*ac, *bc], "", av or bv)
                continue
        out.append(entry)
    return _merge_entries(out, fitz) if len(out) < len(entries) else out


def _reference_groups(entries: list) -> list:
    out, index = [], 0
    while index < len(entries):
        rect, children, style, visual = entries[index]
        if index+1 < len(entries) and children and not style and not visual and \
                _role(children[0], 2) in {"section", "subsection"}:
            other, body, body_style, body_visual = entries[index+1]
            parts = _combined_parts(body)
            if not body_style and not body_visual and len(parts) >= 2 and all(
                    re.match(r"^(?:[•●▪◦]|\d+[.)])\s", p) for p in parts):
                out.append((rect | other, [*children, *body], "concept", False))
                index += 2
                continue
        out.append(entries[index])
        index += 1
    return out


def extract(pdf_bytes: bytes, folder: Path, *, title: str = "", transcriptions: dict | None = None,
            furniture: dict | None = None) -> tuple[dict, dict]:
    """furniture: the lecture's own running header (source_brand.furniture) when the notes print it in
    their own header: its lines at the page's top, bare page numbers and its logo are left out."""
    import fitz

    from .source_brand import is_furniture, is_logo

    header_words = (furniture or {}).get("words") or set()

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    units, blocks, exclusions, attention = [], [], [], []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if pdf.needs_pass or not len(pdf):
            raise SourceError("Upload an unlocked lecture PDF with at least one page.")
        if any(_TEX.search(page.get_text()) for page in list(pdf)[:3]):
            raise SourceError("This PDF contains LaTeX source code. Compile it first, then upload the finished lecture PDF.")

        def add(page, rect, children, *, style="", visual=False, reason=""):
            source_id = f"p{page.number+1:04d}-{len(units)+1:05d}"
            texts = [_text(b) for b in children]
            unit = {"id": source_id, "page": page.number+1, "bbox": list(rect), "texts": texts}
            block = {"kind": "source_text", "source_id": source_id, "text": " ".join(texts),
                     "parts": _combined_parts(children),
                     "role": _role(children[0], page.number+1) if children else "paragraph"}
            if visual:
                name = source_id + ".png"
                clip = fitz.Rect(rect) & page.rect
                pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), clip=clip, alpha=False, annots=True)
                pix.save(str(folder / name))
                unit.update(image=name, sha256=hashlib.sha256((folder / name).read_bytes()).hexdigest())
                block = {"kind": "source_panel", "source_id": source_id, "image": name,
                         "width_pt": clip.width, "height_pt": clip.height,
                         "offset_pt": max(0, clip.x0-32.4), "spacing": "box" if style else "paragraph" if children else "figure"}
            elif style and children:
                block = {"kind": "source_box", "source_id": source_id, "style": style,
                         "title": texts[0], "parts": _combined_parts(children[1:])}
                # A one-block box still retains that entire block, without inventing a title.
                if len(texts) == 1:
                    block.update(title="", parts=_parts(children[0]))
            unit["render"] = block.copy()
            if reason:
                attention.append(f"Source page {page.number+1}: {reason}")
            units.append(unit)
            blocks.append(block)

        for page in pdf:
            if transcriptions and page.number in transcriptions:
                transcript = transcriptions[page.number]
                if transcript["nonacademic"]:
                    exclusions.append({"page": page.number+1, "text": "", "reason": "visual transcription identified a nonacademic page"})
                    continue
                for item in transcript["items"]:
                    if furniture and item["kind"] in {"heading", "paragraph", "list"} and \
                            is_furniture(item["text"], item["bbox"][1] / 1000, header_words):
                        exclusions.append({"page": page.number+1, "text": normal(item["text"]),
                                           "reason": "the lecture's running header or page number (in the notes' header now)"})
                        continue
                    rect = fitz.Rect(item["bbox"][0]*page.rect.width/1000, item["bbox"][1]*page.rect.height/1000,
                                     item["bbox"][2]*page.rect.width/1000, item["bbox"][3]*page.rect.height/1000)
                    if item["kind"] in {"equation", "table", "diagram"}:
                        add(page, rect, [], visual=True)
                        blocks[-1]["center"] = True
                        if item["kind"] == "equation":
                            blocks[-1]["frame_style"] = "formula"
                            blocks[-1]["frame_title"] = "Formula"
                        units[-1]["render"] = blocks[-1].copy()
                        units[-1]["source_caption"] = item["text"]
                        continue
                    text = normal(item["text"])
                    # Reuse the locked-source renderer and audit for every transcribed string.
                    child = {"bbox": tuple(rect), "lines": [{"spans": [{"text": text, "font": "Lato",
                              "size": (20.66 if page.number == 0 else 14.35) if item["kind"] == "heading" else 10.91,
                              "flags": 16 if item["kind"] == "heading" else 0}]}]}
                    add(page, rect, [child])
                    blocks[-1]["math_markup"] = True
                    units[-1]["render"] = blocks[-1].copy()
                    if item["style"] != "none" and item["kind"] != "heading":
                        blocks[-1].update(kind="source_box", style=item["style"], title="", parts=[text])
                        blocks[-1].pop("text", None)
                        blocks[-1].pop("role", None)
                        units[-1]["render"] = blocks[-1].copy()
                attention += [f"Source page {page.number+1}: {problem}" for problem in transcript["unreadable"]]
                continue
            raw = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT | fitz.TEXT_DEHYPHENATE, sort=True)["blocks"]
            textblocks = [b for b in raw if b["type"] == 0 and _text(b)]
            body = []
            for block in textblocks:
                if _decoration(block, page.rect.height):
                    exclusions.append({"page": page.number+1, "text": _text(block), "reason": "reference running header/footer"})
                elif header_words and is_furniture(_text(block), block["bbox"][1] / page.rect.height, header_words):
                    exclusions.append({"page": page.number+1, "text": _text(block),
                                       "reason": "the lecture's running header or page number (in the notes' header now)"})
                else:
                    body.append(block)
            images = [fitz.Rect(i["bbox"]) & page.rect for i in page.get_image_info()]
            for rect in [r for r in images if is_logo(r, furniture)]:
                images.remove(rect)
                exclusions.append({"page": page.number+1, "text": "", "reason": "the lecture's header logo (in the notes' header now)"})
            drawings = page.get_drawings()
            if not body or any(True for _ in page.annots()):
                raise LayoutRequired(page.number)
            # Preserve a page containing a large background scan: its text layer may be incomplete.
            if any(r.get_area() > page.rect.get_area() * .6 for r in images):
                raise LayoutRequired(page.number)
            frames = _frames(page, drawings, fitz)
            regions = [(fitz.Rect(r), style, False) for r, style in frames]
            regions.extend((rect, "", True) for rect in _booktabs(drawings, fitz))
            try:
                tables = page.find_tables().tables
            except Exception:
                tables = []
            for table in tables:
                rect = fitz.Rect(table.bbox)
                if table.row_count >= 2 and table.col_count >= 2 and not any(r.intersects(rect) for r, _, _ in regions):
                    regions.append((rect, "", True))
            for rect in images:
                if rect.is_empty or any(r.contains(rect) for r, _, _ in regions):
                    continue
                regions.append((rect, "", True))
            # Unclassified vector drawings can carry diagrams or labels. Keep the page rather
            # than guessing what can be omitted. Known frames and running rules are harmless.
            unknown = [d for d in drawings if d["rect"].width > 1 and d["rect"].height > 1
                       and d["rect"].get_area() < page.rect.get_area() * .95
                       and not any((r + (-2, -2, 2, 2)).contains(d["rect"]) for r, _, _ in regions)
                       and not _highlight(d, body, fitz)]
            if unknown:
                raise LayoutRequired(page.number)
            claimed = set()
            entries = []
            for rect, style, visual in regions:
                children = [(i, b) for i, b in enumerate(body) if rect.intersects(fitz.Rect(b["bbox"]))]
                # Expand to whole intersecting blocks; a crop must not cut off a label.
                for i, block in children:
                    rect |= fitz.Rect(block["bbox"])
                    claimed.add(i)
                entries.append((rect, [b for _, b in children], style, visual or any(_math(b) for _, b in children)))
            for i, block in enumerate(body):
                if i not in claimed:
                    entries.append((fitz.Rect(block["bbox"]), [block], "", _math(block)))
            entries = _merge_entries(entries, fitz)
            entries = _reference_groups(entries)
            # Overlapping regions or columns make a guessed linear order unsafe.
            overlap = any(a[0].intersects(b[0]) for i, a in enumerate(entries) for b in entries[i+1:])
            columns = any(a[0].width > page.rect.width*.22 and b[0].width > page.rect.width*.22
                          and a[0].x1 < b[0].x0-8 and min(a[0].y1,b[0].y1)-max(a[0].y0,b[0].y0) > 5
                          for a in entries for b in entries)
            if overlap or columns:
                raise LayoutRequired(page.number)
            for rect, children, style, visual in sorted(entries, key=lambda e: (round(e[0].y0, 1), e[0].x0)):
                add(page, rect, children, style=style, visual=visual)
        manifest = {"revision": REVISION, "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                    "source_pages": len(pdf), "units": units, "excluded_furniture": exclusions,
                    "attention": attention}
        if transcriptions:
            manifest["transcription_pages"] = [n+1 for n in sorted(transcriptions)]
            attention.append("AI-read slide content requires review against the original lecture, especially handwriting and symbols.")
    doc = {"title": title or "Lecture notes", "preserve_source": True, "blocks": blocks}
    _restyle(doc, units)
    return doc, manifest


def verify(doc: dict, manifest: dict, folder: Path, *, rendered: bytes | None = None,
           emitted: list[str] | None = None) -> list[str]:
    """Reject dropped, duplicated, reordered or changed units, and altered/missing assets."""
    issues = []
    units, blocks = manifest["units"], doc["blocks"]
    wanted = [u["id"] for u in units]
    if [b.get("source_id") for b in blocks] != wanted:
        return ["Source content was omitted, duplicated or reordered."]
    for block, unit in zip(blocks, units):
        if block != unit["render"]:
            issues.append(f"Source content changed: {unit['id']}.")
        if unit.get("image"):
            path = Path(folder) / unit["image"]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != unit["sha256"]:
                issues.append(f"Source excerpt missing or changed: {unit['id']}.")
    if emitted is not None and emitted != wanted:
        issues.append("The PDF did not draw every source unit exactly once in source order.")
    if rendered is not None:
        import fitz

        with fitz.open(stream=rendered, filetype="pdf") as pdf:
            logical = []
            for page in pdf:
                # Each marked word is generated from the same fragments actually drawn,
                # including shaped Hindi and formula images. Audit serialized logical
                # Unicode rather than MuPDF's heuristic word-spacing reconstruction.
                for encoded in re.findall(rb"/ActualText\s*<([A-Fa-f0-9]+)>", page.read_contents()):
                    logical.append(bytes.fromhex(encoded.decode("ascii")).decode("utf-16"))
            actual = normal(" ".join(logical)) if logical else normal(" ".join(
                p.get_text(clip=fitz.Rect(30,42,566,800), sort=True) for p in pdf))
        offset = 0
        for block, unit in zip(blocks, units):
            if unit.get("image"):
                continue
            segments = unit["texts"] if block["kind"] == "source_table" else [" ".join(unit["texts"])]
            for segment in segments:
                text = normal(segment)
                found = actual.find(text, offset)
                if found < 0:
                    issues.append(f"Rendered source wording could not be verified: {unit['id']}.")
                else:
                    offset = found + len(text)
    return issues
