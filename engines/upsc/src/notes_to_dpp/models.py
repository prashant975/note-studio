from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Difficulty(str, Enum):
    EASY = "Easy"
    MEDIUM = "Medium"
    HARD = "Hard"


class Language(str, Enum):
    ENGLISH = "English"
    HINDI = "Hindi"
    UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True)
class LanguageDetection:
    language: Language
    confidence: float
    devanagari_letters: int
    latin_letters: int


@dataclass(frozen=True, slots=True)
class LocalizedQuestion:
    language: Language
    prompt: str
    options: tuple[str, ...] = ()
    correct_option_indexes: tuple[int, ...] = ()
    solution: str | None = None
    # The printable prompt remains plain text for matching, selection and
    # previews.  A table-bearing source stem additionally keeps its original
    # HTML here so the PDF exporter can reconstruct the table instead of
    # printing its cells as an ambiguous vertical list.
    prompt_markup: str | None = None


@dataclass(frozen=True, slots=True)
class TaxonomyRow:
    row_id: str
    subject: str
    subject_id: str
    chapter: str
    chapter_id: str
    topic: str
    topic_id: str
    subtopic: str | None = None
    subtopic_id: str | None = None

    @property
    def composite_key(self) -> tuple[str, str, str, str | None]:
        return (self.subject_id, self.chapter_id, self.topic_id, self.subtopic_id)

    @property
    def path(self) -> str:
        parts = [self.subject, self.chapter, self.topic]
        if self.subtopic:
            parts.append(self.subtopic)
        return " › ".join(parts)


@dataclass(frozen=True, slots=True)
class QuestionTag:
    subject_id: str
    chapter_id: str
    topic_id: str
    subtopic_id: str | None = None

    @property
    def composite_key(self) -> tuple[str, str, str, str | None]:
        return (self.subject_id, self.chapter_id, self.topic_id, self.subtopic_id)


@dataclass(frozen=True, slots=True)
class QuestionRecord:
    question_id: str
    prompt: str
    options: tuple[str, ...]
    difficulty: Difficulty
    tags: tuple[QuestionTag, ...]
    source_tags: tuple[str, ...]
    content_fingerprint: str
    verified: bool
    localized_content: tuple[LocalizedQuestion, ...] = ()

    def content_for(self, language: Language) -> LocalizedQuestion | None:
        """Return content in exactly ``language`` without cross-language fallback."""
        for localized in self.localized_content:
            if localized.language is language:
                return localized

        # Records constructed by pre-localization callers represent the legacy
        # English view. Keep them usable while making localized workbook rows
        # strict: once localized_content exists, absent languages stay absent.
        if not self.localized_content and language is Language.ENGLISH:
            return LocalizedQuestion(
                language=Language.ENGLISH,
                prompt=self.prompt,
                options=self.options,
            )
        return None

    def supports_language(self, language: Language) -> bool:
        localized = self.content_for(language)
        return bool(
            localized
            and localized.prompt.strip()
            and localized.prompt != "Question text unavailable"
            and localized.options
            and localized.solution
            and localized.solution.strip()
        )


@dataclass(frozen=True, slots=True)
class NotePage:
    page_number: int
    text: str
    extraction_method: str


@dataclass(frozen=True, slots=True)
class NoteDocument:
    filename: str
    pages: tuple[NotePage, ...]
    warnings: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages if page.text)

    @property
    def ocr_page_count(self) -> int:
        return sum(page.extraction_method == "ocr" for page in self.pages)


@dataclass(frozen=True, slots=True)
class CoverageCandidate:
    row_id: str
    confidence: float
    evidence: str
    page_number: int
    method: str
    recommended: bool


@dataclass(frozen=True, slots=True)
class SelectionSlot:
    slot_id: str
    difficulty: Difficulty
    question_id: str
    preferred_row_id: str
