"""Read a Google Doc with tabs literally: every tab becomes one locked source.

The same contract as word_source: AI may choose appearance (word_source.style),
never content. Every paragraph, list item, table cell and image of a tab is kept
in reading order. A tab holding something that cannot be carried over exactly
(an equation, a drawing, footnotes) fails before any AI call is billed.

A tab's doc is word_source's shape ({"title", "preserve_source", "blocks"}), with
three additions the Google Doc writer uses:

- source_text blocks keep "spans" (bold/italic/underline/link runs, UTF-16
  offsets into "text") and, for a list item, "list" {id, level, numbered};
- source_panel blocks keep the image's "object_id", so the writer can insert
  the same picture again from the source document;
- source_table blocks keep "merges" [[row, column, row_span, column_span]].
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Callable

from . import source

REVISION = "gdoc-literal-reference-v1"
NUMBERED = {"DECIMAL", "ZERO_DECIMAL", "UPPER_ALPHA", "ALPHA", "UPPER_ROMAN", "ROMAN"}
HEADING_ROLES = {"TITLE": "section", "HEADING_1": "section", "HEADING_2": "section",
                 "SUBTITLE": "subsection", "HEADING_3": "subsection", "HEADING_4": "subsection",
                 "HEADING_5": "subsection", "HEADING_6": "subsection"}
BULLET = "• "
TEXT_WIDTH_PT = 530.48          # the B.Sc. text width; a wider picture is shown at this width


def u16(text: str) -> int:
    """Google Docs indexes count UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def fetch(docs, document_id: str) -> dict:
    """The whole document with every tab's content (one read request)."""
    return docs.documents().get(documentId=document_id, includeTabsContent=True).execute()


def all_tabs(document: dict) -> list[dict]:
    """Every tab in reading order, child tabs right after their parent."""
    found: list[dict] = []

    def walk(tabs, depth: int, parent: str) -> None:
        for tab in tabs or []:
            props = tab.get("tabProperties") or {}
            tab_id = props.get("tabId", "")
            found.append({"tab_id": tab_id, "title": (props.get("title") or "").strip() or f"Tab {len(found) + 1}",
                          "depth": depth, "parent": parent, "document_tab": tab.get("documentTab") or {}})
            walk(tab.get("childTabs"), depth + 1, tab_id)

    walk(document.get("tabs"), 0, "")
    return found


CODE_BLOCK_MARK = ""   # Google Docs' hidden start/end of a code block: not text, and Docs refuses to insert it


def _clean(text: str) -> str:
    """Line breaks inside a paragraph become spaces, and a code block's hidden marker goes;
    nothing else changes."""
    return text.replace("\n", " ").replace("\x0b", " ").replace("\r", " ").replace(CODE_BLOCK_MARK, "")


class _Reader:
    def __init__(self, tab: dict, folder: Path, fetch_image: Callable[[str], bytes]):
        self.tab = tab
        self.content = tab["document_tab"]
        self.folder = folder
        self.fetch_image = fetch_image
        self.inline = self.content.get("inlineObjects") or {}
        self.positioned = self.content.get("positionedObjects") or {}
        self.lists = self.content.get("lists") or {}
        self.blocks: list[dict] = []
        self.units: list[dict] = []
        self.counts = {"paragraphs": 0, "list_items": 0, "tables": 0, "boxes": 0, "images": 0}
        self.attention: list[str] = []

    # ------------------------------------------------------------- output
    def add(self, block: dict, texts: list[str], **extra) -> None:
        sid = f"g{len(self.units) + 1:05d}"
        block["source_id"] = sid
        self.blocks.append(block)
        self.units.append({"id": sid, "texts": [source.normal(t) for t in texts if source.normal(t)],
                           "render": copy.deepcopy(block), **extra})

    def fail(self, what: str) -> None:
        raise source.SourceError(f"Tab “{self.tab['title']}” contains {what}, which cannot be copied "
                                 "exactly into the new notes. Replace it with text or a picture, then try again.")

    # ------------------------------------------------------------- pieces
    def image(self, object_id: str, positioned: bool) -> None:
        holder = (self.positioned if positioned else self.inline).get(object_id) or {}
        props = holder.get("positionedObjectProperties" if positioned else "inlineObjectProperties") or {}
        embedded = props.get("embeddedObject") or {}
        picture = embedded.get("imageProperties") or {}
        uri = picture.get("contentUri") or ""
        if not uri:
            if embedded.get("embeddedDrawingProperties") is not None:
                self.fail("a Google Drawing")
            if embedded.get("linkedContentReference"):
                self.fail("a linked chart")
            self.fail("an object without a readable picture")
        data = self.fetch_image(uri)
        if not data:
            self.fail("a picture that could not be downloaded")
        from PIL import Image
        import io

        with Image.open(io.BytesIO(data)) as im:
            pixel_w, pixel_h = im.size
            suffix = ".png" if (im.format or "").upper() == "PNG" else ".jpg" if (im.format or "").upper() == "JPEG" \
                else ".gif" if (im.format or "").upper() == "GIF" else ".png"
        size = embedded.get("size") or {}
        width = float(((size.get("width") or {}).get("magnitude")) or pixel_w * 0.75)
        height = float(((size.get("height") or {}).get("magnitude")) or pixel_h * 0.75)
        if width <= 0 or height <= 0:
            width, height = pixel_w * 0.75, pixel_h * 0.75
        self.counts["images"] += 1
        filename = f"gdoc-image-{self.counts['images']:04d}{suffix}"
        (self.folder / filename).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self.add({"kind": "source_panel", "image": filename, "width_pt": round(width, 2), "height_pt": round(height, 2),
                  "center": True, "object_id": object_id, "positioned": positioned},
                 [], image=filename, sha256=digest, object_id=object_id)

    def runs(self, paragraph: dict) -> list:
        """The paragraph's pieces in order: ("text", text, style) or ("image", object id)."""
        pieces = []
        for element in paragraph.get("elements") or []:
            if "textRun" in element:
                run = element["textRun"]
                pieces.append(("text", _clean(run.get("content") or ""), run.get("textStyle") or {}))
            elif "inlineObjectElement" in element:
                pieces.append(("image", element["inlineObjectElement"].get("inlineObjectId", "")))
            elif "equation" in element:
                self.fail("an equation")
            elif "footnoteReference" in element:
                self.fail("a footnote")
            elif "person" in element:
                props = element["person"].get("personProperties") or {}
                pieces.append(("text", props.get("name") or props.get("email") or "", {}))
            elif "richLink" in element:
                props = element["richLink"].get("richLinkProperties") or {}
                pieces.append(("text", props.get("title") or props.get("uri") or "",
                               {"link": {"url": props.get("uri")}} if props.get("uri") else {}))
            elif "dateElement" in element:
                props = element["dateElement"].get("dateElementProperties") or {}
                text = props.get("displayText") or (element["dateElement"].get("textStyle") or {}).get("displayText") or ""
                if not text:
                    self.fail("a date chip")
                pieces.append(("text", text, {}))
            # autoText (page numbers), page/column breaks and horizontal rules are page furniture.
        return pieces

    def list_info(self, paragraph: dict) -> dict | None:
        bullet = paragraph.get("bullet")
        if not bullet:
            return None
        list_id = bullet.get("listId", "")
        level = int(bullet.get("nestingLevel") or 0)
        levels = ((self.lists.get(list_id) or {}).get("listProperties") or {}).get("nestingLevels") or []
        glyph = (levels[level] if level < len(levels) else {}) if levels else {}
        numbered = (glyph.get("glyphType") or "") in NUMBERED
        return {"id": list_id, "level": min(level, 8), "numbered": numbered}

    def role(self, paragraph: dict, text: str, spans_all_bold: bool, size: float, is_list: bool) -> str:
        named = (paragraph.get("paragraphStyle") or {}).get("namedStyleType") or "NORMAL_TEXT"
        role = "paragraph" if is_list else HEADING_ROLES.get(named, "paragraph")
        if role == "paragraph" and not is_list and spans_all_bold and u16(text) <= 90 \
                and not re.search(r"[.;,]$", text.strip()):
            role = "section" if size >= 15 else "subsection"
        if role != "paragraph" and not self.blocks:
            role = "title"
        return role

    def paragraph(self, paragraph: dict, *, in_cell: bool = False) -> list[str]:
        """Add the paragraph's blocks (or, inside a data table, return its text lines)."""
        for object_id in paragraph.get("positionedObjectIds") or []:
            if in_cell:
                self.fail("a picture inside a table")
            self.image(object_id, positioned=True)
        lines: list[str] = []
        text, spans, sizes, bold_chars, letters = "", [], [], 0, 0
        info = self.list_info(paragraph)

        def flush() -> None:
            nonlocal text, spans, sizes, bold_chars, letters
            body = text.strip()
            if body:
                lead = u16(text) - u16(text.lstrip())
                trimmed = [[max(0, s - lead), min(u16(body), e - lead), st] for s, e, st in spans]
                trimmed = [s for s in trimmed if s[1] > s[0]]
                if in_cell:
                    lines.append((BULLET if info else "") + body)
                else:
                    size = max(sizes) if sizes else 11.0
                    role = self.role(paragraph, body, letters > 0 and bold_chars == letters, size, info is not None)
                    block = {"kind": "source_text", "role": role, "text": body, "parts": [body], "spans": trimmed}
                    if info:
                        block["list"] = info
                        self.counts["list_items"] += 1
                    else:
                        self.counts["paragraphs"] += 1
                    self.add(block, [body])
            text, spans, sizes, bold_chars, letters = "", [], [], 0, 0

        for piece in self.runs(paragraph):
            if piece[0] == "image":
                if in_cell:
                    self.fail("a picture inside a table")
                flush()
                self.image(piece[1], positioned=False)
                continue
            _kind, chunk, style = piece
            if not chunk:
                continue
            start = u16(text)
            text += chunk
            end = u16(text)
            marks = {k: True for k in ("bold", "italic", "underline") if style.get(k)}
            url = (style.get("link") or {}).get("url")
            if url:
                marks["link"] = url
            if marks:
                spans.append([start, end, marks])
            if (style.get("fontSize") or {}).get("magnitude"):
                sizes.append(float(style["fontSize"]["magnitude"]))
            visible = sum(1 for ch in chunk if not ch.isspace())
            letters += visible
            if style.get("bold"):
                bold_chars += visible
        flush()
        return lines

    def cell_lines(self, cell: dict) -> list[str]:
        lines: list[str] = []
        for element in cell.get("content") or []:
            if "paragraph" in element:
                lines.extend(self.paragraph(element["paragraph"], in_cell=True))
            elif "table" in element:
                self.fail("a table inside a table")
        return lines

    def table(self, table: dict) -> None:
        rows = table.get("tableRows") or []
        cells = [row.get("tableCells") or [] for row in rows]
        has_media = any(
            "table" in element or (element.get("paragraph") or {}).get("positionedObjectIds") or
            any("inlineObjectElement" in e for e in (element.get("paragraph") or {}).get("elements") or [])
            for row in cells for cell in row for element in cell.get("content") or [])
        if has_media:
            # A layout table (a picture beside its text): its content is kept, in reading order.
            self.attention.append(f"A layout table in “{self.tab['title']}” was laid out as running text "
                                  "and pictures, in the same order.")
            for row in cells:
                for cell in row:
                    self.walk(cell.get("content") or [])
            return
        grid = [[self.cell_lines(cell) for cell in row] for row in cells]
        if not any(line for row in grid for cell in row for line in cell):
            return
        if len(grid) == 1 and len(grid[0]) == 1:
            parts = grid[0][0]
            self.counts["boxes"] += 1
            self.add({"kind": "source_box", "style": "note", "title": "", "parts": parts, "from_table": True}, parts)
            return
        width = max(len(row) for row in grid)
        text_rows = [["\n".join(cell) for cell in row] + [""] * (width - len(row)) for row in grid]
        merges = []
        for r, row in enumerate(cells):
            for c, cell in enumerate(row):
                style = cell.get("tableCellStyle") or {}
                rs, cs = int(style.get("rowSpan") or 1), int(style.get("columnSpan") or 1)
                if rs > 1 or cs > 1:
                    merges.append([r, c, rs, cs])
        self.counts["tables"] += 1
        self.add({"kind": "source_table", "style": "ruled", "columns": text_rows[0], "rows": text_rows[1:],
                  "merges": merges},
                 [line for row in text_rows for cell in row for line in cell.split("\n")])

    def walk(self, content: list) -> None:
        for element in content:
            if "paragraph" in element:
                self.paragraph(element["paragraph"])
            elif "table" in element:
                self.table(element["table"])
            # sectionBreak and tableOfContents are page furniture.


def _identity(blocks: list[dict], units: list[dict]) -> str:
    """The tab's content hash: text, structure and picture bytes (not the short-lived picture links)."""
    shape = [{k: v for k, v in b.items() if k not in ("source_id",)} for b in blocks]
    pictures = [u.get("sha256", "") for u in units]
    return hashlib.sha256(json.dumps([shape, pictures], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def extract(tab: dict, folder: Path, fetch_image: Callable[[str], bytes]) -> tuple[dict, dict]:
    """One tab -> (doc, manifest) in word_source's shape."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    reader = _Reader(tab, folder, fetch_image)
    if (tab["document_tab"].get("footnotes") or {}):
        reader.fail("footnotes")
    reader.walk(((tab["document_tab"].get("body") or {}).get("content")) or [])
    if not reader.blocks:
        raise source.SourceError(f"Tab “{tab['title']}” has no readable content.")
    manifest = {"revision": REVISION, "source_sha256": _identity(reader.blocks, reader.units), "source_format": "gdoc",
                "tab_id": tab["tab_id"], "tab_title": tab["title"], "source_pages": None, "counts": reader.counts,
                "units": reader.units, "attention": reader.attention, "excluded_furniture": []}
    doc = {"title": tab["title"], "preserve_source": True, "blocks": reader.blocks}
    return doc, manifest


def list_levels(document: dict) -> dict[str, dict[str, list]]:
    """{tab id: {list id: nestingLevels}}: each list level's numbering (glyph type,
    format such as "%0.", start number) or bullet symbol, as the source shows it."""
    return {tab["tab_id"]: {list_id: ((found.get("listProperties") or {}).get("nestingLevels") or [])
                            for list_id, found in (tab["document_tab"].get("lists") or {}).items()}
            for tab in all_tabs(document)}


def picture_links(document: dict) -> dict[str, dict[str, str]]:
    """{tab id: {object id: content link}} from a fresh read; links last about 30 minutes."""
    links: dict[str, dict[str, str]] = {}
    for tab in all_tabs(document):
        content = tab["document_tab"]
        found = {}
        for key, holder_key in (("inlineObjects", "inlineObjectProperties"),
                                ("positionedObjects", "positionedObjectProperties")):
            for object_id, holder in (content.get(key) or {}).items():
                uri = ((((holder.get(holder_key) or {}).get("embeddedObject") or {}).get("imageProperties") or {})
                       .get("contentUri"))
                if uri:
                    found[object_id] = uri
        links[tab["tab_id"]] = found
    return links
