from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from typing import Iterable

from openpyxl import load_workbook

from .models import (
    Difficulty,
    Language,
    LocalizedQuestion,
    QuestionRecord,
    QuestionTag,
    TaxonomyRow,
)


class WorkbookValidationError(ValueError):
    """Raised when a master workbook cannot satisfy the closed-set contract."""


@dataclass(frozen=True, slots=True)
class TaxonomyLoadResult:
    rows: tuple[TaxonomyRow, ...]
    sheet_name: str
    source_row_count: int
    warnings: tuple[str, ...]
    column_mapping: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class QuestionBankLoadResult:
    questions: tuple[QuestionRecord, ...]
    sheet_name: str
    source_row_count: int
    accepted_row_count: int
    excluded_row_count: int
    warnings: tuple[str, ...]
    column_mapping: tuple[tuple[str, str], ...]


_TAXONOMY_ALIASES: dict[str, tuple[str, ...]] = {
    "row_id": ("rowid", "row id"),
    "subject": ("subject name", "subject_name", "subject"),
    "subject_id": ("subject id", "subject_id", "subjectid"),
    "chapter": ("chapter name", "chapter_name", "chapter"),
    "chapter_id": ("chapter id", "chapter_id", "chapterid"),
    "topic": ("topic name", "topic_name", "topic"),
    "topic_id": ("topic id", "topic_id", "topicid"),
    "subtopic": ("subtopic name", "subtopic_name", "subtopic"),
    "subtopic_id": ("subtopic id", "subtopic_id", "subtopicid"),
}

_QUESTION_ALIASES: dict[str, tuple[str, ...]] = {
    "unique_id": ("unique_id", "unique id", "uniqueid"),
    "qbgid": ("qbgid", "qbg id", "qbg_id"),
    "content": ("content", "question", "question content"),
    "solutions": ("solutions", "solution", "explanation"),
    "options": ("bilingual_options", "bilingual options", "options"),
    "difficulty": ("difficulty", "difficulty id", "difficulty_id"),
    "subject_id": ("subject_id", "subject id", "subjectid"),
    "chapter_id": ("chapter_id", "chapter id", "chapterid"),
    "topic_id": ("topic_id", "topic id", "topicid"),
    "subtopic_id": ("subtopic_id", "subtopic id", "subtopicid"),
    "source_tag": ("qcm_tag", "qcm tag", "source tag"),
    "language": ("langgg", "language", "language flag", "lang"),
    "verification_status": (
        "verification_status",
        "verification status",
        "verified",
    ),
}

_SOURCE_WHITELIST = frozenset({"dpp", "infinite practice"})
_DIFFICULTY_MAP = {
    "1": Difficulty.EASY,
    "easy": Difficulty.EASY,
    "e": Difficulty.EASY,
    "2": Difficulty.MEDIUM,
    "medium": Difficulty.MEDIUM,
    "m": Difficulty.MEDIUM,
    "3": Difficulty.HARD,
    "hard": Difficulty.HARD,
    "difficult": Difficulty.HARD,
    "d": Difficulty.HARD,
    "h": Difficulty.HARD,
}


def _clean(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def _header_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _resolve_columns(
    headers: Iterable[object],
    aliases: dict[str, tuple[str, ...]],
    *,
    optional: frozenset[str] = frozenset(),
) -> dict[str, int | None]:
    header_list = [str(value or "").strip() for value in headers]
    normalized: dict[str, int] = {}
    for index, header in enumerate(header_list):
        normalized.setdefault(_header_key(header), index)

    resolved: dict[str, int | None] = {}
    missing: list[str] = []
    for canonical, candidates in aliases.items():
        index = next(
            (
                normalized[_header_key(candidate)]
                for candidate in candidates
                if _header_key(candidate) in normalized
            ),
            None,
        )
        resolved[canonical] = index
        if index is None and canonical not in optional:
            missing.append(canonical)
    if missing:
        raise WorkbookValidationError(
            "Missing required columns: "
            + ", ".join(missing)
            + f". Found headers: {header_list}"
        )
    return resolved


def _mapping_report(
    headers: list[str], resolved: dict[str, int | None]
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (canonical, headers[index] if index is not None else "<generated/optional>")
        for canonical, index in resolved.items()
    )


def _open_first_sheet(data: bytes):
    if not data:
        raise WorkbookValidationError("The uploaded workbook is empty.")
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl gives several format-specific exception types
        raise WorkbookValidationError(f"Unable to read XLSX workbook: {exc}") from exc
    if not workbook.sheetnames:
        raise WorkbookValidationError("The workbook has no worksheets.")
    return workbook, workbook[workbook.sheetnames[0]]


def load_taxonomy(data: bytes) -> TaxonomyLoadResult:
    workbook, sheet = _open_first_sheet(data)
    iterator = sheet.iter_rows(values_only=True)
    try:
        header_values = next(iterator)
    except StopIteration as exc:
        raise WorkbookValidationError("The taxonomy workbook is empty.") from exc
    headers = [str(value or "").strip() for value in header_values]
    columns = _resolve_columns(
        headers,
        _TAXONOMY_ALIASES,
        optional=frozenset({"row_id"}),
    )

    rows: list[TaxonomyRow] = []
    composite_keys: set[tuple[str, str, str, str | None]] = set()
    row_ids: set[str] = set()
    warnings: list[str] = []
    source_rows = 0

    for excel_row, values in enumerate(iterator, start=2):
        if not any(_clean(value) for value in values):
            continue
        source_rows += 1

        def value(name: str) -> str | None:
            index = columns[name]
            return _clean(values[index]) if index is not None and index < len(values) else None

        subject = value("subject")
        subject_id = value("subject_id")
        chapter = value("chapter")
        chapter_id = value("chapter_id")
        topic = value("topic")
        topic_id = value("topic_id")
        subtopic = value("subtopic")
        subtopic_id = value("subtopic_id")
        required = {
            "Subject": subject,
            "Subject Id": subject_id,
            "Chapter": chapter,
            "Chapter Id": chapter_id,
            "Topic": topic,
            "Topic Id": topic_id,
        }
        missing = [name for name, item in required.items() if item is None]
        if missing:
            raise WorkbookValidationError(
                f"Taxonomy row {excel_row} is missing: {', '.join(missing)}."
            )
        if (subtopic is None) != (subtopic_id is None):
            raise WorkbookValidationError(
                f"Taxonomy row {excel_row} must contain both Subtopic and "
                "Subtopic Id, or leave both blank."
            )

        row_id = value("row_id") or f"FINAL:{excel_row}"
        if row_id in row_ids:
            raise WorkbookValidationError(f"Duplicate RowID: {row_id}")

        taxonomy_row = TaxonomyRow(
            row_id=row_id,
            subject=subject or "",
            subject_id=subject_id or "",
            chapter=chapter or "",
            chapter_id=chapter_id or "",
            topic=topic or "",
            topic_id=topic_id or "",
            subtopic=subtopic,
            subtopic_id=subtopic_id,
        )
        if taxonomy_row.composite_key in composite_keys:
            raise WorkbookValidationError(
                f"Duplicate taxonomy hierarchy at source row {excel_row}."
            )
        rows.append(taxonomy_row)
        row_ids.add(row_id)
        composite_keys.add(taxonomy_row.composite_key)

    workbook.close()
    if not rows:
        raise WorkbookValidationError("No taxonomy rows were found.")
    if columns["row_id"] is None:
        warnings.append(
            "The workbook has no RowID column; stable source-row IDs were generated "
            "as FINAL:<Excel row>."
        )
    return TaxonomyLoadResult(
        rows=tuple(rows),
        sheet_name=sheet.title,
        source_row_count=source_rows,
        warnings=tuple(warnings),
        column_mapping=_mapping_report(headers, columns),
    )


#: Wraps a rebuilt list marker so it can be re-attached to the text it belongs
#: to, whichever block tags the source puts between the two.
_MARKER_OPEN = "\x00"
_MARKER_CLOSE = "\x01"
_MARKER = re.compile(f"{_MARKER_OPEN}([^{_MARKER_CLOSE}]*){_MARKER_CLOSE}" + r"\s*")

#: A stem that already types its own "1." inside the <li>; numbering that again
#: would print "1. 1. ...".
_ALREADY_NUMBERED = re.compile(r"\(?\d+\s*[.)\]]")

#: How the bank nests a sub-list: an outer item that is only a container and
#: shows no marker of its own.
_HIDDEN_MARKER = re.compile(r"list-style-type\s*:\s*none", re.IGNORECASE)


def _list_start(attrs) -> int:  # noqa: ANN001
    """The first number of an ``<ol>``, honouring an explicit ``start``.

    Only a real ``start`` attribute counts. The bank's markup is full of the
    ``data-start`` character offsets left behind by the editor that produced
    it, and those have nothing to do with numbering.
    """
    for name, value in attrs or ():
        if str(name).casefold() == "start":
            try:
                return int(str(value).strip())
            except (TypeError, ValueError):
                return 1
    return 1


def _marker_hidden(attrs) -> bool:  # noqa: ANN001
    return any(
        str(name).casefold() == "style" and _HIDDEN_MARKER.search(str(value or ""))
        for name, value in attrs or ()
    )


def _attach_markers(text: str) -> str:
    """Bind each rebuilt marker to the text that follows it."""

    def replace(match: "re.Match[str]") -> str:
        marker = match.group(1)
        following = text[match.end():]
        if not marker or following.startswith(_MARKER_OPEN):
            # An empty list item: a bare marker with nothing to label.
            return ""
        if _ALREADY_NUMBERED.match(following):
            return ""
        return f"{marker} "

    return _MARKER.sub(replace, text)


class _PlainTextParser(HTMLParser):
    """HTML to plain text, keeping the markers that list items are numbered by.

    The bank writes a multi-statement stem as ``<ol><li>`` and then has the
    options refer back to those statements by number ("1 only", "2 and 3
    only"). An ``<ol>`` marker lives in the markup rather than in the text, so
    stripping the tags takes the numbering with it and leaves the options
    pointing at statements that are no longer labelled. Two thirds of the
    bank's questions are written that way, which is why the markers are
    rebuilt here instead of being left for the reader to guess at.
    """

    _BREAK_TAGS = {"br", "p", "div", "li", "ol", "ul", "tr", "h1", "h2", "h3"}
    _LIST_TAGS = {"ol", "ul"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # One [ordered, next number] per open list, so a nested list numbers
        # itself without disturbing the one it sits inside.
        self._lists: list[list] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        name = tag.casefold()
        if name in self._BREAK_TAGS:
            self.parts.append("\n")
        if name in self._LIST_TAGS:
            self._lists.append([name == "ol", _list_start(attrs)])
        elif name == "li" and self._lists:
            self.parts.append(self._marker(attrs))

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name in self._LIST_TAGS and self._lists:
            self._lists.pop()
        if name in self._BREAK_TAGS:
            self.parts.append("\n")

    def _marker(self, attrs) -> str:  # noqa: ANN001
        ordered, number = self._lists[-1]
        if ordered:
            self._lists[-1][1] = number + 1
        if _marker_hidden(attrs):
            return ""
        marker = f"{number}." if ordered else "\u2022"
        return f"{_MARKER_OPEN}{marker}{_MARKER_CLOSE}"

    def handle_data(self, data: str) -> None:
        self.parts.append(
            data.replace(_MARKER_OPEN, "").replace(_MARKER_CLOSE, ""))

    def text(self) -> str:
        joined = html.unescape("".join(self.parts)).replace("\u200b", "")
        joined = _attach_markers(joined)
        lines = [re.sub(r"\s+", " ", line).strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


def _strip_html(value: object) -> str:
    parser = _PlainTextParser()
    try:
        parser.feed(str(value or ""))
        parser.close()
        return parser.text()
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(value or ""))).strip()


_LANGUAGE_KEYS = {
    "english": Language.ENGLISH,
    "en": Language.ENGLISH,
    "hindi": Language.HINDI,
    "hi": Language.HINDI,
}


def _language_from_key(value: object) -> Language | None:
    return _LANGUAGE_KEYS.get(str(value or "").strip().casefold())


def _declared_languages(raw: str | None) -> tuple[Language, ...]:
    normalized = re.sub(r"[^a-z]+", " ", (raw or "").casefold()).split()
    declared: list[Language] = []
    if "english" in normalized or "en" in normalized:
        declared.append(Language.ENGLISH)
    if "hindi" in normalized or "hi" in normalized:
        declared.append(Language.HINDI)
    return tuple(declared)


def _decode_payload(
    raw: str | None,
    field: str,
    diagnostics: Counter[str],
) -> object | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        # Plain HTML/text is a supported legacy representation. A value that
        # advertises itself as JSON but is malformed is not silently reinterpreted.
        if raw.lstrip().startswith(("{", "[")):
            diagnostics[f"malformed_{field}"] += 1
            return None
        diagnostics[f"legacy_plain_{field}"] += 1
        return raw


def _language_values(
    payload: object,
    *,
    declared: tuple[Language, ...],
    field: str,
    diagnostics: Counter[str],
) -> dict[Language, object]:
    if isinstance(payload, dict):
        values: dict[Language, object] = {}
        unsupported = False
        for key, value in payload.items():
            language = _language_from_key(key)
            if language is None:
                unsupported = unsupported or value not in (None, "", [], {})
                continue
            if value not in (None, "", [], {}):
                values[language] = value
        if unsupported:
            diagnostics[f"unsupported_{field}_language"] += 1
        return values

    if payload is None:
        return {}
    if len(declared) == 1:
        return {declared[0]: payload}
    if not declared:
        # Historical workbooks without langgg stored unkeyed English content.
        diagnostics[f"implicit_english_{field}"] += 1
        return {Language.ENGLISH: payload}
    diagnostics[f"ambiguous_unkeyed_{field}"] += 1
    return {}


def _solution_values(
    payload: object,
    *,
    declared: tuple[Language, ...],
    diagnostics: Counter[str],
) -> dict[Language, object]:
    if not isinstance(payload, list):
        return _language_values(
            payload,
            declared=declared,
            field="solutions",
            diagnostics=diagnostics,
        )

    values: dict[Language, object] = {}
    unsupported = False
    for entry in payload:
        if not isinstance(entry, dict):
            if len(declared) == 1 and entry not in (None, "", [], {}):
                values.setdefault(declared[0], entry)
            elif entry not in (None, "", [], {}):
                diagnostics["ambiguous_unkeyed_solutions"] += 1
            continue
        for key, value in entry.items():
            language = _language_from_key(key)
            if language is None:
                unsupported = unsupported or value not in (None, "", [], {})
                continue
            if value in (None, "", [], {}):
                continue
            if language in values and values[language] != value:
                diagnostics[f"multiple_{language.value.casefold()}_solutions"] += 1
                continue
            values[language] = value
    if unsupported:
        diagnostics["unsupported_solutions_language"] += 1
    return values


_IMAGE_TAG = re.compile(r"<img\b", re.IGNORECASE)
_TABLE_TAG = re.compile(r"<table\b", re.IGNORECASE)


def _carries_image(value: object) -> bool:
    """Does this cell put part of its content in a picture?"""
    if isinstance(value, dict):
        value = value.get("text") or value.get("content") or ""
    return bool(_IMAGE_TAG.search(str(value or "")))


def _text_value(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("text") or value.get("content") or ""
    return _strip_html(value)


def _table_markup(value: object) -> str | None:
    """Keep original HTML only when a prompt contains a real table.

    Plain text remains the canonical value used for matching and question-bank
    deduplication.  Retaining every editor tag would add a large, unused copy
    of the bank to memory, while retaining tables lets the PDF exporter restore
    the relationships a plain-text conversion cannot express.
    """
    if isinstance(value, dict):
        value = value.get("text") or value.get("content") or ""
    markup = str(value or "")
    return markup if _TABLE_TAG.search(markup) else None


def _is_correct(value: object) -> bool:
    if value is True:
        return True
    return (_clean(value) or "").casefold() in {"1", "true", "yes"}


def _option_values(value: object) -> tuple[tuple[str, ...], tuple[int, ...]]:
    if isinstance(value, dict):
        value = value.get("options") or value.get("items") or []
    if not isinstance(value, list):
        value = [value] if value not in (None, "") else []

    options: list[str] = []
    correct_indexes: list[int] = []
    for item in value:
        is_correct = isinstance(item, dict) and _is_correct(item.get("isCorrect"))
        text = _text_value(item)
        if not text:
            continue
        output_index = len(options)
        options.append(text)
        if is_correct:
            correct_indexes.append(output_index)
    return tuple(options), tuple(correct_indexes)


def _parse_localized_content(
    raw_prompt: str | None,
    raw_options: str | None,
    raw_solutions: str | None,
    raw_language: str | None,
    diagnostics: Counter[str],
) -> tuple[LocalizedQuestion, ...]:
    declared = _declared_languages(raw_language)
    if raw_language and not declared:
        diagnostics["unsupported_language_flag"] += 1

    prompt_payload = _decode_payload(raw_prompt, "content", diagnostics)
    options_payload = _decode_payload(raw_options, "options", diagnostics)
    solutions_payload = _decode_payload(raw_solutions, "solutions", diagnostics)
    prompts = _language_values(
        prompt_payload,
        declared=declared,
        field="content",
        diagnostics=diagnostics,
    )
    options = _language_values(
        options_payload,
        declared=declared,
        field="options",
        diagnostics=diagnostics,
    )
    solutions = _solution_values(
        solutions_payload,
        declared=declared,
        diagnostics=diagnostics,
    )

    localized: list[LocalizedQuestion] = []
    parsed_languages: set[Language] = set()
    for language in (Language.ENGLISH, Language.HINDI):
        raw_prompt_value = prompts.get(language)
        prompt = _text_value(raw_prompt_value)
        if not prompt:
            continue
        if _carries_image(raw_prompt_value):
            # The stem keeps its pairs, map or graph in a picture hosted on a
            # CDN, and these PDFs are built offline, so it can never be shown.
            # Stripping the tag leaves a question with its content removed —
            # "Consider the following pairs: How many are correctly matched?"
            # — which cannot be answered on paper. Declining the language here
            # keeps it out of selection by the same route as any other
            # language the bank does not carry.
            diagnostics[
                f"an image-only stem in {language.value.casefold()}"] += 1
            # Counted as parsed so the more specific diagnostic above is not
            # also reported as plain missing content.
            parsed_languages.add(language)
            continue
        parsed_languages.add(language)
        option_text, correct_indexes = _option_values(options.get(language))
        solution = _text_value(solutions.get(language)) or None
        key = language.value.casefold()
        if not option_text:
            diagnostics[f"missing_{key}_options"] += 1
        elif not correct_indexes:
            diagnostics[f"missing_{key}_correct_answer"] += 1
        if not solution:
            diagnostics[f"missing_{key}_solution"] += 1
        localized.append(
            LocalizedQuestion(
                language=language,
                prompt=prompt,
                options=option_text,
                correct_option_indexes=correct_indexes,
                solution=solution,
                prompt_markup=_table_markup(raw_prompt_value),
            )
        )

    for language in declared:
        if language not in parsed_languages:
            diagnostics[f"declared_{language.value.casefold()}_content_missing"] += 1
    return tuple(localized)


def _difficulty(value: object) -> Difficulty | None:
    cleaned = _clean(value)
    return _DIFFICULTY_MAP.get((cleaned or "").casefold())


def _is_verified(value: object) -> bool:
    cleaned = ( _clean(value) or "").casefold()
    return cleaned in {"1", "true", "yes", "verified"}


def _content_fingerprint(prompt: str, options: tuple[str, ...]) -> str:
    normalized = re.sub(
        r"\s+",
        " ",
        "\n".join((prompt, *options)).casefold(),
    ).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class _QuestionAggregate:
    question_id: str
    difficulty: Difficulty
    localized_content: dict[Language, LocalizedQuestion]
    tags: set[QuestionTag]
    source_tags: set[str]
    verified: bool


def _merge_localized(
    current: LocalizedQuestion,
    incoming: LocalizedQuestion,
) -> LocalizedQuestion | None:
    if (
        current.prompt != incoming.prompt
        or current.options != incoming.options
        or current.correct_option_indexes != incoming.correct_option_indexes
    ):
        return None
    if current.solution and incoming.solution and current.solution != incoming.solution:
        return None
    return LocalizedQuestion(
        language=current.language,
        prompt=current.prompt,
        options=current.options,
        correct_option_indexes=current.correct_option_indexes,
        solution=current.solution or incoming.solution,
        # The printable text above is the identity contract.  Duplicate source
        # rows sometimes differ only in editor attributes, so retain whichever
        # copy has a table rather than rejecting an otherwise identical QBG ID.
        prompt_markup=current.prompt_markup or incoming.prompt_markup,
    )


def load_question_bank(
    data: bytes,
    *,
    verified_only: bool = True,
) -> QuestionBankLoadResult:
    workbook, sheet = _open_first_sheet(data)
    iterator = sheet.iter_rows(values_only=True)
    try:
        header_values = next(iterator)
    except StopIteration as exc:
        raise WorkbookValidationError("The question bank workbook is empty.") from exc
    headers = [str(value or "").strip() for value in header_values]
    columns = _resolve_columns(
        headers,
        _QUESTION_ALIASES,
        optional=frozenset({"verification_status", "solutions", "language"}),
    )

    aggregates: dict[str, _QuestionAggregate] = {}
    conflicted: set[str] = set()
    diagnostics: Counter[str] = Counter()
    source_rows = 0
    accepted_rows = 0
    reasons: dict[str, int] = {
        "non_dpp_source": 0,
        "missing_qbgid": 0,
        "invalid_difficulty": 0,
        "missing_taxonomy": 0,
        "unverified": 0,
        "missing_localized_content": 0,
    }

    for values in iterator:
        if not any(_clean(value) for value in values):
            continue
        source_rows += 1

        def value(name: str) -> str | None:
            index = columns[name]
            return _clean(values[index]) if index is not None and index < len(values) else None

        source_tag = value("source_tag") or ""
        if source_tag.casefold() not in _SOURCE_WHITELIST:
            reasons["non_dpp_source"] += 1
            continue
        question_id = value("qbgid")
        if not question_id:
            reasons["missing_qbgid"] += 1
            continue
        difficulty = _difficulty(value("difficulty"))
        if difficulty is None:
            reasons["invalid_difficulty"] += 1
            continue
        subject_id = value("subject_id")
        chapter_id = value("chapter_id")
        topic_id = value("topic_id")
        if not subject_id or not chapter_id or not topic_id:
            reasons["missing_taxonomy"] += 1
            continue
        verified = _is_verified(value("verification_status"))
        if verified_only and not verified:
            reasons["unverified"] += 1
            continue

        tag = QuestionTag(
            subject_id=subject_id,
            chapter_id=chapter_id,
            topic_id=topic_id,
            subtopic_id=value("subtopic_id"),
        )
        raw_prompt = value("content")
        raw_options = value("options")
        localized_content = _parse_localized_content(
            raw_prompt,
            raw_options,
            value("solutions"),
            value("language"),
            diagnostics,
        )
        if not localized_content:
            reasons["missing_localized_content"] += 1
            continue
        aggregate = aggregates.get(question_id)
        if aggregate is None:
            aggregate = _QuestionAggregate(
                question_id=question_id,
                difficulty=difficulty,
                localized_content={
                    item.language: item for item in localized_content
                },
                tags={tag},
                source_tags={source_tag},
                verified=verified,
            )
            aggregates[question_id] = aggregate
        else:
            if aggregate.difficulty != difficulty:
                conflicted.add(question_id)
                continue
            merged_content = dict(aggregate.localized_content)
            content_conflict = False
            for item in localized_content:
                current = merged_content.get(item.language)
                if current is None:
                    merged_content[item.language] = item
                    continue
                merged = _merge_localized(current, item)
                if merged is None:
                    content_conflict = True
                    break
                merged_content[item.language] = merged
            if content_conflict:
                conflicted.add(question_id)
                continue
            aggregate.localized_content = merged_content
            aggregate.tags.add(tag)
            aggregate.source_tags.add(source_tag)
            aggregate.verified = aggregate.verified or verified
        accepted_rows += 1

    workbook.close()
    for question_id in conflicted:
        aggregates.pop(question_id, None)

    questions: list[QuestionRecord] = []
    for aggregate in aggregates.values():
        localized_content = tuple(
            aggregate.localized_content[language]
            for language in (Language.ENGLISH, Language.HINDI)
            if language in aggregate.localized_content
        )
        primary = next(
            (
                item
                for item in localized_content
                if item.language is Language.ENGLISH
            ),
            localized_content[0],
        )
        questions.append(
            QuestionRecord(
                question_id=aggregate.question_id,
                prompt=primary.prompt,
                options=primary.options,
                difficulty=aggregate.difficulty,
                tags=tuple(
                    sorted(
                        aggregate.tags,
                        key=lambda item: (
                            item.subject_id,
                            item.chapter_id,
                            item.topic_id,
                            item.subtopic_id or "",
                        ),
                    )
                ),
                source_tags=tuple(sorted(aggregate.source_tags)),
                content_fingerprint=_content_fingerprint(
                    primary.prompt,
                    primary.options,
                ),
                verified=aggregate.verified,
                localized_content=localized_content,
            )
        )
    questions.sort(key=lambda item: item.question_id)

    warnings = [
        f"Excluded {count:,} rows for {reason.replace('_', ' ')}."
        for reason, count in reasons.items()
        if count
    ]
    if conflicted:
        warnings.append(
            f"Excluded {len(conflicted):,} QBG IDs with conflicting content or difficulty."
        )
    for diagnostic, count in sorted(diagnostics.items()):
        warnings.append(
            f"Found {count:,} row(s) with {diagnostic.replace('_', ' ')}."
        )
    if not questions:
        raise WorkbookValidationError(
            "No usable DPP/Infinite Practice questions with QBG IDs and difficulty 1–3 were found."
        )

    return QuestionBankLoadResult(
        questions=tuple(questions),
        sheet_name=sheet.title,
        source_row_count=source_rows,
        accepted_row_count=accepted_rows,
        excluded_row_count=source_rows - accepted_rows,
        warnings=tuple(warnings),
        column_mapping=_mapping_report(headers, columns),
    )
