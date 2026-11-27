"""A chapter's lecture PDFs joined into one PDF, in lecture order, with a bookmark per lecture.

The order is the files' names read the way people number them ("Lecture 2" before
"Lecture 10"). The joined PDF is what the team's prompt calls "the merged PDF"; a
copy lighter than AI_PDF_LIMIT is what PW AI reads (pages drawn again as pictures
only when the joined file is too heavy)."""
from __future__ import annotations

import re

MAX_PAGES = 1000                       # what Gemini reads of one PDF
AI_PDF_LIMIT = 40 * 1024 * 1024        # a PDF part Gemini takes comfortably
_DIGITS = re.compile(r"(\d+)")


class ChapterError(ValueError):
    """A plain-words problem with the lecture PDFs."""


def lecture_order(name: str) -> list:
    """Sort key: "Lecture 2.pdf" before "Lecture 10.pdf"."""
    return [int(part) if part.isdigit() else part.lower() for part in _DIGITS.split(str(name or ""))]


def merge(sources: list[tuple[str, bytes]]) -> tuple[bytes, list[dict]]:
    """(the joined PDF, [{"lecture", "file", "first_page", "last_page", "pages"}]) for the
    sources as given (already in lecture order)."""
    import fitz

    if not sources:
        raise ChapterError("Add the chapter's lecture PDFs.")
    joined = fitz.open()
    lectures, toc = [], []
    try:
        for number, (name, data) in enumerate(sources, start=1):
            try:
                part = fitz.open(stream=data, filetype="pdf")
            except Exception:
                raise ChapterError(f"{name} could not be opened as a PDF.") from None
            with part:
                if part.needs_pass:
                    raise ChapterError(f"{name} is locked with a password. Remove the password and add it again.")
                if part.page_count < 1:
                    raise ChapterError(f"{name} has no pages.")
                first = joined.page_count + 1
                joined.insert_pdf(part)
                lectures.append({"lecture": number, "file": name, "first_page": first,
                                 "last_page": joined.page_count, "pages": part.page_count})
                toc.append([1, re.sub(r"\.pdf$", "", name, flags=re.I) or f"Lecture {number}", first])
        if joined.page_count > MAX_PAGES:
            raise ChapterError(f"The chapter has {joined.page_count} pages; PW AI reads at most {MAX_PAGES}. "
                               "Leave out repeated or blank lectures and try again.")
        joined.set_toc(toc)
        return joined.tobytes(garbage=3, deflate=True), lectures
    finally:
        joined.close()


def ai_copy(data: bytes, limit: int = AI_PDF_LIMIT) -> bytes:
    """The joined PDF as PW AI reads it: itself when light enough, else its pages as pictures."""
    if len(data) <= limit:
        return data
    import fitz

    for dpi, quality in ((110, 70), (80, 60)):
        with fitz.open(stream=data, filetype="pdf") as source, fitz.open() as light:
            for page in source:
                picture = page.get_pixmap(dpi=dpi)
                sheet = light.new_page(width=page.rect.width, height=page.rect.height)
                sheet.insert_image(sheet.rect, stream=picture.tobytes("jpeg", jpg_quality=quality))
            smaller = light.tobytes(garbage=3, deflate=True)
        if len(smaller) <= limit:
            return smaller
    raise ChapterError("The chapter's PDFs are too heavy for PW AI even as pictures. "
                       "Add fewer lectures at a time.")


def lecture_map(lectures: list[dict]) -> str:
    """The joined PDF's lectures and their pages, for the script request."""
    lines = [f"The PDF joins {len(lectures)} lecture{'s' if len(lectures) != 1 else ''} of one chapter, in order:"]
    for item in lectures:
        pages = (f"page {item['first_page']}" if item["first_page"] == item["last_page"]
                 else f"pages {item['first_page']}-{item['last_page']}")
        lines.append(f"Lecture {item['lecture']}: {re.sub(r'[.]pdf$', '', item['file'], flags=re.I)} ({pages})")
    return "\n".join(lines)
