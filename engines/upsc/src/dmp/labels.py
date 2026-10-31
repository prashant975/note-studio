"""Every word the DMP feature shows a person, in the language of the notes.

The questions already followed the note's language — they are generated from a
prompt that locks it. The scaffolding around them did not: Hindi questions
arrived inside "Easy / Direct", "Generate Model Answers", "Download Question
Paper". A practice set half in one language reads as a bug in the product, and
the half that is English is the half a Hindi-medium student cannot use.

So the labels live here, keyed by language, and everything — the panel, the
question paper, the answer sheet — reads them through :func:`ui` and
:func:`level_label`. Adding a fourth language is adding one dict, not hunting
for strings in a Streamlit page.

An unknown language falls back to English, which is what every other module in
this project does with an unrecognised code.
"""
from __future__ import annotations

from ..languages import normalise

#: The three difficulty levels. The Hindi names are the faculty's own, taken
#: from the same spreadsheet the prompts came from (सीधे और बुनियादी प्रश्न,
#: मध्यम स्तर के प्रश्न, उच्च स्तरीय / व्यावहारिक प्रश्न).
LEVELS: dict[str, dict[str, str]] = {
    "en": {
        "easy": "Easy / Direct",
        "moderate": "Moderate / Multi-layered",
        "applied": "Applied / Analytical",
    },
    "hi": {
        "easy": "सरल / प्रत्यक्ष",
        "moderate": "मध्यम स्तर",
        "applied": "उच्च स्तरीय / व्यावहारिक",
    },
    "mr": {
        "easy": "सोपे / थेट",
        "moderate": "मध्यम स्तर",
        "applied": "उच्च स्तरीय / उपयोजित",
    },
}

#: Everything else the panel and the exported sheets print.
#:
#: ``{n}``-style placeholders are filled by the caller. They are named rather
#: than positional so a translator can reorder them — Devanagari word order is
#: not English word order, and "4 of 5 answered" does not survive a literal
#: rendering.
UI: dict[str, dict[str, str]] = {
    "en": {
        "panel_title": "Daily Mains Practice (DMP)",
        "intro": ("UPSC Mains practice questions written from these notes. Tick "
                  "the ones you want answered — answers are generated only for "
                  "those, and your set is saved so you can finish the rest later."),
        "generate_questions": "Generate Practice Questions",
        "generate_answers": "Generate Model Answers ({n} selected)",
        "new_set": "New practice set",
        "counts": "**{total} questions** · {answered} answered · {pending} to go",
        "level_progress": "{level} — {answered} of {total} answered",
        "answered_badge": "Answered",
        "model_answer": "Model answer",
        "tick_hint": "Tick a question above to have it answered.",
        "history": "Previous practice sets",
        "history_row": "{when} — {answered} of {total} answered",
        "download_questions": "Download Question Paper",
        "download_answers": "Download Answers",
        "marks": "{marks} marks ({words} words)",
        "spinner_questions": "Writing practice questions from these notes...",
        "spinner_answers": "Writing {n} model answer(s)...",
        "questions_time": "Questions written in {duration}",
        "answers_time": "Answers written in {duration}",
        "no_notes": ("Practice questions need the notes' run folder, which is not "
                     "on this machine any more."),
        "sign_in_questions": "Sign in again before generating practice questions.",
        "sign_in_answers": "Sign in again before generating model answers.",
        "failed_questions": ("Practice questions could not be generated just now. "
                             "Please try again."),
        "failed_answers": ("The model answers could not be generated just now. "
                           "Please try again."),
        "sheet_failed": "That sheet could not be built. Please try again.",
        # The three boxes over the downloads: what the sheets print in the
        # blue title box ("<subject> DMP <No>") and the cream box under it.
        "sheet_subject": "Subject printed on the sheets",
        "sheet_topic": "Topic printed under it",
        "sheet_number": "DMP No.",
        "over_limit": ("This answer is {words} words, well past the {limit}-word "
                       "limit for {marks} marks. It is printed in full; trim it "
                       "before handing it out."),
    },
    "hi": {
        "panel_title": "डेली मेन्स प्रैक्टिस (DMP)",
        "intro": ("इन नोट्स से बनाए गए UPSC मुख्य परीक्षा के अभ्यास प्रश्न। जिन प्रश्नों "
                  "के उत्तर चाहिए उन्हें चुनें — उत्तर केवल उन्हीं के बनेंगे, और आपका सेट "
                  "सुरक्षित रहेगा ताकि बाकी प्रश्न बाद में हल किए जा सकें।"),
        "generate_questions": "अभ्यास प्रश्न बनाएँ",
        "generate_answers": "मॉडल उत्तर बनाएँ ({n} चयनित)",
        "new_set": "नया अभ्यास सेट",
        "counts": "**{total} प्रश्न** · {answered} उत्तरित · {pending} शेष",
        "level_progress": "{level} — {total} में से {answered} उत्तरित",
        "answered_badge": "उत्तरित",
        "model_answer": "मॉडल उत्तर",
        "tick_hint": "उत्तर पाने के लिए ऊपर कोई प्रश्न चुनें।",
        "history": "पिछले अभ्यास सेट",
        "history_row": "{when} — {total} में से {answered} उत्तरित",
        "download_questions": "प्रश्न पत्र डाउनलोड करें",
        "download_answers": "उत्तर डाउनलोड करें",
        "marks": "{marks} अंक ({words} शब्द)",
        "spinner_questions": "इन नोट्स से अभ्यास प्रश्न बनाए जा रहे हैं...",
        "spinner_answers": "{n} मॉडल उत्तर बनाए जा रहे हैं...",
        "questions_time": "प्रश्न {duration} में तैयार हुए",
        "answers_time": "उत्तर {duration} में तैयार हुए",
        "no_notes": ("अभ्यास प्रश्नों के लिए नोट्स का रन फ़ोल्डर चाहिए, जो अब इस मशीन "
                     "पर उपलब्ध नहीं है।"),
        "sign_in_questions": "अभ्यास प्रश्न बनाने से पहले दोबारा साइन इन करें।",
        "sign_in_answers": "मॉडल उत्तर बनाने से पहले दोबारा साइन इन करें।",
        "failed_questions": "अभी अभ्यास प्रश्न नहीं बन सके। कृपया पुनः प्रयास करें।",
        "failed_answers": "अभी मॉडल उत्तर नहीं बन सके। कृपया पुनः प्रयास करें।",
        "sheet_failed": "यह शीट नहीं बन सकी। कृपया पुनः प्रयास करें।",
        "sheet_subject": "शीट पर छपने वाला विषय",
        "sheet_topic": "उसके नीचे छपने वाला टॉपिक",
        "sheet_number": "DMP नं.",
        "over_limit": ("यह उत्तर {words} शब्दों का है — {marks} अंकों की {limit} शब्द "
                       "सीमा से काफ़ी लंबा। यह पूरा छापा जाएगा; बाँटने से पहले "
                       "इसे छोटा कर लें।"),
    },
    "mr": {
        "panel_title": "डेली मेन्स प्रॅक्टिस (DMP)",
        "intro": ("या नोट्सवरून तयार केलेले UPSC मुख्य परीक्षेचे सराव प्रश्न. ज्या प्रश्नांची "
                  "उत्तरे हवी आहेत ते निवडा — उत्तरे फक्त त्यांचीच तयार होतील, आणि तुमचा "
                  "संच जतन राहील जेणेकरून उरलेले प्रश्न नंतर सोडवता येतील."),
        "generate_questions": "सराव प्रश्न तयार करा",
        "generate_answers": "नमुना उत्तरे तयार करा ({n} निवडलेले)",
        "new_set": "नवीन सराव संच",
        "counts": "**{total} प्रश्न** · {answered} उत्तरित · {pending} बाकी",
        "level_progress": "{level} — {total} पैकी {answered} उत्तरित",
        "answered_badge": "उत्तरित",
        "model_answer": "नमुना उत्तर",
        "tick_hint": "उत्तर मिळवण्यासाठी वरील प्रश्न निवडा.",
        "history": "मागील सराव संच",
        "history_row": "{when} — {total} पैकी {answered} उत्तरित",
        "download_questions": "प्रश्नपत्रिका डाउनलोड करा",
        "download_answers": "उत्तरे डाउनलोड करा",
        "marks": "{marks} गुण ({words} शब्द)",
        "spinner_questions": "या नोट्सवरून सराव प्रश्न तयार होत आहेत...",
        "spinner_answers": "{n} नमुना उत्तरे तयार होत आहेत...",
        "questions_time": "प्रश्न {duration} मध्ये तयार झाले",
        "answers_time": "उत्तरे {duration} मध्ये तयार झाली",
        "no_notes": ("सराव प्रश्नांसाठी नोट्सचे रन फोल्डर आवश्यक आहे, जे आता या मशीनवर "
                     "उपलब्ध नाही."),
        "sign_in_questions": "सराव प्रश्न तयार करण्यापूर्वी पुन्हा साइन इन करा.",
        "sign_in_answers": "नमुना उत्तरे तयार करण्यापूर्वी पुन्हा साइन इन करा.",
        "failed_questions": "सध्या सराव प्रश्न तयार होऊ शकले नाहीत. कृपया पुन्हा प्रयत्न करा.",
        "failed_answers": "सध्या नमुना उत्तरे तयार होऊ शकली नाहीत. कृपया पुन्हा प्रयत्न करा.",
        "sheet_failed": "ही शीट तयार होऊ शकली नाही. कृपया पुन्हा प्रयत्न करा.",
        "sheet_subject": "शीटवर छापला जाणारा विषय",
        "sheet_topic": "त्याखाली छापला जाणारा टॉपिक",
        "sheet_number": "DMP क्र.",
        "over_limit": ("हे उत्तर {words} शब्दांचे आहे — {marks} गुणांच्या {limit} "
                       "शब्दमर्यादेपेक्षा बरेच मोठे. ते पूर्ण छापले जाईल; वाटण्यापूर्वी "
                       "ते लहान करा."),
    },
}


def _key(language_code: str | None) -> str:
    code = normalise(language_code) or "en"
    return code if code in UI else "en"


def ui(language_code: str | None = "en") -> dict[str, str]:
    """Every UI string for one language. Unknown codes read English."""
    return UI[_key(language_code)]


def level_label(level: str, language_code: str | None = "en") -> str:
    """"Easy / Direct" / "सरल / प्रत्यक्ष" / "सोपे / थेट"."""
    table = LEVELS[_key(language_code)]
    return table.get(level, LEVELS["en"].get(level, level))


def text(key: str, language_code: str | None = "en", **fields) -> str:
    """One string, filled in. Falls back to English before it falls over —
    a missing translation should degrade to a readable label, never to a
    KeyError in the middle of a rendered page."""
    table = ui(language_code)
    template = table.get(key) or UI["en"].get(key, key)
    try:
        return template.format(**fields)
    except (KeyError, IndexError):
        return template
