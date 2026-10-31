"""Final DOCX/PDF format validation (spec §22).

Two tiers:
- run_local_output_checks(): always runs, no AI — file integrity, package
  structure, marker leaks. Deterministic and free.
- run_visual_format_check(): Strict level only — renders risk-selected PDF
  pages to PNG (fitz) and asks Gemini, through the pipeline's existing
  GeminiClient (image support built in, and the call is tagged with the run's
  shared task_id so the proxy's usage row groups with the rest of the run), to spot
  rendering problems a text diff cannot see: cut-off text, table overflow,
  broken Devanagari, font substitution, blank pages, watermark obstruction.

Failures here never destroy outputs: the DOCX/PDF are kept and the check
reports a warning (spec §32).
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

from ..config import VISUAL_BATCH_PAGES, VISUAL_MAX_OUTPUT_TOKENS

# Substrings that must never appear in student-facing output. Checked against
# the DOCX's own text, not the notes string, so post-render leaks are caught.
_FORBIDDEN_MARKERS = (
    "NOTES_START", "NOTES_END", "REPORT_START", "REPORT_END",
    "[transcript unavailable", "(Note to DTP:",
    # Consumed by the DCN writer (rendered as an image); its literal text in
    # the document means a figure marker escaped the parser.
    "#FIGURE:",
)


def _docx_text(docx_path: Path) -> str:
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    return re.sub(r"<[^>]+>", "", xml)


def run_local_output_checks(
    docx_path: Path | None,
    pdf_path: Path | None,
    *,
    dtp_note_policy: str = "hide_note_insert_image",
) -> dict:
    """Deterministic output integrity. Returns {"status", "problems", "checked"}."""
    problems: list[str] = []
    checked: list[str] = []

    if docx_path is None or not Path(docx_path).exists():
        problems.append("DOCX file was not created.")
    else:
        docx_path = Path(docx_path)
        checked.append("docx")
        if docx_path.stat().st_size == 0:
            problems.append("DOCX file is empty.")
        else:
            try:
                with zipfile.ZipFile(docx_path) as z:
                    names = set(z.namelist())
                    if "word/document.xml" not in names:
                        problems.append("DOCX package is missing word/document.xml.")
                    if not any(n.startswith("word/footer") for n in names):
                        problems.append("DOCX has no footer part (page numbers/footer missing).")
                text = _docx_text(docx_path)
                if len(text.strip()) < 100:
                    problems.append("DOCX text content is nearly empty.")
                for marker in _FORBIDDEN_MARKERS:
                    if marker == "(Note to DTP:" and dtp_note_policy == "keep_note_and_insert_image":
                        continue  # DTP notes are deliberately visible under this policy
                    if marker in text:
                        problems.append(f"Internal marker visible in the student DOCX: {marker!r}")
                if re.search(r"^\s*\|.*\|\s*$", text, re.M):
                    problems.append("Raw markdown table syntax visible in the DOCX.")
            except zipfile.BadZipFile:
                problems.append("DOCX does not open as a ZIP package (corrupt file).")
            except Exception as e:
                problems.append(f"DOCX inspection failed: {e}")

    if pdf_path is None or not Path(pdf_path).exists():
        problems.append("PDF was not created (DOCX remains available).")
    else:
        checked.append("pdf")
        try:
            import fitz

            with fitz.open(str(pdf_path)) as doc:
                if doc.page_count == 0:
                    problems.append("PDF has zero pages.")
                elif all(len(page.get_text().strip()) < 5 for page in doc):
                    problems.append("Every PDF page is blank.")
        except Exception as e:
            problems.append(f"PDF inspection failed: {e}")

    status = "completed" if not problems else "completed_with_warnings"
    return {"status": status, "problems": problems, "checked": checked}


def _select_risk_pages(pdf_path: Path, max_pages: int) -> list[int]:
    """Pages to send to the visual check. 0-based indexes, ordered.

    ``max_pages <= 0`` means EVERY page, which is the default: a sample can
    only ever answer "were the pages I looked at clean?", and that is not the
    question a QC tool is asked. When a positive budget is given, the choice is
    the first page, the last page, and then pages showing risk signals (tables,
    mixed scripts, very dense text near the page end).
    """
    import fitz

    picks: list[int] = []
    with fitz.open(str(pdf_path)) as doc:
        n = doc.page_count
        if n == 0:
            return []
        if max_pages <= 0:
            return list(range(n))
        picks.append(0)
        if n > 1:
            picks.append(n - 1)
        for i, page in enumerate(doc):
            if len(picks) >= max_pages:
                break
            if i in picks:
                continue
            text = page.get_text()
            has_devanagari = re.search(r"[ऀ-ॿ]", text) is not None
            has_latin = re.search(r"[A-Za-z]{4,}", text) is not None
            tableish = text.count("|") > 6 or len(re.findall(r" {3,}\S+ {3,}", text)) > 8
            if tableish or (has_devanagari and has_latin):
                picks.append(i)
    return sorted(picks[:max_pages])


def run_visual_format_check(
    client,
    pdf_path: Path,
    run_dir: Path,
    *,
    max_pages: int = 0,          # 0 = every page
    language_code: str = "en",
) -> dict:
    """Render the pages and ask Gemini to inspect them. Strict level only.

    ``max_pages=0`` (the default) checks EVERY page, in batches. Returns
    {"status", "pages_checked", "pages_failed", "problems", "error"}. Never
    raises — a failed visual check is reported, not fatal — and never reports a
    batch it could not read as a batch that was clean.
    """
    try:
        import fitz

        pages = _select_risk_pages(Path(pdf_path), max_pages)
        if not pages:
            return {"status": "failed", "pages_checked": [], "problems": [],
                    "pages_failed": [], "error": "no pages to render"}
        render_dir = Path(run_dir) / "format_check_pages"
        render_dir.mkdir(parents=True, exist_ok=True)
        rendered: list[tuple[int, Path]] = []
        with fitz.open(str(pdf_path)) as doc:
            for i in pages:
                pix = doc[i].get_pixmap(dpi=110)
                out = render_dir / f"page_{i + 1:03d}.png"
                pix.save(str(out))
                rendered.append((i, out))

        # Every page can now be checked, so the pages have to travel in batches:
        # one call carrying forty inline images would exceed the proxy's body
        # budget, and one 2,000-token reply could not describe forty pages of
        # problems even if it arrived.
        problems: list[dict] = []
        checked: list[int] = []
        failed: list[int] = []
        errors: list[str] = []
        batch = max(1, VISUAL_BATCH_PAGES)

        for start in range(0, len(rendered), batch):
            group = rendered[start:start + batch]
            group_pages = [i for i, _ in group]
            image_paths = [path for _, path in group]
            ok, found, error = _check_one_visual_batch(
                client, group_pages, image_paths, language_code)
            if ok:
                checked.extend(p + 1 for p in group_pages)
                problems.extend(found)
            else:
                failed.extend(p + 1 for p in group_pages)
                if error:
                    errors.append(error)

        if not checked:
            return {"status": "failed", "pages_checked": [], "problems": [],
                    "pages_failed": failed,
                    "error": "; ".join(errors[:2]) or "every visual batch failed"}
        return {
            "status": "completed" if not problems and not failed
                      else "completed_with_warnings",
            "pages_checked": checked,
            "pages_failed": failed,
            "problems": problems,
            "error": "; ".join(errors[:2]),
        }
    except Exception as e:
        return {"status": "failed", "pages_checked": [], "problems": [],
                "pages_failed": [], "error": str(e)[:300]}


def _check_one_visual_batch(client, page_indexes: list[int],
                            image_paths: list[Path],
                            language_code: str) -> tuple[bool, list[dict], str]:
    """One batch of rendered pages. Returns (ok, problems, error).

    ``ok`` is False when the model's answer could not be read. That is reported
    as a failure for these pages rather than as an empty problem list — a reply
    nobody could parse is not evidence the pages are clean, and the old code
    silently turned one into the other.
    """
    try:
        pages = page_indexes
        prompt = f"""These are rendered pages ({', '.join(str(p + 1) for p in pages)}) of a student-facing UPSC notes PDF ({'Hindi/Devanagari + English' if language_code == 'hi' else 'English'}). Inspect ONLY visual/formatting problems:
- text cut off at page edges; table columns overflowing or overlapping
- broken/garbled Devanagari (boxes, tofu, disconnected matras)
- obviously wrong font substitution (mechanical fallback where handwriting style expected)
- blank pages; images cropped mid-diagram; watermark obscuring text
- missing title block on the first page; missing footer/page number
- raw markdown symbols (|, **, #) visible as literal characters
- internal instructions visible to students (e.g. "Note to DTP")

Respond ONLY with JSON: {{"problems": [{{"page": <printed page number>, "problem": "<short description>"}}]}} — empty list when the pages are clean."""
        resp = client.generate(prompt, image_paths, max_images=len(image_paths),
                               max_output_tokens=VISUAL_MAX_OUTPUT_TOKENS,
                               temperature=0.0)
        if getattr(resp, "truncated", False):
            return False, [], "the visual check's reply was cut off at the output limit"
        text = resp.text.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            # No JSON object at all — a refusal, an apology, or prose. Not a
            # clean bill of health.
            return False, [], f"unreadable visual reply: {text[:120]!r}"
        try:
            data = json.loads(text[start:end + 1])
        except (ValueError, TypeError) as e:
            return False, [], f"visual reply was not valid JSON: {str(e)[:80]}"
        if not isinstance(data, dict):
            return False, [], "visual reply was not a JSON object"
        problems = [p for p in (data.get("problems") or [])
                    if isinstance(p, dict) and p.get("problem")]
        return True, problems, ""
    except Exception as e:
        return False, [], str(e)[:200]
