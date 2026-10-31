"""One catalog row, from its PDF link to notes filed in the Drive tree.

    skip list -> plan (category, language, names, page-1 header)
      -> notes already filed for this row?        mark for review, no cost
      -> same lecture PDF already made elsewhere?  copy the PDF, no AI cost
      -> otherwise claim, download, generate, check, upload
    -> Auto Status / Review Status / Concise Notes Link written to the sheet

The notes a run files carry Drive ``properties``: the source PDF
(``shortnotesSource``) and a digest of everything that shapes the rendered
PDF (``shortnotesRender``). The first tells this lecture's notes from another
lecture's file of the same name; the second lets a row that lists the same PDF
under another batch copy the finished notes instead of paying for them again.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from notesbot import categories, generation_profile

from . import catalog, http_source, output_cache
from . import columns as C
from .drive import DriveFile
from .errors import BridgeError, describe_exception, error_code_for
from .generator import GenerationResult
from .lecture_match import choose_lecture_pdf
from .links import KIND_FOLDER, parse_drive_link
from .mapping import SheetHeader, normalize_signature, resolve_subject, sheet_header
from .runner import OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_PLANNED, OUTCOME_SKIPPED, RowOutcome

SOURCE_PROPERTY = "shortnotesSource"
RENDER_PROPERTY = "shortnotesRender"
_DRIVE_HOSTS = ("drive.google.com", "docs.google.com")


@dataclass(frozen=True)
class Plan:
    url: str
    source_key: str
    drive_file: object | None       # the Drive source file; None for a web link
    path: tuple                     # folder levels; the exam level is a tuple of names
    topic: str
    title: str
    category: str
    language: str
    subject: str
    signature: str
    header: SheetHeader
    render_key: str
    note: str = ""

    @property
    def where(self) -> str:
        return catalog.display_path(self.path)

    @property
    def header_text(self) -> str:
        return "\x00".join((self.header.subject, self.header.chapter, self.header.lecture))


def is_drive_url(url: str) -> bool:
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return False
    return host in _DRIVE_HOSTS


def link_of(created) -> str:
    """Browser link of an uploaded/copied file (API dict) or a listed one."""
    if isinstance(created, DriveFile):
        return created.web_view_link or f"https://drive.google.com/file/d/{created.id}/view"
    return str(created.get("webViewLink") or "") or f"https://drive.google.com/file/d/{created.get('id')}/view"


def folder_link_of(folder) -> str:
    """Browser link of a folder of the tree (the lecture's chapter folder)."""
    return f"https://drive.google.com/drive/folders/{folder.id}"


def fill_missing_folder_links(runner, rows) -> int:
    """Rows filed before the Lecture Folder Link column existed: write the
    folder their notes PDF sits in. One Drive lookup per row, no AI cost;
    Status and Review stay as they are. Returns the number of links written
    (or, in a dry run, that would be)."""
    from .links import parse_drive_file_link

    if runner.layout.notes_folder_index is None:
        return 0
    found: dict[int, str] = {}
    for row in rows:
        if row.is_blank or not row.notes_link or row.notes_folder:
            continue
        try:
            parents = runner.drive.get_file(parse_drive_file_link(row.notes_link)).parents
        except Exception as exc:  # noqa: BLE001 - a missing link is cosmetic; never stop the run
            runner._log(row, f"could not find the folder of the filed notes: {describe_exception(exc)}")
            continue
        if parents:
            found[row.number] = f"https://drive.google.com/drive/folders/{parents[0]}"
    if not found:
        return 0
    if runner.options.dry_run:
        runner._log(None, f"would fill the Lecture Folder Link of {len(found)} row(s) filed earlier")
        return len(found)
    try:
        # Written straight after the sheet was read, so the rows are still where they were.
        runner.sheet.write_folder_links(found)
    except Exception as exc:  # noqa: BLE001
        runner._log(None, f"could not fill the Lecture Folder Links: {describe_exception(exc)}")
        return 0
    runner._log(None, f"filled the Lecture Folder Link of {len(found)} row(s) filed earlier")
    return len(found)


def path_key(path) -> tuple:
    from .destination import folder_key

    return tuple(folder_key(level[0] if isinstance(level, tuple) else level) for level in path)


class CatalogFlow:
    """Per-run state of a catalog tab; the Runner hands each row to process()."""

    def __init__(self, runner, tree, link_counts) -> None:
        self.runner = runner
        self.tree = tree
        self.link_counts = link_counts            # link -> rows listing it in this tab
        self.produced: dict[str, dict] = {}       # render key -> notes filed this run
        self.failed: dict[str, tuple] = {}        # render key -> (first row, code)
        self.planned: dict[str, int] = {}         # dry run: render key -> first row
        self.taken: dict[tuple, str] = {}         # (path key, file name) -> source key
        self._doubled_reported = 0

    # -- entry --------------------------------------------------------------------
    def process(self, row) -> RowOutcome:
        r = self.runner
        reason = catalog.skip_reason(row.batch, r.options.skip_batches)
        if reason:
            return self._skip(row, reason)
        try:
            plan = self.plan(row)
            folder = self.tree.find_path(plan.path)
            existing, name = self._existing_or_name(plan, folder)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            code = error_code_for(exc, "resolve")
            detail = exc.detail if isinstance(exc, BridgeError) else ""
            r._log(row, f"resolve failed: {code}{f' - {detail}' if detail else ''} ({describe_exception(exc)})")
            if not r.options.dry_run:
                r._write(row, C.STATUS_FAILED, code=code)
            return RowOutcome(row.number, OUTCOME_FAILED, code=code)
        self._report_doubled(row)
        target = f"{plan.where} / {name}"
        # Review Status "Regenerate": fresh notes, never the filed or a copied set,
        # written over the filed PDF so its link stays the same.
        regenerate = row.wants_regeneration
        replace = existing if regenerate and existing is not None and existing.mime_type == "application/pdf" else None
        if r.options.dry_run:
            return self._plan_only(row, plan, existing, name, target, regenerate=regenerate)
        if existing is not None and not regenerate:
            r._log(row, f"notes already filed, marking for review: {target}")
            return self._finish(row, link_of(existing), folder, note="existing")
        if plan.render_key in self.failed:
            first, code = self.failed[plan.render_key]
            r._log(row, f"the same lecture PDF failed on row {first} in this run ({code}); not paying for it twice")
            r._write(row, C.STATUS_FAILED, code=code)
            return RowOutcome(row.number, OUTCOME_FAILED, code=code)
        if not regenerate:
            donor = self.produced.get(plan.render_key) or self._filed_elsewhere(row, plan)
            if donor is not None:
                return self._copy(row, plan, name, donor, target)
        return self._generate(row, plan, name, target, regenerate=regenerate, replace=replace)

    # -- planning (read-only) -----------------------------------------------------
    def plan(self, row) -> Plan:
        opts = self.runner.options
        url = row.drive_link.strip()
        drive_file = None
        if is_drive_url(url):
            drive_file = self._drive_source(row, url)
            source_key = http_source.source_key(f"drive:{drive_file.id}")
        elif http_source.is_web_link(url):
            source_key = http_source.source_key(url)
        else:
            raise BridgeError("invalid_link")
        category_id, _label = catalog.category_for_exam(row.exam)
        category = category_id or opts.category
        language = catalog.language_for_batch(row.batch)
        if language == "sanskrit" and opts.sanskrit_style != "strict":
            language = "sanskrit_hindi"
        subject = resolve_subject(opts.subject_mode, row.subject)
        # The faculty column (faculty_name), else a teacher named in the
        # subject cell ("Physics By Saleem Ahmad Sir"); the slides' teacher
        # still wins at render time when it differs (mapping.choose_signature).
        signature = normalize_signature(
            opts.signature_override or catalog.clean_name(row.faculty) or catalog.faculty(row.subject))
        number = catalog.lecture_number(row.lecture_topic)
        header = sheet_header(
            subject=catalog.header_subject(row.subject),
            chapter=catalog.header_chapter(row.chapter),
            lecture=str(number) if number is not None else "",
            language="hindi" if language == "sanskrit_hindi" else language,
        )
        # A blank exam cell files the row under the run's category.
        exam = row.exam if row.exam.strip() else categories.LABELS.get(opts.category, opts.category)
        header_text = "\x00".join((header.subject, header.chapter, header.lecture))
        render_key = output_cache.key(
            file_id=source_key, out_name="", language=language, subject=subject, signature=signature,
            header=header_text, show_page_numbers=opts.show_page_numbers, category=category,
            sanskrit_style=opts.sanskrit_style, signature_fixed=bool(opts.signature_override),
        )[: -len(".json")]
        regional = catalog.is_regional(row.batch)
        return Plan(
            url=url, source_key=source_key, drive_file=drive_file,
            path=catalog.folder_path(exam, row.batch, row.subject, row.chapter),
            topic=row.lecture_topic, title=catalog.lecture_title(row.lecture_topic) or "Lecture",
            category=category, language=language, subject=subject, signature=signature,
            header=header, render_key=render_key,
            note=f"{regional.title()} batch: notes are written in English" if regional else "",
        )

    def _drive_source(self, row, url: str):
        """A Drive link in a catalog: a file is used as is, a folder is
        resolved with the lecture number the topic carries."""
        r = self.runner
        link = parse_drive_link(url)
        target = r.drive.get_file(link.id)
        if link.kind == KIND_FOLDER or getattr(target, "is_folder", False):
            children = r._children(target.id, target.drive_id)
            match = choose_lecture_pdf(children, catalog.lecture_number(row.lecture_topic), chapter=row.chapter,
                                       folder_name=str(getattr(target, "name", "") or ""))
            return match.file
        return target

    def _existing_or_name(self, plan: Plan, folder):
        """(file already filed for this row or None, the name to use).

        A file in the chapter folder made from this very PDF is this row's
        notes, whatever it is called. Otherwise the lecture name is used; if
        a different lecture's notes already carry it, " (2)" and so on. A file
        with the name and no stamp was put there by hand, and is adopted.
        """
        pkey = path_key(plan.path)
        if folder is not None:
            for entry in self.tree.children(folder.id):
                if not getattr(entry, "is_folder", False) and entry.prop(SOURCE_PROPERTY) == plan.source_key:
                    return entry, entry.name
        for ordinal in range(1, 100):
            name = catalog.output_name(plan.topic, ordinal)
            entry = self.tree.find_file(folder.id, name) if folder is not None else None
            if entry is not None:
                owner = entry.prop(SOURCE_PROPERTY)
                if not owner or owner == plan.source_key:
                    return entry, name
                continue
            holder = self.taken.get((pkey, name))
            if holder is not None and holder != plan.source_key:
                continue
            return None, name
        raise BridgeError("name_collision", "too many different lectures share this name in one folder")

    def _filed_elsewhere(self, row, plan: Plan):
        """Notes an earlier run made from the same PDF, with the same page-1
        header and signature, filed for another row (another batch)."""
        if self.link_counts.get(plan.url, 0) < 2:
            return None
        try:
            found = self.runner.drive.find_by_property(RENDER_PROPERTY, plan.render_key, self.tree.drive_id)
        except Exception as exc:  # noqa: BLE001 - a missed reuse only costs a generation
            self.runner._log(row, f"could not look for earlier notes of this PDF: {describe_exception(exc)}")
            return None
        for entry in found:
            if not getattr(entry, "is_folder", False) and not getattr(entry, "is_google_native", False):
                return {"id": entry.id, "name": entry.name, "link": link_of(entry), "row": None}
        return None

    def _plan_only(self, row, plan: Plan, existing, name: str, target: str, *,
                   regenerate: bool = False) -> RowOutcome:
        details = (f"lang={plan.language} subject={plan.subject} category={plan.category} "
                   f"sig={plan.signature!r} header=({plan.header.subject!r}, {plan.header.chapter!r}, "
                   f"{plan.header.lecture!r})")
        if regenerate:
            state = ("marked Regenerate -> would make fresh notes and replace "
                     + ("the filed PDF" if existing is not None else "(no filed PDF to replace)"))
        elif existing is not None:
            state = "notes already filed -> would mark for review"
        elif plan.render_key in self.planned:
            state = f"same lecture PDF as row {self.planned[plan.render_key]} -> would copy its notes (no AI cost)"
        else:
            donor = self._filed_elsewhere(row, plan)
            if donor is not None:
                state = f"same lecture PDF already filed as {donor['name']!r} -> would copy it (no AI cost)"
            else:
                self.planned[plan.render_key] = row.number
                state = "would generate"
        self.taken.setdefault((path_key(plan.path), name), plan.source_key)
        self.runner._log(row, f"PLAN {state}: {target} {details}" + (f" [{plan.note}]" if plan.note else ""))
        return RowOutcome(row.number, OUTCOME_PLANNED, link=link_of(existing) if existing is not None else "",
                          note=state)

    # -- doing --------------------------------------------------------------------
    def _skip(self, row, reason: str) -> RowOutcome:
        r = self.runner
        if r.options.dry_run:
            r._log(row, f"PLAN skip: not a lecture - {reason}")
        else:
            r._log(row, f"skipped: not a lecture - {reason}")
            r._write(row, C.STATUS_SKIPPED, code="not_a_lecture")
        return RowOutcome(row.number, OUTCOME_SKIPPED, code="not_a_lecture", note=reason)

    def _finish(self, row, link: str, folder, *, note: str = "", cost: float = 0.0) -> RowOutcome:
        """Auto Status "review", Review Status "Review", the notes link and
        the link to the lecture's folder in the tree."""
        if not self.runner._write(row, C.STATUS_DONE, notes_link=link, folder_link=folder_link_of(folder)):
            return RowOutcome(row.number, OUTCOME_FAILED, code="sheet_write_failed:done", link=link, cost_inr=cost)
        return RowOutcome(row.number, OUTCOME_DONE, link=link, cost_inr=cost, note=note)

    def _properties(self, plan: Plan) -> dict:
        return {SOURCE_PROPERTY: plan.source_key, RENDER_PROPERTY: plan.render_key}

    def _ensure_folder(self, row, plan: Plan):
        before = len(self.tree.created)
        folder = self.tree.ensure_path(plan.path)
        for made in self.tree.created[before:]:
            self.runner._log(row, "created folder " + " / ".join(made))
        return folder

    def _remember(self, row, folder, plan: Plan, name: str, created, link: str) -> None:
        """Later rows of this run see the new file without listing the folder again."""
        entry = DriveFile(
            id=str(created.get("id", "")), name=name, mime_type="application/pdf", size=None,
            parents=(folder.id,), drive_id=self.tree.drive_id, web_view_link=link,
            properties=tuple(sorted(self._properties(plan).items())),
        )
        self.tree.remember(folder.id, entry)
        self.taken[(path_key(plan.path), name)] = plan.source_key
        self.produced.setdefault(plan.render_key, {"id": entry.id, "name": name, "link": link, "row": row.number})

    def _report_doubled(self, row) -> None:
        for name in self.tree.doubled[self._doubled_reported:]:
            self.runner._log(row, f"two or more folders are called {name!r}; using the oldest")
        self._doubled_reported = len(self.tree.doubled)

    def _download(self, plan: Plan) -> bytes:
        r = self.runner
        if plan.drive_file is not None:
            return r.drive.download_pdf(plan.drive_file, r.options.max_input_bytes)
        return r.fetch_url(plan.url, r.options.max_input_bytes)

    def _copy(self, row, plan: Plan, name: str, donor: dict, target: str) -> RowOutcome:
        r = self.runner
        try:
            folder = self._ensure_folder(row, plan)
            created = r.drive.copy_file(donor["id"], folder.id, name, self.tree.drive_id,
                                        properties=self._properties(plan))
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            code = error_code_for(exc, "copy")
            r._log(row, f"copy failed: {code} ({describe_exception(exc)})")
            r._write(row, C.STATUS_FAILED, code=code)
            return RowOutcome(row.number, OUTCOME_FAILED, code=code)
        link = link_of(created)
        self._remember(row, folder, plan, name, created, link)
        origin = f"row {donor['row']}" if donor.get("row") else repr(donor.get("name", ""))
        r._log(row, f"copied the notes of the same lecture PDF ({origin}) to {target} - no AI cost")
        return self._finish(row, link, folder, note="copied")

    def _generate(self, row, plan: Plan, name: str, target: str, *, regenerate: bool = False,
                  replace=None) -> RowOutcome:
        """Make the notes and file them; ``replace`` is the filed PDF that fresh
        notes (``regenerate``) overwrite, keeping its link."""
        r = self.runner
        opts = r.options
        task_id = ""
        if r.access is not None:
            try:
                r.access.ensure_allowed()
                task_id = r.task_id
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                code = error_code_for(exc, "allowlist")
                r._log(row, f"access denied: {code}")
                r._write(row, C.STATUS_FAILED, code=code)
                return RowOutcome(row.number, OUTCOME_FAILED, code=code)

        # Claim BEFORE any expensive work so an interrupted row is visible.
        if not r._write(row, C.STATUS_CLAIMED):
            return RowOutcome(row.number, OUTCOME_FAILED, code="sheet_write_failed:claim")
        r._log(row, f"claimed: {target} lang={plan.language} subject={plan.subject} category={plan.category} "
                    f"sig={plan.signature!r}" + (f" task={task_id}" if task_id else "")
                    + (f" [{plan.note}]" if plan.note else ""))

        cost = 0.0
        stage = "download"
        cache_key = output_cache.key(
            file_id=plan.source_key, out_name=name, language=plan.language, subject=plan.subject,
            signature=plan.signature, header=plan.header_text, show_page_numbers=opts.show_page_numbers,
            category=plan.category, sanskrit_style=opts.sanskrit_style,
            signature_fixed=bool(opts.signature_override),
        )
        if regenerate:
            # Fresh notes were asked for: never hand back the stored copy of the old ones.
            output_cache.discard(cache_key)
            r._log(row, "marked Regenerate: making fresh notes")
        try:
            # Reuse the exact approved bytes after an interrupted upload.
            cached = output_cache.load(cache_key)
            finalized = output_cache.load_finalized(cache_key, cached) if cached is not None else None
            if finalized is not None:
                r._log(row, f"reusing the notes generated by an earlier attempt ({len(cached):,} bytes, no new AI cost)")
                pdf, quality_report = finalized
                result = GenerationResult(pdf=pdf, notes_json=cached, cost_inr=0.0, task_id=task_id,
                                          quality_report=quality_report)
            else:
                pdf_bytes = self._download(plan)
                stage = "generate"
                r._log(row, f"generating ({len(pdf_bytes):,} bytes source) ...")
                with generation_profile.exam_category(plan.category), \
                        generation_profile.exam_subject(plan.subject), \
                        generation_profile.sanskrit_style_as(opts.sanskrit_style), \
                        generation_profile.fixed_signature(bool(opts.signature_override)):
                    result = r.generate_fn(
                        pdf_bytes,
                        language=plan.language,
                        subject=plan.subject,
                        signature=plan.signature,
                        filename=f"{plan.title}.pdf",
                        task_id=task_id,
                        log=lambda message: r._log(row, message),
                        header=plan.header,
                        show_page_numbers=opts.show_page_numbers,
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
            folder = self._ensure_folder(row, plan)
            if replace is not None:
                created = r.drive.update_pdf(replace.id, result.pdf, properties=self._properties(plan))
                link = link_of(created)
                self.produced[plan.render_key] = {"id": replace.id, "name": name, "link": link, "row": row.number}
                r._log(row, f"replaced the filed notes with the fresh notes: {target}")
            else:
                created = r.drive.upload_pdf(folder.id, name, result.pdf, self.tree.drive_id,
                                             properties=self._properties(plan))
                link = link_of(created)
                self._remember(row, folder, plan, name, created, link)
                r._log(row, f"filed the notes: {target}")
        except KeyboardInterrupt as exc:
            partial = max(cost, float(getattr(exc, "partial_cost_inr", 0.0) or 0.0))
            r._write(row, C.STATUS_FAILED, code="interrupted", cost=partial)
            r._interrupted = RowOutcome(row.number, OUTCOME_FAILED, code="interrupted", cost_inr=partial)
            raise
        except Exception as exc:  # noqa: BLE001
            # Money already spent before the failing stage must still be recorded.
            partial = max(cost, float(getattr(exc, "partial_cost_inr", 0.0) or 0.0))
            failed_stage = str(getattr(exc, "failed_stage", "") or stage)
            code = error_code_for(exc, failed_stage)
            r._log(row, f"{failed_stage} failed: {code} ({describe_exception(exc)})")
            r._write(row, C.STATUS_FAILED, code=code, cost=partial)
            self.failed.setdefault(plan.render_key, (row.number, code))
            return RowOutcome(row.number, OUTCOME_FAILED, code=code, cost_inr=partial)

        # The approved bytes stay cached for an upload retry without new AI cost.
        output_cache.touch(cache_key)
        outcome = self._finish(row, link, folder, cost=cost)
        if outcome.outcome == OUTCOME_DONE:
            r._log(row, f"done: {link}  cost=INR {cost:.2f}")
        return outcome
