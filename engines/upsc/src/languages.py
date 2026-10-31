"""The languages notes can be written in, and the facts that follow from that.

Before this module the answer to "is this a Hindi run?" was
``language_code.startswith("hi")``, written out at fourteen call sites. Each one
was really asking one of three different questions — what script does this use,
what do I call this language to the model, which glossary do I load — and they
all happened to have the same answer while there were only two languages.

Adding Marathi separated them: it is Devanagari like Hindi, so it needs the same
font and the same script validation, but it is NOT Hindi and must never be
described to the model as Hindi or given Hindi instruction text. Asking the
right question of one module is what keeps the third language from being a
fourteen-way edit that gets one branch wrong.

``normalise`` returns "" for anything it does not recognise — including the
"auto" sentinel, which means "follow the lecture" and is resolved by detection
long before anything here is asked. Callers treat "" as English, which is what
they did before.
"""
from __future__ import annotations

#: Note languages this build can write, in the order the UI offers them.
SUPPORTED: tuple[str, ...] = ("en", "hi", "mr")

#: What to call each language TO THE MODEL. English names on purpose: the
#: prompts are written in English and a model follows "Write in Marathi" more
#: reliably than it follows a name in a script it is being asked to produce.
NAMES: dict[str, str] = {"en": "English", "hi": "Hindi", "mr": "Marathi"}

#: What to call each language to the USER, in its own script.
ENDONYMS: dict[str, str] = {"en": "English", "hi": "हिंदी", "mr": "मराठी"}

#: Written in Devanagari. Drives font selection (Kalam), the complex-script
#: run properties in the DOCX, and the Latin-script leak check — all three of
#: which are about the SCRIPT, not about which language uses it.
_DEVANAGARI: frozenset[str] = frozenset({"hi", "mr"})

#: Accepted spellings of each code, so a caller passing "hi-IN", "Marathi" or
#: "MR" lands on the same language as one passing "mr".
_ALIASES: dict[str, str] = {
    "en": "en", "eng": "en", "english": "en",
    "hi": "hi", "hin": "hi", "hindi": "hi",
    "mr": "mr", "mar": "mr", "marathi": "mr",
}


def normalise(language_code: str | None) -> str:
    """One of SUPPORTED, or "" for unknown, empty and "auto".

    Prefix matching handles the regional tags transcription engines return
    ("hi-IN", "mr_IN"). It is applied to the *base* subtag only, so "marathi"
    cannot be read as "mar" + noise and "hindi" cannot be read as an alias of
    something else that happens to start with the same two letters.
    """
    raw = (language_code or "").strip().lower()
    if not raw:
        return ""
    base = raw.replace("_", "-").split("-", 1)[0]
    return _ALIASES.get(base, _ALIASES.get(raw, ""))


def is_supported(language_code: str | None) -> bool:
    return normalise(language_code) in SUPPORTED


def is_devanagari(language_code: str | None) -> bool:
    """Does this language print in Devanagari?

    Ask this — not "is it Hindi" — wherever the question is really about the
    font, the complex-script run properties, or whether Latin text in the
    output is a leak worth reporting.
    """
    return normalise(language_code) in _DEVANAGARI


def language_name(language_code: str | None) -> str:
    """The name to use when instructing the model. Defaults to English."""
    return NAMES.get(normalise(language_code), NAMES["en"])


def endonym(language_code: str | None) -> str:
    """The name to show a person, in their own script."""
    return ENDONYMS.get(normalise(language_code), ENDONYMS["en"])


def script_name(language_code: str | None) -> str:
    """The script to name in a prompt, so "write in Marathi" cannot be read as
    "write Marathi words in Roman letters"."""
    return "Devanagari" if is_devanagari(language_code) else "Latin"


#: One worked example per Devanagari language. GPT follows a shown example far
#: more reliably than an abstract rule, and the example is what stops it
#: leaving English words in the running text.
_VOCABULARY_EXAMPLES = {
    "hi": (
        'RIGHT: "सरकार ने राजकोषीय घाटा (Fiscal Deficit) कम करने के लिए व्यय में '
        'कटौती की।"  WRONG: "सरकार ने फिस्कल डेफिसिट कम करने के लिए expenditure '
        'में कटौती की।"'
    ),
    "mr": (
        'RIGHT: "सरकारने राजकोषीय तूट (Fiscal Deficit) कमी करण्यासाठी खर्चात कपात '
        'केली."  WRONG: "सरकारने फिस्कल डेफिसिट कमी करण्यासाठी expenditure मध्ये '
        'कपात केली."'
    ),
}


def vocabulary_rule(language_code: str | None) -> str:
    """How a Devanagari output treats English words: pure vocabulary, with the
    English term in brackets only where the native term is hard.

    The old rule said "keep technical terms in their standard form", which GPT
    reads as "keep them in English" and so writes Hinglish. Empty for English.
    """
    code = normalise(language_code)
    if not is_devanagari(code):
        return ""
    name = language_name(code)
    return (
        f" VOCABULARY: use only {name} words written in Devanagari — standard {name} "
        f"exam vocabulary (e.g. संसद, अनुच्छेद, मौलिक अधिकार), never English words "
        f"in Roman letters and never English words transliterated into Devanagari "
        f"(no गवर्नेंस, पॉलिसी, इकोनॉमी). When a {name} term is difficult or less "
        f"familiar, write the English term in brackets right after it the FIRST time "
        f"it appears, then use the {name} term alone. Only proper nouns, "
        f"abbreviations and official names (UNICEF, GST, PM-KISAN) stay as they are. "
        f"{_VOCABULARY_EXAMPLES.get(code, '')}"
    ).rstrip()
