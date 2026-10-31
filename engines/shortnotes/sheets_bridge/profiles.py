"""Named sheet targets ("profiles") so one install can drive several sheets.

A profile is a name plus a spreadsheet id and a tab gid; the rest of the
per-sheet knobs (signature, cost cap, PDF upload) may be overridden per profile
and otherwise fall back to the install-level :class:`~sheets_bridge.settings.Settings`.

Profiles live in ``sheets.json`` (``SHEETS_BRIDGE_PROFILES`` overrides the
path). The file is written by the Studio's add-sheet form, so it is read and
saved atomically and every field is validated on the way in.

When the file is absent, :func:`resolve_targets` derives a single target from
the existing ``SHEETS_BRIDGE_SPREADSHEET_ID`` / ``_GID`` environment variables,
so an install that never adds a profile behaves exactly as it did before.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .errors import BridgeError

DEFAULT_FILENAME = "sheets.json"
NAME_MAX = 40
# Deliberately narrow: the name is shown in a <select>, used as a run-log value
# and echoed in log lines, so keep it to something unambiguous.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _().&:+-]*$")

_SPREADSHEET_ID = r"[A-Za-z0-9_-]{10,}"
_SPREADSHEET_RE = re.compile(rf"/spreadsheets/(?:u/\d+/)?d/({_SPREADSHEET_ID})(?:[/?#]|$)")
_ID_ONLY_RE = re.compile(rf"^{_SPREADSHEET_ID}$")


@dataclass(frozen=True)
class SheetTarget:
    """One named sheet+tab, with optional per-profile overrides."""

    name: str
    spreadsheet_id: str
    gid: int
    signature: str = ""
    max_cost_inr: float | None = None
    also_pdf: bool | None = None      # None -> inherit from Settings
    enabled: bool = True
    # A whole workbook: expanded at run time into every tab that has the
    # columns the bridge needs and rows ready (see expand_workbook).
    all_tabs: bool = False
    # Catalog tabs (PDF links, no status columns): the Drive folder (id) their
    # notes are filed under as exam / batch / subject / chapter, and the
    # batches that are not lectures (None -> catalog.DEFAULT_SKIP_BATCHES).
    output_folder: str = ""
    skip_batches: tuple | None = None

    def to_json(self) -> dict:
        data: dict = {
            "name": self.name,
            "spreadsheetId": self.spreadsheet_id,
        }
        if self.all_tabs:
            data["allTabs"] = True
        else:
            data["gid"] = self.gid
        if self.signature:
            data["signature"] = self.signature
        if self.max_cost_inr is not None:
            data["maxCostInr"] = self.max_cost_inr
        if self.also_pdf is not None:
            data["alsoPdf"] = self.also_pdf
        if not self.enabled:
            data["enabled"] = False
        if self.output_folder:
            data["outputFolder"] = self.output_folder
        if self.skip_batches is not None:
            data["skipBatches"] = list(self.skip_batches)
        return data

    def public(self) -> dict:
        """Shape sent to the browser (no secrets exist here, but keep it explicit)."""
        return {
            "name": self.name,
            "spreadsheetId": self.spreadsheet_id,
            "gid": self.gid,
            "enabled": self.enabled,
            "allTabs": self.all_tabs,
            "outputFolder": self.output_folder,
        }


# -- links -------------------------------------------------------------------
def parse_sheet_link(raw: object) -> tuple[str, int | None]:
    """``https://docs.google.com/spreadsheets/d/<id>/edit#gid=<n>`` -> (id, gid).

    The gid is returned as ``None`` when the link does not carry one. It is
    deliberately *not* defaulted to 0: a link copied from a non-first tab always
    has its gid, and silently assuming 0 would point the run at the wrong tab.
    A bare spreadsheet id is accepted too (same rule: gid unknown).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise BridgeError("blank_link")
    text = raw.strip()

    match = _SPREADSHEET_RE.search(text)
    if match:
        return match.group(1), _gid_from(text)
    if _ID_ONLY_RE.match(text):
        return text, None
    raise BridgeError("invalid_link", "not a Google Sheets link")


def _gid_from(text: str) -> int | None:
    """gid from the fragment (``#gid=5``) or the query (``?gid=5``)."""
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    for blob in (parts.fragment, parts.query):
        if not blob:
            continue
        values = parse_qs(blob).get("gid") or []
        if len(values) == 1 and values[0].isdigit():
            return int(values[0])
    return None


_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def parse_folder_link(raw: object) -> str:
    """A Drive *folder* link (or a bare folder id) -> the folder id."""
    from .links import KIND_FOLDER, parse_drive_link

    if not isinstance(raw, str) or not raw.strip():
        raise BridgeError("bad_output_folder", "paste the link of the Drive folder the notes go in")
    text = raw.strip()
    if _FOLDER_ID_RE.match(text):
        return text
    try:
        link = parse_drive_link(text)
    except BridgeError:
        raise BridgeError("bad_output_folder", "not a Google Drive folder link") from None
    if link.kind != KIND_FOLDER:
        raise BridgeError("bad_output_folder", "that is a file link; paste the link of a Drive folder")
    return link.id


def verify_output_folder(drive_service, folder_id: str) -> str:
    """Read-only: the folder exists, is a folder and this account can open
    it. Returns its name."""
    from .drive import DriveClient

    try:
        folder = DriveClient(drive_service).get_file(folder_id)
    except BridgeError as exc:
        raise BridgeError("bad_output_folder", f"cannot open the output folder ({exc.code})") from None
    if not folder.is_folder:
        raise BridgeError("bad_output_folder", "the output folder link points at a file")
    return folder.name


# -- validation --------------------------------------------------------------
def clean_name(raw: object) -> str:
    name = (raw if isinstance(raw, str) else "").strip()
    if not name:
        raise BridgeError("bad_profile", "name is required")
    if len(name) > NAME_MAX:
        raise BridgeError("bad_profile", f"name must be at most {NAME_MAX} characters")
    if not _NAME_RE.match(name):
        raise BridgeError("bad_profile", "name may use letters, digits, spaces and - _ ( ) . & : + only")
    return name


def _target_from_json(raw: object, *, where: str) -> SheetTarget:
    if not isinstance(raw, dict):
        raise BridgeError("bad_profiles", f"{where} is not an object")
    name = clean_name(raw.get("name"))
    spreadsheet_id = (raw.get("spreadsheetId") or "").strip() if isinstance(raw.get("spreadsheetId"), str) else ""
    if not spreadsheet_id:
        raise BridgeError("bad_profiles", f"{where} has no spreadsheetId")
    all_tabs = raw.get("allTabs") is True
    gid = raw.get("gid")
    if all_tabs and gid is None:
        gid = 0
    if isinstance(gid, bool) or not isinstance(gid, int) or gid < 0:
        raise BridgeError("bad_profiles", f"{where} has a non-integer gid")
    max_cost = raw.get("maxCostInr")
    if max_cost is not None:
        if isinstance(max_cost, bool) or not isinstance(max_cost, (int, float)) or max_cost <= 0:
            raise BridgeError("bad_profiles", f"{where} has an invalid maxCostInr (a positive number, or omit it)")
    also_pdf = raw.get("alsoPdf")
    output_folder = ""
    if raw.get("outputFolder") not in (None, ""):
        try:
            output_folder = parse_folder_link(raw.get("outputFolder"))
        except BridgeError as exc:
            raise BridgeError("bad_profiles", f"{where} has an invalid outputFolder ({exc.detail})") from None
    skip = raw.get("skipBatches")
    if skip is not None:
        if not isinstance(skip, list) or not all(isinstance(entry, str) for entry in skip):
            raise BridgeError("bad_profiles", f"{where} has an invalid skipBatches (a list of batch names)")
        skip = tuple(entry.strip() for entry in skip if entry.strip())
    return SheetTarget(
        name=name,
        spreadsheet_id=spreadsheet_id,
        gid=gid,
        signature=(raw.get("signature") or "").strip() if isinstance(raw.get("signature"), str) else "",
        max_cost_inr=float(max_cost) if max_cost is not None else None,
        also_pdf=bool(also_pdf) if isinstance(also_pdf, bool) else None,
        enabled=raw.get("enabled") is not False,
        all_tabs=all_tabs,
        output_folder=output_folder,
        skip_batches=skip,
    )


# -- storage -----------------------------------------------------------------
def profiles_path() -> Path:
    override = os.environ.get("SHEETS_BRIDGE_PROFILES", "").strip()
    if override:
        return Path(override).expanduser()
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / DEFAULT_FILENAME


def load_profiles(path: Path | None = None) -> tuple[SheetTarget, ...]:
    """Parse ``sheets.json``; an absent file is not an error (returns ())."""
    target_path = path or profiles_path()
    if not target_path.is_file():
        return ()
    try:
        raw = json.loads(target_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise BridgeError("bad_profiles", f"cannot read {target_path.name}") from exc
    except ValueError as exc:
        raise BridgeError("bad_profiles", f"{target_path.name} is not valid JSON") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), list):
        raise BridgeError("bad_profiles", f"{target_path.name} needs a top-level 'profiles' list")

    targets = tuple(
        _target_from_json(entry, where=f"profile #{index + 1}")
        for index, entry in enumerate(raw["profiles"])
    )
    seen: set[str] = set()
    for target in targets:
        key = target.name.casefold()
        if key in seen:
            raise BridgeError("bad_profiles", f"duplicate profile name {target.name!r}")
        seen.add(key)
    return targets


def save_profiles(targets: "tuple[SheetTarget, ...] | list[SheetTarget]", path: Path | None = None) -> None:
    """Write ``sheets.json`` atomically (temp file + replace)."""
    target_path = path or profiles_path()
    body = json.dumps(
        {"profiles": [t.to_json() for t in targets]}, ensure_ascii=False, indent=2
    ) + "\n"
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = target_path.with_suffix(target_path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target_path)
    except OSError as exc:
        raise BridgeError("bad_profiles", f"cannot write {target_path.name}") from exc


def find(targets: "tuple[SheetTarget, ...]", name: str) -> SheetTarget | None:
    key = (name or "").strip().casefold()
    for target in targets:
        if target.name.casefold() == key:
            return target
    return None


def add_profile(target: SheetTarget, path: Path | None = None) -> tuple[SheetTarget, ...]:
    existing = load_profiles(path)
    if find(existing, target.name) is not None:
        raise BridgeError("duplicate_profile", f"a profile named {target.name!r} already exists")
    updated = existing + (target,)
    save_profiles(updated, path)
    return updated


def remove_profile(name: str, path: Path | None = None) -> tuple[SheetTarget, ...]:
    existing = load_profiles(path)
    if find(existing, name) is None:
        raise BridgeError("profile_not_found", name)
    key = (name or "").strip().casefold()
    updated = tuple(t for t in existing if t.name.casefold() != key)
    save_profiles(updated, path)
    return updated


# -- verification ------------------------------------------------------------
def verify_target(sheets_service, target: SheetTarget) -> dict:
    """Read-only check that a profile will actually work, run when it is added.

    Confirms the signed-in account can open the spreadsheet, that the gid points
    at a real tab, and that row 1 matches the column layout the bridge reads by
    position. Catching all three here turns "the first live run failed" into an
    error message next to the input box, and costs nothing.

    Returns a small summary for the UI; raises ``BridgeError`` on any problem.
    """
    from . import columns as C
    from .rows import is_eligible, rows_from_grid
    from .sheets import SheetClient, apply_cell_kinds, control_cells_in_use

    sheet = SheetClient(sheets_service, target.spreadsheet_id, target.gid)
    tab = sheet.tab()                    # raises tab_not_found for a wrong gid
    grid = sheet.read_grid()
    if not grid:
        raise BridgeError("empty_sheet", "the tab has no header row")

    layout = C.Layout.from_header(grid[0])
    problems = layout.problems()
    if problems:
        raise BridgeError("column_layout_mismatch", "; ".join(problems))

    rows = rows_from_grid(grid, layout)
    used = control_cells_in_use(layout, grid[0], rows)
    if used:
        raise BridgeError(
            "control_column_conflict",
            f"column(s) {', '.join(used)} hold data but have no header; add a header or free the columns",
        )
    warnings: list[str] = []
    first = next((row for row in rows if not row.is_blank), None)
    if first is not None:
        # Same guard the run applies: a computed roll-up tab is refused here,
        # a self-stamping timestamp column is noted.
        layout, warnings = apply_cell_kinds(layout, sheet.probe_cell_kinds(first.number, layout.last_index))
    for name in layout.missing_recommended():
        warnings.append(f"no {C.LABELS[name]!r} column: folder links cannot be resolved to one PDF")
    if layout.catalog and not target.output_folder:
        # A catalog's notes have no source folder to sit beside.
        raise BridgeError(
            "output_folder_required",
            "this tab lists lecture PDF links: add the Drive folder its notes are filed in",
        )
    return {
        "catalog": layout.catalog,
        "tabTitle": tab.title,
        "rows": sum(1 for row in rows if not row.is_blank),
        "eligible": sum(1 for row in rows if is_eligible(row)),
        "columns": layout.describe(),
        "warnings": warnings,
    }


def verify_workbook(sheets_service, target: SheetTarget) -> dict:
    """Read-only check that a workbook profile has something to run."""
    from .tabs import KIND_COMPUTED, list_tabs

    title, summaries = list_tabs(sheets_service, target.spreadsheet_id)
    runnable = [s for s in summaries if s.complete]
    if not runnable:
        raise BridgeError(
            "no_runnable_tabs",
            f"none of the {len(summaries)} tab(s) in {title!r} has the columns the automation needs",
        )
    return {
        "workbook": title,
        "tabs": [s.public() for s in runnable],
        "runnable": len(runnable),
        "eligible": sum(s.eligible or 0 for s in runnable),
        "computed": [s.title for s in summaries if s.kind == KIND_COMPUTED],
    }


def expand_workbook(sheets_service, target: SheetTarget) -> tuple[str, list[SheetTarget], list[str]]:
    """(workbook title, one target per tab with rows ready, what was skipped).

    Called at run time, never when the profile is saved, so a batch tab added
    later is picked up on its own. Computed roll-ups, templates and tabs
    without the needed columns are skipped with a reason; so are notes tabs
    with nothing ready. The workbook's cap is applied by the batch as one
    budget across its tabs, so the sub-targets carry none of their own.
    """
    from .tabs import KIND_OTHER, list_tabs

    title, summaries = list_tabs(sheets_service, target.spreadsheet_id)
    chosen: list[SheetTarget] = []
    skipped: list[str] = []
    for summary in summaries:
        if not summary.supported:
            if summary.kind != KIND_OTHER:
                skipped.append(f"{summary.title} ({summary.kind})")
            continue
        if not summary.complete:
            # Runnable by hand (file links need no lecture number) but not
            # unattended: with folder links every row would fail, and those
            # failures would be written into a tab nobody meant to automate.
            skipped.append(f"{summary.title} (no Lecture Number column)")
            continue
        if not summary.eligible:
            skipped.append(f"{summary.title} (nothing ready)")
            continue
        if getattr(summary, "catalog", False) and not target.output_folder:
            skipped.append(f"{summary.title} (catalog of PDF links, but the profile has no output folder)")
            continue
        chosen.append(replace(
            target,
            name=f"{target.name} / {summary.title}",
            gid=summary.gid,
            all_tabs=False,
            max_cost_inr=None,
        ))
    return title, chosen, skipped


# -- resolution --------------------------------------------------------------
def env_target(settings) -> "SheetTarget | None":
    """The single implicit profile described by the .env variables, or None
    when no sheet is configured there.

    No signature here: both entry points already seed RunOptions from
    settings.signature, and a target-level value would beat an explicit
    --signature on the command line.
    """
    if not (settings.spreadsheet_id or "").strip():
        return None
    return SheetTarget(
        name="default",
        spreadsheet_id=settings.spreadsheet_id,
        gid=settings.gid,
        max_cost_inr=settings.max_cost_inr,
    )


def resolve_targets(settings, path: Path | None = None) -> tuple[SheetTarget, ...]:
    """Configured profiles, or the single env-derived target when none exist."""
    targets = load_profiles(path)
    if targets:
        return targets
    env = env_target(settings)
    return (env,) if env is not None else ()


def select(
    targets: "tuple[SheetTarget, ...]",
    names: "list[str] | tuple[str, ...] | None",
    *,
    all_profiles: bool = False,
) -> tuple[SheetTarget, ...]:
    """Pick the targets to run.

    No selection with several profiles is refused rather than guessed: running
    every sheet because a flag was forgotten spends real money.
    """
    enabled = tuple(t for t in targets if t.enabled)
    if all_profiles:
        if not enabled:
            raise BridgeError("no_profiles", "no enabled profiles")
        return enabled
    if names:
        chosen = []
        for name in names:
            found = find(targets, name)
            if found is None:
                known = ", ".join(t.name for t in targets) or "(none)"
                raise BridgeError("profile_not_found", f"{name!r}; known profiles: {known}")
            chosen.append(found)
        return tuple(chosen)
    if len(enabled) == 1:
        return enabled
    if not enabled:
        raise BridgeError(
            "no_profiles",
            "no sheet configured: add one in the Studio (+ Add sheet) or set "
            "SHEETS_BRIDGE_SPREADSHEET_ID / SHEETS_BRIDGE_GID in .env",
        )
    known = ", ".join(t.name for t in enabled)
    raise BridgeError(
        "profile_required",
        f"several profiles are configured; pass --profile NAME or --all-profiles ({known})",
    )
