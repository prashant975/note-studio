from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

import pw_access


def _utc_now_iso() -> str:
    """Wall-clock stamp for run_log.json (UTC, second resolution)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

from .error_locator import LocatorSources, locate_row, segments_from_text
from .error_report_detailed import render_detailed_error_report
from .languages import is_devanagari, language_name
from .note_budget import (
    accept_condensed,
    build_condense_prompt,
    build_page_compress_prompt,
    max_compression_passes,
    page_limit,
    pdf_page_count,
    condense_threshold,
    count_words,
    estimated_pages,
    word_budget,
    words_per_page,
)
from .ai_client import (
    GeminiClient,
    GeminiError,
    generate_mock_dcn_notes,
    generate_mock_notes,
)
from .config import (
    DCN_TARGET_PAGES,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_MODEL,
    DEFAULT_QUALITY_LEVEL,
    DEFAULT_QUALITY_OPTIONS,
    DEFAULT_UPSC_NOTE_DEPTH,
    DEFAULT_UPSC_NOTE_TYPE,
    DEFAULT_UPSC_FACT_MODE,
    DEFAULT_UPSC_OUTPUT_FORMAT,
    DEFAULT_DCN_LAYOUT_OPTIONS,
    CHUNK_OVERLAP_WORDS,
    CHUNK_TRANSCRIPT_WORDS,
    MAX_PARALLEL_AI_CALLS,
    DEFAULT_DCN_SOURCE_OPTIONS,
    DEFAULT_ERROR_REPORT_STRICTNESS,
    DEFAULT_GENERATE_ERROR_REPORT,
    DEVANAGARI_SCRIPT_REPAIR,
    ERROR_REPORT_STRICTNESS,
    LANGUAGE_CODES,
    MAX_SINGLE_CALL_TRANSCRIPT_WORDS,
    stamp_ist,
    QUALITY_OPTIONS,
    RUNS_DIR,
    SUPPORTED_EXTENSIONS,
    UPSC_FORMAT_NOTE_TYPE,
    UPSC_NOTE_TYPE_LABELS,
    UPSC_NOTE_TYPES,
    UPSC_OUTPUT_FORMATS,
    VISUALS_ENABLED,
    version_label,
)
from .docx_layout import derive_chapter_title
from .docx_writer import write_notes_docx, write_quality_report_docx
from .extract_pdf import extract_pdf
from .extract_pptx import extract_pptx
from .language_detect import detect_language, detect_notes_language
from .models import NormalizedTranscript, PipelineResult, SlideData, TranscriptIssue
from .notes_markup import normalize_notes_markup
from .pdf_exporter import export_docx_to_pdf
from .script_repair import repair_latin_in_devanagari
from .prompt_builder import (
    build_generation_prompt,
    build_merge_prompt,
    build_transcript_notes_prompt,
    build_upsc_prompt,
)
from .quality_checker import (
    build_quality_summary,
    count_structures,
    parse_correction_blocks,
    quality_check,
    render_quality_report_md,
    split_ai_output,
)
from .error_report import render_error_report_md
from .error_report_writer import (
    build_report_rows,
    extract_error_report_block,
    summarise,
    write_error_report_xlsx,
)
from .qc.engine import run_qc
from .qc.error_fixed_report import qc_quality_report_meta, render_error_fixed_report_md
from .qc.format_check import run_local_output_checks, run_visual_format_check
from .slide_filter import filter_slides
from .title_utils import filename_subject, resolve_lecture_title, subject_display_name
from .transcript import acquire_transcript, slice_transcript, validate_transcript
from .utils import chunked, copy_input, ensure_dir, make_run_id, safe_name, write_json, zip_dir
from .video_sources.errors import EmptyTranscript


# Detailed notes for a 2-hour class are long, and the transcript-driven path
# produces the whole set in one response — so it needs far more headroom than the
# per-chunk default (12k). Gemini 2.5 Pro allows up to 65k output tokens.
TRANSCRIPT_NOTES_MAX_OUTPUT_TOKENS = 32000


def _api_call_metadata(resp, **extra) -> dict:
    metadata = {"provider": resp.provider, "model": resp.model}
    metadata.update(extra)
    if getattr(resp, "usage", None):
        metadata.update(resp.usage)
    return metadata


def _load_slides_from_run(run_dir: Path) -> list[SlideData]:
    """Reconstruct SlideData objects from a run's slides_raw.json (for rebuilds)."""
    import json

    data = json.loads((run_dir / "slides_raw.json").read_text(encoding="utf-8"))
    slides: list[SlideData] = []
    for s in data:
        img = s.get("image_path")
        slides.append(
            SlideData(
                slide_no=s["slide_no"],
                heading=s.get("heading", ""),
                text=s.get("text", ""),
                cleaned_text=s.get("cleaned_text", ""),
                image_path=Path(img) if img else None,
                source_type=s.get("source_type", ""),
                metadata=s.get("metadata", {}) or {},
            )
        )
    return slides


def rebuild_outputs(
    run_dir: Path,
    edited_notes_text: str,
    *,
    stem: str,
    image_insert_mode: str = "smart_crop",
    dtp_note_policy: str = "keep_note_and_insert_image",
    subject: str = "",
    chapter_title: str | None = None,
) -> tuple[Path, Path | None, list[str]]:
    """Regenerate DOCX/PDF from (possibly user-edited) notes, reusing a run folder.

    Returns (docx_path, pdf_path, warnings). Used by the UI's edit-and-rebuild flow.
    Subject/chapter default to values recorded in run_metadata.json when omitted.

    The rebuild uses the SAME writer the run used. It used to always call the
    legacy image-inserting writer, so rebuilding a DCN run's note produced a
    different document than the run had delivered — same 7,000 words, 67 pages
    instead of 33, no DCN layout. run_metadata records which writer ran; the
    rebuild follows it.
    """
    import json

    run_dir = Path(run_dir)
    slides = _load_slides_from_run(run_dir)
    meta_path = run_dir / "run_metadata.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    subject = subject or meta.get("subject", "")
    if chapter_title is None and meta.get("input_file"):
        chapter_title = derive_chapter_title(Path(meta["input_file"]).stem)
    if chapter_title is None:
        chapter_title = derive_chapter_title(stem)
    output_dir = ensure_dir(run_dir / "output")
    stem = safe_name(stem)
    (run_dir / "notes_raw.txt").write_text(edited_notes_text, encoding="utf-8")
    docx_path = output_dir / f"{stem}_upsc_notes.docx"
    if meta.get("dcn_mode"):
        from .upsc_dcn_docx_writer import write_upsc_dcn_docx
        from .visuals.metadata import VisualRegistry

        # The run's figure registry, so an edit-and-rebuild keeps the diagrams
        # the original run inserted (the edited text still carries the
        # #FIGURE markers, and this is what resolves them to images).
        visual_registry = VisualRegistry.load(run_dir)
        # The run recorded the subject line it printed; reuse that rather than
        # re-deriving it, so an edit-and-rebuild cannot quietly retitle the
        # document. Older runs have no such key and fall back to the subject.
        rebuild_language = meta.get("detected_language") or meta.get("language") or "en"
        # Key presence, not truthiness: a run that deliberately printed NO
        # subject line stores "", and re-deriving one for it would retitle the
        # document the rebuild is supposed to reproduce. Only a run from before
        # this key existed falls through to derivation.
        if "subject_line" in meta:
            subject_line = meta.get("subject_line") or ""
        else:
            subject_line = subject_display_name(subject, rebuild_language)
        source_stem = Path(meta.get("input_file") or stem).stem
        rebuilt_title = resolve_lecture_title(
            lecture_no=meta.get("lecture_no"), dcn_no=meta.get("dcn_no"),
            marker_title=meta.get("lecture_title") or _title_from_notes(edited_notes_text),
            filename_stem=source_stem,
        )
        # Recover the explicit course field for older automatically titled
        # runs that suffered the filename-label bug. Preserve intentional
        # empty/custom subject lines in every other rebuild.
        if not subject_line and "topic name" in (meta.get("lecture_title") or "").lower():
            subject_line = filename_subject(source_stem)
        docx_path, warnings = write_upsc_dcn_docx(
            edited_notes_text,
            docx_path,
            language_code=rebuild_language,
            lecture_title=rebuilt_title.display if rebuilt_title.title else "",
            lecture_no=rebuilt_title.lecture_no,
            subject_line=subject_line,
            visuals=visual_registry.writer_map() if visual_registry else None,
            highlight_spans=_rebuild_highlight_spans(run_dir, edited_notes_text),
        )
    else:
        docx_path, warnings = write_notes_docx(
            edited_notes_text, docx_path, slides, run_dir=run_dir,
            image_insert_mode=image_insert_mode, dtp_note_policy=dtp_note_policy,
            subject=subject, chapter_title=chapter_title,
        )
    pdf_path, pdf_warning = export_docx_to_pdf(docx_path, output_dir)
    if pdf_warning:
        warnings.append(pdf_warning)
    return docx_path, pdf_path, warnings


def _title_from_notes(notes_text: str) -> str:
    """The lecture title the MODEL wrote, for naming a video-only run's files.

    A run with no lecture file has no file name to build an output stem from,
    and "lecture_notes.docx" in a downloads folder is indistinguishable from
    every other one. The notes always carry a ``#LECTURE_TITLE:`` line by the
    time this is read, so that is the name to use.
    """
    import re

    from .title_utils import parse_marker_title

    match = re.search(r"^[ 	]*#LECTURE_TITLE:[ 	]*(.+)$", notes_text or "", re.I | re.M)
    return parse_marker_title(match.group(1)).title if match else ""


def _rebuild_highlight_spans(run_dir: Path, notes_text: str) -> list[str]:
    """The run's yellow spans, re-checked against the (possibly edited) notes.

    A rebuild runs no QC — the corrections were decided once, during the run —
    so the spans are read back from correction_highlights.json. Any span the
    editor has since reworded is simply no longer found in the text and is
    dropped, which is the behaviour that matters: a highlight must never be
    approximated onto wording nobody checked.
    """
    import json

    path = Path(run_dir) / "correction_highlights.json"
    if not path.exists():
        return []
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    spans = [str(r.get("highlighted_text") or "") for r in records if isinstance(r, dict)]
    return [s for s in spans if s and s in notes_text]


def _extract_input(
    input_path: Path, run_dir: Path, *, render_scale: float = 2.0,
) -> tuple[list[SlideData], list[str]]:
    ext = input_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}. Supported: {sorted(SUPPORTED_EXTENSIONS)}")
    if ext == ".pdf":
        return extract_pdf(input_path, run_dir, render_scale=render_scale), []
    if ext in {".pptx", ".ppt"}:
        return extract_pptx(input_path, run_dir)
    raise ValueError(f"Unsupported file type: {ext}")


def _stage(callback, label: str) -> None:
    """Report a coarse progress stage to the UI. Never lets a UI error kill
    the run — the callback is display-only."""
    if callback is None:
        return
    try:
        callback(label)
    except Exception:
        pass


def _chunk_words(text: str, size: int, overlap: int) -> list[str]:
    """Split on word boundaries with an overlap between neighbours.

    The overlap is what stops a point being lost at a seam: the teacher's
    sentence rarely ends where the word counter does, so each chunk repeats the
    tail of the one before and the merge drops the duplicate.
    """
    words = (text or "").split()
    if not words:
        return []
    size = max(200, size)
    overlap = max(0, min(overlap, size // 2))
    chunks: list[str] = []
    start = 0
    while start < len(words):
        chunks.append(" ".join(words[start:start + size]))
        if start + size >= len(words):
            break
        start += size - overlap
    return chunks


def _generate_dcn_in_chunks(
    *,
    client,
    run_dir: Path,
    subject: str,
    depth: str,
    transcript: str,
    slides: list,
    language_code: str,
    glossary: list[str],
    quality_level: str,
    quality_options: dict,
    error_report: bool,
    error_report_strictness: str,
    lecture_title: str,
    api_metadata: list[dict],
    warnings: list[str],
    stage_callback=None,
    visuals: list[dict] | None = None,
) -> tuple[str, int]:
    """Write each stretch of a long class in full, then join them.

    Returns ``(merged response text, chunk count)``. Each part's raw response is
    cached in the run folder, so a merge that goes wrong can be diagnosed — and
    re-merged — without paying for the generation again.

    Every part's error-report block is carried through to the merged text so the
    caller's existing splitter still finds one array to parse.
    """
    from .error_report_writer import extract_error_report_block
    from .prompt_builder import build_dcn_chunk_prompt, build_dcn_merge_prompt

    chunks = _chunk_words(transcript, CHUNK_TRANSCRIPT_WORDS, CHUNK_OVERLAP_WORDS)
    total = len(chunks)
    error_blocks: list[str] = []

    # Each part is written from its own stretch of the transcript plus the whole
    # deck, so no part depends on another and they can be in flight together.
    # These are the slowest calls the run makes — 32k output tokens each — so on
    # a 2-hour class this is the difference between five long waits and one.
    #
    # The prompts are built up front, in order, so the files written to the run
    # folder are identical to the sequential version and a diff of two runs
    # still lines up.
    prompts: dict[int, str] = {}
    for index, chunk in enumerate(chunks, start=1):
        prompt = build_dcn_chunk_prompt(
            subject=subject,
            depth=depth,
            chunk_index=index,
            chunk_total=total,
            transcript_chunk=chunk,
            slides=slides,
            language_code=language_code,
            glossary=glossary,
            quality_level=quality_level,
            quality_options=quality_options,
            error_report=error_report,
            error_report_strictness=error_report_strictness,
            visuals=visuals,
        )
        prompts[index] = prompt
        (run_dir / f"prompt_dcn_part_{index:02d}.txt").write_text(prompt, encoding="utf-8")

    def _write_part(index: int):
        resp = client.generate(
            prompts[index], [], max_images=0,
            max_output_tokens=TRANSCRIPT_NOTES_MAX_OUTPUT_TOKENS,
        )
        return index, resp

    responses: dict[int, object] = {}
    workers = max(1, min(MAX_PARALLEL_AI_CALLS, total))
    _stage(stage_callback, f"Building notes (0 of {total})")
    # Not a `with` block: its __exit__ calls shutdown(wait=True), so once one
    # part fails the run would sit and wait for the others to finish — up to the
    # client's 300 s HTTP timeout each, on a run that is already lost. Failing
    # fast is worth more here than tidily joining threads we no longer need.
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [ex.submit(_write_part, i) for i in sorted(prompts)]
        for done, fut in enumerate(as_completed(futures), start=1):
            try:
                index, resp = fut.result()
            except GeminiError:
                raise
            except Exception as e:  # noqa: BLE001 — surfaced as a GeminiError below
                raise GeminiError(f"Gemini generation failed: {e}") from e
            responses[index] = resp
            # Counts completions, not position: with parts running together
            # "3 of 5" means three are finished, which is the honest reading.
            _stage(stage_callback, f"Building notes ({done} of {total})")
    finally:
        # Drops queued parts immediately; parts already in flight are left to
        # finish on their own rather than held onto.
        ex.shutdown(wait=False, cancel_futures=True)

    # Reassembled in transcript order — completion order is not lecture order.
    parts: list[str] = []
    for index in sorted(responses):
        resp = responses[index]
        (run_dir / f"dcn_part_{index:02d}.txt").write_text(resp.text, encoding="utf-8")
        api_metadata.append(_api_call_metadata(resp, chunk=f"dcn_part_{index}"))
        body, block, _ = extract_error_report_block(resp.text)
        if block:
            error_blocks.append(block)
        parts.append(body.strip())

    empty = sum(1 for p in parts if not p)
    parts = [p for p in parts if p]
    if not parts:
        raise GeminiError("No usable notes came back from any part of the lecture.")
    if empty:
        # Silently dropping these produced notes covering four fifths of a
        # lecture with nothing anywhere saying which fifth was missing.
        warnings.append(
            f"{empty} of {total} part(s) of this lecture came back empty and are "
            f"NOT in these notes. The notes cover the remaining {len(parts)} part(s). "
            f"Re-run the file for complete coverage.")

    if len(parts) == 1:
        merged = parts[0]
    else:
        _stage(stage_callback, "Building notes (joining parts)")
        merge_prompt = build_dcn_merge_prompt(
            subject=subject,
            depth=depth,
            parts=parts,
            lecture_title=lecture_title,
            language_code=language_code,
            glossary=glossary,
            figures=bool(visuals),
        )
        (run_dir / "prompt_dcn_merge.txt").write_text(merge_prompt, encoding="utf-8")
        try:
            resp = client.generate(
                merge_prompt, [], max_images=0,
                max_output_tokens=TRANSCRIPT_NOTES_MAX_OUTPUT_TOKENS,
            )
            api_metadata.append(_api_call_metadata(resp, chunk="dcn_merge"))
            merged, _, _ = extract_error_report_block(resp.text)
            merged = merged.strip()
            # A merge that came back shorter than half the input did not join,
            # it summarised — which is exactly the failure chunking exists to
            # avoid. The concatenation keeps every point; the seams are cosmetic.
            if len(merged.split()) < 0.5 * sum(len(p.split()) for p in parts):
                warnings.append(
                    "The merge step returned a much shorter document than the parts it was "
                    "given, so the parts were joined directly to keep the detail.")
                merged = "\n\n".join(parts)
        except Exception as e:  # noqa: BLE001 - the parts are the valuable output
            warnings.append(f"Joining the parts failed ({e}); they were concatenated instead.")
            merged = "\n\n".join(parts)

    if error_blocks:
        merged = (f"{merged}\n\n# ERROR_REPORT_START\n"
                  f"{_merge_error_blocks(error_blocks)}\n# ERROR_REPORT_END")
    return merged, total


def _merge_error_blocks(blocks: list[str]) -> str:
    """One JSON array from the per-part arrays, duplicates removed."""
    import json

    rows: list[dict] = []
    seen: set[str] = set()
    for block in blocks:
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, list):
            continue
        for row in data:
            if not isinstance(row, dict):
                continue
            key = str(row.get("original_statement", "")).strip().lower()[:120]
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rows.append(row)
    return json.dumps(rows, ensure_ascii=False, indent=2)


def run_pipeline(
    input_path: Path | None,
    *,
    subject: str,
    language: str,
    mode: str,
    google_token: str = "",
    model: str = DEFAULT_MODEL,
    send_images_to_ai: bool = True,
    strict_filter: bool = True,
    batch_size: int | None = None,
    max_images_per_call: int = 4,
    allow_mock: bool = False,
    image_insert_mode: str = "smart_crop",
    dtp_note_policy: str = "keep_note_and_insert_image",
    ai_redraw_diagrams: bool = False,
    image_model: str = DEFAULT_IMAGE_MODEL,
    video_url: str = "",
    force_audio_transcript: bool = False,
    transcription_engine: str = "",
    workflow: str = "default",
    note_type: str = "",
    area: str = "auto",
    quality_level: str = DEFAULT_QUALITY_LEVEL,
    quality_options: dict | None = None,
    generate_quality_report: bool = True,
    show_corrections: bool = True,
    qc_options: dict | None = None,
    stage_callback=None,
    # --- PW OnlyIAS DCN format (see src/upsc_dcn_docx_writer.py) -------------
    # Additive: the default output format keeps the legacy writer, so every
    # existing caller behaves exactly as before.
    upsc_output_format: str = DEFAULT_UPSC_OUTPUT_FORMAT,
    note_depth: str = DEFAULT_UPSC_NOTE_DEPTH,
    fact_mode: str = DEFAULT_UPSC_FACT_MODE,
    dcn_options: dict | None = None,
    template_path: Path | None = None,
    logo_path: Path | None = None,
    # --- Error Report XLSX (see src/error_report_writer.py) ------------------
    generate_error_report: bool = DEFAULT_GENERATE_ERROR_REPORT,
    error_report_strictness: str = DEFAULT_ERROR_REPORT_STRICTNESS,
    error_report_template_path: Path | None = None,
    # --- Lecture identity + source handling ---------------------------------
    lecture_no: str = "",
    dcn_no: str = "",
    lecture_title: str = "",
    dmp_no: str = "",
    dpp_no: str = "",
    source_options: dict | None = None,
    transcript_text_override: str = "",
    require_transcript: bool = True,
    youtube_url: str = "",
    # The signed-in person's own Google Drive read access (src/drive_auth.py):
    # what opens a class recording shared only inside PW. Empty = public links only.
    drive_token: str = "",
    drive_account: str = "",
) -> PipelineResult:
    # The lecture file is OPTIONAL. A class recording carries the whole lecture,
    # and for many classes it is the only thing that exists — there is no deck.
    # `None` (or a blank path) means "video only": the slides simply contribute
    # nothing, and every place that used to read the file's name falls back to
    # what the run does know.
    input_path = Path(input_path) if input_path else None
    has_slide_file = input_path is not None
    # The file name is used for the output stem, the chapter title, subject
    # detection and the lecture-title fallback. With no file, each of those has
    # its own better source; these two are what the shared code paths read.
    input_stem = input_path.stem if has_slide_file else ""
    input_name = input_path.name if has_slide_file else "class recording"
    # `youtube_url` was this parameter's name when YouTube was the only source it
    # accepted. Kept as an alias so existing callers and tests keep working.
    video_url = (video_url or youtube_url or "").strip()
    use_mock = allow_mock and not google_token
    # Defense in depth: every entry point (UI, CLI, or future integration) must
    # pass the live PW proxy gate immediately before the paid/main task. Any
    # missing token, denial, proxy error, or network failure denies the run.
    if not use_mock and not pw_access.check_allowed(google_token):
        raise PermissionError("Not authorized for this app.")
    run_dir = ensure_dir(RUNS_DIR / make_run_id("run"))
    warnings: list[str] = []
    # The UPSC note-type workflow. Selected by workflow="upsc", or implied by a
    # note_type the caller passed without setting workflow.
    note_type = (note_type or "").strip().lower()
    upsc_mode = (workflow or "").strip().lower() == "upsc" or note_type in set(UPSC_NOTE_TYPES)
    if upsc_mode and note_type not in set(UPSC_NOTE_TYPES):
        note_type = DEFAULT_UPSC_NOTE_TYPE
    area = (area or "auto").strip().lower()
    # Practice identifiers are descriptive metadata for the optional DMP/DPP
    # exports. They deliberately stay separate from a DCN/lecture title.
    dmp_no = str(dmp_no or "").strip()
    dpp_no = str(dpp_no or "").strip()

    # PW OnlyIAS DCN format. Only meaningful inside the UPSC workflow; anywhere
    # else it stays off so the legacy writer keeps ownership of the output.
    upsc_output_format = (upsc_output_format or "").strip().lower()
    if upsc_output_format not in UPSC_OUTPUT_FORMATS:
        upsc_output_format = DEFAULT_UPSC_OUTPUT_FORMAT
    dcn_mode = upsc_mode and upsc_output_format == "dcn"
    # A non-DCN format still needs a prompt template, so map it onto the legacy
    # note type it corresponds to.
    if upsc_mode and not dcn_mode and upsc_output_format in UPSC_FORMAT_NOTE_TYPE:
        note_type = UPSC_FORMAT_NOTE_TYPE[upsc_output_format]
    note_depth = (note_depth or DEFAULT_UPSC_NOTE_DEPTH).strip().lower()
    fact_mode = (fact_mode or DEFAULT_UPSC_FACT_MODE).strip().lower()
    dcn_opts = dict(DEFAULT_DCN_LAYOUT_OPTIONS)
    dcn_opts.update({k: bool(v) for k, v in (dcn_options or {}).items() if k in dcn_opts})
    dcn_quality_report = None
    dcn_render_check = None
    visual_qc_result = None

    # Error Report XLSX. The rows are collected from every AI call the run makes
    # (one for a single-call run, one per chunk otherwise) and written once at
    # the end, so a chunked lecture still produces a single sheet.
    error_report_strictness = (error_report_strictness or DEFAULT_ERROR_REPORT_STRICTNESS).strip().lower()
    if error_report_strictness not in ERROR_REPORT_STRICTNESS:
        error_report_strictness = DEFAULT_ERROR_REPORT_STRICTNESS
    generate_error_report = bool(generate_error_report)
    error_report_blocks: list[str] = []
    error_report_marker_found = False
    error_report_xlsx_path: Path | None = None
    error_report_detailed_path: Path | None = None
    error_report_summary: dict = {"total": 0, "high": 0, "medium": 0, "low": 0}
    error_report_status = "disabled" if not generate_error_report else "pending"
    error_report_error = ""
    pdf_error = ""

    # Slides-as-truth, transcript-for-explanation, table and layout repair. All
    # on by default: they are what makes the output correct, not merely different.
    src_opts = dict(DEFAULT_DCN_SOURCE_OPTIONS)
    src_opts.update({k: bool(v) for k, v in (source_options or {}).items() if k in src_opts})
    slide_glossary: list[str] = []
    transcript_clean_summary: dict = {}
    format_repair_summary: dict = {}
    #: Markup the model spelled its own way (## H2:, $\rightarrow$, a bare pipe
    #: table) and that was rewritten; for run_log.json, like the format repairs.
    markup_repairs: list[str] = []
    script_repair_summary: dict = {}
    chunk_count = 0
    resolved_title = None
    # What the WRITER is told the title is. Normally the resolved title's
    # display form; empty on a run with nothing to resolve one from, which
    # leaves the title to the model instead of printing "Class Notes".
    forced_lecture_title = ""

    upsc_options = dict(DEFAULT_QUALITY_OPTIONS)
    upsc_options.update({k: bool(v) for k, v in (quality_options or {}).items() if k in QUALITY_OPTIONS})
    quality_report_md_path: Path | None = None
    quality_report_docx_path: Path | None = None
    quality_summary: dict = {}
    marker_fallback = False
    transcript_text: str = ""
    transcript_source: str = ""
    #: The normalized transcript's own record (engine, language, timings, gaps),
    #: written to the run folder and summarised into run_metadata.
    transcript_meta: dict = {}
    # Transcript problems are collected, not raised — see acquire_transcript. They
    # become the downloadable error report at the end of the run.
    transcript_issues: list[TranscriptIssue] = []
    error_report_md_path: Path | None = None
    error_report_docx_path: Path | None = None
    error_fixed_report_md_path: Path | None = None
    error_fixed_report_docx_path: Path | None = None
    raw_notes_path: Path | None = None
    #: The notes exactly as the DOCX/PDF were built from them — after QC, after
    #: layout repair, after any compression pass. Written below once the
    #: documents exist, because only then is "final" true.
    notes_final_path: Path | None = None
    docx_path: Path | None = None
    pdf_path: Path | None = None
    zip_path: Path | None = None
    # One task id for this whole run (pw-app-kit v2). Every Gemini call the run
    # makes — notes generation, QC review, claim verification, visual check —
    # carries it, so the proxy's raw usage rows can be grouped back into one
    # task by task_id + app + email + model. Created before the try so it is
    # available to the failure path and to run_metadata either way.
    task_id = pw_access.new_task_id("upsc-notes")
    # Wall clock for the log, monotonic for the duration — a clock adjustment
    # mid-run must not produce a negative elapsed time.
    run_started_at = _utc_now_iso()
    run_started_monotonic = time.time()
    try:
        # Two labels because the two runs are different work. With no deck
        # there is no file to read, and "Reading lecture file" for a run that
        # has none describes something that is not happening. app.py has always
        # mapped both; only one was ever sent.
        _stage(stage_callback,
               "Extracting lecture" if has_slide_file
               else "Extracting lecture (recording only)")
        if not has_slide_file and not (video_url or transcript_text_override.strip()):
            raise ValueError(
                "Nothing to build notes from: pass a lecture file, a video link, "
                "or a transcript."
            )
        copied_input = copy_input(input_path, run_dir / "input") if has_slide_file else None
        # DCN output never embeds slide images (the writer places only the
        # logo) and the AI is called with max_images=0, so the page renders are
        # consulted only for their EXISTENCE — the slide filter's keep-this-
        # diagram-page decision and the prompt's "image available" flag. A
        # 92-page PDF at scale 2.0 spends 22 s and 245 MB on that; 1.5 keeps
        # every behaviour (and the images, for redraw/rebuild) at half the
        # cost. The legacy path keeps 2.0 because it prints these images into
        # the student's document.
        if has_slide_file:
            slides, extract_warnings = _extract_input(
                copied_input, run_dir, render_scale=1.5 if dcn_mode else 2.0)
            warnings.extend(extract_warnings)
        else:
            slides = []
            warnings.append(
                "No lecture file was provided; the notes are built from the class "
                "recording alone. Slide diagrams and slide spellings are therefore "
                "not available to this run."
            )
        # The UPSC area picker doubles as the subject: it names the cover page and
        # picks the per-subject emphasis. "auto" falls through to detection below.
        if upsc_mode and area not in {"", "auto"}:
            subject = area
        # With no slides there is nothing to detect FROM yet, so detection waits
        # for the transcript rather than falling back to the default subject —
        # which would put the wrong subject at the top of the notes.
        detect_subject_from_transcript = False
        if not subject or subject.strip().lower() in {"auto", ""}:
            from .subject_detect import detect_subject

            slide_text = "\n".join((s.heading or "") + " " + (s.text or "") for s in slides[:12])
            if has_slide_file:
                subject = detect_subject(input_stem, slide_text)
                warnings.append(f"Subject auto-detected as: {subject}")
            else:
                detect_subject_from_transcript = True
        write_json(run_dir / "slides_raw.json", [s.__dict__ for s in slides])
        active_slides, filter_report = filter_slides(slides, strict=strict_filter)
        write_json(run_dir / "filter_report.json", filter_report)
        write_json(run_dir / "slides_for_ai.json", [s.__dict__ for s in active_slides])
        if has_slide_file and not active_slides:
            raise RuntimeError("No slides/pages left after filtering. Disable strict filtering and try again.")
        # Notes come out in the language the class is actually taught in. This is
        # a first pass from the slides only; once a video transcript is fetched
        # it is re-decided from both (the transcript is the better signal — see
        # detect_notes_language). The slide pass still matters: it is the
        # language hint used when transcribing audio.
        slide_text = "\n".join((s.heading or "") + " " + (s.text or "") for s in active_slides)
        auto_language = not language or language.strip().lower() == "auto"
        if auto_language:
            language_code = detect_language(slide_text)
        else:
            language_code = LANGUAGE_CODES.get(language, language).lower()
        if batch_size is None or batch_size <= 0:
            # Keep image batches small: each page image is sent inline through the
            # Vercel proxy, which caps request bodies at ~4.5 MB. Images are also
            # compressed in ai_client, but a small batch keeps a safety margin.
            batch_size = 4 if send_images_to_ai else 18
        partial_notes: list[str] = []
        api_metadata: list[dict] = []
        total_slides = len(slides)
        active_slide_count = len(active_slides)
        client = None
        if not use_mock:
            # Every Gemini call this run makes goes through this one client, so
            # tagging it here is what puts the shared task_id (and the file
            # metadata that makes a ledger row readable) on all of them.
            client = GeminiClient(
                google_token,
                model=model,
                task_id=task_id,
                filename=input_name,
                input_unit="No. of pages",
                count=active_slide_count,
            )

        # Class video: the transcript carries the teacher's explanation and exam
        # emphasis, which the slides do not have. Fetched outside the AI branch
        # because YouTube captions need no AI at all — only the STT path uses
        # `client`.
        #
        # This raises if no transcript can be had. It used to fall back to
        # slides-only notes, but those are indistinguishable from real ones
        # while missing everything that was actually taught, so a run that
        # cannot transcribe the class now stops and says so. Recoverable
        # problems — auto-generated captions, one failed audio chunk — still
        # only warn (see acquire_transcript).
        if transcript_text_override.strip():
            # Supplied text must take precedence BEFORE fetching the video.
            # Otherwise an inaccessible native transcript still starts a costly
            # audio pass, whose result is immediately replaced below.
            _stage(stage_callback, "Using supplied transcript")
            transcript = NormalizedTranscript(
                full_text=transcript_text_override.strip(),
                source_type="supplied", source_label="supplied by the user",
                engine="supplied",
            )
            validate_transcript(transcript)
            transcript_text = transcript.full_text
            transcript_source = transcript.source_label
            transcript_meta = {**transcript.to_dict(), "retrieval_seconds": 0.0}
            (run_dir / "transcript.txt").write_text(transcript_text, encoding="utf-8")
            write_json(run_dir / "transcript_segments.json", transcript_meta)
        elif video_url:
            _stage(stage_callback, "Preparing transcript")
            transcript_started = time.time()
            transcript = acquire_transcript(
                video_url,
                client=client,
                work_dir=run_dir,
                # NOT language_code: that is the language the NOTES are
                # wanted in, and the user now picks it independently of the
                # class. Passing it here told the transcriber "the lecture is
                # in Hindi" whenever Hindi notes were requested — which would
                # push an English lecture into Devanagari. Nothing in the run
                # knows the spoken language yet (detection reads the finished
                # transcript), so the engines are left to detect it, which is
                # what both already do with an empty hint.
                language_hint="",
                warnings=warnings,
                issues=transcript_issues,
                engine=transcription_engine,
                force_audio=force_audio_transcript,
                stage_callback=stage_callback,
                drive_token=drive_token,
                drive_account=drive_account,
                # Transcribing a 2-hour class is the longest single wait in the
                # run, so it reports as it goes: a line that never changes for
                # ten minutes reads as a hang. Gemini counts audio chunks;
                # Whisper has none to count and reports percent as (n, 100).
                progress_callback=lambda done, total: _stage(
                    stage_callback,
                    f"Transcribing the lecture ({done}%)" if total == 100
                    else f"Transcribing the lecture ({done} of {total})"),
            )
            transcript_text = transcript.full_text
            transcript_source = transcript.source_label
            transcript_meta = transcript.to_dict()
            transcript_meta["retrieval_seconds"] = round(time.time() - transcript_started, 2)
            (run_dir / "transcript.txt").write_text(transcript_text, encoding="utf-8")
            # Timings alongside the text: they are what lets a line in the notes
            # be traced back to a minute of the class.
            write_json(run_dir / "transcript_segments.json", transcript_meta)
            warnings.append(
                f"Transcript source: {transcript_source} ({transcript.word_count:,} words)."
            )
        # The lecture video is the second required input, and notes written
        # without it are not the product: they silently drop everything the
        # faculty said. Stop here rather than produce a document that looks
        # complete. `require_transcript=False` is for slides-only tests.
        # Without slides the recording is the ONLY source, so a missing
        # transcript cannot be survived whatever the caller asked for.
        if (require_transcript or not has_slide_file) and not transcript_text.strip():
            raise EmptyTranscript(
                "No transcript was produced for this lecture.",
                user_message=(
                    "No transcript could be obtained from the lecture video, so no "
                    "notes were generated. Please check the video link and try again."
                ),
            )

        # Repair the captions against the slides before anything reads them.
        # YouTube's Hindi recogniser transliterates English into Devanagari
        # ("probity" -> "प्रोबेबिलिटी"), and a corrupted term that reaches the
        # model reaches the student: the model cannot know which spelling the
        # slide used, but the slide does.
        if transcript_text and src_opts["slides_source_of_truth"]:
            from .transcript_cleaner import clean_transcript

            cleaned = clean_transcript(
                transcript_text,
                [(s.heading or "") + "\n" + (s.prompt_text or "") for s in slides],
                remove_filler=True,
                restore_slide_terms=src_opts["transcript_explanation_only"],
            )
            slide_glossary = cleaned.glossary
            transcript_clean_summary = cleaned.summary()
            if cleaned.text.strip():
                transcript_text = cleaned.text
                (run_dir / "transcript_cleaned.txt").write_text(
                    transcript_text, encoding="utf-8")
            write_json(run_dir / "transcript_clean.json", {
                **transcript_clean_summary,
                "repairs": [{"found": a, "replaced_with": b} for a, b in cleaned.repairs],
                "glossary": slide_glossary[:200],
            })

        if detect_subject_from_transcript:
            from .subject_detect import detect_subject

            # The transcript is the whole class, so a generous slice of it is a
            # better signal than a file name ever was.
            subject = detect_subject("", transcript_text[:20000])
            warnings.append(f"Subject auto-detected from the recording as: {subject}")

        # Now that the transcript is in hand, decide the language from BOTH it and
        # the slides. The transcript carries the class's spoken medium, which the
        # slides can contradict (PW decks are often English in a Hindi-taught class).
        if auto_language:
            language_code, reason = detect_notes_language(slide_text, transcript_text)
            warnings.append(
                f"Language auto-detected as: {'Hindi' if language_code == 'hi' else 'English'} — {reason}."
            )

        # ---------------- Lecture visuals (DCN format only) ----------------
        # Diagrams, maps and flowcharts lifted from the slides into the final
        # document. DCN output otherwise embeds no slide imagery at all, which
        # is the gap this fills; the legacy writer keeps its own DTP-note
        # insertion path untouched. Additive and never fatal: any failure here
        # leaves the run exactly as it was before this feature.
        visual_registry = None
        visuals_for_prompt: list[dict] = []
        # Initialised here, not at the placement block that sets it: that block
        # is inside two conditionals, and a name bound only on the branch that
        # runs is a NameError waiting for the run that takes the other one.
        visuals_selected_for_placement = 0
        if dcn_mode and VISUALS_ENABLED and has_slide_file:
            _stage(stage_callback, "Selecting lecture diagrams")
            try:
                from .visuals import prepare_visuals

                visual_registry = prepare_visuals(
                    copied_input, slides, run_dir,
                    client=client, language_code=language_code,
                    stage_callback=stage_callback,
                )
                visuals_for_prompt = visual_registry.prompt_entries()
                if visuals_for_prompt:
                    warnings.append(
                        f"Slide visuals: {len(visual_registry.visuals)} candidate(s) found, "
                        f"{len(visuals_for_prompt)} selected for insertion "
                        f"(classifier: {visual_registry.classifier_status})."
                    )
            except Exception as e:  # noqa: BLE001 — visuals are additive, never fatal
                visual_registry = None
                visuals_for_prompt = []
                warnings.append(
                    f"Slide diagram selection failed; the notes carry no figures. ({e})")

        ai_report = ""
        _stage(stage_callback, "Generating draft notes")
        if upsc_mode:
            # ONE text-only call: the note type, the area, the quality settings and
            # both sources go in together, and the model returns the notes and its
            # own quality report between markers. Slide images are deliberately not
            # sent — diagrams are inserted afterwards via the DTP notes' slide
            # numbers, which is what lets the whole class fit in a single call.
            if dcn_mode:
                from .prompt_builder import build_upsc_dcn_prompt

                # Decide the lecture title before generating, so the prompt can
                # carry the real one. Leaving the template's own example in place
                # is what put "Lxx_DCNxx_..." on a student's page.
                resolved_title = resolve_lecture_title(
                    lecture_no=lecture_no,
                    dcn_no=dcn_no,
                    title=lecture_title,
                    filename_stem=input_stem,
                    headings=[s.heading for s in active_slides if s.heading],
                )
                # With no file name, no typed title and no slide headings there is
                # nothing to resolve a title FROM, and `display` would fall back to
                # "Class Notes". Passing "" instead leaves the title to the model,
                # which has just read the whole class — and the writer's
                # placeholder and file-identifier guards still apply to whatever it
                # writes. A run that HAS slides keeps the old behaviour exactly.
                forced_lecture_title = (
                    resolved_title.display
                    if (resolved_title.title or has_slide_file) else ""
                )
                prompt = build_upsc_dcn_prompt(
                    subject=area if area != "auto" else subject,
                    depth=note_depth,
                    fact_mode=fact_mode,
                    slides=active_slides,
                    transcript=transcript_text,
                    quality_level=quality_level,
                    quality_options=upsc_options,
                    error_report=generate_error_report,
                    error_report_strictness=error_report_strictness,
                    language_code=language_code,
                    lecture_title=forced_lecture_title,
                    glossary=slide_glossary,
                    visuals=visuals_for_prompt,
                )
            else:
                prompt = build_upsc_prompt(
                    note_type=note_type,
                    area=area,
                    language_code=language_code,
                    slides=active_slides,
                    transcript=transcript_text,
                    quality_level=quality_level,
                    quality_options=upsc_options,
                    error_report=generate_error_report,
                    error_report_strictness=error_report_strictness,
                )
            (run_dir / "prompt_upsc.txt").write_text(prompt, encoding="utf-8")
            transcript_words = len(transcript_text.split()) if transcript_text else 0
            use_chunked = (
                dcn_mode
                and not use_mock
                and transcript_words > MAX_SINGLE_CALL_TRANSCRIPT_WORDS
            )
            if use_mock:
                ai_text = (
                    generate_mock_dcn_notes(area or subject, len(active_slides))
                    if dcn_mode
                    else generate_mock_notes(subject, note_type, language_code, len(active_slides))
                )
                api_metadata.append({"provider": "mock", "model": "mock"})
            elif use_chunked:
                # A 15,000-word class in one call does not fail loudly: it comes
                # back with a detailed first half and a summarised second half,
                # because the model is budgeting its own output length. Splitting
                # the input removes that pressure — each part is written in full,
                # and the merge only has to join them.
                ai_text, chunk_count = _generate_dcn_in_chunks(
                    client=client,
                    run_dir=run_dir,
                    subject=area if area != "auto" else subject,
                    depth=note_depth,
                    transcript=transcript_text,
                    slides=active_slides,
                    language_code=language_code,
                    glossary=slide_glossary,
                    quality_level=quality_level,
                    quality_options=upsc_options,
                    error_report=generate_error_report,
                    error_report_strictness=error_report_strictness,
                    lecture_title=forced_lecture_title,
                    api_metadata=api_metadata,
                    warnings=warnings,
                    stage_callback=stage_callback,
                    visuals=visuals_for_prompt,
                )
            else:
                resp = client.generate(
                    prompt, [], max_images=0,
                    max_output_tokens=TRANSCRIPT_NOTES_MAX_OUTPUT_TOKENS,
                )
                ai_text = resp.text
                api_metadata.append(_api_call_metadata(
                    resp, chunk=f"upsc_{'dcn' if dcn_mode else note_type}"))
            (run_dir / "ai_response_raw.txt").write_text(ai_text, encoding="utf-8")
            # Peel the error report off first, so neither splitter below can
            # typeset a JSON array into the student's notes.
            ai_text, err_block, err_found = extract_error_report_block(ai_text)
            error_report_marker_found |= err_found
            if err_block:
                error_report_blocks.append(err_block)
            if dcn_mode:
                # DCN output is marker-formatted end to end (#LECTURE_TITLE:, #H1: ...)
                # and deliberately carries no separate AI quality report: corrections
                # are embedded in the notes themselves, so there is nothing to split.
                # split_ai_output is still used because the error-report contract
                # wraps the notes in # NOTES_START/# NOTES_END; with no markers it
                # returns the text unchanged, which is the pre-error-report shape.
                notes_text, _, _ = split_ai_output(ai_text)
                notes_text, ai_report, used_markers = notes_text.strip(), "", True
            else:
                notes_text, ai_report, used_markers = split_ai_output(ai_text)
            if not used_markers:
                marker_fallback = True
                warnings.append(
                    "The AI response had no # NOTES_START / # NOTES_END markers, so the whole "
                    "response was used as the notes and no AI quality report was available."
                )
            partial_notes = [notes_text]
            generation_method = (f"{chunk_count} parallel draft parts and a merge"
                                 if chunk_count else "a single AI call")
            warnings.append(
                f"UPSC notes built using {generation_method}: note type '{note_type}', area '{area}', "
                f"quality check '{quality_level}', {active_slide_count} slide(s)"
                + (f", transcript {len(transcript_text.split()):,} words." if transcript_text else ", no transcript.")
            )
        elif use_mock:
            notes_text = generate_mock_notes(subject, mode, language_code, len(active_slides))
            partial_notes = [notes_text]
            api_metadata.append({"provider": "mock", "model": "mock"})
        elif transcript_text:
            # TRANSCRIPT-DRIVEN: the class recording is the content, the slides are
            # reference. Done in ONE call — a 2-hour transcript is only ~21K tokens,
            # so the whole class fits, and the model sees the full arc of the lecture
            # instead of chunks that have to be stitched back together. Slide images
            # are not sent (that is what forces small chunks); diagrams are still
            # inserted afterwards via the DTP notes' slide numbers.
            prompt = build_transcript_notes_prompt(
                subject, mode, language_code, active_slides, transcript_text,
                error_report=generate_error_report,
                error_report_strictness=error_report_strictness,
            )
            (run_dir / "prompt_transcript_notes.txt").write_text(prompt, encoding="utf-8")
            resp = client.generate(
                prompt, [], max_images=0,
                max_output_tokens=TRANSCRIPT_NOTES_MAX_OUTPUT_TOKENS,
            )
            notes_text, err_block, err_found = extract_error_report_block(resp.text)
            error_report_marker_found |= err_found
            if err_block:
                error_report_blocks.append(err_block)
            partial_notes = [notes_text]
            api_metadata.append(_api_call_metadata(resp, chunk="transcript_notes"))
            warnings.append(
                f"Notes built from the class transcript ({len(transcript_text.split()):,} words) "
                f"with {active_slide_count} slide(s) as reference, in a single AI call."
            )
        else:
            chunks = list(chunked(active_slides, batch_size))
            # One slice per slide chunk, so the transcript is paid for once.
            transcript_slices = slice_transcript(transcript_text, len(chunks))

            def _run_chunk(i: int, slide_chunk):
                prompt = build_generation_prompt(
                    subject, mode, language_code, slide_chunk,
                    chunk_label=f"{i}/{len(chunks)}",
                    transcript=transcript_slices[i - 1],
                    error_report=generate_error_report,
                    error_report_strictness=error_report_strictness,
                )
                images = [s.image_path for s in slide_chunk if s.image_path] if send_images_to_ai else []
                resp = client.generate(
                    prompt, images, max_images=max_images_per_call,
                    max_output_tokens=TRANSCRIPT_NOTES_MAX_OUTPUT_TOKENS)
                (run_dir / f"notes_chunk_{i:02d}.txt").write_text(resp.text, encoding="utf-8")
                return i, resp

            # Run chunk calls concurrently (they are network-bound). Cap workers to
            # stay clear of rate limits; the client already retries on 429.
            chunk_results: dict[int, object] = {}
            # Same fail-fast shutdown as the DCN path above: waiting on the
            # remaining calls after one has failed adds their full timeout to a
            # run that is already going to raise.
            slide_ex = ThreadPoolExecutor(max_workers=min(4, max(1, len(chunks))))
            try:
                futures = [slide_ex.submit(_run_chunk, i, ch)
                           for i, ch in enumerate(chunks, start=1)]
                for fut in as_completed(futures):
                    try:
                        i, resp = fut.result()
                    except GeminiError:
                        raise
                    except Exception as e:
                        raise GeminiError(f"Gemini generation failed: {e}") from e
                    chunk_results[i] = resp
            finally:
                slide_ex.shutdown(wait=False, cancel_futures=True)
            for i in sorted(chunk_results):
                resp = chunk_results[i]
                # Each chunk reports on its own slides, so the arrays concatenate
                # cleanly. Peeling them off here also keeps the merge call's input
                # clean — the merge prompt has no idea what a JSON array is doing
                # in the middle of a set of notes.
                chunk_notes, err_block, err_found = extract_error_report_block(resp.text)
                error_report_marker_found |= err_found
                if err_block:
                    error_report_blocks.append(err_block)
                partial_notes.append(chunk_notes)
                api_metadata.append(_api_call_metadata(resp, chunk=i))
            if len(partial_notes) == 1:
                notes_text = partial_notes[0]
            else:
                merge_prompt = build_merge_prompt(subject, mode, language_code, partial_notes)
                try:
                    # The merge emits the WHOLE lecture's notes; on the 12,000
                    # default a long Devanagari set was cut off mid-document and
                    # shipped looking complete.
                    resp = client.generate(
                        merge_prompt, [], max_images=0,
                        max_output_tokens=TRANSCRIPT_NOTES_MAX_OUTPUT_TOKENS)
                    notes_text, _, _ = extract_error_report_block(resp.text)
                    api_metadata.append(_api_call_metadata(resp, chunk="merge"))
                except Exception as e:
                    warnings.append(f"Merge call failed; concatenating chunk outputs instead. Error: {e}")
                    notes_text = "\n\n".join(partial_notes)
        raw_notes_path = run_dir / "notes_raw.txt"
        raw_notes_path.write_text(notes_text, encoding="utf-8")
        if upsc_mode:
            # Same content as notes_raw.txt, but .md so the tables and pointer
            # blocks render when the run folder is opened in an editor.
            (run_dir / "notes_raw.md").write_text(notes_text, encoding="utf-8")
        # Straight after the model, so the word budget, the QC and the files the
        # practice builders read all see the writer's markup, not the model's.
        notes_text, repaired = normalize_notes_markup(notes_text, dcn=dcn_mode)
        markup_repairs.extend(repaired)

        # ---------------- length budget (DCN only) ----------------
        # The layout is fixed to the published house style, so pages can only be
        # bought back with words. The prompts carry the budget, but a long class
        # still overruns it: the draft is measured here and, if it came back well
        # over, condensed once. Never truncated — cutting at a word count drops
        # whatever was taught last, silently and unrecoverably.
        draft_words = count_words(notes_text)
        min_words, max_words = word_budget(language_code)
        if dcn_mode:
            metadata_length = {
                "draft_words": draft_words,
                "budget_words": [min_words, max_words],
                "words_per_page": words_per_page(language_code),
                "estimated_pages_before": round(
                    estimated_pages(draft_words, language_code), 1),
            }
            if draft_words > condense_threshold(language_code) and client is not None:
                _stage(stage_callback, "Tightening notes to the page budget")
                (run_dir / "notes_before_condense.md").write_text(
                    notes_text, encoding="utf-8")
                try:
                    resp = client.generate(
                        build_condense_prompt(notes_text, language_code=language_code,
                                              max_words=max_words),
                        [], max_images=0,
                        max_output_tokens=TRANSCRIPT_NOTES_MAX_OUTPUT_TOKENS)
                    condensed, _, _ = extract_error_report_block(resp.text)
                    api_metadata.append(_api_call_metadata(resp, chunk="condense"))
                    if getattr(resp, "truncated", False):
                        # A cut-off "shortening" is not a shortening, it is the
                        # notes with the end missing.
                        raise ValueError(
                            "the tightening pass was cut off by the token limit")
                    after = count_words(condensed)
                    if accept_condensed(notes_text, condensed):
                        notes_text = condensed
                        (run_dir / "notes_condensed.md").write_text(
                            notes_text, encoding="utf-8")
                        warnings.append(
                            f"Notes ran to {draft_words:,} words "
                            f"(about {estimated_pages(draft_words, language_code):.0f} pages); "
                            f"tightened to {after:,} words "
                            f"(about {estimated_pages(after, language_code):.0f} pages) to hold the "
                            f"{min_words:,}-{max_words:,} word budget. The full draft is "
                            f"kept as notes_before_condense.md."
                        )
                    else:
                        warnings.append(
                            f"Notes ran to {draft_words:,} words "
                            f"(about {estimated_pages(draft_words, language_code):.0f} pages). The "
                            f"tightening pass returned {after:,} words, which is not a "
                            f"safe shortening, so the full draft was kept."
                        )
                except Exception as exc:  # noqa: BLE001 - never fail a run on this
                    warnings.append(
                        f"Notes ran to {draft_words:,} words "
                        f"(about {estimated_pages(draft_words, language_code):.0f} pages) and the "
                        f"tightening pass failed ({exc}); the full draft was kept."
                    )
            final_words = count_words(notes_text)
            metadata_length["final_words"] = final_words
            metadata_length["estimated_pages"] = round(
                estimated_pages(final_words, language_code), 1)
            # Against the TOLERANCE, not against the ceiling: a note that lands
            # at exactly the top of the band is what was asked for, and warning
            # "16 pages, above the 10-16 page target" would be nonsense.
            if final_words > condense_threshold(language_code):
                warnings.append(
                    f"These notes come to about "
                    f"{estimated_pages(final_words, language_code):.0f} pages, above the "
                    f"{DCN_TARGET_PAGES[0]}-{DCN_TARGET_PAGES[1]} page target. Nothing was "
                    f"removed — shorten them by hand if the length matters more than the "
                    f"detail."
                )

        # ---------------- English inside Hindi / Marathi notes ----------------
        # Before the QC, so the reviewer pass reads the repaired lines. What the
        # repair leaves behind is still listed by latin_script_warnings below.
        if (DEVANAGARI_SCRIPT_REPAIR and client is not None and upsc_mode
                and is_devanagari(language_code)):
            script_repair = repair_latin_in_devanagari(notes_text, client, language_code)
            if script_repair.response is not None:
                api_metadata.append(_api_call_metadata(script_repair.response,
                                                       chunk="script_repair"))
            if script_repair.repaired_lines:
                (run_dir / "notes_before_script_repair.md").write_text(
                    notes_text, encoding="utf-8")
                notes_text = script_repair.text
            if script_repair.flagged_lines:
                script_repair_summary = script_repair.summary()

        # ---------------- QC stage (UPSC workflow only) ----------------
        # Independent review + source verification + safe corrections. The
        # draft is preserved as notes_before_qc.md; the engine never raises —
        # a failed stage records an honest status and the run continues.
        qc_result = None
        if upsc_mode:
            _stage(stage_callback, "Running structural checks")
            (run_dir / "notes_before_qc.md").write_text(notes_text, encoding="utf-8")
            transcript_gaps = [i.time_range for i in transcript_issues if i.time_range]
            qc_result = run_qc(
                notes_text=notes_text, run_dir=run_dir, client=client,
                google_token=google_token, slides=active_slides,
                transcript_text=transcript_text, transcript_gaps=transcript_gaps,
                subject=subject, language_code=language_code, note_type=note_type,
                quality_level=quality_level, quality_options=upsc_options,
                qc_options=qc_options, stage_callback=stage_callback,
            )
            warnings.extend(qc_result.warnings)
            if qc_result.notes_changed:
                notes_text = qc_result.final_notes
                applied_n = sum(1 for a in qc_result.applied if a.applied)
                warnings.append(
                    f"QC applied {applied_n} high-confidence correction(s); "
                    f"{qc_result.human_review_items} item(s) need human review."
                )
        # Again after the tightening and QC passes: both are model rewrites of
        # the whole text and can bring the model's own markup back.
        notes_text, repaired = normalize_notes_markup(notes_text, dcn=dcn_mode)
        markup_repairs.extend(f"after rewriting: {r}" for r in repaired)
        if upsc_mode:
            (run_dir / "notes_after_qc.md").write_text(notes_text, encoding="utf-8")

        # Optional: AI-redraw the diagrams referenced by DTP notes into handwritten
        # blue-on-white style, then insert those instead of the raw slide crops.
        if ai_redraw_diagrams and client is not None:
            from .dtp_parser import find_dtp_notes
            from .image_ai import redraw_slides_handwritten

            dtp_slide_nums = {n.slide_no for n in find_dtp_notes(notes_text) if n.slide_no}
            if dtp_slide_nums:
                redraw_dir = ensure_dir(run_dir / "ai_diagrams")
                n_redrawn, redraw_warnings = redraw_slides_handwritten(client, slides, dtp_slide_nums, redraw_dir, image_model=image_model)
                warnings.extend(redraw_warnings)
                warnings.append(f"AI-redrew {n_redrawn} diagram image(s) in handwritten style.")
                api_metadata.append({"provider": "image", "model": image_model, "redrawn": n_redrawn})

        _stage(stage_callback, "Creating DOCX/PDF")
        output_dir = ensure_dir(run_dir / "output")
        # With no lecture file the resolved title names the outputs, so a
        # video-only run still downloads as something a person can find later.
        stem = safe_name(input_stem or (resolved_title.title if resolved_title else "")
                         or _title_from_notes(notes_text) or "lecture_notes")
        docx_path = output_dir / f"{stem}_upsc_notes.docx"
        chapter_title = derive_chapter_title(input_stem) or (
            resolved_title.title if resolved_title else "")
        # Declared before the branch: both are DCN-only, and the run metadata
        # below is written for every workflow.
        subject_line = ""
        correction_highlights: list = []
        #: Lines that narrate a correction instead of stating the fact. Collected
        #: in the DCN branch, reported in the Error Report far below.
        correction_language_hits: list = []
        if dcn_mode:
            # PW OnlyIAS Daily Class Notes layout. The writer reproduces the
            # reference document (green centred title, red/blue/black heading
            # ladder, right-hand "Space for Notes" box, footer strip); passing
            # the reference DOCX as `template_path` makes it exact, because the
            # template carries the real header/footer and the embedded font.
            from .upsc_dcn_docx_writer import write_upsc_dcn_docx
            from .visual_format_checker import check_and_repair_notes

            # Repair before writing: a placeholder title, a 320-character table
            # row and a missing contents section are all fixable here and none
            # of them are fixable once the DOCX exists. Silent by design — the
            # trail goes to run_log.json, not to the person who uploaded a PPT.
            _stage(stage_callback, "Creating DOCX/PDF")
            repair = check_and_repair_notes(
                notes_text,
                lecture_title=forced_lecture_title,
                language_code=language_code,
                convert_wide_tables=src_opts["tables_to_bullets"],
            )
            if src_opts["auto_repair_layout"]:
                notes_text = repair.notes_text
            format_repair_summary = repair.summary()

            # Figure placement, made final: validate the model's #FIGURE lines
            # (unknown/duplicate ids dropped), place any selected figure the
            # model forgot next to its best-matching heading, and record every
            # decision in the registry the writer and the visual QC read.
            from .visuals.section_mapper import (
                finalize_figure_markers,
                strip_figure_markers,
            )

            if visual_registry is not None and visual_registry.selected:
                selected_before = len(visual_registry.selected)
                # Kept because `visual_registry.selected` is about to change:
                # the mapper deselects everything it could not place, so after
                # this call the registry can no longer answer "how many were
                # chosen for insertion" — only "how many survived placement".
                visuals_selected_for_placement = selected_before
                notes_text, placement_report = finalize_figure_markers(
                    notes_text, visual_registry)
                if ai_redraw_diagrams and client is not None:
                    from .image_ai import clean_visuals_for_notes

                    n_cleaned, cleanup_warnings = clean_visuals_for_notes(
                        client, visual_registry, ensure_dir(run_dir / "ai_diagrams"),
                        image_model=image_model, stage_callback=stage_callback)
                    warnings.extend(cleanup_warnings)
                    api_metadata.append({"provider": "image", "model": image_model,
                                         "redrawn": n_cleaned})
                visual_registry.save(run_dir)
                write_json(run_dir / "visual_placement.json", placement_report)

                # The earlier warning promises "N selected for insertion". The
                # mapper can then drop every one of them, and until now said so
                # only in a JSON file — so a run that announced 13 diagrams and
                # delivered none looked, to the person reading the warnings,
                # exactly like a run that delivered 13.
                not_placed = placement_report["not_placed"]
                if not_placed:
                    placed = selected_before - len(not_placed)
                    unclassified = placement_report["unclassified"]
                    if len(unclassified) == len(not_placed):
                        why = (f"none of them were classified, so there was no "
                               f"topic to match a section against (classifier: "
                               f"{visual_registry.classifier_status})")
                    elif unclassified:
                        why = (f"{len(unclassified)} were never classified and the "
                               f"rest matched no section in these notes")
                    else:
                        why = "no section in these notes matched their topic"
                    warnings.append(
                        f"Slide visuals: {placed} of {selected_before} selected "
                        f"diagram(s) were placed in the notes — {why}. A diagram "
                        f"on the wrong topic is worse than one missing, so the "
                        f"rest were left out rather than forced in.")
            else:
                # No registry (visuals off/failed): a stray marker must never
                # reach the student as literal text.
                notes_text = strip_figure_markers(notes_text)
            (run_dir / "notes_repaired.md").write_text(notes_text, encoding="utf-8")

            # Correction meta-language must not reach a student document —
            # but killing the run is the wrong way to enforce that. A hit here
            # used to discard the transcript, the notes and the QC pass, all
            # already paid for, over a phrase a reviewer fixes in seconds.
            #
            # So it is reported instead: a warning on the run, and one High row
            # per hit in the Error Report, which is the file a reviewer reads
            # and the student never sees. The documents are still produced.
            from .correction_language_guard import find_correction_metalanguage

            correction_language_hits = find_correction_metalanguage(notes_text)
            if correction_language_hits:
                warnings.append(
                    f"{len(correction_language_hits)} line(s) in the notes announce a "
                    f"correction instead of stating the fact. The notes were still "
                    f"produced; every line is listed in the Error Report under "
                    f"'Correction meta-language' so it can be reworded before the "
                    f"notes are issued.")

            if is_devanagari(language_code):
                # Report, never strip (script contract §5.1): stripping a
                # Roman run would corrupt the sentence it sits in. The check
                # is about the SCRIPT, so Marathi gets it for the same reason
                # Hindi does.
                from .script_validator import latin_script_warnings

                warnings.extend(latin_script_warnings(notes_text))

            # The two-level heading the reference opens with: SUBJECT above
            # LECTURE N + TITLE. Both parts come from metadata the run already
            # resolved — the subject the area picker set or detection found, and
            # the lecture number the user typed or the file name carried. A
            # missing number is omitted rather than invented (see title_utils).
            subject_line = subject_display_name(
                area if area and area != "auto" else subject, language_code) or filename_subject(input_stem)

            # Yellow highlights, from the QC audit trail rather than from a
            # second pass over the notes: every span here is a correction the QC
            # engine decided on and the applier actually spliced in, so the
            # shading in the document and the rows in the Error Report always
            # describe the same set of changes. Grammar/formatting edits are
            # filtered out inside collect_highlights, not here.
            from .correction_highlights import collect_highlights, highlight_spans

            # The Error Report rows are the second source, and a weaker one:
            # its "Correct information" column is written for a human and is
            # usually a description of the fix rather than the fixed sentence.
            # collect_highlights only uses a row when its wording is verbatim in
            # the notes, so this adds highlights where it can and stays silent
            # where it cannot. Parsed here for reading only — the XLSX below
            # still parses and reports the same blocks exactly as before.
            highlight_error_rows: list = []
            if generate_error_report and error_report_marker_found:
                try:
                    highlight_error_rows, _ = build_report_rows(
                        "\n".join(b for b in error_report_blocks if b.strip()),
                        marker_found=True,
                        strictness=error_report_strictness,
                    )
                except Exception:  # noqa: BLE001 — a highlight is never worth a run
                    highlight_error_rows = []

            def _render_documents(text: str):
                """Write the DOCX, export the PDF, and report the page count.

                Highlights are recomputed per render rather than reused: a
                compression pass rewrites the sentences the QC corrections sit
                in, and a span that no longer appears verbatim would either shade
                nothing or shade the wrong words.
                """
                highlights = collect_highlights(
                    text,
                    applied=(qc_result.applied if qc_result is not None else None),
                    error_rows=highlight_error_rows,
                )
                written, write_warnings = write_upsc_dcn_docx(
                    text,
                    docx_path,
                    logo_path=logo_path,
                    template_path=template_path,
                    use_notes_box=dcn_opts["use_notes_box"],
                    add_footer_strip=dcn_opts["add_footer_strip"],
                    language_code=language_code,
                    lecture_title=forced_lecture_title,
                    lecture_no=resolved_title.lecture_no if resolved_title else "",
                    subject_line=subject_line,
                    visuals=(visual_registry.writer_map()
                             if visual_registry is not None else None),
                    highlight_spans=highlight_spans(highlights),
                )
                rendered, render_warning = export_docx_to_pdf(written, output_dir)
                return (written, write_warnings, rendered, render_warning,
                        pdf_page_count(rendered), highlights)

            # ---------------- adaptive page control ----------------
            # The word budget earlier in the run is a PREDICTION. This is the
            # measurement: render the real document, count the pages Word
            # actually produced, and if it is over the limit hand the notes back
            # to the model to be compressed — then render again.
            #
            # Bounded at DCN_MAX_COMPRESSION_PASSES. A document still over after
            # that ships at its real length with a warning: truncating it would
            # drop whatever the class covered last, which is worse than a long
            # document and impossible for the reader to notice.
            limit = page_limit()
            page_attempts: list[dict] = []
            (docx_path, docx_warnings, pdf_path, pdf_warning,
             page_count, correction_highlights) = _render_documents(notes_text)
            page_attempts.append({"attempt": 0, "pages": page_count,
                                  "words": count_words(notes_text)})

            for attempt in range(1, max_compression_passes() + 1):
                if not page_count or page_count <= limit or client is None:
                    break
                _stage(stage_callback,
                       f"Compressing notes to {limit} pages "
                       f"(pass {attempt} of {max_compression_passes()})")
                (run_dir / f"notes_before_page_compress_{attempt}.md").write_text(
                    notes_text, encoding="utf-8")
                try:
                    resp = client.generate(
                        build_page_compress_prompt(
                            notes_text, language_code=language_code,
                            actual_pages=page_count, attempt=attempt),
                        [], max_images=0,
                        max_output_tokens=TRANSCRIPT_NOTES_MAX_OUTPUT_TOKENS)
                    compressed, _, _ = extract_error_report_block(resp.text)
                    api_metadata.append(
                        _api_call_metadata(resp, chunk=f"page-compress-{attempt}"))
                except Exception as exc:  # noqa: BLE001 — never fail a finished run
                    warnings.append(
                        f"The notes rendered to {page_count} pages and the compression "
                        f"pass failed ({exc}); the full document was kept.")
                    break

                # The same guard the word-budget pass uses: shorter, but not so
                # much shorter that what came back is a summary rather than these
                # notes tightened.
                if not accept_condensed(notes_text, compressed):
                    warnings.append(
                        f"The notes rendered to {page_count} pages, but the compression "
                        f"pass returned something that is not a safe shortening of them, "
                        f"so the full document was kept.")
                    break

                notes_text = compressed
                (run_dir / f"notes_page_compressed_{attempt}.md").write_text(
                    notes_text, encoding="utf-8")
                (docx_path, docx_warnings, pdf_path, pdf_warning,
                 page_count, correction_highlights) = _render_documents(notes_text)
                page_attempts.append({"attempt": attempt, "pages": page_count,
                                      "words": count_words(notes_text)})

            metadata_length["page_attempts"] = page_attempts
            metadata_length["final_pages"] = page_count
            metadata_length["page_limit"] = limit
            # The limit is enforced by measuring a render. No render, no
            # measurement, no enforcement — recorded either way so the run log
            # answers "was this document actually held to the limit?".
            metadata_length["page_limit_enforced"] = bool(page_count)
            if not page_count:
                # The limit is enforced by MEASURING the rendered PDF. With no
                # PDF there is no measurement, so the loop above never ran even
                # once. Saying nothing would leave the reader believing a
                # document of unknown length had been held to the limit.
                warnings.append(
                    f"The {limit}-page limit could not be enforced: the notes could not "
                    f"be rendered to a PDF on this machine, and page count is measured "
                    f"from that render. The document was written at its full length. "
                    f"Check the PDF export error above to restore page limiting.")
            elif page_count > limit:
                warnings.append(
                    f"These notes are {page_count} pages, above the {limit}-page limit. "
                    f"{len(page_attempts) - 1} compression pass(es) were run; nothing was "
                    f"removed beyond what those passes could safely tighten.")
            elif len(page_attempts) > 1:
                warnings.append(
                    f"The notes first rendered to {page_attempts[0]['pages']} pages and were "
                    f"compressed to {page_count} to hold the {limit}-page limit. Every "
                    f"intermediate draft is kept in the run folder.")
            if correction_highlights:
                write_json(run_dir / "correction_highlights.json",
                           [h.as_dict() for h in correction_highlights])
                warnings.append(
                    f"{len(correction_highlights)} QC correction(s) are highlighted in "
                    "yellow in the notes; the Error Report explains each one.")
        else:
            docx_path, docx_warnings = write_notes_docx(notes_text, docx_path, slides, run_dir=run_dir, image_insert_mode=image_insert_mode, dtp_note_policy=dtp_note_policy, subject=subject, chapter_title=chapter_title, show_corrections=show_corrections)
            pdf_path, pdf_warning = export_docx_to_pdf(docx_path, output_dir)
        warnings.extend(docx_warnings)

        # The text the documents above were built from, kept as its own file.
        # notes_repaired.md predates the compression loop, so on a run that
        # compressed, no stored draft matches what the student received. Written
        # for every workflow, not just DCN — a default-workflow run has no
        # equivalent file at all.
        notes_final_path = run_dir / "notes_final.md"
        notes_final_path.write_text(notes_text, encoding="utf-8")
        if pdf_warning:
            warnings.append(pdf_warning)
            # Also kept as its own field: the UI needs to name the cause next to
            # the missing download, and picking it back out of `warnings` by
            # substring is unreliable.
            pdf_error = pdf_warning

        # DCN post-checks. Both are advisory: a bad score or a failed render
        # warns but never withholds the documents the user came for.
        if dcn_mode:
            if dcn_opts["separate_quality_report"]:
                try:
                    from .upsc_quality_checker import check_dcn_notes

                    # Computed here but written further down, after the shared
                    # UPSC report block — that block also writes
                    # quality_report.md, and for a DCN run the DCN findings are
                    # the ones that must survive.
                    dcn_quality_report = check_dcn_notes(notes_text)
                    warnings.extend(dcn_quality_report.format_warnings)
                    warnings.extend(dcn_quality_report.layout_warnings)
                except Exception as e:  # noqa: BLE001 — advisory check only
                    warnings.append(f"DCN quality check failed: {e}")

            if dcn_opts["render_verify"]:
                _stage(stage_callback, "Verifying layout")
                try:
                    from .docx_render_check import render_and_verify

                    # Reuse the PDF exported just above: it is a render of this
                    # exact DOCX, and converting it again means a second trip
                    # through Word over COM for a byte-identical result.
                    dcn_render_check = render_and_verify(
                        docx_path, ensure_dir(run_dir / "render_check"),
                        existing_pdf=pdf_path)
                    warnings.extend(dcn_render_check.warnings)
                    for page in dcn_render_check.overlap_pages:
                        warnings.append(
                            f"Layout check: page {page} has body text reaching into the "
                            "'Space for Notes' column.")
                except Exception as e:  # noqa: BLE001 — advisory check only
                    warnings.append(f"DOCX render verification failed: {e}")

            # VISUAL QC: were the selected diagrams inserted, once each, in the
            # right sections, at readable size? Advisory like every other DCN
            # post-check — problems warn, the documents are never withheld.
            try:
                from .qc.visual_qc import run_visual_qc

                visual_qc_result = run_visual_qc(visual_registry, notes_text, docx_path)
                write_json(run_dir / "visual_qc.json", visual_qc_result)
                for problem in visual_qc_result.get("problems", []):
                    warnings.append(f"Visual QC: {problem}")
            except Exception as e:  # noqa: BLE001 — advisory check only
                warnings.append(f"Visual QC failed: {e}")

        # Final output-format validation (spec §22): deterministic checks always;
        # the paid visual inspection only at Strict with its toggle on. A failed
        # check warns — it never discards the DOCX/PDF.
        format_check_result: dict = {}
        if upsc_mode:
            _stage(stage_callback, "Checking final formatting")
            format_check_result = run_local_output_checks(
                docx_path, pdf_path, dtp_note_policy=dtp_note_policy)
            visual_wanted = (
                quality_level == "strict"
                and (qc_options or {}).get("visual_pdf_check", True)
                and client is not None and pdf_path is not None
            )
            if visual_wanted:
                if pw_access.check_allowed(google_token):
                    visual = run_visual_format_check(
                        client, pdf_path, run_dir,
                        language_code=language_code)
                    format_check_result["visual"] = visual
                    for p in visual.get("problems", []):
                        warnings.append(
                            f"Visual format check, page {p.get('page')}: {p.get('problem')}")
                else:
                    format_check_result["visual"] = {
                        "status": "unavailable",
                        "error": "proxy denied access before the visual check (fail closed)",
                    }
            elif quality_level == "strict" and client is not None:
                format_check_result["visual"] = {"status": "disabled"}
            for p in format_check_result.get("problems", []):
                warnings.append(f"Output format check: {p}")
            write_json(run_dir / "final_format_check.json", format_check_result)

        # Surface any model fallback that fired, so the run report says which
        # model actually wrote the notes.
        if client is not None:
            warnings.extend(client.fallback_notices)

        # No usage flush here by design: with pw-app-kit v2 the proxy logs each
        # provider call it serves as its own raw row, so there is nothing left
        # for the app to write. The rows for this run are tied together by
        # task_id (recorded in run_metadata below).
        warnings.extend(quality_check(notes_text, slides, docx_path, pdf_path, send_images_to_ai=send_images_to_ai))

        # ---------------- Error Report XLSX ----------------
        # The one report a normal user is given, for every run and every
        # workflow. Written even when the model said nothing, because "checked,
        # found nothing" and "never checked" have to be distinguishable — the
        # sheet says which, in a row a reviewer can read.
        if generate_error_report:
            _stage(stage_callback, "Building error report")
            try:
                combined = "\n".join(b for b in error_report_blocks if b.strip())
                error_rows, error_report_status = build_report_rows(
                    combined,
                    marker_found=error_report_marker_found,
                    strictness=error_report_strictness,
                )
                # Correction meta-language found in the finished notes. Added
                # here rather than raised earlier, so the run survives and the
                # reviewer gets the line numbers. If the model reported nothing
                # else, the "no issues" placeholder is replaced — there IS an
                # issue, and a sheet saying otherwise would be wrong.
                if correction_language_hits:
                    from .correction_language_guard import correction_language_rows

                    extra = correction_language_rows(correction_language_hits)
                    if error_report_status == "no_issues":
                        error_rows = extra
                    else:
                        error_rows = list(error_rows) + extra
                    error_report_status = "ok"
                (run_dir / "error_report_raw.json").write_text(
                    combined or "[]", encoding="utf-8")

                # A second, cold look at each finding before it reaches the
                # sheet. One call for the whole list, and it can only DEMOTE a
                # row — a check that could delete a real correction, or block
                # the report by failing, would be worse than no check.
                verify_summary = {"ran": False, "checked": 0, "demoted": 0,
                                  "reason": "not attempted"}
                try:
                    from .error_report_verifier import verify_rows

                    error_rows, verify_summary = verify_rows(
                        error_rows, client, today=date.today().isoformat())
                    write_json(run_dir / "error_report_verification.json", verify_summary)
                    if verify_summary.get("demoted"):
                        warnings.append(
                            f"{verify_summary['demoted']} of {verify_summary['checked']} "
                            f"error-report finding(s) could not be supported on a second "
                            f"check and were downgraded to Low, with the doubt written "
                            f"into the row.")
                except Exception as exc:  # noqa: BLE001 — never lose a report over this
                    verify_summary = {"ran": False, "checked": 0, "demoted": 0,
                                      "reason": f"{type(exc).__name__}: {exc}"}
                    warnings.append(f"Error-report verification pass skipped: {exc}")
                error_report_summary = summarise(error_rows)
                # Carried into the summary the results card reads. Whether a
                # finding survived a second opinion is the single most useful
                # thing to know about it, and it used to live only in a JSON
                # file in the run folder that no user opens.
                error_report_summary["verified"] = bool(verify_summary.get("ran"))
                error_report_summary["verify_checked"] = int(
                    verify_summary.get("checked") or 0)
                error_report_summary["verify_demoted"] = int(
                    verify_summary.get("demoted") or 0)
                error_report_summary["verify_reason"] = str(
                    verify_summary.get("reason") or "")
                # ---------- source locations ----------
                # Source locations make each row independently checkable and
                # provide the real recording timestamp for the XLSX column.
                #
                # Locations are found by searching the real transcript segments
                # and the real slides for the text each row quotes. A row whose
                # text is not found is reported as unlocated — never given an
                # estimated timestamp, because a wrong one costs the reader
                # their trust in every other row.
                try:
                    # Timed segments when the transcript has them. When it does
                    # not — a user-supplied transcript, or an engine that
                    # returns none — the text is cut into untimed segments so
                    # there is still something to search. The alternative is an
                    # empty haystack, which reports every row as "unable to
                    # verify from provided source" without having looked.
                    timed_segments = [
                        {"start": seg.get("start"), "end": seg.get("end"),
                         "text": seg.get("text", "")}
                        for seg in (transcript_meta.get("segments") or [])
                    ]
                    locator_segments = timed_segments or segments_from_text(
                        transcript_text)
                    locator_sources = LocatorSources(
                        segments=locator_segments,
                        slides=[
                            {"slide_no": sl.slide_no, "heading": sl.heading,
                             "text": sl.prompt_text}
                            for sl in active_slides
                        ],
                        video_url=video_url or "",
                        slide_source_type=(active_slides[0].source_type
                                           if active_slides else ""),
                    )
                    error_locations = [locate_row(r, locator_sources) for r in error_rows]
                    for row, location in zip(error_rows, error_locations):
                        # A timestamp is meaningful only for a timed recording.
                        # A slide/page is precise but does not have a clock time,
                        # and an unmatched statement must never be estimated.
                        row["timestamp"] = (
                            location.timestamp if location.kind == "video"
                            else "Not applicable" if location.located
                            else location.timestamp
                        )
                    write_json(
                        run_dir / "error_locations.json",
                        [loc.as_dict() for loc in error_locations])

                    detailed = render_detailed_error_report(
                        error_rows, error_locations,
                        lecture_title=(resolved_title.display if resolved_title
                                       else forced_lecture_title),
                        video_url=video_url or "",
                        input_name=input_name,
                        language=language_name(language_code),
                        transcript_has_timestamps=locator_sources.has_timestamps,
                    )
                    error_report_detailed_path = (
                        output_dir / f"{stem}_error_report_detailed.md")
                    error_report_detailed_path.write_text(detailed, encoding="utf-8")

                    located = sum(1 for loc in error_locations if loc.located)
                    # How many findings a reviewer can jump straight to. This is
                    # the entire value of the located report, and it was
                    # reported only in a warnings list the results card does not
                    # show.
                    error_report_summary["located"] = located
                    error_report_summary["locatable_against"] = (
                        "recording" if locator_sources.has_timestamps
                        else "transcript" if locator_sources.searchable
                        else "")
                    if error_rows and not locator_sources.searchable:
                        # Nothing was searched, so "0 of 2 were traced" would be
                        # a lie of omission: it reads as "we looked and did not
                        # find them". Name the real reason instead.
                        warnings.append(
                            "Error Report: no transcript text or slides were available "
                            "to trace the finding(s) back to, so every row is marked as "
                            "not locatable. This is a missing source, not a failed "
                            "search.")
                    elif error_rows:
                        how = ("timed recording" if locator_sources.has_timestamps
                               else "transcript text (no timings, so positions are "
                                    "given as segment numbers rather than clock times)")
                        warnings.append(
                            f"Error Report: {located} of {len(error_rows)} finding(s) "
                            f"were traced against the {how}; the rest are marked as not "
                            f"locatable rather than being given an estimated timestamp.")
                except Exception as exc:  # noqa: BLE001 — retain the report without locations
                    for row in error_rows:
                        row["timestamp"] = "Timestamp unavailable"
                    warnings.append(
                        f"Source locations could not be added to the error report "
                        f"({exc}); the Error Report XLSX marks timestamps unavailable.")
                error_report_xlsx_path = write_error_report_xlsx(
                    error_rows,
                    output_dir / f"{stem}_error_report.xlsx",
                    template_path=error_report_template_path,
                )
                if error_report_status == "missing":
                    warnings.append(
                        "The AI response carried no # ERROR_REPORT_START block, so the error "
                        "report could not be filled in. The XLSX records that rather than "
                        "claiming the lecture was error-free."
                    )
                elif error_report_status == "parse_failed":
                    warnings.append(
                        "The AI's error report was not valid JSON. The XLSX records that; "
                        "see error_report_raw.json in the run folder for what it sent."
                    )
            except Exception as e:  # noqa: BLE001 — never lose the notes over a report
                error_report_status = "failed"
                error_report_error = str(e)
                warnings.append(f"Error report XLSX could not be created: {e}")

        # Everything the frontend no longer shows still has to be recoverable.
        # This is the whole technical picture of the run, in one place.
        technical_log = {
            **stamp_ist(),
            "run_id": run_dir.name,
            # "ethics" is a UPSC *area*, not a subject. Logging it as the subject
            # is what made the UI announce "Subject auto-detected as: ethics".
            "subject": "upsc" if upsc_mode else subject,
            "upsc_area": area if upsc_mode else None,
            "detected_language": language_code,
            "note_depth": note_depth if dcn_mode else None,
            # What the page really says. `resolved_title.display` falls back to
            # "Class Notes" when nothing could be resolved, and logging that for a
            # run whose title the model supplied would misreport the document.
            "lecture_title": (forced_lecture_title
                              or _title_from_notes(notes_text) or None),
            "subject_line": subject_line,
            "correction_highlight_count": len(correction_highlights),
            **({"note_length": metadata_length} if dcn_mode else {}),
            "lecture_no": resolved_title.lecture_no if resolved_title else None,
            "dcn_no": resolved_title.dcn_no if resolved_title else None,
            "dmp_no": dmp_no or None,
            "dpp_no": dpp_no or None,
            "title_inferred": resolved_title.inferred if resolved_title else None,
            "slides_source_of_truth": src_opts["slides_source_of_truth"],
            "transcript_explanation_only": src_opts["transcript_explanation_only"],
            "transcript_source": transcript_source or None,
            "transcript_words": len(transcript_text.split()) if transcript_text else 0,
            # Where the transcript came from and how it was made. The segments
            # themselves stay in transcript_segments.json — a 2-hour lecture has
            # thousands, and they would swamp the log.
            "video_source_type": transcript_meta.get("source_type") or None,
            "transcription_engine": transcript_meta.get("engine") or None,
            # How the transcript was obtained, stated directly rather than left
            # to be inferred from the engine name — this is what makes "how much
            # time does the native path save" answerable from the run logs.
            "native_transcript_found": (
                transcript_meta.get("engine") in ("captions", "drive_native")
                if transcript_meta else None),
            "stt_used": (
                transcript_meta.get("engine") in ("gemini", "faster_whisper")
                if transcript_meta else None),
            "transcript_retrieval_seconds": transcript_meta.get("retrieval_seconds"),
            "transcript_language": transcript_meta.get("language") or None,
            "transcript_duration_seconds": transcript_meta.get("duration_seconds") or None,
            "transcript_segments": len(transcript_meta.get("segments") or []),
            "transcript_gaps": transcript_meta.get("gaps") or [],
            "transcript_clean": transcript_clean_summary or None,
            "slide_glossary_terms": len(slide_glossary),
            "chunked": bool(chunk_count),
            "chunk_count": chunk_count,
            "ai_call_count": len(api_metadata),
            "format_repair": format_repair_summary or None,
            "markup_repairs": markup_repairs or None,
            "script_repair": script_repair_summary or None,
            # Lecture visuals: what was found, what went in, and whether the
            # AI classifier actually ran — the registry itself lives in
            # visual_registry.json.
            "visuals_found": len(visual_registry.visuals) if visual_registry else 0,
            # Chosen for insertion, BEFORE placement had its say. The line below
            # is the post-placement count, and the two differing is exactly the
            # case the results card needs to report: diagrams the slides had,
            # that the notes do not.
            "visuals_selected_for_placement": visuals_selected_for_placement,
            "visuals_selected": len(visual_registry.selected) if visual_registry else 0,
            "visuals_inserted": (visual_qc_result or {}).get("inserted", 0),
            "visual_classifier_status": (
                visual_registry.classifier_status if visual_registry else "not_run"),
            "visual_qc_status": (visual_qc_result or {}).get("status", "not_run"),
        }

        # Recorded in run_log.json and run_metadata.json for both workflows.
        error_report_log = {
            "generate_error_report": generate_error_report,
            "error_report_strictness": error_report_strictness,
            "error_report_template_used": bool(error_report_template_path),
            "error_report_status": error_report_status,
            "error_report_xlsx_path": (
                str(error_report_xlsx_path) if error_report_xlsx_path else None),
            "error_report_detailed_path": (
                str(error_report_detailed_path) if error_report_detailed_path else None),
            "error_report_counts": error_report_summary,
        }

        if upsc_mode:
            _stage(stage_callback, "Preparing reports")
            # Built after the DOCX/PDF and the standard checks so the report's
            # Warnings section carries everything that went wrong in the run.
            corrections = parse_correction_blocks(notes_text)
            structures = count_structures(notes_text)
            quality_summary = build_quality_summary(
                corrections,
                total_slides=total_slides,
                retained_slides=active_slide_count,
                removed_slides=total_slides - active_slide_count,
                structures=structures,
            )
            report_md = render_quality_report_md(
                quality_summary,
                corrections,
                ai_report=ai_report,
                warnings=warnings,
                meta={
                    "note_type": note_type,
                    "area": area,
                    "language": language_code,
                    "quality_level": quality_level,
                    "model": "mock" if use_mock else model,
                    "class_video": video_url or "not provided",
                    # QC counters + honest per-stage statuses (spec §24):
                    # completed / disabled / failed / unavailable are distinct.
                    **(qc_quality_report_meta(qc_result) if qc_result is not None else {}),
                    **({"format_check": format_check_result.get("status", ""),
                        "visual_format_check": (format_check_result.get("visual") or {}).get("status", "not run")}
                       if format_check_result else {}),
                },
            )
            quality_report_md_path = run_dir / "quality_report.md"
            quality_report_md_path.write_text(report_md, encoding="utf-8")

            # Error Report — the faculty's factual errors and what each one changed
            # in the notes. Built from the same correction blocks as the quality
            # report, but standalone so a reviewer can read just the fact-checking.
            error_md = render_error_report_md(
                corrections,
                filename=input_name,
                chapter_title=chapter_title,
                youtube_url=video_url,
                note_type=UPSC_NOTE_TYPE_LABELS.get(note_type, note_type),
                quality_level=quality_level,
                fact_check_on=quality_level != "off" and bool(upsc_options.get("check_facts", True)),
                transcript_issues=transcript_issues,
                transcript_words=len(transcript_text.split()) if transcript_text else 0,
            )
            error_report_md_path = run_dir / "error_report.md"
            error_report_md_path.write_text(error_md, encoding="utf-8")
            try:
                error_report_docx_path, _ = write_quality_report_docx(
                    error_md, output_dir / f"{stem}_error_report.docx",
                    title="Error Report", subject=subject, chapter_title=chapter_title,
                )
            except Exception as e:  # the .md is the real artefact; DOCX is convenience
                warnings.append(f"Error report DOCX could not be created: {e}")

            # Error Fixed Report — what the QC stage found, verified against
            # approved sources, and changed (spec §23). Additional to the
            # faculty-facts Error Report above, never a replacement.
            if qc_result is not None:
                try:
                    error_fixed_md = render_error_fixed_report_md(
                        qc_result,
                        run_id=run_dir.name,
                        filename=input_name,
                        chapter_title=chapter_title,
                        subject=subject,
                        language=language_code,
                        note_type=UPSC_NOTE_TYPE_LABELS.get(note_type, note_type),
                        quality_level=quality_level,
                        model="mock" if use_mock else model,
                        transcript_source=transcript_source,
                        transcript_gaps=[i.time_range for i in transcript_issues if i.time_range],
                        enabled_checks={k: v for k, v in upsc_options.items() if k.startswith("check_")},
                        enrichments={k: v for k, v in upsc_options.items() if k.startswith("add_")},
                        source_verification_available=(qc_result.stage("claim_verification").status
                                                       in ("completed", "completed_with_warnings")),
                    )
                    error_fixed_report_md_path = run_dir / "error_fixed_report.md"
                    error_fixed_report_md_path.write_text(error_fixed_md, encoding="utf-8")
                    try:
                        error_fixed_report_docx_path, _ = write_quality_report_docx(
                            error_fixed_md, output_dir / f"{stem}_error_fixed_report.docx",
                            title="Error Fixed Report", subject=subject, chapter_title=chapter_title,
                        )
                    except Exception as e:
                        warnings.append(f"Error Fixed Report DOCX could not be created: {e}")
                except Exception as e:
                    warnings.append(f"Error Fixed Report could not be created: {e}")
            if generate_quality_report:
                try:
                    quality_report_docx_path, report_warnings = write_quality_report_docx(
                        report_md, output_dir / f"{stem}_quality_report.docx",
                        subject=subject, chapter_title=chapter_title,
                    )
                    warnings.extend(report_warnings)
                except Exception as e:  # never lose the notes over a failed report
                    warnings.append(f"Quality report DOCX could not be created: {e}")
            # DCN runs: the DCN checker's findings are the headline report, with
            # the shared UPSC summary kept underneath so nothing is lost. This
            # file is for the reviewer only — the student DOCX stays clean.
            if dcn_mode and dcn_quality_report is not None:
                visual_qc_md = ""
                if visual_qc_result is not None:
                    from .qc.visual_qc import render_visual_qc_md

                    visual_qc_md = "\n\n---\n\n" + render_visual_qc_md(visual_qc_result)
                quality_report_md_path = run_dir / "quality_report.md"
                quality_report_md_path.write_text(
                    dcn_quality_report.to_markdown()
                    + visual_qc_md
                    + "\n\n---\n\n## Pipeline quality summary\n\n"
                    + report_md,
                    encoding="utf-8",
                )

            run_log = {
                "workflow": "upsc",
                "app_version": version_label(),
                "note_type": note_type,
                "upsc_output_format": upsc_output_format,
                "note_depth": note_depth,
                "fact_mode": fact_mode,
                "dcn_options": dcn_opts if dcn_mode else None,
                "template_used": bool(template_path) if dcn_mode else None,
                "logo_used": bool(logo_path) if dcn_mode else None,
                "notes_model": "mock" if use_mock else model,
                "fact_check_model": (
                    None if use_mock or quality_level == "off" else model
                ),
                "start_time": run_started_at,
                "end_time": _utc_now_iso(),
                "total_seconds": round(time.time() - run_started_monotonic, 2),
                "corrections_count": (
                    dcn_quality_report.corrections if dcn_quality_report else None
                ),
                "updated_current_data_count": (
                    dcn_quality_report.updated_current_data if dcn_quality_report else None
                ),
                "verification_needed_count": (
                    dcn_quality_report.needs_verification if dcn_quality_report else None
                ),
                "render_verification": (
                    {
                        "ok": dcn_render_check.ok,
                        "page_count": dcn_render_check.page_count,
                        "overlap_pages": dcn_render_check.overlap_pages,
                        "pdf": str(dcn_render_check.pdf_path) if dcn_render_check.pdf_path else None,
                        "page_images": [str(p) for p in dcn_render_check.page_images],
                    }
                    if dcn_render_check is not None
                    else "not run"
                ),
                "area": area,
                "language": language_code,
                "quality_level": quality_level,
                "quality_options": upsc_options,
                "generate_quality_report": generate_quality_report,
                "show_corrections": show_corrections,
                "model": "mock" if use_mock else model,
                "input_file": str(input_path) if has_slide_file else None,
                "video_url": video_url or None,
                # Kept under the old key too: run_log.json is read by tooling
                # that predates the rename.
                "youtube_url": video_url or None,
                "transcript_source": transcript_source or None,
                "transcript_words": len(transcript_text.split()) if transcript_text else 0,
                "marker_fallback": marker_fallback,
                "total_slides": total_slides,
                "active_slide_count": active_slide_count,
                "quality_summary": quality_summary,
                "warnings": warnings,
                "docx_path": str(docx_path) if docx_path else None,
                "pdf_path": str(pdf_path) if pdf_path else None,
                "quality_report_md_path": str(quality_report_md_path),
                "quality_report_docx_path": str(quality_report_docx_path) if quality_report_docx_path else None,
                **error_report_log,
                **technical_log,
            }
            write_json(run_dir / "run_log.json", run_log)
        else:
            # Non-UPSC runs have never written a run_log.json. They do now, but
            # only with what this feature promises to record — the error report
            # — so nothing here duplicates or contradicts run_metadata.json.
            write_json(run_dir / "run_log.json", {
                "workflow": "default",
                "app_version": version_label(),
                "start_time": run_started_at,
                "end_time": _utc_now_iso(),
                "total_seconds": round(time.time() - run_started_monotonic, 2),
                "model": "mock" if use_mock else model,
                "input_file": str(input_path) if has_slide_file else None,
                "docx_path": str(docx_path) if docx_path else None,
                "pdf_path": str(pdf_path) if pdf_path else None,
                **error_report_log,
                **technical_log,
            })

        run_metadata = {
            "input_file": str(input_path) if has_slide_file else None,
            "copied_input": str(copied_input),
            "subject": subject,
            "language": language_code,
            "mode": mode,
            "workflow": "upsc" if upsc_mode else "default",
            "note_type": note_type or None,
            "area": area if upsc_mode else None,
            "quality_level": quality_level if upsc_mode else None,
            "model": model,
            "provider_requested": "pw_proxy",
            # Groups this run's rows in the proxy's Raw Usage Ledger Export.
            "task_id": task_id,
            "send_images_to_ai": send_images_to_ai,
            "strict_filter": strict_filter,
            "batch_size": batch_size,
            "max_images_per_call": max_images_per_call,
            "total_slides": total_slides,
            "active_slide_count": active_slide_count,
            "input_unit": "slide/page",
            # The SUBJECT printed above the lecture title, and how many QC
            # corrections were shaded yellow in the notes. Recorded so an
            # edit-and-rebuild reproduces the same heading and the same
            # highlights instead of re-deriving them.
            "subject_line": subject_line,
            "correction_highlight_count": len(correction_highlights),
            **({"note_length": metadata_length} if dcn_mode else {}),
            "video_url": video_url or None,
            "youtube_url": video_url or None,
            "transcript_source": transcript_source or None,
            "transcript_words": len(transcript_text.split()) if transcript_text else 0,
            "api_calls": api_metadata,
            "warnings": warnings,
            "docx_path": str(docx_path) if docx_path else None,
            "pdf_path": str(pdf_path) if pdf_path else None,
            "notes_final_path": str(notes_final_path) if notes_final_path else None,
            "quality_report_md_path": str(quality_report_md_path) if quality_report_md_path else None,
            "quality_report_docx_path": str(quality_report_docx_path) if quality_report_docx_path else None,
            "quality_summary": quality_summary,
            "marker_fallback": marker_fallback,
            "transcript_issue_count": len(transcript_issues),
            "error_report_md_path": str(error_report_md_path) if error_report_md_path else None,
            "error_report_docx_path": str(error_report_docx_path) if error_report_docx_path else None,
            "error_fixed_report_md_path": str(error_fixed_report_md_path) if error_fixed_report_md_path else None,
            "error_fixed_report_docx_path": str(error_fixed_report_docx_path) if error_fixed_report_docx_path else None,
            # QC summary incl. framework/prompt/registry versions, claim and
            # correction counters, per-stage statuses, fallback events.
            **({"qc": qc_result.summary} if qc_result is not None else {"qc": {"qc_enabled": False}}),
            "format_check": format_check_result or None,
            "fallback_used": bool(client.fallback_notices) if client is not None else False,
            # DCN-format run settings and layout verdict, so the UI can report
            # the layout check honestly instead of assuming it passed.
            "upsc_output_format": upsc_output_format if upsc_mode else None,
            "dcn_mode": dcn_mode,
            "note_depth": note_depth if dcn_mode else None,
            "fact_mode": fact_mode if dcn_mode else None,
            "dcn_template_used": bool(template_path) if dcn_mode else None,
            "dcn_render_check": (
                {
                    "ok": dcn_render_check.ok,
                    "page_count": dcn_render_check.page_count,
                    "overlap_pages": dcn_render_check.overlap_pages,
                    "page_images": [str(p) for p in dcn_render_check.page_images],
                    "warnings": dcn_render_check.warnings,
                }
                if dcn_render_check is not None
                else None
            ),
            **error_report_log,
            **technical_log,
        }
        write_json(run_dir / "run_metadata.json", run_metadata)
        zip_path = zip_dir(run_dir, run_dir.with_suffix(".zip"))
        return PipelineResult(
            run_dir=run_dir, docx_path=docx_path, pdf_path=pdf_path, zip_path=zip_path,
            raw_notes_path=raw_notes_path, notes_final_path=notes_final_path,
            warnings=warnings, metadata=run_metadata,
            quality_report_md_path=quality_report_md_path,
            quality_report_docx_path=quality_report_docx_path,
            quality_summary=quality_summary,
            error_report_md_path=error_report_md_path,
            error_report_docx_path=error_report_docx_path,
            transcript_issues=transcript_issues,
            error_fixed_report_md_path=error_fixed_report_md_path,
            error_fixed_report_docx_path=error_fixed_report_docx_path,
            qc_summary=(qc_result.summary if qc_result is not None else {}),
            error_report_xlsx_path=error_report_xlsx_path,
            error_report_detailed_path=error_report_detailed_path,
            error_report_summary=error_report_summary,
            error_report_status=error_report_status,
            pdf_error=pdf_error,
            error_report_error=error_report_error,
        )
    except Exception as e:
        warnings.append(str(e))
        # No usage rescue needed on this path any more: under pw-app-kit v2 the
        # proxy logs each paid call when it serves it, so calls made before the
        # crash are already in the ledger. (Under v1 the combined row was
        # written late and a crash here lost it entirely.) task_id goes into
        # error.json so a failed run's rows are still findable.
        try:
            write_json(run_dir / "error.json",
                       {"error": str(e), "task_id": task_id, "warnings": warnings})
            zip_path = zip_dir(run_dir, run_dir.with_suffix(".zip"))
        except Exception:
            pass
        raise


def run_batch(
    input_paths: list[Path],
    *,
    progress_callback=None,
    **kwargs,
) -> list[tuple[Path, PipelineResult | None, str | None]]:
    """Run the pipeline over many files, isolating per-file failures.

    Returns a list of (input_path, result_or_None, error_message_or_None) so one
    bad file never aborts the whole batch. `progress_callback(index, total, path)`
    is called before each file if provided.
    """
    results: list[tuple[Path, PipelineResult | None, str | None]] = []
    total = len(input_paths)
    for i, path in enumerate(input_paths, start=1):
        path = Path(path)
        if progress_callback:
            try:
                progress_callback(i, total, path)
            except Exception:
                pass
        try:
            result = run_pipeline(path, **kwargs)
            results.append((path, result, None))
        except Exception as e:
            results.append((path, None, str(e)))
    return results
