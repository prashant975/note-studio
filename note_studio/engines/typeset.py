"""B.Sc. lectures are read and checked page by page with AI by default.
The fixed reference template retains source wording and order; the ordered
source map and rendered wording are verified before delivery.

The optional AI rewrite mode retains the original pipeline:
PW AI writes the notes -> they are tidied and checked against the lecture ->
labelled figures are drawn and their spelling checked -> the PDF is drawn in
the category's layout, with a quality report.

    1 notes        the lecture PDF and the engine's prompt + the category's rules  (stage "notes")
    2 review       the lecture PDF and the notes: corrections by JSON pointer       (stage "qc", optional)
    3 figures      a picture per figure block, then a label check, once redrawn     (stages "figures", "qc")
    4 PDF          render.render_pdf, then quality.report

A figure that cannot be drawn is left out (the notes stand without it); the
run ends Needs attention when the report asks a person to look."""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ENGINE = "typeset"
MAX_INPUT_BYTES = 100 * 1024 * 1024
FAILURE_WORDS = {
    "source": "The lecture PDF could not be opened.",
    "notes": "The notes could not be written for this lecture.",
    "render": "The notes were written but the PDF could not be drawn.",
}
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


@dataclass
class Settings:
    content_mode: str = "reference_ai"
    transcribe_scans: bool = False
    category: str = "bsc"
    subject: str = "auto"
    language: str = "english"
    title: str = ""
    figures: int = 6
    review: bool = True
    full_quality: bool = True
    instructions: str = ""
    brand: str = "B.Sc. Wallah"
    audience: str = "B.Sc."
    branding: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)


@dataclass
class Result:
    state: str                      # complete | failed
    output_path: str = ""
    filename: str = ""
    pages: int = 0
    cost_inr: float = 0.0
    calls: int = 0
    failure: str = ""               # the stage that failed, or sign_in / model_budget_exceeded
    message: str = ""
    detail: str = ""
    quality_report: dict = field(default_factory=dict)


def run_settings(options: dict, pack: dict, subject: str, language: str, instructions: str,
                 models: dict) -> Settings:
    """Settings from a run's saved options and its category pack (its branding is data)."""
    from .. import categories

    branding = dict((pack or {}).get("branding") or {})
    label = str((pack or {}).get("label") or "B.Sc.")
    brand = str(branding.get("header_left") or label)
    try:
        figures = max(0, min(8, int(str(options.get("figures", "6")).strip() or 0)))
    except ValueError:
        figures = 6
    return Settings(content_mode="rewrite" if options.get("content_mode") == "rewrite" else "reference_ai",
                    transcribe_scans=True,
                    category=str((pack or {}).get("id") or ""), subject=subject or "auto",
                    language=language or "english", title=" ".join(str(options.get("lecture_title") or "").split()),
                    figures=figures, review=bool(options.get("review", True)), instructions=instructions,
                    brand=brand, audience=str(branding.get("audience") or label), models=models,
                    branding={"header_left": brand, "header_right": str(branding.get("header_right") or f"{label} Notes"),
                              "footer_left": str(branding.get("footer_left") or brand),
                              "footer_right": str(branding.get("footer_right") or "Concept • Understand • Apply"),
                              "pw_logo": categories.pw_logo(pack)})


def output_name(source_name: str) -> str:
    stem = re.sub(r"\.(?:pdf|docx)$", "", " ".join(_UNSAFE.sub("", str(source_name or "")).split()), flags=re.I).strip(". ")
    return f"{(stem or 'Lecture')[:90]} Notes.pdf"


def _pages(data: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(data)).pages)


def _review(session, settings: Settings, pdf_bytes: bytes, doc: dict) -> dict:
    from ..typeset import ai, document, prompts

    outcome = {"asked": True, "done": False, "applied": [], "skipped": [], "missing": [], "problem": ""}
    lines = "\n".join(f"{pointer}\t{text}" for pointer, text in document.texts(doc))
    try:
        found = session.json("review", settings.models.get("qc") or settings.models.get("notes") or [],
                             prompts.review(settings.brand), [ai.pdf_part(pdf_bytes), {"text": lines}],
                             prompts.review_schema(), max_tokens=20000)
    except ai.AIError as problem:           # the notes are made; they go out unchecked, flagged
        outcome["problem"] = f"The check against the lecture could not be made: {problem}"
        return outcome
    for fix in found.get("corrections") or []:
        if not isinstance(fix, dict):
            continue
        entry = {k: str(fix.get(k) or "") for k in ("pointer", "old", "new", "reason")}
        if entry["new"] and entry["new"] != entry["old"] and document.replace(doc, entry["pointer"], entry["old"],
                                                                             entry["new"]):
            outcome["applied"].append(entry)
        else:
            outcome["skipped"].append(entry)
    outcome["missing"] = [{"topic": document.clean(m.get("topic")), "page": m.get("page")}
                          for m in found.get("missing") or [] if isinstance(m, dict) and document.clean(m.get("topic"))]
    outcome["done"] = True
    return outcome


def _picture(data: bytes) -> bytes | None:
    """A drawn picture made printable: RGB on white, at most 2000 px wide, as JPEG; None if blank or broken."""
    from PIL import Image, ImageStat

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.width < 200 or image.height < 120:
                return None
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")
                flat = Image.new("RGB", image.size, "white")
                flat.paste(image, mask=image.split()[-1])
                image = flat
            image = image.convert("RGB")
            if max(ImageStat.Stat(image.convert("L")).stddev) < 4:          # one flat colour: nothing drawn
                return None
            if image.width > 2000:
                image = image.resize((2000, round(image.height * 2000 / image.width)))
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=90, optimize=True)
            return out.getvalue()
    except Exception:
        return None


def _figures(session, settings: Settings, doc: dict, folder: Path, say) -> list[dict]:
    """Draw each figure block; keep the better of two tries; leave out what cannot be drawn."""
    from ..typeset import ai, document, prompts

    wanted = document.figures(doc)
    extra = wanted[settings.figures:]
    wanted = wanted[:settings.figures]
    doc["blocks"] = [b for b in doc["blocks"] if not any(b is e for e in extra)]
    outcomes = []
    stopped = ""                            # a spent budget or a lost sign-in: no more pictures this run
    for n, block in enumerate(wanted, start=1):
        best, best_score, best_issues, problems = None, None, [], []
        if stopped:
            best_issues = [stopped]
        else:
            say(f"Drawing figure {n} of {len(wanted)}: {block['title'][:60]}",
                55 + round(25 * (n - 1) / max(1, len(wanted))))
        for attempt in range(0 if stopped else 2):
            try:
                drawn = _picture(session.image(settings.models.get("figures") or [],
                                               prompts.figure(block["description"], block["labels"], problems)))
            except ai.AIError as problem:
                if problem.code in ("budget", "sign_in"):
                    stopped = str(problem)
                    best_issues = [stopped]
                    break
                best_issues = best_issues or [f"PW AI could not draw it ({problem})"]
                continue
            if drawn is None:
                best_issues = best_issues or ["the picture came back empty"]
                continue
            score, issues = _label_check(session, settings, drawn, block["labels"])
            if best is None or score < best_score:
                best, best_score, best_issues = drawn, score, issues
            if score == 0:
                break
            problems = issues
        entry = {"caption": block["title"], "labels": block["labels"], "source_page": block.get("source_page")}
        if best is None or best_score >= 100:
            doc["blocks"] = [b for b in doc["blocks"] if b is not block]
            outcomes.append({**entry, "result": "warn", "issues": ["left out: " + "; ".join(best_issues or ["not drawn"])]})
            continue
        (folder / "figures").mkdir(parents=True, exist_ok=True)
        name = f"figure-{n:02d}.jpg"
        (folder / "figures" / name).write_bytes(best)
        block["image"] = name
        outcomes.append({**entry, "image": f"figures/{name}", "result": "ok" if best_score == 0 else "attention",
                         "issues": best_issues})
    return outcomes


def _label_check(session, settings: Settings, picture: bytes, labels: list[str]) -> tuple[int, list[str]]:
    """(problems score, plain words); 100 or more: not printable. Unchecked pictures are kept, flagged."""
    from ..typeset import ai, prompts

    try:
        found = session.json("label check", settings.models.get("qc") or settings.models.get("notes") or [],
                             prompts.label_check(labels), [ai.image_part(picture), {"text": "Check this picture."}],
                             prompts.label_schema(), max_tokens=4000)
    except ai.AIError:
        return 1, ["its labels could not be checked; look at them"]
    issues, score = [], 0
    for wrong in found.get("misspelled") or []:
        if isinstance(wrong, dict) and wrong.get("found"):
            issues.append(f'"{wrong.get("found")}" should read "{wrong.get("expected", "")}"')
            score += 2
    for text in found.get("extra") or []:
        if str(text).strip():
            issues.append(f'extra text "{str(text).strip()[:40]}"')
            score += 1
    missing = [str(m) for m in found.get("missing") or [] if str(m).strip()]
    if len(missing) > 2:
        issues.append("labels missing: " + ", ".join(missing))
        score += len(missing) - 2
    if found.get("usable") is False:
        issues.append("PW AI judged the drawing not fit to print")
        score += 100
    return score, issues


def _make_word_notes(data: bytes, source_name: str, out_dir: Path, settings: Settings, say, token_provider) -> Result:
    from ..typeset import ai, render, source, source_quality, word_source

    result = Result(state="failed", failure="source", message=FAILURE_WORDS["source"])
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    session = None
    def ensure_session():
        nonlocal session
        if session is None:
            from .. import engine_catalog

            engine_catalog.put_shortnotes_on_path()
            import pw_access

            session = ai.Session(token_provider(), pw_access.new_task_id("studio-typeset"), source_name[:120],
                                 1, log=lambda line: say(line, None))
        return session
    try:
        if not data or len(data) > MAX_INPUT_BYTES:
            raise source.SourceError("Upload a Word reference smaller than 100 MB.")
        say("Reading every Word paragraph, table and embedded image", 10)
        assets = out_dir / "source_excerpts"
        doc, manifest = word_source.extract(data, assets)
        reused_formatting = (out_dir / "word_styles.json").is_file()
        if not reused_formatting:
            ensure_session()
        word_source.style(doc, manifest, out_dir, session, settings.models.get("notes") or [], say)
        (out_dir / "source_map.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        say("Drawing all reference content in the B.Sc. template", 65)
        data_pdf, layout = render.render_pdf(doc, render.Branding(**settings.branding), assets)
        problems = source.verify(doc, manifest, assets, rendered=data_pdf, emitted=layout["source_emitted"])
        if problems:
            raise source.SourceError("The PDF was not delivered because source content could not be verified. " + " ".join(problems[:3]))
        counts = manifest["counts"]
        checks = [{"check": "Source content and order", "result": "ok", "detail":
                   f"All {counts['paragraphs']} paragraphs, {counts['tables']} tables and {counts['images']} images retained in source order. "
                   "AI selected formatting only; source wording and original images were locked."}]
        for issue in layout["issues"]:
            checks.append({"check": "Layout", "result": "attention", "detail": issue})
        if any(fill > 1.002 for fill in layout["fills"]):
            checks.append({"check": "Page margins", "result": "attention", "detail": "A page runs past its margin."})
        final = source_quality.review(data_pdf, doc, manifest, out_dir, ensure_session,
                                      settings.models.get("qc") or settings.models.get("notes") or [], say) if settings.full_quality else {}
        if final:
            checks.append({"check": "NEET/JEE final quality checks", "result": "ok" if final["verdict"] == "passed" else "attention",
                           "detail": f"Inspected {len(final['reviewed_pages'])} of {layout['pages']} final pages. " +
                           (final["problem"] or f"{len(final['blocking_issues'])} blocking and {len(final['advisory_issues'])} advisory issue(s).")})
        report = {"verdict": "needs_attention" if any(c["result"] == "attention" for c in checks) else "passed",
                  "engine": ENGINE, "revision": word_source.REVISION, "content_mode": "locked_word_ai_style",
                  "source_format": "docx", "source_sha256": manifest["source_sha256"], "source_counts": counts,
                  "source_items": len(manifest["units"]), "checks": checks, "layout": layout,
                  "cost_inr": session.cost if session else 0, "calls": session.calls if session else [],
                  "reused_formatting": reused_formatting}
        report["final_quality"] = final
        target = out_dir / output_name(source_name)
        target.write_bytes(data_pdf)
        (out_dir / "notes.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return Result(state="complete", output_path=str(target), filename=target.name, pages=layout["pages"],
                      cost_inr=session.cost if session else 0, calls=len(session.calls) if session else 0, quality_report=report)
    except Exception as failure:
        if isinstance(failure, (source.SourceError, ai.AIError)):
            result.message = str(failure)
        code = getattr(failure, "code", "")
        if type(failure).__name__ == "SignInNeeded" or code == "sign_in":
            result.failure = "sign_in"
        elif code == "budget":
            result.failure = "model_budget_exceeded"
        result.cost_inr = session.cost if session else 0
        result.calls = len(session.calls) if session else 0
        result.detail = type(failure).__name__
        return result


def _make_source_notes(pdf_bytes: bytes, source_name: str, out_dir: Path, settings: Settings, say, token_provider) -> Result:
    from ..typeset import ai, render, source, source_quality, transcribe

    result = Result(state="failed", failure="source", message=FAILURE_WORDS["source"])
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    session = None
    try:
        if not pdf_bytes or len(pdf_bytes) > MAX_INPUT_BYTES or not pdf_bytes.lstrip().startswith(b"%PDF"):
            raise source.SourceError("Upload a lecture PDF smaller than 100 MB.")
        say("Preserving the lecture wording and source order", 10)
        assets = out_dir / "source_excerpts"
        transcripts = {}
        reused = set()

        def read_pages(numbers):
            nonlocal session
            cached = transcribe.load_cached(pdf_bytes, numbers, out_dir / "transcription")
            transcripts.update(cached)
            reused.update(cached)
            missing = [n for n in numbers if n not in cached]
            if not missing:
                return
            if not settings.transcribe_scans and settings.content_mode != "reference_ai":
                raise source.LayoutRequired(numbers[0])
            if session is None:
                from .. import engine_catalog

                engine_catalog.put_shortnotes_on_path()
                import pw_access

                session = ai.Session(token_provider(), pw_access.new_task_id("studio-typeset"), source_name[:120],
                                     _pages(pdf_bytes), log=lambda line: say(line, None))
            transcripts.update(transcribe.pages(pdf_bytes, missing, out_dir / "transcription", session,
                                               settings.models.get("notes") or [], say))

        scanned = source.scanned_pages(pdf_bytes)  # also rejects passwords/printed LaTeX before billing
        pending = list(range(_pages(pdf_bytes))) if settings.content_mode == "reference_ai" else scanned
        if pending:
            read_pages(pending)
        while True:
            try:
                doc, manifest = source.extract(pdf_bytes, assets, title=settings.title, transcriptions=transcripts)
                break
            except source.LayoutRequired as problem:
                if problem.page in transcripts:
                    raise
                read_pages([problem.page])
        if not doc["blocks"]:
            raise source.SourceError("The uploaded PDF has no academic content to typeset.")
        (out_dir / "source_map.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        problems = source.verify(doc, manifest, assets)
        if problems:
            raise source.SourceError(" ".join(problems))
        result.failure, result.message = "render", FAILURE_WORDS["render"]
        say("Drawing the source content in the B.Sc. layout", 65)
        data, layout = render.render_pdf(doc, render.Branding(**settings.branding), assets)
        say("Checking every source item and the rendered wording", 90)
        problems = source.verify(doc, manifest, assets, rendered=data, emitted=layout["source_emitted"])
        if problems:
            raise source.SourceError("The PDF was not delivered because source content could not be verified. " + " ".join(problems[:3]))
        checks = [{"check": "Source content and order", "result": "ok",
                   "detail": f"All {len(manifest['units'])} source items retained and drawn in order; rendered wording matches " +
                   ("the reviewed transcription." if transcripts else "the source PDF text.")},
                  {"check": "Lecture content", "result": "ok", "detail": "Content is read and checked page by page without paraphrasing. " +
                   ("Saved AI page responses reused where available." if transcripts and not session else "")}]
        final = {}
        if settings.full_quality and settings.content_mode == "reference_ai":
            def ensure_quality_session():
                nonlocal session
                if session is None:
                    from .. import engine_catalog

                    engine_catalog.put_shortnotes_on_path()
                    import pw_access

                    session = ai.Session(token_provider(), pw_access.new_task_id("studio-typeset"), source_name[:120],
                                         _pages(pdf_bytes), log=lambda line: say(line, None))
                return session
            final = source_quality.review(data, doc, manifest, out_dir, ensure_quality_session,
                                          settings.models.get("qc") or settings.models.get("notes") or [], say)
            checks.append({"check": "NEET/JEE final quality checks", "result": "ok" if final["verdict"] == "passed" else "attention",
                           "detail": f"Inspected {len(final['reviewed_pages'])} of {layout['pages']} final pages. " +
                           (final["problem"] or f"{len(final['blocking_issues'])} blocking and {len(final['advisory_issues'])} advisory issue(s).")})
        for detail in manifest["attention"] + layout["issues"]:
            checks.append({"check": "Source layout", "result": "attention", "detail": detail})
        if any(f > 1.002 for f in layout["fills"]):
            checks.append({"check": "Page margins", "result": "attention", "detail": "A page runs past its margin."})
        report = {"verdict": "needs_attention" if any(c["result"] == "attention" for c in checks) else "passed",
                  "engine": ENGINE, "revision": source.REVISION, "content_mode": settings.content_mode, "language": "source",
                  "checks": checks, "source_pages": manifest["source_pages"], "source_sha256": manifest["source_sha256"],
                  "source_items": len(manifest["units"]), "layout": layout,
                  "transcription_pages": manifest.get("transcription_pages", []),
                  "reused_transcription_pages": sorted(n + 1 for n in reused),
                  "final_quality": final,
                  "cost_inr": session.cost if session else 0, "calls": session.calls if session else []}
        filename = output_name(source_name)
        target = out_dir / filename
        target.write_bytes(data)
        (out_dir / "notes.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return Result(state="complete", output_path=str(target), filename=filename, pages=layout["pages"],
                      cost_inr=session.cost if session else 0, calls=len(session.calls) if session else 0, quality_report=report)
    except Exception as failure:
        if isinstance(failure, source.SourceError):
            result.message = str(failure)
        elif isinstance(failure, ai.AIError):
            result.message = str(failure)
        code = getattr(failure, "code", "")
        if type(failure).__name__ == "SignInNeeded" or code == "sign_in":
            result.message, result.failure = str(failure), "sign_in"
        elif code == "budget":
            result.message, result.failure = str(failure), "model_budget_exceeded"
        result.cost_inr = session.cost if session else 0
        result.calls = len(session.calls) if session else 0
        result.detail = f"{type(failure).__module__}.{type(failure).__qualname__}"
        return result


def make_notes(pdf_bytes: bytes, source_name: str, out_dir: Path, settings: Settings, *,
               token_provider: Callable[[], Callable[[], str]],
               step: Callable[[str, int | None], None] | None = None) -> Result:
    """Make typeset notes for one lecture PDF into ``out_dir``."""
    say = step or (lambda text, pct=None: None)
    if source_name.lower().endswith(".docx"):
        return _make_word_notes(pdf_bytes, source_name, out_dir, settings, say, token_provider)
    if settings.content_mode != "rewrite":
        return _make_source_notes(pdf_bytes, source_name, out_dir, settings, say, token_provider)
    from .. import engine_catalog
    from ..typeset import ai, document, prompts, quality, render

    engine_catalog.put_shortnotes_on_path()                 # pw_access, as for the hand-drawn notes
    import pw_access

    say = step or (lambda text, pct=None: None)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = Result(state="failed")
    stage = "source"
    session = None
    try:
        if not pdf_bytes or len(pdf_bytes) > MAX_INPUT_BYTES or not pdf_bytes.lstrip()[:5].startswith(b"%PDF"):
            raise ValueError("not a PDF")
        pages = _pages(pdf_bytes)
        stage = "notes"
        provider = token_provider()
        session = ai.Session(provider, pw_access.new_task_id("studio-typeset"), source_name[:120], pages,
                             log=lambda line: say(line, None))
        say("Writing the notes", 10)
        system = prompts.notes(brand=settings.brand, audience=settings.audience, language=settings.language,
                               figures=settings.figures, rules=settings.instructions)
        raw = session.json("notes", settings.models.get("notes") or [], system,
                           [ai.pdf_part(pdf_bytes), {"text": prompts.request(source_name, settings.title)}],
                           document.schema())
        (out_dir / "notes_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        doc, tidied = document.tidy(raw, title=settings.title)
        if not doc["blocks"]:
            raise ai.AIError("the notes came back empty")
        review = {"asked": False, "done": False, "applied": [], "skipped": [], "missing": []}
        if settings.review:
            say("Checking the notes against the lecture", 40)
            review = _review(session, settings, pdf_bytes, doc)
            doc, more = document.tidy(doc, title=doc["title"])
            tidied += more
        figures = _figures(session, settings, doc, out_dir, say) if settings.figures else []
        for block in document.figures(doc):                  # no picture, no figure
            if not block.get("image"):
                doc["blocks"].remove(block)
        stage = "render"
        say("Drawing the notes PDF", 85)
        data, layout = render.render_pdf(doc, render.Branding(**settings.branding), out_dir / "figures")
        if _pages(data) < 1:
            raise ValueError("empty PDF")
        filename = output_name(source_name)
        target = out_dir / filename
        target.write_bytes(data)
        (out_dir / "notes.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        report = quality.report(doc=doc, tidy=tidied, review=review, figures=figures, layout=layout,
                                language=settings.language)
        report["cost_inr"] = session.cost
        report["calls"] = session.calls
        result = Result(state="complete", output_path=str(target), filename=filename, pages=layout["pages"],
                        cost_inr=session.cost, calls=len(session.calls), quality_report=report)
    except Exception as failure:                            # noqa: BLE001 - every failure gets plain words
        result.failure = stage
        result.message = FAILURE_WORDS[stage]
        result.detail = f"{type(failure).__module__}.{type(failure).__qualname__}"
        code = getattr(failure, "code", "")
        if type(failure).__name__ == "SignInNeeded" or code == "sign_in":
            result.message, result.failure = str(failure), "sign_in"
        elif code == "budget":
            result.message, result.failure = str(failure), "model_budget_exceeded"
        result.cost_inr = session.cost if session else 0.0
        result.calls = len(session.calls) if session else 0
    return result
