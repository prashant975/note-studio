"""Build small PDFs that each carry exactly one known defect.

Real generated notes are the primary test material (see tests/test_regression.py),
but they only exercise the defects those particular lectures happen to have. These
fixtures cover the rest of the list: a clean note, a table that runs off the page,
a page with no number, a page with placeholder title text, a blank page, a page
whose text is clipped by the page edge, and a corrupt file.

Every fixture is drawn with the DCN geometry and palette from src/config.py, so
the checks see the same page they see in a real note.

    python tests/make_fixtures.py [outdir]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

from src.config import (  # noqa: E402
    DCN_COLOR_H1,
    DCN_COLOR_H2,
    DCN_COLOR_TITLE,
    DCN_MARGIN_LEFT_IN,
    DCN_PAGE_HEIGHT_IN,
    DCN_PAGE_WIDTH_IN,
    DCN_SIZE_BODY_PT,
    DCN_SIZE_H1_PT,
    DCN_SIZE_H2_PT,
    DCN_SIZE_TITLE_PT,
)

PT = 72.0
PAGE = (DCN_PAGE_WIDTH_IN * PT, DCN_PAGE_HEIGHT_IN * PT)
LEFT = DCN_MARGIN_LEFT_IN * PT
FOOTER_Y = DCN_PAGE_HEIGHT_IN * PT - 20


def _rgb(hex_color: str) -> tuple[float, float, float]:
    value = int(hex_color, 16)
    return ((value >> 16 & 255) / 255, (value >> 8 & 255) / 255, (value & 255) / 255)


TITLE_RGB = _rgb(DCN_COLOR_TITLE)
H1_RGB = _rgb(DCN_COLOR_H1)
BODY_RGB = (0.12, 0.12, 0.12)

#: Body text must wrap inside the 5.5 in content column, or the layout scan
#: reports an overflow that the fixture, not the checker, put there. At 11 pt
#: Helvetica the column holds about this many characters.
_WRAP_CHARS = 62

#: Sentences long enough to pass check_dcn_notes' full-sentence rule, and enough
#: of them to clear the 400-word minimum, so a "clean" fixture really is clean
#: and any finding against it is a real regression.
_SENTENCES = [
    "The Right to Information Act, 2005 gives every citizen a legal route to "
    "hold a public authority to account for what it decided and why.",
    "Probity in governance means adherence to moral principles by a public "
    "servant, and it is measured by outcomes rather than by stated intent.",
    "A citizen's charter states what service a department will provide and "
    "within what time, so that a delay becomes visible and answerable.",
    "The Second Administrative Reforms Commission recommended that every "
    "department publish measurable service standards against which it is judged.",
    "Social audit brings the beneficiaries of a scheme into the verification "
    "process, which makes a paper record harder to falsify than an audit alone.",
    "A public servant who declares a conflict of interest before a decision "
    "protects both the decision and their own standing in the service.",
    "Transparency reduces the space in which discretion can be exercised "
    "arbitrarily, and that reduction is what makes accountability enforceable.",
]


def _body(n: int, offset: int = 0) -> list[str]:
    """``n`` distinct sentences, so no two pages read as duplicates."""
    return [_SENTENCES[(offset + i) % len(_SENTENCES)] for i in range(n)]


_BODY = _SENTENCES[:3]


def _logo_pixmap():
    """A blue square standing in for the PW logo in the header.

    Sized so its effective DPI at the 30 pt box it prints in clears
    VISUAL_MIN_READABLE_DPI — a 2 px placeholder is a genuine "unreadable
    figure" finding, and a fixture must not carry a defect it is not testing.
    """
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 256, 256), False)
    pix.set_rect(pix.irect, (11, 83, 148))
    return pix


def _page(doc, *, title="", heading="", body=_BODY, page_no=1, footer=True,
          logo=False):
    page = doc.new_page(width=PAGE[0], height=PAGE[1])
    y = 90.0
    if logo:
        # A stand-in for the PW logo. Without one, every fixture would report
        # "Page 1 contains no image", which is a true finding about the fixture
        # and pure noise in a test.
        page.draw_rect(fitz.Rect(LEFT, 24, LEFT + 40, 54), fill=(0.1, 0.3, 0.6))
        page.insert_image(fitz.Rect(LEFT + 50, 24, LEFT + 80, 54),
                          pixmap=_logo_pixmap())
    if title:
        page.insert_text((LEFT + 8, y), title, fontsize=DCN_SIZE_TITLE_PT,
                         color=TITLE_RGB, fontname="helv")
        y += 40
    if heading:
        page.insert_text((LEFT, y), heading, fontsize=DCN_SIZE_H1_PT,
                         color=H1_RGB, fontname="helv")
        y += 28
    for line in body:
        page.insert_text((LEFT + 18, y), "●", fontsize=DCN_SIZE_BODY_PT,
                         color=BODY_RGB, fontname="helv")
        for wrapped in _wrap(line, _WRAP_CHARS):
            page.insert_text((LEFT + 36, y), wrapped, fontsize=DCN_SIZE_BODY_PT,
                             color=BODY_RGB, fontname="helv")
            y += 16
        y += 4
    page.insert_text((LEFT, FOOTER_Y), "PW OnlyIAS Daily Class Notes",
                     fontsize=9, color=BODY_RGB, fontname="helv")
    if footer:
        page.insert_text((PAGE[0] - 120, FOOTER_Y), f"Page {page_no}",
                         fontsize=9, color=BODY_RGB, fontname="helv")
    return page


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def _closing(doc, page_no):
    """QUICK REVISION + Key Sources — every complete DCN note ends with these."""
    page = _page(doc, heading="QUICK REVISION", page_no=page_no, body=_body(2, 3))
    page.insert_text((LEFT, 420), "Key Sources", fontsize=DCN_SIZE_BODY_PT,
                     color=(0, 0, 0), fontname="hebo")
    page.insert_text((LEFT + 18, 440), "● ARC Second Report; RTI Act, 2005.",
                     fontsize=DCN_SIZE_BODY_PT, color=BODY_RGB, fontname="helv")
    return page


def _h2(page, text: str, y: float) -> None:
    """A blue #H2 — a note with an H3 but no H2 fails the heading-ladder check."""
    page.insert_text((LEFT, y), text, fontsize=DCN_SIZE_H2_PT,
                     color=_rgb(DCN_COLOR_H2), fontname="helv")


def clean(path: Path) -> None:
    doc = fitz.open()
    page = _page(doc, title="L03_DCN03_Probity", heading="Core Concepts",
                 page_no=1, body=_body(5), logo=True)
    _h2(page, "Definitions", 520)
    _page(doc, heading="Measurable Indicators", page_no=2, body=_body(6, 2))
    _page(doc, heading="Institutional Mechanisms", page_no=3, body=_body(6, 4))
    _closing(doc, 4)
    doc.save(str(path))
    doc.close()


def missing_page_number(path: Path) -> None:
    """Clean in every other respect — only page 2 has lost its number."""
    doc = fitz.open()
    page = _page(doc, title="L03_DCN03_Probity", heading="Core Concepts",
                 page_no=1, body=_body(5), logo=True)
    _h2(page, "Definitions", 520)
    _page(doc, heading="Measurable Indicators", page_no=2, body=_body(6, 2),
          footer=False)
    _page(doc, heading="Institutional Mechanisms", page_no=3, body=_body(6, 4))
    _closing(doc, 4)
    doc.save(str(path))
    doc.close()


def placeholder_title(path: Path) -> None:
    doc = fitz.open()
    _page(doc, title="Lxx_DCNxx_Probity in Governance", heading="Core Concepts",
          page_no=1)
    _closing(doc, 2)
    doc.save(str(path))
    doc.close()


def table_overflow(path: Path) -> None:
    """A table drawn wider than the 5.5 in content column, running off the page."""
    doc = fitz.open()
    page = _page(doc, title="L04_DCN04_Accountability", heading="Comparison",
                 page_no=1)
    top, row_h = 320.0, 26.0
    #: Deliberately past the page's right edge: columns at 0.5, 3.0, 5.5, 8.2 in.
    xs = [LEFT, LEFT + 180, LEFT + 360, LEFT + 555]
    for r in range(3):
        y = top + r * row_h
        page.draw_line(fitz.Point(xs[0], y), fitz.Point(xs[-1], y))
        for x in xs:
            page.draw_line(fitz.Point(x, top), fitz.Point(x, top + 3 * row_h))
        for c, x in enumerate(xs[:-1]):
            page.insert_text((x + 4, y + 17),
                             f"Indicator {r}.{c} with a long descriptive label",
                             fontsize=9, color=BODY_RGB, fontname="helv")
    page.draw_line(fitz.Point(xs[0], top + 3 * row_h),
                   fitz.Point(xs[-1], top + 3 * row_h))
    _closing(doc, 2)
    doc.save(str(path))
    doc.close()


def blank_page(path: Path) -> None:
    doc = fitz.open()
    _page(doc, title="L05_DCN05_Ethics", heading="Core Concepts", page_no=1)
    doc.new_page(width=PAGE[0], height=PAGE[1])       # nothing on it at all
    _closing(doc, 3)
    doc.save(str(path))
    doc.close()


def text_clipping(path: Path) -> None:
    doc = fitz.open()
    page = _page(doc, title="L06_DCN06_Transparency", heading="Core Concepts",
                 page_no=1)
    page.insert_text((PAGE[0] - 60, 400),
                     "This sentence starts near the right edge and runs straight "
                     "off the page where a reader loses it entirely.",
                     fontsize=DCN_SIZE_BODY_PT, color=BODY_RGB, fontname="helv")
    _closing(doc, 2)
    doc.save(str(path))
    doc.close()


def other_template(path: Path) -> None:
    """A note on a completely different type scale and palette.

    Nothing here is DCN: the title is 18 pt navy, headings are 13 pt and 11 pt
    black, body is 9 pt. Judged against the DCN scale (21/15/12.5 pt boundaries)
    every line of it is body text and the note has no structure at all. It is
    here to prove measure_type_scale() reads the scale off the document.
    """
    doc = fitz.open()
    page = doc.new_page(width=PAGE[0], height=PAGE[1])
    navy = (0.05, 0.1, 0.4)
    page.insert_text((LEFT, 90), "L09_DCN09_Federalism", fontsize=18,
                     color=navy, fontname="hebo")
    y = 130.0
    for heading, level, size in (("Constitutional Basis", 1, 13),
                                 ("Division of Powers", 2, 11),
                                 ("Fiscal Federalism", 2, 11)):
        page.insert_text((LEFT, y), heading, fontsize=size, color=(0, 0, 0),
                         fontname="hebo")
        y += size + 10
        for sentence in _body(3, level * 2):
            page.insert_text((LEFT + 12, y), "•", fontsize=9,
                             color=BODY_RGB, fontname="helv")
            for wrapped in _wrap(sentence, 88):
                page.insert_text((LEFT + 26, y), wrapped, fontsize=9,
                                 color=BODY_RGB, fontname="helv")
                y += 13
            y += 3
        y += 8
    page.insert_text((LEFT, FOOTER_Y), "Page 1", fontsize=8, color=BODY_RGB,
                     fontname="helv")
    doc.save(str(path))
    doc.close()


def corrupt(path: Path) -> None:
    """Not a PDF at all — the batch must survive it and say so."""
    path.write_bytes(b"%PDF-1.7\nthis file is truncated and unreadable\n")


FIXTURES = {
    "clean.pdf": clean,
    "missing_page_number.pdf": missing_page_number,
    "placeholder_title.pdf": placeholder_title,
    "table_overflow.pdf": table_overflow,
    "blank_page.pdf": blank_page,
    "text_clipping.pdf": text_clipping,
    "other_template.pdf": other_template,
    "corrupt.pdf": corrupt,
}


def build(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for name, fn in FIXTURES.items():
        target = out_dir / name
        fn(target)
        made.append(target)
    return made


if __name__ == "__main__":
    where = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "fixtures"
    for made in build(where):
        print(f"  {made}")
