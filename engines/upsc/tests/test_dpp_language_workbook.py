from __future__ import annotations

import json

from src.notes_to_dpp import models
from src.notes_to_dpp.workbooks import load_question_bank


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def test_question_bank_parses_hindi_and_bilingual_question_solution_fields(
    xlsx_bytes,
) -> None:
    headers = [
        "unique_id",
        "qbgid",
        "content",
        "solutions",
        "bilingual_options",
        "difficulty",
        "subject_id",
        "chapter_id",
        "topic_id",
        "subtopic_id",
        "qcm_tag",
        "verification_status",
        "langgg",
    ]
    hindi_prompt = "भारत की सबसे लंबी नदी कौन-सी है?"
    hindi_solution = "उत्तर: गंगा। यह भारत की प्रमुख नदी है।"
    bilingual_hindi_prompt = "हिमालय किस प्रकार का पर्वत है?"
    bilingual_english_prompt = "What type of mountain is the Himalaya?"
    rows = [
        [
            "U-HI",
            "Q-HI",
            _json({"hindi": f"<p><strong>{hindi_prompt}</strong></p>"}),
            _json(
                [
                    {
                        "hindi": {
                            "text": f"<p>{hindi_solution}</p>",
                            "videoSolution": {"type": 0, "url": ""},
                            "otherSolution": "<p>--Not Available--</p>",
                        }
                    }
                ]
            ),
            _json(
                {
                    "hindi": [
                        {"isCorrect": True, "text": "<p>गंगा</p>"},
                        {"isCorrect": False, "text": "<p>गोदावरी</p>"},
                    ]
                }
            ),
            1,
            "S-1",
            "C-1",
            "T-1",
            None,
            "DPP",
            "Verified",
            "Hindi",
        ],
        [
            "U-BI",
            "Q-BI",
            _json(
                {
                    "english": f"<p>{bilingual_english_prompt}</p>",
                    "hindi": f"<p>{bilingual_hindi_prompt}</p>",
                }
            ),
            # Language entries are intentionally reversed: parsing must use keys,
            # not list position.
            _json(
                [
                    {"hindi": {"text": "<p>उत्तर: नवीन वलित पर्वत।</p>"}},
                    {"english": {"text": "<p>Answer: Young fold mountain.</p>"}},
                ]
            ),
            _json(
                {
                    "english": [
                        {"isCorrect": True, "text": "<p>Young fold mountain</p>"},
                        {"isCorrect": False, "text": "<p>Block mountain</p>"},
                    ],
                    "hindi": [
                        {"isCorrect": False, "text": "<p>ब्लॉक पर्वत</p>"},
                        {"isCorrect": True, "text": "<p>नवीन वलित पर्वत</p>"},
                    ],
                }
            ),
            2,
            "S-1",
            "C-2",
            "T-2",
            None,
            "Infinite Practice",
            "yes",
            "English-Hindi",
        ],
    ]

    result = load_question_bank(xlsx_bytes(headers, rows, sheet_name="Questions"))
    questions = {question.question_id: question for question in result.questions}
    hindi = {
        item.language: item for item in questions["Q-HI"].localized_content
    }
    bilingual = {
        item.language: item for item in questions["Q-BI"].localized_content
    }

    assert set(hindi) == {models.Language.HINDI}
    assert hindi[models.Language.HINDI].prompt == hindi_prompt
    assert hindi[models.Language.HINDI].options == ("गंगा", "गोदावरी")
    assert hindi[models.Language.HINDI].correct_option_indexes == (0,)
    assert hindi[models.Language.HINDI].solution == hindi_solution

    assert set(bilingual) == {models.Language.ENGLISH, models.Language.HINDI}
    assert bilingual[models.Language.ENGLISH].prompt == bilingual_english_prompt
    assert bilingual[models.Language.ENGLISH].correct_option_indexes == (0,)
    assert bilingual[models.Language.ENGLISH].solution == "Answer: Young fold mountain."
    assert bilingual[models.Language.HINDI].prompt == bilingual_hindi_prompt
    assert bilingual[models.Language.HINDI].correct_option_indexes == (1,)
    assert bilingual[models.Language.HINDI].solution == "उत्तर: नवीन वलित पर्वत।"

    # Existing consumers retain a deterministic primary-language view.
    assert questions["Q-HI"].prompt == hindi_prompt
    assert questions["Q-BI"].prompt == bilingual_english_prompt
    assert ("solutions", "solutions") in result.column_mapping
    assert ("language", "langgg") in result.column_mapping
