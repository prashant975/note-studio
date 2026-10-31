"""Driving the DPP question-set builder from saved or uploaded notes.

Saved lectures use the text and language already recorded in the app's run
folder, so no PDF extraction or OCR is needed. The optional upload workspace
uses the standalone ``notes_to_dpp`` extractor for PDF/TXT/Markdown sources,
including OCR when requested, without creating a fake generated-notes run.

Everything after that — coverage matching, selection, curation, export — is the
`notes_to_dpp` package unchanged. This module only supplies its inputs.

The one thing it does carefully is loading the masters. The question bank is
~55 MB and takes about 23 seconds to parse, so it is cached on the file's path
and modification time: a Streamlit rerun happens on every checkbox click, and
re-reading the bank each time would make the panel unusable.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from .config import ROOT_DIR, RUNS_DIR
from .languages import normalise
from .notes_to_dpp.models import Language, NoteDocument, NotePage

#: Where each master workbook is looked for, and the environment variable that
#: overrides it. Names match the standalone tool so an existing machine that
#: already sets these keeps working.
MASTERS = {
    "taxonomy": ("QBG_TAGS_PATH", "UPSC QBG Tags.xlsx"),
    "questions": ("QUESTION_BANK_PATH", "DPP and Infinite Practice Questions.xlsx"),
}

#: The note's language, as the question bank understands it. The bank holds
#: English and Hindi rows only.
_LANGUAGE_BY_CODE = {"en": Language.ENGLISH, "hi": Language.HINDI}


class DppError(RuntimeError):
    """Something the user needs told, in words they can act on."""


# ---------------------------------------------------------------- the notes

def note_document(note_id: str, runs_dir: Path | None = None) -> NoteDocument:
    """The finished notes of a run, as a NoteDocument.

    No PDF and no OCR: the text is what the pipeline wrote. Headings are kept
    (the marker prefixes are resolved to plain text) because coverage matching
    reads headings as its strongest evidence of what a lecture covered.

    Pages are the note's own `#SEPARATOR`-free sections rather than rendered
    PDF pages. The matcher only uses page numbers to report where a match came
    from, so sections are a more useful unit than an arbitrary page break.
    """
    # plain_note_text belongs to the DCN marker format rather than to DMP; it
    # is imported rather than copied so the two features cannot drift on what
    # "the note as prose" means.
    from .dmp.service import _NOTES_CANDIDATES, plain_note_text
    from .dmp.store import note_run_dir

    run_dir = note_run_dir(note_id, runs_dir or RUNS_DIR)
    if not run_dir.exists():
        raise DppError(
            "Those notes are no longer on this machine, so a practice set "
            "cannot be built from them.")

    for name in _NOTES_CANDIDATES:
        path = run_dir / name
        if not path.exists():
            continue
        text = plain_note_text(path.read_text(encoding="utf-8", errors="replace"))
        if not text.strip():
            continue
        blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
        pages = tuple(
            NotePage(page_number=i, text=block, extraction_method="generated")
            for i, block in enumerate(blocks, start=1)
        )
        return NoteDocument(filename=f"{note_id}.md", pages=pages)

    raise DppError(
        "These notes have no readable text, so there is nothing to match "
        "against the question bank.")


def note_language(note_id: str, runs_dir: Path | None = None) -> tuple[Language, str]:
    """(language for the question bank, the note's own language code).

    Read from run metadata rather than detected: the app chose the language
    before writing a word, so guessing it back off the page can only be worse.

    Marathi has no counterpart in the bank, which holds English and Hindi rows
    only. It returns UNKNOWN rather than quietly resolving to Hindi — the two
    share a script, and serving Hindi questions for a Marathi note is exactly
    the cross-language fallback the question bank is built to refuse.
    """
    from .dmp.service import note_context

    code = normalise(note_context(note_id, runs_dir)["language"]) or "en"
    return _LANGUAGE_BY_CODE.get(code, Language.UNKNOWN), code


def extract_uploaded_document(
    data: bytes,
    filename: str,
    *,
    enable_ocr: bool = True,
    max_pages: int = 200,
    progress=None,
) -> NoteDocument:
    """Read a one-session DPP upload without turning it into a notes run.

    The app's own finished notes stay on the disk-backed path above. An upload
    is deliberately different: it is only a source for the practice builder,
    so it is not persisted as a generated lecture or made available to DMP.
    """
    from .notes_to_dpp.notes import NoteExtractionError, extract_notes

    try:
        return extract_notes(
            data, filename, enable_ocr=enable_ocr, max_pages=max_pages,
            progress=progress,
        )
    except NoteExtractionError as exc:
        raise DppError(str(exc)) from exc


def coverage_document(document: NoteDocument, taxonomy_rows, *,
                      max_candidates: int = 300):
    """Closed-set coverage for an uploaded ``NoteDocument``."""
    from .notes_to_dpp.matching import match_coverage

    return match_coverage(document, tuple(taxonomy_rows), max_candidates=max_candidates)


# -------------------------------------------------------------- the masters

def resolve_master(kind: str, uploaded: bytes | None = None) -> tuple[bytes | None, str]:
    """(file bytes, where it came from) for one master workbook.

    Resolution order matches the standalone tool: an upload for this session,
    then the environment variable, then the data folder. An upload never
    touches the copy on disk.
    """
    env_name, filename = MASTERS[kind]
    if uploaded:
        return uploaded, "uploaded for this session"

    configured = os.environ.get(env_name)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise DppError(f"{env_name} points at a file that is not there: {path}")
        return path.read_bytes(), f"{env_name}"

    for candidate in _disk_candidates(filename):
        if candidate.is_file():
            return candidate.read_bytes(), f"data/{filename}"
    return None, "not found"


def _disk_candidates(filename: str) -> tuple[Path, ...]:
    """Where a master may sit when no environment variable names it.

    ROOT_DIR first: the checkout's data/ folder, or PyInstaller's _internal/
    in a frozen build. Then, frozen only, the data folder BESIDE THE EXE — the
    place the panel and the README tell people to use. The launcher does
    export that folder through the env var, but only for files that exist
    when the app starts; workbooks copied in afterwards, or a dist/ tree that
    PyInstaller has just wiped and rebuilt, stayed invisible until a restart
    while the panel kept saying "put them in the app's data folder" — the
    folder they were already in. Checking it at lookup time makes that
    sentence true on the next click. An admin's env var still wins.
    """
    candidates = [Path(ROOT_DIR) / "data" / filename, Path(ROOT_DIR) / filename]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "data" / filename)
    return tuple(candidates)


def master_path(kind: str) -> Path | None:
    """Where a master workbook sits on disk, or None. Used for cache keying."""
    env_name, filename = MASTERS[kind]
    configured = os.environ.get(env_name)
    if configured and Path(configured).expanduser().is_file():
        return Path(configured).expanduser().resolve()
    for candidate in _disk_candidates(filename):
        if candidate.is_file():
            return candidate.resolve()
    return None


@lru_cache(maxsize=2)
def _load_taxonomy(path: str, mtime_ns: int):
    from .notes_to_dpp.workbooks import load_taxonomy

    return load_taxonomy(Path(path).read_bytes())


@lru_cache(maxsize=2)
def _load_questions(path: str, mtime_ns: int, verified_only: bool):
    from .notes_to_dpp.workbooks import load_question_bank

    return load_question_bank(Path(path).read_bytes(), verified_only=verified_only)


def load_masters(*, verified_only: bool = True):
    """(taxonomy result, question-bank result), cached on path + mtime.

    The bank is ~55 MB and takes about 23 seconds to parse. Streamlit reruns
    the whole script on every widget interaction, so without this the panel
    would spend half a minute rebuilding the same object on each checkbox.

    Verified-only is the default the question bank itself ships with, and the
    one the standalone builder recommends. It matters: 6,906 of the bank's
    25,985 eligible rows have not been through verification, so defaulting the
    other way quietly puts unchecked questions in front of students in more
    than a quarter of the pool.
    """
    missing = [kind for kind in MASTERS if master_path(kind) is None]
    if missing:
        names = ", ".join(MASTERS[k][1] for k in missing)
        raise DppError(
            f"The master workbook(s) {names} are not on this machine. Put them "
            f"in the data folder, or upload them below.")

    tax_path = master_path("taxonomy")
    q_path = master_path("questions")
    taxonomy = _load_taxonomy(str(tax_path), tax_path.stat().st_mtime_ns)
    questions = _load_questions(str(q_path), q_path.stat().st_mtime_ns, verified_only)
    return taxonomy, questions


def masters_ready() -> bool:
    """Can a practice set be built at all on this machine?"""
    return all(master_path(kind) is not None for kind in MASTERS)


# ------------------------------------------------------------------ coverage

def coverage(note_id: str, taxonomy_rows, *, runs_dir: Path | None = None,
             max_candidates: int = 300):
    """Taxonomy rows this lecture covers, best first.

    Closed-set: every candidate is a row that exists in the QBG workbook. The
    matcher ranks, it never invents a label.
    """
    document = note_document(note_id, runs_dir)
    return coverage_document(document, taxonomy_rows, max_candidates=max_candidates)
