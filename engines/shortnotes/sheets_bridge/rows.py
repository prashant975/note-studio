"""Row model over the values grid returned by the Sheets values API.

Cells are addressed through a :class:`~sheets_bridge.columns.Layout`, resolved
from the tab's own header row, so the same code reads a tab whose columns sit
in a different order. Rows built without a layout use the reference layout.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import columns as C


def _cell(values: tuple, index: int | None) -> str:
    if index is None or index < 0 or index >= len(values):
        return ""
    value = values[index]
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


@dataclass(frozen=True)
class SheetRow:
    """One data row. ``number`` is the 1-based row number shown in the UI."""

    number: int
    values: tuple = field(default_factory=tuple)
    layout: C.Layout = field(default=C.DEFAULT_LAYOUT, repr=False, compare=False)

    @classmethod
    def from_values(cls, number: int, values: list | tuple | None, layout: C.Layout | None = None) -> "SheetRow":
        return cls(number=number, values=tuple(values or ()), layout=layout or C.DEFAULT_LAYOUT)

    def cell(self, index: int) -> str:
        return _cell(self.values, index)

    def named(self, name: str) -> str:
        """Value of a logical column ("" when the tab has no such column)."""
        return _cell(self.values, self.layout.index(name))

    @property
    def batch(self) -> str:
        return self.named("batch")

    @property
    def subject(self) -> str:
        return self.named("subject")

    @property
    def faculty(self) -> str:
        return self.named("faculty")

    @property
    def chapter(self) -> str:
        """Often two spellings: 'English name\\nहिन्दी नाम'."""
        return self.named("chapter")

    @property
    def lecture_number_text(self) -> str:
        return self.named("lecture_number")

    @property
    def drive_link(self) -> str:
        """The source link: a Drive file/folder, or (catalog) a direct PDF URL."""
        return self.named("drive_link")

    @property
    def exam(self) -> str:
        """Catalog tabs: the exam ("NEET", "IIT-JEE")."""
        return self.named("exam")

    @property
    def lecture_topic(self) -> str:
        """Catalog tabs: the lecture's full name with its scheduling notes."""
        return self.named("lecture_topic")

    @property
    def notes_link(self) -> str:
        """Catalog tabs: the Drive link to the notes written by an earlier run."""
        index = self.layout.notes_link_index
        return self.cell(index).strip() if index is not None else ""

    @property
    def notes_folder(self) -> str:
        """Catalog tabs: the Drive link to the folder the notes were filed in."""
        index = self.layout.notes_folder_index
        return self.cell(index).strip() if index is not None else ""

    @property
    def bm_done(self) -> bool:
        """'Class Notes Status (By BM)' == Done (the trigger)."""
        return _is_done(self.named("bm_status"))

    @property
    def final_done(self) -> bool:
        """'Final File Status (by Content Team)' == Done."""
        return _is_done(self.named("final_status"))

    @property
    def review_status(self) -> str:
        """Column R: "" | "Review" (generated, awaiting a human) | "Done" |
        "Regenerate" (a human wants fresh notes)."""
        return self.cell(self.layout.review_status_index).strip()

    @property
    def wants_regeneration(self) -> bool:
        """Review Status set to "Regenerate": make the notes again from scratch."""
        return is_regenerate_request(self.review_status)

    @property
    def auto_status(self) -> str:
        """First word of the Q cell: '', 'claimed', 'done' or 'failed'.

        A claim is written as ``claimed <timestamp>`` so the in-flight guard
        works within the two control columns; the label alone is compared.
        """
        head, _sep, _rest = self.cell(self.layout.auto_status_index).strip().partition(" ")
        return head.rstrip(":").lower()

    @property
    def claim_stamp(self) -> str:
        """When the row was claimed: the rest of the Q cell, else the legacy
        'Auto Timestamp' column older runs wrote."""
        _head, _sep, rest = self.cell(self.layout.auto_status_index).strip().partition(" ")
        return rest.strip() or self.auto_timestamp

    @property
    def auto_timestamp(self) -> str:
        """Legacy 'Auto Timestamp', still honoured for claims written by older runs."""
        return _cell(self.values, self.layout.legacy.get("auto_timestamp")).strip()

    @property
    def is_blank(self) -> bool:
        """True when every human column is empty (trailing empty grid rows)."""
        return not any(self.cell(i).strip() for i in range(self.layout.control_start))


def _is_done(text: str) -> bool:
    return text.strip().strip(".!").lower() == "done"


STALE_CLAIM_SECONDS = 3 * 60 * 60
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def claim_is_stale(timestamp_text: str, *, now: datetime | None = None) -> bool:
    """A 'claimed' row whose Auto Timestamp is missing, unparseable or older
    than STALE_CLAIM_SECONDS is treated as abandoned (crashed run) and retried."""
    text = (timestamp_text or "").strip().lstrip("'")
    if not text:
        return True
    try:
        stamped = datetime.strptime(text, _TIMESTAMP_FORMAT)
    except ValueError:
        return True
    current = now or datetime.now(timezone.utc)
    return (current - stamped).total_seconds() > STALE_CLAIM_SECONDS


def is_regenerate_request(review_text: str) -> bool:
    """A Review Status cell asking for fresh notes ("Regenerate", any case)."""
    text = " ".join(str(review_text or "").casefold().replace("-", " ").split())
    return text in ("regenerate", "regeneration", "re generate", "re generation")


def is_eligible(row: SheetRow, *, redo: bool = False, now: datetime | None = None) -> bool:
    """J (BM status) == Done is the trigger; on a catalog tab, a PDF link.

    The automation no longer marks M on the content team's behalf - a lecture
    it generates is *awaiting review*, not final - so M can no longer be what
    stops a row being generated twice. Q ("done") and R ("Review"/"Done") are
    that record now: either means this row already has its notes. A human
    marking M = Done is still respected, "failed" rows are retried, and a fresh
    "claimed" (another run in flight) is skipped. ``redo`` ignores all of it.
    """
    if row.is_blank:
        return False
    if row.layout.catalog:
        # A catalog lists finished lectures: every row with a link is ready.
        if not row.drive_link.strip():
            return False
    elif not row.bm_done:
        return False
    if redo:
        return True
    if row.wants_regeneration:
        # Asked for by a human, whatever the row's status - unless a run is on it now.
        return not (row.auto_status == C.STATUS_CLAIMED and not claim_is_stale(row.claim_stamp, now=now))
    if row.final_done:
        return False
    if row.auto_status in (C.STATUS_DONE, "done"):    # "done" = written by an older run
        return False
    if row.auto_status == C.STATUS_SKIPPED:           # not a lecture (catalog skip list)
        return False
    if row.review_status:
        return False
    if row.auto_status == C.STATUS_CLAIMED and not claim_is_stale(row.claim_stamp, now=now):
        return False
    return True


def rows_from_grid(grid: list, layout: C.Layout | None = None) -> list[SheetRow]:
    """Values grid (row 1 = header) -> data rows numbered from 2.

    Without an explicit layout the header row is resolved here, so callers
    that only have the grid still address cells by header, not by letter.
    """
    if not grid:
        return []
    resolved = layout
    if resolved is None:
        # The runner always passes the layout it validated. A bare grid whose
        # header cannot be driven (tests, ad-hoc callers) reads positionally,
        # as it always did, instead of yielding empty cells.
        detected = C.Layout.from_header(grid[0])
        resolved = detected if not detected.problems() else C.DEFAULT_LAYOUT
    return [
        SheetRow.from_values(index + 2, values, resolved)
        for index, values in enumerate(grid[1:])
    ]
