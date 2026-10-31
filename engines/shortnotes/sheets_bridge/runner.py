"""Sequential per-row orchestration: resolve -> claim -> download -> generate ->
upload -> mark done, with per-row error isolation and a final summary."""
from __future__ import annotations

import secrets
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import columns as C
from .errors import BridgeError, describe_exception, error_code_for, safe_print  # noqa: F401 - re-exported
from .lecture_match import choose_lecture_pdf, parse_lecture_number
from .links import KIND_FOLDER, DriveLink, parse_drive_link
from .mapping import SheetHeader, map_language, normalize_signature, output_name, resolve_subject, sheet_header
from notesbot import generation_profile
from notesbot.errors import ModelBudgetError

from . import output_cache
from .generator import GenerationResult
from .rows import SheetRow, is_eligible, rows_from_grid
from .sheets import apply_cell_kinds, control_cells_in_use


def new_task_id() -> str:
    """One id per lecture row; every model call of that row carries it."""
    from .pw_gate import new_task_id as _new

    return _new()


OUTCOME_DONE = "done"
OUTCOME_FAILED = "failed"
OUTCOME_PLANNED = "planned"        # dry-run only
OUTCOME_STOPPED = "stopped"        # cost cap reached before this row
OUTCOME_SKIPPED = "skipped"        # catalog row that is not a lecture (skip list)


@dataclass(frozen=True)
class RunOptions:
    dry_run: bool = False
    limit: int | None = None
    row: int | None = None
    redo: bool = False
    show_page_numbers: bool = True
    subject_mode: str = "sheet"
    # NEET-JEE / Foundation: whose format commands the notes follow.
    category: str = "neet_jee"
    # Sanskrit lectures: "hindi" = verses verbatim with Hindi meanings (the
    # default); "strict" = everything in Sanskrit.
    sanskrit_style: str = "hindi"
    max_cost_inr: float | None = None
    signature_override: str = ""
    max_input_bytes: int = 100 * 1024 * 1024
    # Spend already booked by earlier sheets in the same batch. The cap is a
    # budget for the whole run, not per sheet, so a multi-sheet batch carries
    # what it has spent so far into each target.
    cost_spent_before: float = 0.0
    # Catalog tabs (PDF links): the Drive folder the notes are filed under,
    # and the batches that are not lectures (None -> the built-in list).
    output_folder_id: str = ""
    skip_batches: tuple | None = None


@dataclass(frozen=True)
class RowOutcome:
    row: int
    outcome: str
    code: str = ""
    link: str = ""
    cost_inr: float = 0.0
    note: str = ""


@dataclass
class RunSummary:
    run_id: str
    outcomes: list[RowOutcome] = field(default_factory=list)
    total_cost_inr: float = 0.0
    skipped_not_eligible: Counter = field(default_factory=Counter)
    interrupted: bool = False

    def counts(self) -> Counter:
        return Counter(o.outcome for o in self.outcomes)


# Failures of choosing the lecture file inside a folder; their detail names
# the files involved.
_FILE_CHOICE_CODES = frozenset({
    "lecture_ambiguous", "lecture_not_found", "chapter_not_found", "lecture_number_missing", "no_pdf_in_folder",
})


@dataclass(frozen=True)
class _Resolved:
    file: object            # drive.DriveFile
    file_id: str
    file_name: str
    how: str                # "file_link" | "lecture_token" | "loose_number" | "single_file"
    folder_id: str
    drive_id: str
    out_name: str           # the studygram PDF - the row's only output
    existing_link: str      # non-empty when the output already exists
    language: str
    subject: str
    signature: str
    header: SheetHeader = SheetHeader()   # page-1 subject / chapter / lecture from D, G, H
    existing_id: str = ""   # the existing output PDF, which a "Regenerate" overwrites


def _now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def _final_stamp_text() -> str:
    """Locale-safe value for column P; Sheets parses ISO 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


class Runner:
    def __init__(
        self,
        sheet,
        drive,
        options: RunOptions,
        *,
        generate_fn: Callable | None = None,
        run_id: str | None = None,
        out: Callable[[str], None] | None = None,
        clock: Callable[[], str] = _now_text,
        final_clock: Callable[[], str] = _final_stamp_text,
        stop_check: Callable[[], bool] | None = None,
        on_progress: Callable[[dict], None] | None = None,
        task_id: str = "",
        access=None,
        fetch_url: Callable | None = None,
    ) -> None:
        self.sheet = sheet
        self.drive = drive
        # Catalog rows link straight to a PDF on the web (static.pw.live).
        if fetch_url is None:
            from .http_source import download_pdf as fetch_url
        self.fetch_url = fetch_url
        self._catalog = None          # CatalogFlow, when the tab is a catalog
        self.options = options
        self.access = access          # pw_gate.ProxyAccess - allowlist gate
        self.final_clock = final_clock
        self.stop_check = stop_check or (lambda: False)
        self.on_progress = on_progress
        # One task_id for the whole run: the proxy writes a raw usage row per
        # provider call, and its own guidance is to combine them by task_id.
        self.task_id = task_id or new_task_id()
        if generate_fn is None:
            from .generator import generate_notes_pdf

            generate_fn = generate_notes_pdf
        self.generate_fn = generate_fn
        self.run_id = run_id or new_run_id()
        # Resolved from the header row in run(); the reference layout until then.
        self.layout: C.Layout = C.DEFAULT_LAYOUT
        # Folder listings fetched this run. Rows of one batch share folders
        # (6-8 lectures per chapter folder in production), and the listing
        # already answers the duplicate check, so each folder is read once.
        self._folder_cache: dict[tuple[str, str], list] = {}
        self.out = out or safe_print
        self.clock = clock
        self.summary = RunSummary(run_id=self.run_id)
        self._interrupted: RowOutcome | None = None
        self._position = 0
        self._total = 0

    # -- helpers --------------------------------------------------------------
    def _log(self, row: SheetRow | None, message: str) -> None:
        """Row lines carry their position in the run.

        A lecture takes minutes, so "[2/5 row 3]" is what tells the operator
        how far along the batch is while it is still working on that row.
        """
        if row is None:
            self.out(message)
            return
        place = f"{self._position}/{self._total} " if self._total else ""
        self.out(f"[{place}row {row.number}] {message}")

    def _report(self, outcome: RowOutcome) -> None:
        """One progress line per finished row, plus the caller's callback."""
        counts = self.summary.counts()
        done = counts.get(OUTCOME_DONE, 0)
        failed = counts.get(OUTCOME_FAILED, 0)
        left = max(self._total - self._position, 0)
        self.out(
            f"progress: {done} done, {failed} failed, {left} left"
            + (f" (INR {self.summary.total_cost_inr:.2f} so far)" if self.summary.total_cost_inr else "")
        )
        if self.on_progress is None:
            return
        try:
            self.on_progress({
                "row": outcome.row,
                "outcome": outcome.outcome,
                "position": self._position,
                "total": self._total,
                "done": done,
                "failed": failed,
                "costInr": round(self.summary.total_cost_inr, 2),
            })
        except Exception:  # noqa: BLE001 - progress reporting never fails a run
            pass

    def _control(self, status: str, *, link: str = "", code: str = "", cost: float | None = None) -> list:
        """Q = status (with the failure code when there is one), R = review state.

        A finished lecture sets R to "Review" - the dropdown a human switches
        to "Done" after checking the PDF. ``cost`` is tracked for the run
        summary and the PW usage ledger, never written to the sheet.
        """
        del cost, link
        if status in (C.STATUS_FAILED, C.STATUS_SKIPPED) and code:
            label = f"{status}: {code}"
        elif status == C.STATUS_CLAIMED:
            # The stamp lets another run (or a restart) tell a live claim from
            # one left behind by a crash - see rows.claim_is_stale.
            label = f"{status} {self.clock()}"
        else:
            label = status
        return [label, C.REVIEW_PENDING if status == C.STATUS_DONE else ""]

    def _final(self) -> str | None:
        """Timestamp for the content team's column, or None in dry runs."""
        return self.final_clock()

    def _row_still_there(self, row: SheetRow) -> bool:
        """A run lasts hours and people edit the sheet meanwhile. Rows are
        addressed by number, so an inserted, deleted or sorted row would put
        this row's result on someone else's lecture. Re-read the identity
        cells right before writing; refuse when they no longer match."""
        check = getattr(self.sheet, "read_row_identity", None)
        if check is None:
            return True
        try:
            current = tuple(check(row.number))
        except Exception as exc:  # noqa: BLE001 - best effort; the write itself will fail loudly
            self._log(row, f"could not re-check the row before writing: {describe_exception(exc)}")
            return True
        expected = tuple(row.named(name).strip() for name in self.layout.identity_columns)
        if current != expected:
            self._log(row, "row moved: the sheet changed under this run - not writing to it")
            return False
        return True

    def _write(self, row: SheetRow, status: str, *, final: bool = False, notes_link: str | None = None,
               folder_link: str | None = None, **kwargs) -> bool:
        if not self._row_still_there(row):
            return False
        try:
            # Catalog tabs also get the links to the notes filed in Drive and their folder.
            extra = {name: value for name, value in (("notes_link", notes_link), ("folder_link", folder_link))
                     if value is not None}
            values = self._control(status, **kwargs)
            if row.wants_regeneration and status in (C.STATUS_CLAIMED, C.STATUS_FAILED):
                # Until the fresh notes are in place the request stays in R, so an
                # interrupted or failed attempt is simply tried again next run.
                values[1] = C.REVIEW_REGENERATE
            self.sheet.write_control(
                row.number,
                values,
                final_timestamp=self._final() if final else None,
                **extra,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - never kill the batch on a sheet write
            self._log(row, f"sheet write of '{status}' failed: {describe_exception(exc)}")
            return False

    # -- selection ------------------------------------------------------------
    def select_rows(self, grid: list) -> list[SheetRow]:
        rows = rows_from_grid(grid, self.layout)
        if self.options.row is not None:
            rows = [r for r in rows if r.number == self.options.row]
            if not rows:
                raise BridgeError("row_not_found", f"row {self.options.row} has no data")
        selected: list[SheetRow] = []
        for row in rows:
            if is_eligible(row, redo=self.options.redo):
                selected.append(row)
            elif not row.is_blank:
                if row.layout.catalog and not row.drive_link.strip():
                    reason = "no PDF link"
                elif row.auto_status == C.STATUS_SKIPPED:
                    reason = "not a lecture"
                elif not row.layout.catalog and not row.bm_done:
                    reason = "J not Done"
                elif row.final_done:
                    reason = "M already Done"
                elif row.auto_status in (C.STATUS_DONE, "done") or row.review_status:
                    reason = "already generated"
                elif row.auto_status == C.STATUS_CLAIMED:
                    reason = "claimed by another run"
                else:
                    reason = "Q " + (row.auto_status or "?")
                self.summary.skipped_not_eligible[reason] += 1
        if self.options.limit is not None:
            selected = selected[: max(0, self.options.limit)]
        return selected

    # -- per-row stages -------------------------------------------------------
    def _link_for(self, row: SheetRow) -> DriveLink:
        """Parse the Drive Link cell; fall back to the cell's hyperlink/chip URL
        when the visible text is not itself a Drive link."""
        try:
            return parse_drive_link(row.drive_link)
        except BridgeError as exc:
            lookup = getattr(self.sheet, "cell_hyperlink", None)
            if exc.code not in ("invalid_link", "blank_link") or lookup is None:
                raise
            url = lookup(row.number, row.layout.require("drive_link"))
            if not url:
                raise
            return parse_drive_link(url)

    def _children(self, folder_id: str, drive_id: str) -> list:
        """A folder's listing, fetched once per run."""
        key = (folder_id, drive_id or "")
        if key not in self._folder_cache:
            self._folder_cache[key] = self.drive.list_folder(folder_id, drive_id)
        return self._folder_cache[key]

    def _forget_folder(self, folder_id: str, drive_id: str) -> None:
        """After an upload the cached listing is stale for that folder."""
        self._folder_cache.pop((folder_id, drive_id or ""), None)

    def _existing_output(self, folder_id: str, name: str, drive_id: str) -> tuple[str, str]:
        """(link, id of the PDF) of an output already in the folder; ("", "")
        when there is none. The id is "" for an output that is not a PDF (the
        review documents of older builds), which a regeneration cannot overwrite.

        Uses the listing the lecture match just fetched when there is one -
        it is the same non-trashed-children query, so no second request.
        """
        cached = self._folder_cache.get((folder_id, drive_id or ""))
        if cached is not None:
            for entry in cached:
                if getattr(entry, "name", "") == name:
                    file_id = str(getattr(entry, "id", "") or "")
                    link = str(getattr(entry, "web_view_link", "") or "")
                    is_pdf = getattr(entry, "mime_type", "") == "application/pdf"
                    return (link or f"https://docs.google.com/document/d/{file_id}/edit"), (file_id if is_pdf else "")
            return "", ""
        existing = self.drive.find_in_folder(folder_id, name, drive_id)
        if not existing:
            return "", ""
        file_id = str(existing[0].get("id") or "")
        is_pdf = existing[0].get("mimeType", "application/pdf") == "application/pdf"
        link = str(existing[0].get("webViewLink") or "") or f"https://docs.google.com/document/d/{file_id}/edit"
        return link, (file_id if is_pdf else "")

    def _locate_source(self, row: SheetRow) -> tuple[object, str, str]:
        """-> (source DriveFile, folder id, how). A folder link is resolved to
        the lecture PDF via column H; a file link is used directly."""
        link = self._link_for(row)
        target = self.drive.get_file(link.id)
        if link.kind == KIND_FOLDER or getattr(target, "is_folder", False):
            if not getattr(target, "is_folder", False):
                raise BridgeError("not_a_folder")
            children = self._children(target.id, target.drive_id)
            number = parse_lecture_number(row.lecture_number_text)
            match = choose_lecture_pdf(children, number, chapter=row.chapter, folder_name=str(getattr(target, "name", "") or ""))
            how = f"{match.how}; {match.note}" if getattr(match, "note", "") else match.how
            return match.file, target.id, how
        if not target.parents:
            raise BridgeError("no_parent_folder")
        return target, target.parents[0], "file_link"

    def _resolve(self, row: SheetRow) -> _Resolved:
        language = map_language(row.batch)
        if language == "sanskrit" and self.options.sanskrit_style != "strict":
            language = "sanskrit_hindi"
        subject = resolve_subject(self.options.subject_mode, row.subject)
        signature = normalize_signature(self.options.signature_override or row.faculty)

        file, folder_id, how = self._locate_source(row)
        out_name = output_name(file.name)
        existing_link, existing_id = self._existing_output(folder_id, out_name, file.drive_id)
        if not existing_link:
            # Outputs made by earlier builds carry an older name.
            from notesbot.output_naming import legacy_output_names
            for legacy in legacy_output_names(file.name):
                existing_link, existing_id = self._existing_output(folder_id, legacy, file.drive_id)
                if existing_link:
                    break
        return _Resolved(
            file=file,
            file_id=file.id,
            file_name=file.name,
            how=how,
            folder_id=folder_id,
            drive_id=file.drive_id,
            out_name=out_name,
            existing_link=existing_link,
            language=language,
            subject=subject,
            signature=signature,
            header=sheet_header(
                subject=row.subject, chapter=row.chapter, lecture=row.lecture_number_text,
                language="hindi" if language == "sanskrit_hindi" else language),
            existing_id=existing_id,
        )

    def _process(self, row: SheetRow) -> RowOutcome:
        if self._catalog is not None:
            return self._catalog.process(row)
        # 1) resolve (read-only): link -> file -> parent folder -> duplicate check
        try:
            resolved = self._resolve(row)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            code = error_code_for(exc, "resolve")
            # Which files clashed or were missing is our own wording, never
            # document or provider text, and it is what the operator must fix.
            detail = getattr(exc, "detail", "") if code in _FILE_CHOICE_CODES else ""
            self._log(row, f"resolve failed: {code}{f' - {detail}' if detail else ''} ({describe_exception(exc)})")
            if not self.options.dry_run:
                self._write(row, C.STATUS_FAILED, code=code, cost=0.0)
            return RowOutcome(row.number, OUTCOME_FAILED, code=code)

        plan = (
            f"file={resolved.file_id} ({resolved.how}) name={resolved.file_name!r} -> {resolved.out_name!r} "
            f"folder={resolved.folder_id} lang={resolved.language} subject={resolved.subject} "
            f"sig={resolved.signature!r} header=({resolved.header.subject!r}, "
            f"{resolved.header.chapter!r}, {resolved.header.lecture!r})"
        )
        regenerate = row.wants_regeneration
        if self.options.dry_run:
            if regenerate:
                state = ("marked Regenerate -> would make fresh notes and replace "
                         + ("the old PDF" if resolved.existing_id else "(no old PDF to replace)"))
            else:
                state = "output already exists -> would mark done" if resolved.existing_link else "would generate"
            self._log(row, f"PLAN {state}: {plan}")
            return RowOutcome(row.number, OUTCOME_PLANNED, link=resolved.existing_link, note=state)

        # 2) output already present: skip generation, mark done
        if resolved.existing_link and not regenerate:
            self._log(row, f"notes already in the folder, marking for review: {resolved.out_name!r}")
            ok = self._write(row, C.STATUS_DONE, link=resolved.existing_link, cost=0.0, final=True)
            return RowOutcome(
                row.number,
                OUTCOME_DONE if ok else OUTCOME_FAILED,
                code="" if ok else "sheet_write_failed",
                link=resolved.existing_link,
                note="existing",
            )

        # 3) allowlist gate: every paid action is checked, and fails closed
        task_id = ""
        if self.access is not None:
            try:
                self.access.ensure_allowed()
                task_id = self.task_id
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                code = error_code_for(exc, "allowlist")
                self._log(row, f"access denied: {code}")
                self._write(row, C.STATUS_FAILED, code=code, cost=0.0)
                return RowOutcome(row.number, OUTCOME_FAILED, code=code)

        # 4) claim BEFORE any expensive work so an interrupted row is visible
        if not self._write(row, C.STATUS_CLAIMED):
            return RowOutcome(row.number, OUTCOME_FAILED, code="sheet_write_failed:claim")
        self._log(row, f"claimed: {plan}" + (f" task={task_id}" if task_id else ""))

        cost = 0.0
        stage = "download"
        cache_key = output_cache.key(
            file_id=resolved.file_id,
            out_name=resolved.out_name,
            language=resolved.language,
            subject=resolved.subject,
            signature=resolved.signature,
            header="\x00".join((resolved.header.subject, resolved.header.chapter, resolved.header.lecture)),
            show_page_numbers=self.options.show_page_numbers,
            category=self.options.category,
            sanskrit_style=self.options.sanskrit_style,
            signature_fixed=bool(self.options.signature_override),
        )
        if regenerate:
            # Fresh notes were asked for: never hand back the stored copy of the old ones.
            output_cache.discard(cache_key)
            self._log(row, "marked Regenerate: making fresh notes")
        try:
            # Reuse the exact approved bytes after an interrupted upload.
            cached = output_cache.load(cache_key)
            finalized = output_cache.load_finalized(cache_key, cached) if cached is not None else None
            if finalized is not None:
                self._log(row, f"reusing the notes generated by an earlier attempt ({len(cached):,} bytes, no new AI cost)")
                pdf, quality_report = finalized
                result = GenerationResult(pdf=pdf, notes_json=cached, cost_inr=0.0, task_id=task_id, quality_report=quality_report)
            else:
                pdf_bytes = self.drive.download_pdf(resolved.file, self.options.max_input_bytes)
                stage = "generate"
                self._log(row, f"generating ({len(pdf_bytes):,} bytes source) ...")
                with generation_profile.exam_category(self.options.category), \
                        generation_profile.exam_subject(resolved.subject), \
                        generation_profile.sanskrit_style_as(self.options.sanskrit_style), \
                        generation_profile.fixed_signature(bool(self.options.signature_override)):
                    result = self.generate_fn(
                        pdf_bytes,
                        language=resolved.language,
                        subject=resolved.subject,
                        signature=resolved.signature,
                        filename=resolved.file_name,
                        task_id=task_id,
                        log=lambda message: self._log(row, message),
                        header=resolved.header,
                        show_page_numbers=self.options.show_page_numbers,
                    )
                del pdf_bytes
            cost = float(result.cost_inr)
            stage = "quality"
            from notesbot.final_quality import valid_receipt
            if not valid_receipt(result.notes_json, result.pdf, result.quality_report):
                from notesbot.errors import NotesQualityError
                raise NotesQualityError()
            output_cache.store_finalized(cache_key, result.notes_json, result.pdf, result.quality_report)
            stage = "upload"
            if regenerate and resolved.existing_id:
                # The old PDF gets the fresh notes as a new version: same file, same link.
                created = self.drive.update_pdf(resolved.existing_id, result.pdf, name=resolved.out_name)
                self._log(row, f"replaced the old notes PDF with the fresh notes: {resolved.out_name!r}")
            else:
                created = self.drive.upload_pdf(resolved.folder_id, resolved.out_name, result.pdf, resolved.drive_id)
                self._log(row, f"uploaded the studygram PDF: {resolved.out_name!r}")
            self._forget_folder(resolved.folder_id, resolved.drive_id)
            link = str(created.get("webViewLink") or "") or f"https://drive.google.com/file/d/{created.get('id')}/view"
        except KeyboardInterrupt as exc:
            partial = max(cost, float(getattr(exc, "partial_cost_inr", 0.0) or 0.0))
            self._write(row, C.STATUS_FAILED, code="interrupted", cost=partial)
            self._interrupted = RowOutcome(row.number, OUTCOME_FAILED, code="interrupted", cost_inr=partial)
            raise
        except Exception as exc:  # noqa: BLE001
            # Money already spent before the failing stage must still be recorded.
            partial = max(cost, float(getattr(exc, "partial_cost_inr", 0.0) or 0.0))
            failed_stage = str(getattr(exc, "failed_stage", "") or stage)
            code = error_code_for(exc, failed_stage)
            self._log(row, f"{failed_stage} failed: {code} ({describe_exception(exc)})")
            if isinstance(exc, ModelBudgetError):
                self._log(row, str(exc))
            self._write(row, C.STATUS_FAILED, code=code, cost=partial)
            return RowOutcome(row.number, OUTCOME_FAILED, code=code, cost_inr=partial)

        # The approved bytes stay cached for an upload retry without new AI cost.
        output_cache.touch(cache_key)
        ok = self._write(row, C.STATUS_DONE, link=link, cost=cost, final=True)
        if not ok:
            # The file IS uploaded; the next run's duplicate check will mark it done.
            return RowOutcome(row.number, OUTCOME_FAILED, code="sheet_write_failed:done", link=link, cost_inr=cost)
        self._log(row, f"done: {link}  cost=INR {cost:.2f}")
        return RowOutcome(row.number, OUTCOME_DONE, link=link, cost_inr=cost)

    def _open_catalog(self, rows: list[SheetRow]):
        """The catalog flow for this tab: its output folder, opened read-only."""
        from .catalog_flow import CatalogFlow
        from .destination import FolderTree

        folder_id = (self.options.output_folder_id or "").strip()
        if not folder_id:
            raise BridgeError(
                "output_folder_missing",
                "this tab lists lecture PDF links: set the Drive folder its notes are filed in "
                "(outputFolder in sheets.json, the Studio's + Add sheet, or --output-folder)",
            )
        tree = FolderTree.open(self.drive, folder_id)
        self._log(None, f"catalog tab: notes are filed in Drive folder {tree.root.name!r} "
                        "as exam / batch / subject / chapter / <lecture> Concise Notes.pdf")
        links = Counter(row.drive_link.strip() for row in rows if not row.is_blank and row.drive_link.strip())
        repeated = sum(count - 1 for count in links.values() if count > 1)
        if repeated:
            self._log(None, f"{repeated} row(s) repeat a PDF listed on another row: its notes are made once and copied")
        return CatalogFlow(self, tree, links)

    def _guard_formulas(self, layout: C.Layout, rows: list[SheetRow]) -> C.Layout:
        """Refuse a computed roll-up tab; stop stamping a self-stamping column.

        One request on the first data row - see ``sheets.apply_cell_kinds``.
        """
        probe = getattr(self.sheet, "probe_cell_kinds", None)
        first = next((row for row in rows if not row.is_blank), None)
        if probe is None or first is None:
            return layout
        layout, notes = apply_cell_kinds(layout, probe(first.number, layout.last_index))
        for note in notes:
            self._log(None, note)
        return layout

    # -- main loop --------------------------------------------------------------
    def run(self) -> RunSummary:
        grid = self.sheet.read_grid()
        if not grid:
            raise BridgeError("empty_sheet", "the tab has no header row")
        header = grid[0]
        # Columns are found by their header text, so a tab laid out differently
        # still reads the right cells; a tab missing what the bridge needs is
        # refused here - before anything is selected, claimed or generated, in
        # dry runs too - instead of being misread in silence.
        layout = C.Layout.from_header(header)
        problems = layout.problems()
        if problems:
            raise BridgeError("column_layout_mismatch", "; ".join(problems))
        rows = rows_from_grid(grid, layout)
        layout = self._guard_formulas(layout, rows)
        used = control_cells_in_use(layout, header, rows)
        if used:
            raise BridgeError(
                "control_column_conflict",
                f"column(s) {', '.join(used)} hold data but have no header; add a header or free the columns",
            )
        self.layout = layout
        self.sheet.layout = layout
        self._log(None, "columns: " + layout.describe_text())
        for name in layout.missing_recommended():
            self._log(None, f"no {C.LABELS[name]!r} column: folder links cannot be resolved to one PDF")
        if layout.catalog:
            # Refused before anything is written when there is nowhere to file.
            self._catalog = self._open_catalog(rows)
        letters = f"{C.column_letter(layout.control_indexes[0])}1:{C.column_letter(layout.control_indexes[-1])}1"
        if self.options.dry_run:
            complete, conflicts = self.sheet.check_control_headers(header)
            if conflicts:
                raise BridgeError("control_header_conflict", ", ".join(conflicts))
            if not complete:
                self._log(None, f"control headers {letters} are missing; a live run would create them")
        else:
            try:
                self.sheet.ensure_review_validation(len(grid))
            except Exception as exc:  # noqa: BLE001 - the dropdown is cosmetic
                self._log(None, f"could not set the review dropdown: {describe_exception(exc)}")
            if self.sheet.ensure_control_headers(header):
                self._log(None, f"created control headers {letters}")
            try:
                if self.sheet.clear_legacy_columns(len(grid)):
                    self._log(None, "cleared the retired columns S:V (Auto Run ID / Error Code / Timestamp / Cost)")
            except Exception as exc:  # noqa: BLE001 - cosmetic cleanup only
                self._log(None, f"could not clear the retired columns: {describe_exception(exc)}")

        selected = self.select_rows(grid)
        if layout.catalog and self.options.row is None:
            # Rows filed before the Lecture Folder Link column existed get it
            # now; the rows this run processes get it when they finish.
            from .catalog_flow import fill_missing_folder_links
            chosen = {row.number for row in selected}
            fill_missing_folder_links(self, [row for row in rows if row.number not in chosen])
        self._total = len(selected)
        mode = "DRY RUN" if self.options.dry_run else "LIVE"
        self._log(None, f"{mode} run {self.run_id}: {len(selected)} row(s) selected")

        try:
            for index, row in enumerate(selected):
                if self.stop_check():
                    remaining = selected[index:]
                    self._log(None, f"stop requested; {len(remaining)} row(s) left untouched")
                    for left in remaining:
                        self.summary.outcomes.append(RowOutcome(left.number, OUTCOME_STOPPED, code="stop_requested"))
                    self.summary.interrupted = True
                    break
                cap = self.options.max_cost_inr
                spent = self.options.cost_spent_before + self.summary.total_cost_inr
                if cap is not None and spent > cap:
                    remaining = selected[index:]
                    self._log(None, f"cost cap INR {cap:.2f} passed (spent INR {spent:.2f}); stopping with {len(remaining)} row(s) left")
                    for left in remaining:
                        self.summary.outcomes.append(RowOutcome(left.number, OUTCOME_STOPPED, code="cost_cap"))
                    break
                self._position = index + 1
                outcome = self._process(row)  # strictly sequential: no concurrency
                self.summary.outcomes.append(outcome)
                self.summary.total_cost_inr += outcome.cost_inr
                self._report(outcome)
                if outcome.code.endswith(":" + ModelBudgetError.code):
                    remaining = selected[index + 1:]
                    self._log(None, f"model gateway budget exhausted; stopping with {len(remaining)} row(s) pending")
                    self.summary.outcomes.extend(
                        RowOutcome(left.number, OUTCOME_STOPPED, code=ModelBudgetError.code)
                        for left in remaining
                    )
                    # Batch.run_one also stops later tabs on an interrupted run.
                    self.summary.interrupted = True
                    break
        except KeyboardInterrupt:
            self.summary.interrupted = True
            if self._interrupted is not None:
                self.summary.outcomes.append(self._interrupted)
                self.summary.total_cost_inr += self._interrupted.cost_inr
            self._log(None, "interrupted by user")

        self._print_summary()
        return self.summary

    def _print_summary(self) -> None:
        counts = self.summary.counts()
        parts = [f"{name}={counts.get(name, 0)}" for name in (OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_PLANNED, OUTCOME_STOPPED)]
        if counts.get(OUTCOME_SKIPPED):
            parts.append(f"{OUTCOME_SKIPPED}={counts[OUTCOME_SKIPPED]}")
        skipped = ", ".join(f"{k}={v}" for k, v in sorted(self.summary.skipped_not_eligible.items()))
        self._log(None, "-" * 60)
        hint = "" if self.options.redo or not skipped else " - use --redo to force (an existing PDF is adopted, not regenerated)"
        self._log(None, f"run {self.run_id} summary: " + " ".join(parts) + (f"  (skipped not eligible: {skipped}{hint})" if skipped else ""))
        self._log(None, f"total cost: INR {self.summary.total_cost_inr:.2f}")
        failed = [o for o in self.summary.outcomes if o.outcome == OUTCOME_FAILED]
        if failed:
            by_code = Counter(o.code for o in failed)
            self._log(None, "failure codes: " + ", ".join(f"{code}={n}" for code, n in by_code.most_common()))


def run_batch(sheet, drive, options: RunOptions, **kwargs) -> RunSummary:
    return Runner(sheet, drive, options, **kwargs).run()

