"""Prove a build can actually run the app, before anyone ships it.

Run as ``UpscNotesApp.exe --selftest`` (or ``python streamlit_launcher_stable.py
--selftest`` in a checkout). It imports every module under ``src`` and then
exercises the one feature whose dependencies have gone missing before — the
DPP practice-set export — end to end, in the same process the app would use,
and exits 0 only when all of it works.

Why this exists: ``src/`` travels inside the PyInstaller bundle as plain data,
so PyInstaller never reads a single import statement in it. Every third-party
package ``src`` needs has to be named in ``UpscNotesApp.spec`` by hand, and
v2.11.0 shipped with rapidfuzz and reportlab missing. The page rendered, the
login worked, the browser smoke test passed, and the DPP tab crashed on its
first click. An import walk catches that class of bug in seconds; a smoke
test never will, because the failing code sits behind a button on a page
behind a login.

The walk executes module-level imports only, so the packages ``src`` imports
inside functions (most of them — PDF, Office, Google auth, video) are named
in REQUIRED_LAZY_IMPORTS and imported directly.

The report is written for the person holding the release, not for a parser:
one line per problem, naming the module and the exception.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Callable, Iterable

Report = Callable[[str], None]

#: Packages src/ (and app.py) import only inside functions, so the module
#: walk never touches them. Each must load in the built EXE or some feature
#: dies on its first click — the v2.11.0 failure shape. Kept complete by
#: tests/test_frozen_build_manifest.py, which derives the same list from the
#: source and fails when an entry is missing here.
REQUIRED_LAZY_IMPORTS: tuple[str, ...] = (
    "streamlit",
    "authlib.deprecate",
    # src/drive_auth.py keeps the Drive refresh token from the sign-in through it.
    "authlib.integrations.starlette_client",
    "requests",
    "httpx",
    "dotenv",
    "yaml",
    "PIL",
    "PIL.Image",
    "fitz",
    "pymupdf",
    "numpy",
    "docx",
    "pptx",
    "openpyxl",
    "lxml",
    "lxml.etree",
    "pythoncom",
    "win32com.client",
    "yt_dlp",
    "youtube_transcript_api",
    "google.auth.transport.requests",
    "google.oauth2",
    "google.oauth2.service_account",
    "pw_access",
)

#: Third-party engines the build leaves out on purpose. Every module that uses
#: one imports it inside a function and degrades with a message, so the import
#: walk passes without them. They are listed here only so the report says
#: where they stand, and so nobody reads "not bundled" as a failure.
OPTIONAL_ENGINES: tuple[tuple[str, str], ...] = (
    ("rapidocr", "OCR for scanned PDFs; a text PDF needs none, a scan is "
                 "refused with a clear message"),
    ("faster_whisper", "local speech-to-text; the Gemini route is the default"),
    ("playwright", "QBG portal bridge; not wired into the app"),
)


def module_names(bundle_dir: Path | str, package: str = "src") -> list[str]:
    """Every module under ``bundle_dir/package``, as dotted import names.

    Packages are included (``src.dmp``), ``__pycache__`` is not, and the
    order is stable so two runs report failures in the same sequence.
    """
    bundle_dir = Path(bundle_dir)
    names: list[str] = []
    for path in sorted((bundle_dir / package).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(bundle_dir).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def import_all(names: Iterable[str], out: Report) -> list[tuple[str, str]]:
    """Import each module; return (name, error) for the ones that fail.

    Nothing short-circuits: a release with three missing packages should
    report three, not make the person fix and rebuild three times.
    """
    failures: list[tuple[str, str]] = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — every failure is reported, none hides the next
            message = f"{type(exc).__name__}: {exc}"
            failures.append((name, message))
            out(f"  FAIL  {name}: {message}")
    return failures


def dpp_export_probe(out: Report) -> list[str]:
    """Build a one-question practice set in both scripts, the way the DPP tab does.

    Returns the problems found — empty when the matcher's C extension loads,
    all three PDFs come out of reportlab in English and in Hindi, a text PDF
    with a blank page extracts without OCR, and the Devanagari font is
    registered with shaping ON. That last check is the
    one nothing else makes: reportlab does not raise when uharfbuzz is
    missing, it just renders conjuncts as separate glyphs.
    """
    problems: list[str] = []
    try:
        from rapidfuzz import fuzz
        from reportlab.pdfbase import pdfmetrics

        from .notes_to_dpp import models, pdf_export
    except Exception as exc:  # noqa: BLE001 — the whole probe hinges on these
        return [f"DPP modules did not import: {type(exc).__name__}: {exc}"]

    # rapidfuzz picks its C extension at import and falls back to pure Python
    # WITHOUT a warning when the .pyd is missing; a working score proves
    # nothing about the bundle. The implementing module does.
    same = fuzz.ratio("Fundamental Rights", "Fundamental Rights")
    different = fuzz.ratio("Fundamental Rights", "Monetary Policy Committee")
    implementation = getattr(fuzz.ratio, "__module__", "") or ""
    if same != 100 or different >= 60:
        problems.append(
            f"rapidfuzz gave implausible scores (identical={same}, unrelated={different})")
    elif not implementation.startswith("rapidfuzz.fuzz_cpp"):
        problems.append(
            f"rapidfuzz is running its pure-Python fallback ({implementation or 'unknown'}): "
            "the C extension (.pyd) is not in the build")
    else:
        out(f"  ok    rapidfuzz matcher loaded from {implementation} "
            f"(identical={same:.0f}, unrelated={different:.0f})")

    samples = (
        (models.Language.ENGLISH,
         "Which Article of the Constitution guarantees the right to constitutional remedies?",
         ("Article 32", "Article 226", "Article 14", "Article 21"),
         "Article 32 gives the Supreme Court the power to issue writs."),
        (models.Language.HINDI,
         "भारत की सबसे लंबी नदी कौन-सी है?",
         ("गंगा", "गोदावरी", "कृष्णा", "यमुना"),
         "सही उत्तर गंगा है; इसकी लंबाई लगभग 2525 किलोमीटर है।"),
    )
    for language, prompt, options, solution in samples:
        localized = models.LocalizedQuestion(
            language=language, prompt=prompt, options=options,
            correct_option_indexes=(0,), solution=solution)
        question = models.QuestionRecord(
            question_id="SELFTEST-1", prompt=prompt, options=options,
            difficulty=models.Difficulty.EASY,
            tags=(models.QuestionTag("S-1", "C-1", "T-1"),),
            source_tags=("DPP",), content_fingerprint="selftest",
            verified=True, localized_content=(localized,))
        slot = models.SelectionSlot(
            slot_id="slot-1", difficulty=question.difficulty,
            question_id=question.question_id, preferred_row_id="ROW-1")
        try:
            export = pdf_export.export_practice_pdfs(
                (slot,), (question,), language, dpp_no="00")
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            problems.append(
                f"{language.value} DPP export failed: {type(exc).__name__}: {exc}")
            continue
        sizes = []
        for label, payload in (("questions", export.question_pdf),
                               ("solutions", export.solutions_pdf),
                               ("combined", export.combined_pdf)):
            if not payload.startswith(b"%PDF"):
                problems.append(f"{language.value} {label} PDF is not a PDF")
            sizes.append(f"{label} {len(payload)} B")
        out(f"  ok    {language.value} DPP export: " + ", ".join(sizes))

    # Verify layout in the frozen app, not just valid PDF bytes. Old EXEs had
    # compiled modules that flattened tables even when newer source tests passed.
    try:
        import pymupdf

        markup = (
            "<p>Consider the following pairs:</p><table>"
            "<tr><td>SRNO</td><td>INSTITUTION</td><td>OBJECTIVE</td></tr>"
            "<tr><td>1</td><td>IFC</td><td>Investment</td></tr></table>"
            "<p>Which pair is correctly matched?</p>"
        )
        localized = models.LocalizedQuestion(
            language=models.Language.ENGLISH, prompt="Consider the following pairs",
            prompt_markup=markup, options=("1 only", "None"),
            correct_option_indexes=(0,), solution="A table layout probe.")
        question = models.QuestionRecord(
            question_id="SELFTEST-TABLE", prompt=localized.prompt,
            options=localized.options, difficulty=models.Difficulty.EASY,
            tags=(), source_tags=("DPP",), content_fingerprint="selftest-table",
            verified=True, localized_content=(localized,))
        slot = models.SelectionSlot(
            slot_id="slot-table", difficulty=question.difficulty,
            question_id=question.question_id, preferred_row_id="ROW-1")
        exported = pdf_export.export_practice_pdfs((slot,), (question,), models.Language.ENGLISH)
        for payload in (exported.question_pdf, exported.combined_pdf):
            with pymupdf.open(stream=payload, filetype="pdf") as pdf:
                words = {word[4]: word[:4] for page in pdf for word in page.get_text("words")}
                positions = [words[name] for name in ("SRNO", "INSTITUTION", "OBJECTIVE")]
                if not (positions[0][0] < positions[1][0] < positions[2][0]
                        and max(p[1] for p in positions) - min(p[1] for p in positions) < 4):
                    raise ValueError("table columns were flattened or misaligned")
        out("  ok    DPP table layout preserved in question and combined PDFs")
    except Exception as exc:  # noqa: BLE001 — build gate reports all failures
        problems.append(f"DPP table layout failed: {type(exc).__name__}: {exc}")

    # The upload path: a text PDF with one blank page (the cover/diagram case
    # that used to raise a raw ModuleNotFoundError on the frozen build).
    try:
        import pymupdf

        from .notes_to_dpp.notes import extract_notes

        document = pymupdf.open()
        document.new_page().insert_text(
            (72, 72), "Fundamental Rights are enshrined in Part III of the Constitution. " * 3,
            fontsize=10)
        document.new_page()
        payload = document.tobytes()
        document.close()
        extracted = extract_notes(payload, "selftest.pdf", enable_ocr=True)
        methods = [page.extraction_method for page in extracted.pages]
        if methods[:1] != ["text"] or len(methods) != 2 or methods[1] not in {"empty", "ocr"}:
            problems.append(f"notes upload extraction gave unexpected pages: {methods}")
        else:
            note = " (blank page skipped: no OCR in this build)" if methods[1] == "empty" else ""
            out(f"  ok    notes upload extraction: pages {methods}{note}")
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        problems.append(f"notes upload extraction failed: {type(exc).__name__}: {exc}")

    try:
        font = pdfmetrics.getFont(pdf_export._FONT_REGULAR)
        shapable = bool(getattr(font, "shapable", False))
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        problems.append(
            f"Devanagari font is not registered: {type(exc).__name__}: {exc}")
        shapable = False
    if shapable:
        out("  ok    Devanagari shaping on (uharfbuzz present)")
    else:
        problems.append(
            "Devanagari shaping is OFF: uharfbuzz is missing from the build, so "
            "Hindi PDFs would render conjuncts as separate glyphs")
    problems.extend(docx_export_probe(out))
    return problems


def docx_export_probe(out: Report) -> list[str]:
    """Exercise the notes/DMP renderer, including its bundled handwritten fonts."""
    import tempfile

    problems: list[str] = []
    try:
        import pymupdf
        from reportlab.pdfbase import pdfmetrics

        from .docx_pdf import _KALAM, render_docx
        from .dmp.export import build_answer_document
        from .dmp.models import DmpSession, Question, STATUS_ANSWERED
        from .upsc_dcn_docx_writer import write_upsc_dcn_docx

        with tempfile.TemporaryDirectory(prefix="upsc-selftest-pdf-") as workspace:
            root = Path(workspace)
            for code in ("en", "hi", "mr"):
                title = "Parliament" if code == "en" else "संसद"
                body = "A complete legislative review." if code == "en" else "संसद जनता का प्रतिनिधित्व करती है।"
                source, _ = write_upsc_dcn_docx(
                    f"#LECTURE_TITLE: {title}\n#H1: Review\n- {body}\n- FINAL POINT\n",
                    root / f"notes_{code}.docx", language_code=code)
                pdf = root / f"notes_{code}.pdf"
                render_docx(source, pdf)
                with pymupdf.open(pdf) as document:
                    text = " ".join(p.get_text() for p in document)
                    if "FINAL POINT" not in text or "Space for Notes" not in text:
                        problems.append(f"Standalone notes PDF ({code}) lost content or notes furniture")
                    else:
                        out(f"  ok    standalone notes PDF ({code}): {len(document)} page(s), no Office")
            if not getattr(pdfmetrics.getFont(_KALAM), "shapable", False):
                problems.append("Kalam shaping is OFF in standalone notes PDFs")
            session = DmpSession("probe", "probe", subject_line="Polity", language="hi")
            session.set_level("easy", [Question(
                "q1", "easy", "संसद की भूमिका स्पष्ट करें।", status=STATUS_ANSWERED,
                answer="# APPROACH\n- Introduction: भूमिका स्पष्ट करें।\n# ANSWER\nFINAL ANSWER: संसद जनता का प्रतिनिधित्व करती है।")])
            document, _ = build_answer_document(session, dmp_no="01")
            source, pdf = root / "dmp.docx", root / "dmp.pdf"
            document.save(source)
            render_docx(source, pdf)
            with pymupdf.open(pdf) as document:
                if "FINAL ANSWER" not in " ".join(p.get_text() for p in document):
                    problems.append("Standalone DMP PDF lost its answer")
                else:
                    out(f"  ok    standalone Hindi DMP PDF: {len(document)} page(s), no Office")
    except Exception as exc:
        problems.append(f"Standalone DOCX/PDF export failed: {type(exc).__name__}: {exc}")
    return problems


def run(
    bundle_dir: Path | str,
    *,
    package: str = "src",
    out: Report = print,
    probe: Callable[[Report], list[str]] | None = dpp_export_probe,
    lazy_imports: Iterable[str] | None = None,
) -> int:
    """Import everything under ``package`` and every lazy dependency, run
    ``probe``, report; 0 = pass. ``lazy_imports`` defaults to
    REQUIRED_LAZY_IMPORTS (read at call time so tests can substitute)."""
    if lazy_imports is None:
        lazy_imports = REQUIRED_LAZY_IMPORTS
    lazy_imports = tuple(lazy_imports)
    bundle_dir = Path(bundle_dir)
    frozen = bool(getattr(sys, "frozen", False))
    out(f"Self-test of {bundle_dir}")
    out(f"Python {sys.version.split()[0]}, {'frozen build' if frozen else 'checkout'}")

    names = module_names(bundle_dir, package)
    out(f"Importing {len(names)} modules under {package}/ ...")
    problems = [f"{name}: {error}" for name, error in import_all(names, out)]
    if not names:
        problems.append(f"no modules found under {bundle_dir / package}")

    if lazy_imports:
        out(f"Importing {len(lazy_imports)} packages {package}/ defers into functions ...")
        problems.extend(f"{name}: {error}" for name, error in import_all(lazy_imports, out))

    if probe is not None:
        out("DPP export probe ...")
        problems.extend(probe(out))

    for name, why in OPTIONAL_ENGINES:
        try:
            importlib.import_module(name)
            state = "present"
        except Exception:  # noqa: BLE001 — absence is the expected state
            state = "not bundled"
        out(f"  info  {name}: {state} ({why})")

    if problems:
        out(f"SELF-TEST FAILED: {len(problems)} problem(s)")
        for problem in problems:
            out(f"  - {problem}")
        return 1
    out(f"SELF-TEST PASSED: {len(names)} modules and {len(lazy_imports)} deferred "
        "packages import"
        + ("; DPP export works in English and Hindi." if probe is not None else "."))
    return 0
