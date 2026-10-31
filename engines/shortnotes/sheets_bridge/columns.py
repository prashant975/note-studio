"""Column layout of a notes tab, resolved from its header row.

The bridge used to read every column by a fixed letter (Drive Link = I, the
trigger = J ...). Real workbooks drift: headers gain line breaks, a tab is
copied with a column inserted, an "Audio Notes" tab with a different layout
shares the workbook. :meth:`Layout.from_header` finds each logical column by
its header text instead, so a re-ordered tab still reads the right cells and a
tab that lacks something the bridge needs is refused with the missing column
named - never silently misread.

The fixed ``COL_*`` constants remain: they are the canonical layout (what
``DEFAULT_LAYOUT`` resolves to for the reference header) and what existing
callers and tests index by.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Mapping

from .errors import BridgeError

# Human-owned columns of the reference layout (never written by the bridge
# except M and P on success). Index is 0-based.
HUMAN_HEADERS = (
    "Project Code",                                     # A
    "Project Name",                                     # B
    "Batch",                                            # C
    "Subject",                                          # D
    "Lecture Faculty",                                  # E
    "Lecture Date",                                     # F
    "Chapter",                                          # G
    "Lecture Number",                                   # H
    "Drive Link",                                       # I
    "Class Notes Status (By BM)",                       # J
    "Content SPOC",                                     # K
    "Time Taken",                                       # L
    "Final File Status (by Content Team)",              # M
    "Batch Upload status (For BM)",                     # N
    "Class Notes Status (By BM) Time Stamp",            # O
    "Final File Status (by Content Team) Time Stamp",   # P
)
HUMAN_COLUMN_COUNT = len(HUMAN_HEADERS)  # 16 -> A..P

CONTROL_HEADERS = (
    "Auto Status",
    "Review Status",
)
CONTROL_COLUMN_COUNT = len(CONTROL_HEADERS)  # 2 -> Q..R
# A catalog sheet (exam / batch / subject / chapter / lecture topic / PDF link,
# no status columns) files its notes in a Drive tree, so its control columns
# also carry the link to the notes and to the lecture's folder in that tree:
# H..K after its seven columns.
CATALOG_CONTROL_HEADERS = CONTROL_HEADERS + ("Concise Notes Link", "Lecture Folder Link")

# Written by earlier versions; cleared once when they are still present.
LEGACY_CONTROL_HEADERS = (
    "Auto Run ID",
    "Auto Error Code",
    "Auto Timestamp",
    "Auto Cost INR",
)
TOTAL_COLUMN_COUNT = HUMAN_COLUMN_COUNT + CONTROL_COLUMN_COUNT  # 22

# 0-based indexes of the reference layout.
COL_BATCH = 2            # C
COL_SUBJECT = 3          # D
COL_FACULTY = 4          # E
COL_CHAPTER = 6          # G  disambiguates lectures inside a shared folder
COL_LECTURE_NUMBER = 7   # H
COL_DRIVE_LINK = 8       # I
COL_BM_STATUS = 9        # J  Class Notes Status (By BM)  - the trigger ("Done")
COL_FINAL_STATUS = 12    # M  Final File Status (by Content Team) - written on success
COL_FINAL_TIMESTAMP = 15 # P  Final File Status Time Stamp        - written on success

DONE_TEXT = "Done"

COL_AUTO_STATUS = HUMAN_COLUMN_COUNT + 0        # Q
COL_REVIEW_STATUS = HUMAN_COLUMN_COUNT + 1      # R
LEGACY_FIRST_COLUMN = HUMAN_COLUMN_COUNT + CONTROL_COLUMN_COUNT          # S
LEGACY_LAST_COLUMN = LEGACY_FIRST_COLUMN + len(LEGACY_CONTROL_HEADERS) - 1  # V

# The review column is a dropdown, not a link: the automation sets "Review"
# when a lecture's notes are uploaded, and a human switches it to "Done" once
# they have checked the PDF. The PDF sits beside its source in Drive, so the
# sheet carries no link. "Regenerate" asks the next run for fresh notes: they
# replace the old PDF in its folder (same file, so the same link) and the row
# goes back to "Review".
REVIEW_PENDING = "Review"
REVIEW_DONE = "Done"
REVIEW_REGENERATE = "Regenerate"
REVIEW_OPTIONS = (REVIEW_PENDING, REVIEW_DONE, REVIEW_REGENERATE)

STATUS_EMPTY = ""
STATUS_CLAIMED = "claimed"
# A finished lecture is *awaiting review*: Q carries "Review" rather than
# "done", matching the R dropdown, while M is marked Done for the content team.
STATUS_DONE = "review"
STATUS_FAILED = "failed"
# A catalog row that is not a lecture (a batch on the skip list).
STATUS_SKIPPED = "skipped"

KIND_CLASSIC = "classic"
KIND_CATALOG = "catalog"


# --------------------------------------------------------------------------- #
# Header-driven resolution
# --------------------------------------------------------------------------- #
# Logical columns, in the order they claim header cells. Control and timestamp
# columns go first so a fuzzy match for "link" or "status" can never steal
# "Auto Output Link" or "... Time Stamp".
LOGICAL_COLUMNS = (
    "auto_status",
    "review_status",
    "notes_link",
    "final_timestamp",
    "final_status",
    "bm_status",
    "drive_link",
    # After drive_link, so a batch tab's source column is never taken for it.
    "notes_folder",
    "lecture_number",
    "lecture_topic",
    "chapter",
    "faculty",
    "subject",
    "batch",
    "project_code",
    "exam",
)
REQUIRED_COLUMNS = ("drive_link", "bm_status", "final_status")
# What a catalog tab must have: the PDF link, the lecture's name and the three
# folder levels under the exam (the exam column itself is optional).
CATALOG_REQUIRED_COLUMNS = ("drive_link", "lecture_topic", "batch", "subject", "chapter")
# Needed to resolve a *folder* link to one lecture PDF; a file link works without.
RECOMMENDED_COLUMNS = ("lecture_number", "chapter")

LABELS = {
    "project_code": "Project Code",
    "batch": "Batch",
    "subject": "Subject",
    "faculty": "Lecture Faculty",
    "chapter": "Chapter",
    "lecture_number": "Lecture Number",
    "drive_link": "Drive Link",
    "bm_status": "Class Notes Status (By BM)",
    "final_status": "Final File Status (by Content Team)",
    "final_timestamp": "Final File Status Time Stamp",
    "auto_status": "Auto Status",
    "review_status": "Review Status",
    "notes_link": "Concise Notes Link",
    "notes_folder": "Lecture Folder Link",
    "lecture_topic": "Lecture Topic",
    "exam": "Exam",
}

# Exact (normalised) header spellings seen in production workbooks.
ALIASES = {
    "project_code": ("project code",),
    "batch": ("batch", "batch name"),
    "subject": ("subject", "subject name"),
    "faculty": ("lecture faculty", "faculty", "faculty name", "teacher", "teacher name"),
    "chapter": ("chapter", "chapter name", "chapter title", "topic", "topic name"),
    "lecture_number": (
        "lecture number", "lecture no", "lecture num", "lecture", "lec no", "lec number",
        "lecture id", "lecture count",
    ),
    "drive_link": (
        "drive link", "drive url", "google drive link", "drive folder link", "folder link",
        "class notes link", "notes link", "pdf link", "source link", "lecture link", "link",
        "downloadable link", "download link", "downloadable pdf link", "pdf download link", "pdf url",
        "file link",
    ),
    "lecture_topic": (
        "lecture topic", "lecture name", "lecture title", "lecture topic name", "class topic",
    ),
    "exam": ("exam", "exam name", "exam category", "exam type", "target exam"),
    "notes_link": (
        "concise notes link", "concise notes", "concise notes pdf", "generated notes link",
        "notes pdf link", "short notes link",
    ),
    "notes_folder": ("lecture folder link", "notes folder link", "concise notes folder link"),
    "bm_status": (
        "class notes status by bm", "class notes status", "notes status by bm", "notes status",
        "status by bm", "bm status", "chapter completion status by bm", "chapter completion status",
    ),
    "final_status": (
        "final file status by content team", "final file status", "final status",
        "content team status", "final file status by content",
    ),
    "final_timestamp": (
        "final file status by content team time stamp", "final file status by content team timestamp",
        "final file status time stamp", "final file status timestamp", "final status time stamp",
        "final status timestamp", "final time stamp", "final timestamp",
    ),
    "auto_status": ("auto status",),
    # "auto output link" is the pre-dropdown spelling: a sheet created by an
    # older run keeps that header until ensure_control_headers renames it, and
    # must not be read as a conflicting human column meanwhile.
    "review_status": (
        "review status", "review", "auto output link", "auto output", "auto link",
    ),
}

_LEGACY_ALIASES = {
    "auto_run_id": "auto run id",
    "auto_error_code": "auto error code",
    "auto_timestamp": "auto timestamp",
    "auto_cost_inr": "auto cost inr",
}

_NON_WORD = re.compile(r"[^0-9a-z]+")


def normalize_header(value: object) -> str:
    """Comparison form: NFKC, casefolded, punctuation and line breaks -> one space.

    ``'Class Notes Status\\n(By BM)'`` and ``'Class Notes Status (By BM)'``
    both become ``'class notes status by bm'``.
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_NON_WORD.sub(" ", text).split())


def _tokens(normalized: str) -> set[str]:
    return set(normalized.split())


def _fuzzy(name: str, normalized: str) -> bool:
    """Loose match for headers that are not a known spelling.

    Deliberately conservative: each rule excludes the words that would make
    it grab a neighbouring column (a timestamp, an upload status, our own
    control columns).
    """
    words = _tokens(normalized)
    has = words.__contains__
    timed = has("time") or has("timestamp") or has("stamp") or has("date")
    # "Upload status", "Uploading status (by BM Team)", "Uploaded": the BM
    # team's upload tracking, never a column the bridge reads or writes.
    upload = any(word.startswith("upload") for word in words)
    if name == "drive_link":
        return ((has("link") or has("url")) and not has("auto") and not has("output") and not has("concise")
                and normalized not in ALIASES["notes_folder"])
    if name == "notes_link":
        return has("concise") and (has("link") or has("url") or has("pdf"))
    if name == "lecture_topic":
        return has("lecture") and (has("topic") or has("title") or has("name")) and not has("number")
    if name == "exam":
        return has("exam") and not timed and not has("status")
    if name == "bm_status":
        return has("status") and (has("bm") or has("notes")) and not timed and not upload and not has("final")
    if name == "review_status":
        # A reviewer team may add its name: "Review Status (by ARD Team)".
        return has("review") and has("status") and not timed and not upload
    if name == "final_status":
        return has("final") and has("status") and not timed
    if name == "final_timestamp":
        return has("final") and timed
    if name == "lecture_number":
        return has("lecture") and (has("number") or has("no") or has("num") or len(words) == 1)
    if name == "chapter":
        return has("chapter") and not has("status") and not has("completion") and not has("link")
    if name == "faculty":
        # The catalog's faculty_id / faculty_email sit beside faculty_name.
        not_a_name = {"id", "email", "mail", "code", "phone", "mobile", "contact"}
        return (has("faculty") or has("teacher")) and not words & not_a_name
    if name == "subject":
        return has("subject")
    if name == "batch":
        return has("batch") and not upload and not has("status")
    return False


@dataclass(frozen=True)
class Layout:
    """Where each logical column lives in one tab."""

    indexes: Mapping[str, int]
    legacy: Mapping[str, int]
    header: tuple[str, ...]
    width: int                      # header cells in use (last non-empty + 1)
    control_start: int              # first control column, present or to be created
    ambiguous: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Cleared by the runner when the tab computes its own timestamp (column P
    # holding a formula): the bridge must then leave that cell alone.
    write_final_timestamp: bool = True
    # KIND_CLASSIC: the batch sheets (J = Done triggers, notes beside the
    # source). KIND_CATALOG: exam/batch/subject/chapter/topic/PDF link rows,
    # notes filed in a Drive tree (see sheets_bridge/catalog.py).
    kind: str = KIND_CLASSIC

    # -- construction ----------------------------------------------------------
    @classmethod
    def from_header(cls, header_row: "list | tuple | None") -> "Layout":
        raw = [("" if v is None else str(v)) for v in (header_row or ())]
        while raw and not raw[-1].strip():
            raw.pop()
        normalized = [normalize_header(v) for v in raw]
        claimed: set[int] = set()
        indexes: dict[str, int] = {}
        ambiguous: dict[str, tuple[str, ...]] = {}

        for name in LOGICAL_COLUMNS:
            found = None
            for alias in ALIASES[name]:
                for index, text in enumerate(normalized):
                    if index not in claimed and text == alias:
                        found = index
                        break
                if found is not None:
                    break
            if found is None:
                candidates = [i for i, text in enumerate(normalized) if i not in claimed and text and _fuzzy(name, text)]
                if len(candidates) > 1 and name == "drive_link":
                    preferred = [i for i in candidates if "drive" in _tokens(normalized[i])]
                    if len(preferred) == 1:
                        candidates = preferred
                if len(candidates) == 1:
                    found = candidates[0]
                elif len(candidates) > 1:
                    ambiguous[name] = tuple(raw[i].strip() for i in candidates)
            if found is not None:
                indexes[name] = found
                claimed.add(found)

        # A batch tab whose "Lecture Faculty" header cell is blank still keeps
        # the names in the column after Subject (Lakshya NEET 2027: E1 empty),
        # and without them every note there was signed "PW Faculty".
        after_subject = indexes.get("subject", -2) + 1
        if ("faculty" not in indexes and 0 < after_subject < len(raw)
                and after_subject not in claimed and not normalized[after_subject]):
            indexes["faculty"] = after_subject
            claimed.add(after_subject)

        legacy: dict[str, int] = {}
        for key, alias in _LEGACY_ALIASES.items():
            for index, text in enumerate(normalized):
                if index not in claimed and text == alias:
                    legacy[key] = index
                    claimed.add(index)
                    break

        width = len(raw)
        # No status columns at all, but everything a catalog needs: a catalog.
        catalog = (
            "bm_status" not in indexes and "final_status" not in indexes
            and all(name in indexes for name in CATALOG_REQUIRED_COLUMNS)
        )
        if "auto_status" in indexes:
            control_start = indexes["auto_status"]
        elif "review_status" in indexes and indexes["review_status"] > 0:
            control_start = indexes["review_status"] - 1
        elif catalog and indexes.get("notes_link", 0) > 1:
            control_start = indexes["notes_link"] - 2
        else:
            control_start = width
        return cls(
            indexes=indexes,
            legacy=legacy,
            header=tuple(raw),
            width=width,
            control_start=control_start,
            ambiguous=ambiguous,
            kind=KIND_CATALOG if catalog else KIND_CLASSIC,
        )

    # -- lookups ----------------------------------------------------------------
    def index(self, name: str) -> int | None:
        return self.indexes.get(name)

    def require(self, name: str) -> int:
        index = self.indexes.get(name)
        if index is None:
            raise BridgeError("column_missing", LABELS.get(name, name))
        return index

    def letter(self, name: str) -> str:
        index = self.indexes.get(name)
        return column_letter(index) if index is not None else ""

    @property
    def auto_status_index(self) -> int:
        return self.indexes.get("auto_status", self.control_start)

    @property
    def review_status_index(self) -> int:
        return self.indexes.get("review_status", self.control_start + 1)

    @property
    def catalog(self) -> bool:
        return self.kind == KIND_CATALOG

    @property
    def notes_link_index(self) -> int | None:
        """Catalog tabs only: the column holding the Drive link to the notes."""
        if not self.catalog:
            return None
        return self.indexes.get("notes_link", self.control_start + 2)

    @property
    def notes_folder_index(self) -> int | None:
        """Catalog tabs only: the column holding the link to the lecture's
        folder in the Drive tree, just after the notes link - or, when a team
        already uses that column for something else ("Uploading status (by BM
        Team)"), the first free column after the tab's headers."""
        if not self.catalog:
            return None
        if "notes_folder" in self.indexes:
            return self.indexes["notes_folder"]
        slot = self.notes_link_index + 1
        if slot < len(self.header) and self.header[slot].strip():
            return len(self.header)
        return slot

    @property
    def control_names(self) -> tuple[str, ...]:
        """The logical column behind each of ``control_indexes``."""
        names = ("auto_status", "review_status")
        if not self.catalog:
            return names
        names += ("notes_link",)
        return names + (("notes_folder",) if self.notes_folder_index is not None else ())

    @property
    def control_headers(self) -> tuple[str, ...]:
        if not self.catalog:
            return CONTROL_HEADERS
        return CATALOG_CONTROL_HEADERS if self.notes_folder_index is not None else CATALOG_CONTROL_HEADERS[:-1]

    @property
    def control_indexes(self) -> tuple[int, ...]:
        """Columns the bridge owns, in the order of ``control_headers``."""
        indexes = (self.auto_status_index, self.review_status_index)
        if not self.catalog:
            return indexes
        folder = self.notes_folder_index
        return indexes + (self.notes_link_index,) + ((folder,) if folder is not None else ())

    @property
    def identity_columns(self) -> tuple[str, str, str]:
        """Cells re-read before a write to prove the row has not moved."""
        if self.catalog:
            return ("drive_link", "lecture_topic", "chapter")
        return ("drive_link", "lecture_number", "chapter")

    @property
    def required_columns(self) -> tuple[str, ...]:
        return CATALOG_REQUIRED_COLUMNS if self.catalog else REQUIRED_COLUMNS

    @property
    def control_present(self) -> bool:
        return all(name in self.indexes for name in
                   ("auto_status", "review_status") + (("notes_link",) if self.catalog else ()))

    @property
    def last_index(self) -> int:
        """Highest column index the bridge reads or writes."""
        candidates = [self.width - 1, *self.control_indexes, *self.legacy.values()]
        return max(candidates) if candidates else 0

    def without_timestamp(self) -> "Layout":
        return replace(self, write_final_timestamp=False)

    # -- validation -------------------------------------------------------------
    def missing(self) -> list[str]:
        return [name for name in self.required_columns if name not in self.indexes]

    def missing_recommended(self) -> list[str]:
        if self.catalog:
            return []       # a catalog row names its lecture itself; no folder matching needed
        return [name for name in RECOMMENDED_COLUMNS if name not in self.indexes]

    def problems(self) -> list[str]:
        """Why this tab cannot be run, in words; empty means it can."""
        if not self.header:
            return ["the tab has no header row"]
        out = [f"no column for {LABELS[name]!r}" for name in self.missing()]
        for name, options in self.ambiguous.items():
            if name in self.required_columns:
                out.append(f"{LABELS[name]!r} is ambiguous between {', '.join(repr(o) for o in options)}")
        return out

    def describe(self) -> dict[str, str]:
        """Logical column -> letter, for logs and the UI."""
        return {name: column_letter(index) for name, index in self.indexes.items()}

    def describe_text(self) -> str:
        parts = ["catalog sheet"] if self.catalog else []
        names = (("exam", "batch", "subject", "faculty", "chapter", "lecture_topic", "drive_link") if self.catalog else
                 ("drive_link", "lecture_number", "chapter", "bm_status", "final_status", "final_timestamp", "batch", "subject", "faculty"))
        for name in names:
            index = self.indexes.get(name)
            parts.append(f"{LABELS[name]}={column_letter(index) if index is not None else '-'}")
        parts.append(f"control={column_letter(self.control_indexes[0])}:{column_letter(self.control_indexes[-1])}")
        return ", ".join(parts)


DEFAULT_LAYOUT = Layout.from_header(HUMAN_HEADERS + CONTROL_HEADERS + LEGACY_CONTROL_HEADERS)


def validate_header(header_row: "list | tuple") -> list[str]:
    """Human-readable reasons a header row cannot be driven; empty = fine.

    Columns are found by header text, so a re-ordered tab passes. What fails
    is a tab missing something the bridge must read or write - and that used
    to be misread in silence.
    """
    return Layout.from_header(header_row).problems()


# --------------------------------------------------------------------------- #
# A1 helpers
# --------------------------------------------------------------------------- #
def column_letter(index: int) -> str:
    """0-based column index -> A1 letter(s): 0 -> A, 16 -> Q, 26 -> AA."""
    if index < 0:
        raise ValueError("column index must be >= 0")
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def control_column_letters() -> tuple[str, ...]:
    """Letters of the control columns in the reference layout."""
    return tuple(
        column_letter(HUMAN_COLUMN_COUNT + offset)
        for offset in range(CONTROL_COLUMN_COUNT)
    )


def control_range(row_number: int) -> str:
    """A1 range (without sheet title) covering the control cells of one row
    in the reference layout."""
    if row_number < 1:
        raise ValueError("row number must be >= 1")
    letters = control_column_letters()
    return f"{letters[0]}{row_number}:{letters[-1]}{row_number}"


def read_range() -> str:
    """Reference-layout read range (human + control + legacy columns)."""
    return f"A1:{column_letter(LEGACY_LAST_COLUMN)}"


def legacy_range(row_number: int) -> str:
    first, last = column_letter(LEGACY_FIRST_COLUMN), column_letter(LEGACY_LAST_COLUMN)
    return f"{first}{row_number}:{last}{row_number}"
