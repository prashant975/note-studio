"""Turn tables that cannot fit the writing column into bullets that can.

The DCN page reserves the right 2.5 in for the "Space for Notes" box, leaving a
5.5 in column. A three-column table in that width gives each cell about 1.8 in —
roughly 25 characters a line. A comparison row like

    Nature of Accountability | Indirect and Conditional: before the RTI Act
    citizens had no enforceable route to... | Direct and Legal: after the Act...

is 320 characters, so it renders as a 12-line-tall row. Word then either pushes
it past the bottom margin and clips it, or breaks it across pages mid-sentence.

Nothing about the *table* can fix that — the content is prose, and prose in a
narrow cell is unreadable however the borders are drawn. So the table is rewritten
as the heading-and-bullets it should have been:

    #H3: Nature of Accountability
    - **Before RTI**: citizens had no enforceable route to...
    - **After RTI**: the Act created a direct legal obligation...

which wraps across the full 5.5 in, breaks across pages cleanly, and reads better.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

TABLE_START = "#TABLE_START"
TABLE_END = "#TABLE_END"

_START_RE = re.compile(r"^\s*#TABLE_START\s*$", re.I)
_END_RE = re.compile(r"^\s*#TABLE_END\s*$", re.I)
_RULE_ROW_RE = re.compile(r"^[\s|:.\-–—]+$")

#: A row wider than this cannot render as one line in any of the columns.
MAX_ROW_CHARS = 90
#: A single cell longer than this is prose, not a data point.
MAX_CELL_CHARS = 60
#: Beyond this many columns the cells are too narrow to hold words.
MAX_COLUMNS = 4

#: "Indirect and Conditional: before the RTI Act..." — a label the model wrote
#: into the cell itself. Splitting on it recovers the bold lead-in.
_CELL_LABEL_RE = re.compile(r"^\s*(?P<label>[^:]{2,40}?)\s*:\s+(?P<rest>\S.*)$", re.S)


@dataclass
class TableAction:
    """What was decided about one table, for the run log and the repair trail."""

    line_no: int                     # 1-based line of its #TABLE_START
    rows: int = 0
    columns: int = 0
    converted: bool = False
    reason: str = ""
    widest_row_chars: int = 0
    widest_cell_chars: int = 0

    @property
    def needs_conversion(self) -> bool:
        return bool(self.reason)

    def describe(self) -> str:
        where = f"table at line {self.line_no}"
        if self.converted:
            return f"{where}: converted to bullets ({self.reason})."
        if self.reason:
            # Reported without converting — say what is wrong, not what was
            # decided, or the reader sees "kept as a table" next to a warning
            # that it does not fit.
            return f"{where}: {self.reason} — it will be clipped unless it is rewritten as bullets."
        return f"{where}: kept as a table ({self.rows}x{self.columns})."


@dataclass
class SanitizeResult:
    text: str
    actions: list[TableAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def converted_count(self) -> int:
        return sum(1 for a in self.actions if a.converted)


def split_row(line: str) -> list[str]:
    """Cells from one ``a | b | c`` row, with outer pipes tolerated."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _row_width(cells: list[str]) -> int:
    """Characters the row occupies, counting the separators the reader sees."""
    return sum(len(c) for c in cells) + max(0, len(cells) - 1) * 3


def assess(rows: list[list[str]]) -> tuple[bool, str, int, int]:
    """(needs_conversion, reason, widest row, widest cell)."""
    widest_row = max((_row_width(r) for r in rows), default=0)
    widest_cell = max((len(c) for r in rows for c in r), default=0)
    columns = max((len(r) for r in rows), default=0)
    if columns > MAX_COLUMNS:
        return True, f"{columns} columns is too many for a 5.5 in column", widest_row, widest_cell
    if widest_row > MAX_ROW_CHARS:
        return True, f"a row is {widest_row} characters", widest_row, widest_cell
    if widest_cell > MAX_CELL_CHARS:
        return True, f"a cell is {widest_cell} characters of prose", widest_row, widest_cell
    return False, "", widest_row, widest_cell


def _bold(text: str) -> str:
    """Wrap in ** unless it is already emphasised."""
    stripped = text.strip()
    if not stripped:
        return ""
    if stripped.startswith("**") and stripped.endswith("**"):
        return stripped
    return f"**{stripped}**"


def _cell_to_bullet(header: str, cell: str) -> str:
    """One sub-bullet for a data cell, led by whatever names it.

    The column header names it when there is one. Failing that, a
    ``Label: text`` cell carries its own label and that is used instead — the
    model very often writes one, and keeping it is what makes the bullet read
    like a sentence rather than a fragment.
    """
    text = cell.strip()
    if not text:
        return ""
    label = header.strip()
    match = _CELL_LABEL_RE.match(text)
    if not label:
        if match:
            return f"  - {_bold(match.group('label'))}: {match.group('rest').strip()}"
        return f"  - {text}"
    if match:
        # Both the column and the cell name this value. Two colons in one
        # bullet reads as a stutter, so the cell's own label becomes the start
        # of the sentence instead: "Before RTI: Indirect and conditional — ...".
        return f"  - {_bold(label)}: {match.group('label').strip()} — {match.group('rest').strip()}"
    return f"  - {_bold(label)}: {text}"


def table_to_bullets(rows: list[list[str]]) -> list[str]:
    """Rewrite one table's rows as marker-format bullet lines.

    The first row is treated as headers only when it looks like headers — short
    cells, no sentence punctuation. A table whose first row is already data
    keeps every row.
    """
    if not rows:
        return []
    header: list[str] = []
    body = rows
    first = rows[0]
    looks_like_header = (
        len(rows) > 1
        and all(len(c) <= 40 for c in first)
        and not any(c.rstrip().endswith((".", "।")) for c in first)
    )
    if looks_like_header:
        header, body = first, rows[1:]

    lines: list[str] = []
    for row in body:
        if not any(cell.strip() for cell in row):
            continue
        lead = row[0].strip()
        rest = row[1:]
        if not rest:
            lines.append(f"- {lead}" if lead else "")
            continue
        # The row's first cell is its subject, so it leads the bullet in bold.
        lines.append(f"- {_bold(lead)}" if lead else "- ")
        for index, cell in enumerate(rest, start=1):
            label = header[index] if index < len(header) else ""
            bullet = _cell_to_bullet(label, cell)
            if bullet:
                lines.append(bullet)
    return [line for line in lines if line.strip()]


def sanitize_tables(
    notes_text: str,
    *,
    convert: bool = True,
    max_row_chars: int = MAX_ROW_CHARS,
    max_cell_chars: int = MAX_CELL_CHARS,
) -> SanitizeResult:
    """Rewrite every unsafe ``#TABLE_START`` block in ``notes_text``.

    With ``convert=False`` nothing is changed and the actions are reported
    anyway, so the caller can warn without touching the notes.
    """
    global MAX_ROW_CHARS, MAX_CELL_CHARS  # noqa: PLW0603 - thresholds are the knob
    previous = (MAX_ROW_CHARS, MAX_CELL_CHARS)
    MAX_ROW_CHARS, MAX_CELL_CHARS = max_row_chars, max_cell_chars
    try:
        return _sanitize(notes_text, convert=convert)
    finally:
        MAX_ROW_CHARS, MAX_CELL_CHARS = previous


def _sanitize(notes_text: str, *, convert: bool) -> SanitizeResult:
    if not notes_text:
        return SanitizeResult(text=notes_text or "")
    lines = notes_text.splitlines()
    out: list[str] = []
    actions: list[TableAction] = []
    warnings: list[str] = []

    index = 0
    while index < len(lines):
        line = lines[index]
        if not _START_RE.match(line):
            out.append(line)
            index += 1
            continue

        start_line_no = index + 1
        index += 1
        raw_rows: list[str] = []
        closed = False
        while index < len(lines):
            if _END_RE.match(lines[index]):
                closed = True
                index += 1
                break
            raw_rows.append(lines[index])
            index += 1

        rows = [split_row(r) for r in raw_rows if r.strip() and not _RULE_ROW_RE.match(r)]
        if not rows:
            warnings.append(f"An empty table at line {start_line_no} was removed.")
            continue
        if not closed:
            warnings.append(
                f"The table at line {start_line_no} was never closed with #TABLE_END; "
                "its rows were rewritten as bullets.")

        needs, reason, widest_row, widest_cell = assess(rows)
        action = TableAction(
            line_no=start_line_no,
            rows=len(rows),
            columns=max(len(r) for r in rows),
            widest_row_chars=widest_row,
            widest_cell_chars=widest_cell,
            reason=reason,
        )
        # An unterminated table is rewritten whatever its width: leaving it as a
        # table would swallow the rest of the note into its last cell.
        if (needs or not closed) and convert:
            action.converted = True
            action.reason = reason or "the table was never closed"
            out.extend(table_to_bullets(rows))
        else:
            out.append(TABLE_START)
            out.extend(" | ".join(r) for r in rows)
            out.append(TABLE_END)
        actions.append(action)

    return SanitizeResult(text="\n".join(out), actions=actions, warnings=warnings)


def unsafe_table_report(notes_text: str) -> list[str]:
    """Human-readable problems, without changing anything.

    Used by the quality checker so its warnings and the sanitizer's decisions
    can never disagree — both read the same thresholds from this module.
    """
    result = sanitize_tables(notes_text, convert=False)
    return [a.describe() for a in result.actions if a.reason] + result.warnings
