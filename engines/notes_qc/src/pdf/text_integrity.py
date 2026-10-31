"""Can the PDF's text layer be trusted?

This question does not exist in the note-generation project, and it has to be
answered here. That app checks the notes it just wrote, as text. This app is
handed a typeset PDF and has to read the text back out of it — and for Hindi
notes, reading it back does not work.

The DCN PDFs are exported through Word/LibreOffice with the handwritten face
(Kalam) embedded as an ``Identity-H`` subset. Devanagari is *shaped* before it
is drawn: matras are reordered, consonants are fused into conjuncts, and the
subset's ToUnicode map is built from the shaped glyphs. Extracting text from
that gives back the visual order, not the typed order. Measured on a real
20-page Hindi DCN:

    typed   जाति व्यवस्था की उत्पत्ति के सिद्धांत
    read    जातत व्यिस्था की उत्पत्ति के त्तसद्ांत

Nothing downstream can repair that, and running a factual check over it would
report errors that are artefacts of the export. So it is detected instead, and
the pages are read as images by the AI stages when it is found.

The detector looks for sequences Unicode's Devanagari model does not allow.
Measured on the same document, typed text against extracted text:

    word begins with a vowel sign      0.00%  vs  3.98%
    two vowel signs in a row           0.00%  vs  9.25%
    word ends with a virama                0  vs     41

Both classes are structurally impossible in well-formed Devanagari — a vowel
sign has to hang off a preceding consonant — so any material rate of them means
the glyphs came back in visual order.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Dependent vowel signs (matras) and the vowel-sign-like marks that behave the
#: same way: every one of them must follow a consonant.
_MATRA = r"[ा-ौॎॕ-ॗॢॣ]"
_VIRAMA = "्"

_DEVANAGARI_WORD_RE = re.compile(r"[ऀ-ॿ]+")
_LEADING_MATRA_RE = re.compile(rf"^{_MATRA}")
_DOUBLE_MATRA_RE = re.compile(rf"{_MATRA}{_MATRA}")

#: Share of Devanagari words that may show an impossible sequence before the
#: text layer is called unreliable. Real extraction noise sits near zero and the
#: corrupted document sits above 10% on the combined measure, so 2% separates
#: them with a wide margin in both directions.
CORRUPTION_THRESHOLD = 0.02

#: Fewer Devanagari words than this and there is not enough signal to judge.
MIN_WORDS = 40


@dataclass
class TextIntegrity:
    """What the text layer looks like, and whether it can be read."""

    devanagari_words: int = 0
    leading_matra: int = 0
    double_matra: int = 0
    trailing_virama: int = 0
    reliable: bool = True
    reason: str = ""

    @property
    def corruption_ratio(self) -> float:
        if not self.devanagari_words:
            return 0.0
        broken = self.leading_matra + self.double_matra + self.trailing_virama
        return broken / self.devanagari_words


def check_text_integrity(text: str) -> TextIntegrity:
    """Judge one document's extracted text.

    A document with no Devanagari always comes back reliable: this failure mode
    is specific to complex scripts, and Latin text extracts faithfully.
    """
    words = _DEVANAGARI_WORD_RE.findall(text or "")
    result = TextIntegrity(devanagari_words=len(words))
    if len(words) < MIN_WORDS:
        return result

    result.leading_matra = sum(1 for w in words if _LEADING_MATRA_RE.match(w))
    result.double_matra = sum(1 for w in words if _DOUBLE_MATRA_RE.search(w))
    result.trailing_virama = sum(1 for w in words if w.endswith(_VIRAMA))

    if result.corruption_ratio > CORRUPTION_THRESHOLD:
        result.reliable = False
        result.reason = (
            f"{result.corruption_ratio:.0%} of the Devanagari words in this PDF's "
            "text layer contain letter sequences Hindi does not allow "
            f"({result.leading_matra} start with a matra, {result.double_matra} carry "
            f"two matras in a row, {result.trailing_virama} end in a halant). The "
            "text was recovered in the order the glyphs are drawn rather than the "
            "order they were typed, which is what an Identity-H font subset does to "
            "Devanagari. The words on the page are correct; only the copyable text "
            "behind them is scrambled."
        )
    return result
