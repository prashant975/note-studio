"""Decide which language the notes should be written in.

Two sources, and they can disagree:

* **Slides** — often English even in a Hindi-medium class, so they alone are a
  poor guide to the class's medium.
* **Transcript** — reflects what the teacher actually spoke, which is what the
  student is studying in. This is the stronger signal, so it wins when present.

The catch: YouTube's Hindi ASR writes *English speech in Devanagari*
("गवर्नेंस" = governance), so a Devanagari-vs-Latin ratio calls every such
transcript "Hindi". To see through that, Devanagari text is scored on function
words — genuine Hindi grammar words (है, में, नहीं) versus transliterated English
ones (टू, एंड, ऑफ). Function words are used because they are frequent, short, and
cannot be avoided in either language, while topic words are shared by both.
"""
from __future__ import annotations

import re

# Devanagari block (Hindi). Latin letters cover the English side.
DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
LATIN_RE = re.compile(r"[A-Za-z]")

# Share of Devanagari (out of Devanagari + Latin letters) at/above which text is
# treated as Hindi. Deliberately low: genuinely English text has almost no
# Devanagari, while Hindi text carries plenty of English terms.
HINDI_THRESHOLD = 0.15

# Below this many letters there isn't enough signal to judge.
MIN_LETTERS = 30

# Devanagari tokens that are really English function words, transliterated by
# YouTube's Hindi ASR. Deliberately excludes forms that are also real Hindi
# words — e.g. "ही" (he / emphatic particle), "दे" (they / "give"), "सो"
# (so / "sleep"), "आई" (I / "came"), "इन" (in / "these") — which would otherwise
# score English hits inside ordinary Hindi speech.
TRANSLITERATED_ENGLISH_MARKERS = frozenset({
    "द", "वी", "विल", "आवर", "इज", "आर", "ऑफ", "ऑफ़", "एंड", "टू", "दिस", "दैट",
    "हैव", "हैज", "विद", "फॉर", "ऑन", "इट", "नॉट", "बट", "व्हाट", "व्हिच", "यू",
    "कैन", "कुड", "शुड", "वुड", "देयर", "व्हेन", "वेयर", "हाउ", "ऑल", "बिकॉज़",
    "बिकॉज", "इफ", "एज", "फ्रॉम", "बाय", "आल्सो", "देन", "दैन", "वेरी", "ओनली",
    "अबाउट", "वेल", "मस्ट", "मे", "बीन", "वाज़", "वर", "आफ्टर", "बिफोर",
})

# Common Hindi grammar words. These have no English homograph in Devanagari.
HINDI_FUNCTION_MARKERS = frozenset({
    "है", "हैं", "का", "की", "के", "को", "में", "से", "और", "यह", "वह", "कि",
    "नहीं", "पर", "तो", "ने", "हो", "था", "थे", "थी", "कर", "जो", "अब", "भी",
    "एक", "लिए", "गया", "गई", "रहा", "रही", "रहे", "क्या", "जब", "तब", "सकते",
    "सकता", "चाहिए", "हम", "आप", "कोई", "कुछ", "जैसे", "दिया", "करते", "करना",
    "होता", "होती", "अगर", "लेकिन", "क्योंकि", "यहां", "वहां", "इसलिए", "बहुत",
})

# At/above this share of genuine-Hindi function words, Devanagari speech is
# treated as a Hindi-medium class rather than transliterated English.
HINDI_SPEECH_THRESHOLD = 0.50

# Fewer marker hits than this is too thin to trust.
MIN_MARKER_HITS = 20


def hindi_ratio(text: str) -> float:
    """Share of Devanagari letters among all Devanagari+Latin letters (0.0-1.0)."""
    devanagari = len(DEVANAGARI_RE.findall(text or ""))
    latin = len(LATIN_RE.findall(text or ""))
    total = devanagari + latin
    if not total:
        return 0.0
    return devanagari / total


def detect_language(text: str = "", default: str = "en") -> str:
    """Script-based detection, suitable for slides/handouts.

    Not reliable for ASR transcripts of Hinglish speech — use
    detect_spoken_language for those.
    """
    devanagari = len(DEVANAGARI_RE.findall(text or ""))
    latin = len(LATIN_RE.findall(text or ""))
    if devanagari + latin < MIN_LETTERS:
        return default
    return "hi" if hindi_ratio(text) >= HINDI_THRESHOLD else "en"


def hindi_speech_share(text: str) -> float | None:
    """Share of genuine-Hindi function words among Devanagari function words.

    ~1.0 = the teacher is speaking Hindi; ~0.0 = English transliterated into
    Devanagari by the ASR. Returns None when there aren't enough markers.
    """
    tokens = (text or "").split()
    if not tokens:
        return None
    # Strip punctuation that would stop a marker from matching.
    cleaned = [t.strip("।,.?!\"'()[]{}:;") for t in tokens]
    hindi = sum(1 for t in cleaned if t in HINDI_FUNCTION_MARKERS)
    english = sum(1 for t in cleaned if t in TRANSLITERATED_ENGLISH_MARKERS)
    if hindi + english < MIN_MARKER_HITS:
        return None
    return hindi / (hindi + english)


def detect_spoken_language(transcript: str, default: str = "en") -> tuple[str | None, str]:
    """Detect the language a class was *taught* in from its transcript.

    Returns (language_code_or_None, reason). None means "not confident" so the
    caller can fall back to the slides.
    """
    if not (transcript or "").strip():
        return None, "no transcript"

    ratio = hindi_ratio(transcript)
    # A mostly-Latin transcript is a plain English caption — believe it.
    if ratio < HINDI_THRESHOLD:
        return "en", "transcript is in English"

    # Devanagari transcript: could be real Hindi, or English transliterated by
    # the ASR. Function words tell them apart.
    share = hindi_speech_share(transcript)
    if share is None:
        return None, "transcript too short to judge"
    if share >= HINDI_SPEECH_THRESHOLD:
        return "hi", f"class is taught in Hindi ({share:.0%} Hindi speech)"
    return "en", f"class is taught in English, transliterated by the captions ({1 - share:.0%} English speech)"


def detect_notes_language(
    slide_text: str = "",
    transcript_text: str = "",
    default: str = "en",
) -> tuple[str, str]:
    """Pick the notes language from the class transcript and the slides.

    The transcript wins when it is confident: PW decks are frequently in English
    even when the class is taught in Hindi, and the student needs notes in the
    language they are actually learning in. The slides are the fallback.
    """
    spoken, reason = detect_spoken_language(transcript_text, default=default)
    if spoken is not None:
        return spoken, reason

    slide_lang = detect_language(slide_text, default=default)
    return slide_lang, "from slides (no usable transcript signal)"
