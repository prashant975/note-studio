"""Run several sheet profiles in one pass.

The per-row engine (:class:`~sheets_bridge.runner.Runner`) already takes an
already-bound ``SheetClient``, so driving several sheets is a matter of
building one client per target and looping. What is genuinely shared across
the loop lives here:

* **Isolation** - a target that cannot be opened at all (missing tab, no
  permission, a column layout that does not match) fails *that profile* and the
  batch carries on, mirroring the per-row isolation inside ``Runner``.
* **Budgets** - a global ``--max-cost`` is a ceiling for the whole run, a
  profile's own ``maxCostInr`` a ceiling for that profile, carried into each
  runner via ``RunOptions.cost_spent_before``.
* **Workbooks** - a profile saved with ``allTabs`` is expanded *now*, at run
  time, into every tab of its spreadsheet that has the columns the bridge
  needs and rows ready, so a batch tab added since the profile was saved is
  picked up without anyone touching the configuration.
* **Stopping** - an operator interrupt inside one sheet ends the whole batch;
  the next sheet must not start claiming and paying for rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import run_log
from .errors import BridgeError, describe_exception
from .runner import (
    OUTCOME_DONE,
    OUTCOME_FAILED,
    RunOptions,
    RunSummary,
    Runner,
    new_run_id,
    new_task_id,
)


@dataclass
class TargetResult:
    """Outcome of one profile: a RunSummary, why it never started, or why
    there was nothing to do."""

    name: str
    spreadsheet_id: str
    gid: int
    summary: RunSummary | None = None
    error: str = ""
    note: str = ""              # nothing ran, and that is not a failure

    @property
    def cost_inr(self) -> float:
        return self.summary.total_cost_inr if self.summary else 0.0

    @property
    def done(self) -> int:
        return self.summary.counts().get(OUTCOME_DONE, 0) if self.summary else 0

    @property
    def failed(self) -> int:
        return self.summary.counts().get(OUTCOME_FAILED, 0) if self.summary else 0


@dataclass
class BatchSummary:
    batch_id: str
    results: list[TargetResult] = field(default_factory=list)

    @property
    def total_cost_inr(self) -> float:
        return sum(r.cost_inr for r in self.results)

    @property
    def total_done(self) -> int:
        return sum(r.done for r in self.results)

    @property
    def total_failed(self) -> int:
        return sum(r.failed for r in self.results)

    @property
    def interrupted(self) -> bool:
        return any(r.summary is not None and r.summary.interrupted for r in self.results)


def options_for(
    target,
    options: RunOptions,
    *,
    spent: float,
    group_cap: float | None = None,
    group_spent: float = 0.0,
) -> RunOptions:
    """Per-profile options.

    A global ``--max-cost`` is one budget for the whole batch, so cumulative
    spend is carried into each sheet. Otherwise a workbook's cap is one budget
    for all of its tabs (``group_cap`` / ``group_spent``), and a single
    profile's own ``maxCostInr`` is a cap for *that sheet*, so it starts from
    zero - money spent on sheet A must not stop sheet B before its first row.
    """
    changes: dict = {}
    if options.max_cost_inr is not None:
        changes["cost_spent_before"] = spent
    elif group_cap is not None:
        changes["max_cost_inr"] = group_cap
        changes["cost_spent_before"] = group_spent
    else:
        changes["cost_spent_before"] = 0.0
        if target.max_cost_inr is not None:
            changes["max_cost_inr"] = target.max_cost_inr
    if target.signature:
        changes["signature_override"] = target.signature
    # Catalog tabs: where the notes are filed, and which batches are not lectures.
    if getattr(target, "output_folder", ""):
        changes["output_folder_id"] = target.output_folder
    if getattr(target, "skip_batches", None) is not None:
        changes["skip_batches"] = tuple(target.skip_batches)
    return replace(options, **changes)


class _Batch:
    """State shared by every target of one run."""

    def __init__(self, sheets_service, drive, options: RunOptions, *, access, emit, external_stop, operator, on_progress=None):
        self.sheets_service = sheets_service
        self.drive = drive
        self.options = options
        self.access = access
        self.emit = emit
        self.external_stop = external_stop
        self.operator = operator
        self.on_progress = on_progress
        # One task_id for the whole button press, across every sheet it runs.
        self.task_id = new_task_id()
        self.spent = 0.0
        # An operator's Ctrl+C is caught inside Runner.run() (the row is marked
        # 'failed: interrupted' and the summary returned). It must end the
        # whole batch, not just that sheet.
        self.aborted = False

    def _progress_for(self, target):
        """Tag each row event with its sheet, so a batch over several tabs
        reports "3/5 in <tab>" rather than a bare row number."""
        if self.on_progress is None:
            return None

        def report(event: dict) -> None:
            self.on_progress({**event, "sheet": target.name})

        return report

    def should_stop(self) -> bool:
        return self.aborted or self.external_stop()

    def stopped(self, target) -> TargetResult:
        self.emit(f"stop requested; skipping profile '{target.name}'")
        return TargetResult(target.name, target.spreadsheet_id, target.gid, error="stop_requested")

    def run_one(self, target, *, group_cap: float | None = None, group_spent: float = 0.0) -> TargetResult:
        from .sheets import SheetClient

        self.emit(f"=== profile '{target.name}' ({target.spreadsheet_id} gid {target.gid}) ===")
        result = TargetResult(target.name, target.spreadsheet_id, target.gid)
        try:
            sheet = SheetClient(self.sheets_service, target.spreadsheet_id, target.gid)
            runner = Runner(
                sheet,
                self.drive,
                options_for(target, self.options, spent=self.spent, group_cap=group_cap, group_spent=group_spent),
                out=self.emit,
                stop_check=self.should_stop,
                access=self.access,
                on_progress=self._progress_for(target),
                task_id=self.task_id,
            )
            result.summary = runner.run()
            self.spent += result.summary.total_cost_inr
            if result.summary.interrupted:
                self.aborted = True
        except BridgeError as exc:
            result.error = str(exc)
            self.emit(f"profile '{target.name}' failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - class + location only, never content
            result.error = describe_exception(exc)
            self.emit(f"profile '{target.name}' failed: {result.error}")

        return result


def run_targets(
    targets,
    sheets_service,
    drive_service,
    options: RunOptions,
    *,
    access=None,
    out=None,
    stop_check=None,
    operator: str = "",
    batch_id: str | None = None,
    on_progress=None,
) -> BatchSummary:
    """Run each target in turn; never let one bad sheet stop the rest."""
    from .drive import DriveClient
    from .profiles import expand_workbook

    emit = out or (lambda _line: None)
    summary = BatchSummary(batch_id=batch_id or new_run_id())
    batch = _Batch(
        sheets_service,
        DriveClient(drive_service),
        options,
        access=access,
        emit=emit,
        external_stop=stop_check or (lambda: False),
        operator=operator,
        on_progress=on_progress,
    )

    for target in targets:
        if batch.should_stop():
            summary.results.append(batch.stopped(target))
            continue
        if not getattr(target, "all_tabs", False):
            summary.results.append(batch.run_one(target))
            continue

        # A workbook: discover its tabs now, so a batch tab added since the
        # profile was saved is picked up, then run the ones with rows ready.
        emit(f"=== workbook '{target.name}' ({target.spreadsheet_id}): discovering tabs ===")
        try:
            title, subs, skipped = expand_workbook(sheets_service, target)
        except BridgeError as exc:
            emit(f"workbook '{target.name}' failed: {exc}")
            summary.results.append(TargetResult(target.name, target.spreadsheet_id, target.gid, error=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - class + location only, never content
            text = describe_exception(exc)
            emit(f"workbook '{target.name}' failed: {text}")
            summary.results.append(TargetResult(target.name, target.spreadsheet_id, target.gid, error=text))
            continue
        for note in skipped:
            emit(f"  skipped: {note}")
        if not subs:
            emit(f"  {title!r}: no tab has rows ready")
            summary.results.append(TargetResult(target.name, target.spreadsheet_id, target.gid, note="nothing ready"))
            continue
        emit(f"  {len(subs)} tab(s) with rows ready: " + ", ".join(t.name.split(" / ", 1)[-1] for t in subs))
        group_spent = 0.0
        for sub in subs:
            if batch.should_stop():
                summary.results.append(batch.stopped(sub))
                continue
            result = batch.run_one(sub, group_cap=target.max_cost_inr, group_spent=group_spent)
            group_spent += result.cost_inr
            summary.results.append(result)

    _log_totals(summary, emit)
    if not options.dry_run:
        _write_run_summary(sheets_service, summary, operator=operator, emit=emit)
    return summary


def _write_run_summary(sheets_service, summary: BatchSummary, *, operator: str, emit) -> None:
    """One row for the whole run. A logging failure never fails the run."""
    if not run_log.enabled():
        return
    ran = [r for r in summary.results if r.summary is not None]
    # A run that touched nothing (everything already generated) is not worth a
    # row: the log should read as a list of real work, not of no-ops.
    if not ran or (summary.total_done == 0 and summary.total_failed == 0):
        return
    # A run cut short must not read like one that finished.
    outcomes = [o for r in ran for o in r.summary.outcomes]
    if any(o.code == "cost_cap" for o in outcomes):
        status = "stopped: cost cap"
    elif summary.interrupted or any(o.code == "stop_requested" for o in outcomes):
        status = "stopped"
    else:
        status = "completed"
    try:
        tab = run_log.append_run(
            sheets_service,
            status=status,
            run_id=summary.batch_id,
            operator=operator,
            sheets=", ".join(r.name for r in ran),
            lectures_done=summary.total_done,
            failed=summary.total_failed,
            total_cost_inr=summary.total_cost_inr,
        )
        emit(
            f"run logged to '{tab}': {status} - {summary.total_done} lecture(s), "
            f"{summary.total_failed} failed, INR {summary.total_cost_inr:.2f}"
        )
    except Exception as exc:  # noqa: BLE001 - cosmetic; the work already succeeded
        emit(f"could not write the run summary: {describe_exception(exc)}")


def _log_totals(summary: BatchSummary, emit) -> None:
    if len(summary.results) <= 1:
        return
    emit("-" * 60)
    for result in summary.results:
        if result.error:
            emit(f"  {result.name}: failed ({result.error})")
        elif result.note:
            emit(f"  {result.name}: {result.note}")
        else:
            emit(f"  {result.name}: done={result.done} failed={result.failed} INR {result.cost_inr:.2f}")
    emit(
        f"batch {summary.batch_id}: done={summary.total_done} "
        f"failed={summary.total_failed} total INR {summary.total_cost_inr:.2f}"
    )
