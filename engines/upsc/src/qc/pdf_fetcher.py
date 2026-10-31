"""Fetch an official PDF and extract its text for claim verification.

Reuses page_fetcher.fetch_raw so PDFs get the exact same redirect/domain/size
discipline as HTML pages — one downloader, one security posture.

PDF-specific decisions and why:

- The body magic (%PDF-) is accepted as an alternative to the
  application/pdf content type because government servers routinely mislabel
  PDFs as application/octet-stream; the magic check is the more reliable of
  the two signals.
- fitz (PyMuPDF) is imported inside the extraction step so this module always
  imports; a machine without PyMuPDF gets error="pymupdf unavailable" and the
  verifier records SOURCE_UNAVAILABLE instead of the pipeline dying.
- A PDF averaging under 50 extractable characters per page is flagged
  scanned=True. The extracted (near-empty) pages are returned as-is: the
  fact-checker must see that there is no text rather than have content
  invented for it — a scanned source can support no claim.
- The raw file plus a JSON sidecar of extracted metadata are cached for
  7 days (vs 24h for pages) because official PDFs are static publications;
  re-downloading a 10 MB economic survey for every claim would be waste.
  Cache hits do not need fitz, so previously extracted PDFs keep working
  even if PyMuPDF disappears later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .page_fetcher import (
    DEFAULT_MAX_BYTES,
    cache_key,
    fetch_raw,
    is_fresh,
    read_cache_json,
    utc_now_iso,
    write_cache_json,
)

PDF_CACHE_FRESH_SECONDS = 7 * 24 * 60 * 60
SCANNED_CHARS_PER_PAGE = 50
_PDF_MAGIC = b"%PDF-"


@dataclass
class PdfFetchResult:
    """One PDF fetch + extraction, successful or not. error=None means the
    text_pages are usable (though possibly near-empty when scanned=True)."""

    url: str
    final_url: str
    status: int
    path: str | None = None
    page_count: int = 0
    text_pages: list[str] = field(default_factory=list)
    scanned: bool = False
    publication_date: str | None = None
    retrieved_at: str = ""
    from_cache: bool = False
    error: str | None = None


def _pdf_metadata_date(metadata: dict) -> str | None:
    """PDF dates look like D:20250405120000+05'30'; keep just YYYY-MM-DD.
    Best effort — a missing date is fine, a fabricated one is not."""
    for key in ("creationDate", "modDate"):
        raw = str(metadata.get(key) or "")
        m = re.search(r"D:(\d{4})(\d{2})?(\d{2})?", raw)
        if m:
            return f"{m.group(1)}-{m.group(2) or '01'}-{m.group(3) or '01'}"
    return None


def _extract_pdf(data: bytes) -> tuple[list[str], int, str | None, str | None]:
    """(text_pages, page_count, publication_date, error). Never raises."""
    try:
        import fitz  # deferred: module must import even without PyMuPDF
    except Exception:  # noqa: BLE001 — any import failure means 'unavailable'
        return [], 0, None, "pymupdf unavailable"
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001 — corrupt download, not a crash
        return [], 0, None, f"pdf could not be opened: {e.__class__.__name__}: {e}"
    try:
        pages: list[str] = []
        for page in doc:
            try:
                pages.append(page.get_text("text"))
            except Exception:  # noqa: BLE001 — one damaged page loses one page
                pages.append("")
        return pages, doc.page_count, _pdf_metadata_date(doc.metadata or {}), None
    finally:
        doc.close()


def fetch_pdf(
    url: str,
    approved_domains: list[str],
    *,
    timeout: int = 20,
    max_bytes: int = DEFAULT_MAX_BYTES,
    cache_dir=None,
    refresh: bool = False,
    session=None,
) -> PdfFetchResult:
    """Fetch one approved official PDF. Never raises; failures fill .error."""
    retrieved_at = utc_now_iso()
    key = cache_key(url)

    pdf_path: Path | None = None
    meta_path: Path | None = None
    if cache_dir is not None:
        pdf_path = Path(cache_dir) / (key + ".pdf")
        meta_path = Path(cache_dir) / (key + ".pdf.json")

    if pdf_path is not None and meta_path is not None and not refresh:
        meta = read_cache_json(meta_path)
        if (
            meta is not None
            and is_fresh(meta.get("retrieved_at"), PDF_CACHE_FRESH_SECONDS)
            and pdf_path.exists()
        ):
            return PdfFetchResult(
                url=url,
                final_url=str(meta.get("final_url") or url),
                status=200,  # only successful fetches get a sidecar
                path=str(pdf_path),
                page_count=int(meta.get("page_count") or 0),
                text_pages=[str(p) for p in (meta.get("text_pages") or [])],
                scanned=bool(meta.get("scanned")),
                publication_date=meta.get("publication_date"),
                retrieved_at=str(meta.get("retrieved_at") or ""),
                from_cache=True,
            )

    raw = fetch_raw(url, approved_domains, timeout=timeout, max_bytes=max_bytes, session=session)
    if raw.error:
        return PdfFetchResult(
            url=url, final_url=raw.final_url, status=raw.status,
            retrieved_at=retrieved_at, error=raw.error,
        )

    base_type = raw.content_type.split(";", 1)[0].strip().lower()
    if base_type != "application/pdf" and not raw.body.startswith(_PDF_MAGIC):
        return PdfFetchResult(
            url=url, final_url=raw.final_url, status=raw.status,
            retrieved_at=retrieved_at,
            error=(
                f"not a pdf: content type {base_type or '(none)'!r} "
                "and body does not start with %PDF-"
            ),
        )

    # Cache the raw bytes even before extraction: a later run with PyMuPDF
    # installed can re-extract without re-downloading. Best effort only.
    path_str: str | None = None
    if pdf_path is not None:
        try:
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(raw.body)
            path_str = str(pdf_path)
        except OSError:
            path_str = None

    text_pages, page_count, publication_date, extract_error = _extract_pdf(raw.body)
    if extract_error:
        return PdfFetchResult(
            url=url, final_url=raw.final_url, status=raw.status, path=path_str,
            retrieved_at=retrieved_at, error=extract_error,
        )

    total_chars = sum(len(p.strip()) for p in text_pages)
    scanned = (total_chars / page_count if page_count else 0.0) < SCANNED_CHARS_PER_PAGE

    result = PdfFetchResult(
        url=url,
        final_url=raw.final_url,
        status=raw.status,
        path=path_str,
        page_count=page_count,
        text_pages=text_pages,
        scanned=scanned,
        publication_date=publication_date,
        retrieved_at=retrieved_at,
    )

    # Sidecar only after successful extraction: an error sidecar would keep
    # serving the failure from cache even after the cause is fixed.
    if meta_path is not None and path_str is not None:
        write_cache_json(meta_path, {
            "url": result.url,
            "final_url": result.final_url,
            "page_count": result.page_count,
            "text_pages": result.text_pages,
            "scanned": result.scanned,
            "publication_date": result.publication_date,
            "retrieved_at": result.retrieved_at,
        })
    return result
