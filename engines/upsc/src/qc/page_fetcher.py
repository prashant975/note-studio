"""Fetch an approved HTML page for evidence, safely and reproducibly.

Everything downloaded here gets quoted verbatim into evidence logs, so the
fetcher is deliberately paranoid:

- redirects are followed manually (allow_redirects=False) because requests'
  automatic following would skip our checks; every hop is re-validated with
  url_safety AND the approved-domain list, so an approved page cannot bounce
  the fetcher to an unapproved host (open-redirect endpoints on government
  portals are common);
- the body is streamed and abandoned the moment it passes max_bytes, so a
  misconfigured or compromised source cannot balloon memory;
- downloaded bytes are only ever parsed as text — never executed, evaluated,
  or rendered.

The 24-hour cache exists so a QC run that verifies many claims against the
same press release touches the network once; the key is sha256(url) to keep
filenames Windows-safe regardless of what the URL contains. fetch_raw() is
shared with pdf_fetcher so the redirect/size discipline cannot drift between
the two fetchers.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

from .url_safety import is_approved_url, validate_url_for_fetch

USER_AGENT = "UPSC-Notes-QC/1.0 (+notes quality check)"
MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 15 * 1024 * 1024
PAGE_CACHE_FRESH_SECONDS = 24 * 60 * 60

#: How long a FAILED fetch is remembered. Only successes used to be cached, so
#: an official domain that was down got re-fetched once per claim — twenty
#: claims x a 20 s timeout, sequentially, is over six minutes of a Strict run
#: spent waiting for the same dead host.
#:
#: Deliberately much shorter than the success TTL: a site that is briefly down
#: should not be written off for a day. Ten minutes covers one QC run and
#: expires long before the next.
PAGE_CACHE_FAILURE_SECONDS = 10 * 60
_CHUNK_SIZE = 65536
_HTML_TYPES = {"text/html", "application/xhtml+xml", "application/xhtml"}

# Whole blocks whose contents are boilerplate or executable, removed outright.
# <head> is removed from the *text* only — title/date are extracted first.
_REMOVE_BLOCKS = ("script", "style", "noscript", "template", "nav",
                  "header", "footer", "form", "aside", "head")
_BLOCK_RE = re.compile(
    r"<(" + "|".join(_REMOVE_BLOCKS) + r")\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
# Tags that end a visual line: turned into newlines so heading/paragraph
# order survives into the plain text.
_BLOCK_BREAK_RE = re.compile(
    r"</?(p|div|br|hr|h[1-6]|li|ul|ol|tr|table|section|article|blockquote|dt|dd)\b[^>]*>",
    re.IGNORECASE,
)


@dataclass
class FetchResult:
    """One page fetch, successful or not. error=None means text is usable."""

    url: str
    final_url: str
    status: int
    content_type: str = ""
    title: str = ""
    text: str = ""
    publication_date: str | None = None
    retrieved_at: str = ""
    from_cache: bool = False
    error: str | None = None


@dataclass
class RawFetch:
    """Shared low-level download outcome (also used by pdf_fetcher)."""

    url: str
    final_url: str
    status: int
    content_type: str = ""
    body: bytes = b""
    error: str | None = None


# ---------------------------------------------------------------- utilities

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cache_key(url: str) -> str:
    """sha256 of the exact URL: collision-safe and filename-safe on Windows."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def is_fresh(retrieved_at: object, max_age_seconds: int) -> bool:
    """True when an ISO timestamp is within max_age_seconds of now.

    Unparseable timestamps count as stale — a corrupt cache entry must cause
    a refetch, not an exception."""
    try:
        ts = datetime.fromisoformat(str(retrieved_at))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() < max_age_seconds


def read_cache_json(path: Path) -> dict | None:
    """Best-effort read of a cache sidecar; any problem means 'no cache'."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_cache_json(path: Path, payload: dict) -> None:
    """Best-effort write; a full disk degrades to 'no cache', never a crash."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _peer_address_is_public(resp) -> tuple[bool, str]:
    """Re-check the address the connection actually reached.

    validate_url_for_fetch resolves the hostname itself, but requests resolves
    again when it connects, so the two can differ (DNS rebinding). Reading the
    live socket's peer address closes that check-then-connect window. A response
    whose socket cannot be inspected (mocked sessions, connection already
    recycled) is allowed through: the pre-fetch validation still applies, and
    failing closed here would break every offline test double.
    """
    from .url_safety import _is_public

    sock = None
    try:
        raw = getattr(resp, "raw", None)
        conn = getattr(raw, "_connection", None) or getattr(raw, "_fp", None)
        sock = getattr(conn, "sock", None) or getattr(
            getattr(conn, "connection", None), "sock", None
        )
        if sock is None:
            return True, "peer address not inspectable"
        peer = sock.getpeername()
    except Exception:  # noqa: BLE001 — inspection must never break a fetch
        return True, "peer address not inspectable"

    try:
        addr = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
    except (ValueError, IndexError, TypeError):
        return True, "peer address not parseable"
    if not _is_public(addr):
        return False, f"connection reached non-public address {addr}"
    return True, "ok"


# --------------------------------------------------------- shared downloader

def fetch_raw(
    url: str,
    approved_domains: list[str],
    *,
    timeout: int = 20,
    max_bytes: int = DEFAULT_MAX_BYTES,
    session=None,
) -> RawFetch:
    """Download url with manual redirect following, validating every hop.

    Both is_approved_url and validate_url_for_fetch run on each hop even
    though the latter subsumes the former — the double check is deliberate
    belt-and-braces for the one property that must never regress: no request
    ever leaves the approved-domain set.
    """
    owns_session = session is None
    if owns_session:
        import requests  # local import keeps module import dependency-light

        session = requests.Session()
    current = url
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            ok, reason = validate_url_for_fetch(current, approved_domains)
            if not ok:
                return RawFetch(url, current, 0, error=f"blocked: {reason}")
            if not is_approved_url(current, approved_domains):
                return RawFetch(url, current, 0, error="blocked: host is not on the approved domain list")

            resp = session.get(
                current,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            )
            try:
                # The pre-fetch DNS check and the connection requests actually
                # opened are two separate resolutions, so a TTL-0 record can
                # answer "public" to the check and "127.0.0.1"/169.254.169.254
                # to the connect. Judge the address really in use.
                peer_ok, peer_reason = _peer_address_is_public(resp)
                if not peer_ok:
                    return RawFetch(url, current, 0, error=f"blocked: {peer_reason}")

                status = int(getattr(resp, "status_code", 0) or 0)
                headers = getattr(resp, "headers", None) or {}
                if status in (301, 302, 303, 307, 308):
                    location = headers.get("Location") or headers.get("location")
                    if not location:
                        return RawFetch(url, current, status, error="redirect without Location header")
                    current = urljoin(current, str(location))
                    continue

                content_type = str(headers.get("Content-Type") or headers.get("content-type") or "")
                if status != 200:
                    return RawFetch(url, current, status, content_type, error=f"http status {status}")

                declared = str(headers.get("Content-Length") or "")
                if declared.isdigit() and int(declared) > max_bytes:
                    return RawFetch(
                        url, current, status, content_type,
                        error=f"content-length {declared} exceeds limit of {max_bytes} bytes",
                    )

                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        return RawFetch(
                            url, current, status, content_type,
                            error=f"body exceeded limit of {max_bytes} bytes; download aborted",
                        )
                    chunks.append(chunk)
                return RawFetch(url, current, status, content_type, b"".join(chunks))
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
        return RawFetch(url, current, 0, error=f"too many redirects (more than {MAX_REDIRECTS})")
    except Exception as e:  # noqa: BLE001 — a fetch failure must never crash QC
        return RawFetch(url, current, 0, error=f"fetch failed: {e.__class__.__name__}: {e}")
    finally:
        if owns_session:
            session.close()


# ------------------------------------------------------------ html handling

def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w\-]+)", content_type, re.IGNORECASE)
    if m:
        try:
            return body.decode(m.group(1), errors="replace")
        except LookupError:
            pass
    return body.decode("utf-8", errors="replace")


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title\s*>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return " ".join(unescape(_TAG_RE.sub(" ", m.group(1))).split())


def _extract_publication_date(html: str) -> str | None:
    """Best effort only: a wrong date is worse than none for the time-
    sensitivity checks, so only explicit machine-readable markers count."""
    for pat in (
        r"<meta\b[^>]*property=[\"']article:published_time[\"'][^>]*>",
        r"<meta\b[^>]*name=[\"']date[\"'][^>]*>",
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            c = re.search(r"content=[\"']([^\"']+)[\"']", m.group(0), re.IGNORECASE)
            if c:
                return c.group(1).strip()
    m = re.search(r"<time\b[^>]*datetime=[\"']([^\"']+)[\"']", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _html_to_text(html: str) -> str:
    """Crude but safe html-to-text: strip boilerplate blocks, keep document
    order, collapse whitespace. Never interprets the markup beyond regexes."""
    cleaned = _COMMENT_RE.sub(" ", html)
    for _ in range(10):  # bounded loop: nested removable blocks need re-passes
        stripped = _BLOCK_RE.sub(" ", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    cleaned = _BLOCK_BREAK_RE.sub("\n", cleaned)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = unescape(cleaned)
    lines = []
    for line in cleaned.split("\n"):
        collapsed = " ".join(line.split())
        if collapsed:
            lines.append(collapsed)
    return "\n".join(lines)


# ---------------------------------------------------------------- main entry

def fetch_page(
    url: str,
    approved_domains: list[str],
    *,
    timeout: int = 20,
    max_bytes: int = DEFAULT_MAX_BYTES,
    cache_dir=None,
    refresh: bool = False,
    session=None,
) -> FetchResult:
    """Fetch one approved HTML page. Never raises; failures fill .error."""
    retrieved_at = utc_now_iso()

    def _remember(result: FetchResult) -> FetchResult:
        """Record a failure so the next claim does not wait for it all over again."""
        if cache_dir is not None:
            write_cache_json(Path(cache_dir) / (cache_key(url) + ".json"), {
                "url": result.url,
                "final_url": result.final_url,
                "content_type": result.content_type,
                "status": result.status,
                "error": result.error,
                "retrieved_at": result.retrieved_at,
            })
        return result

    if cache_dir is not None and not refresh:
        cached = read_cache_json(Path(cache_dir) / (cache_key(url) + ".json"))
        if cached is not None:
            # An entry with an `error` is a remembered failure and expires much
            # sooner. Entries written before this existed have no `error` key
            # and are still read as successes.
            cached_error = str(cached.get("error") or "")
            ttl = PAGE_CACHE_FAILURE_SECONDS if cached_error else PAGE_CACHE_FRESH_SECONDS
            if is_fresh(cached.get("retrieved_at"), ttl):
                if cached_error:
                    return FetchResult(
                        url=url,
                        final_url=str(cached.get("final_url") or url),
                        status=int(cached.get("status") or 0),
                        content_type=str(cached.get("content_type") or ""),
                        retrieved_at=str(cached.get("retrieved_at") or ""),
                        error=cached_error,
                        from_cache=True,
                    )
                return FetchResult(
                    url=url,
                    final_url=str(cached.get("final_url") or url),
                    status=200,
                    content_type=str(cached.get("content_type") or ""),
                    title=str(cached.get("title") or ""),
                    text=str(cached.get("text") or ""),
                    publication_date=cached.get("publication_date"),
                    retrieved_at=str(cached.get("retrieved_at") or ""),
                    from_cache=True,
                )

    raw = fetch_raw(url, approved_domains, timeout=timeout, max_bytes=max_bytes, session=session)
    if raw.error:
        return _remember(FetchResult(
            url=url, final_url=raw.final_url, status=raw.status,
            content_type=raw.content_type, retrieved_at=retrieved_at, error=raw.error,
        ))

    base_type = raw.content_type.split(";", 1)[0].strip().lower()
    if base_type not in _HTML_TYPES:
        return _remember(FetchResult(
            url=url, final_url=raw.final_url, status=raw.status,
            content_type=raw.content_type, retrieved_at=retrieved_at,
            error=f"unsupported content type {base_type or '(none)'!r}: html only",
        ))

    html = _decode(raw.body, raw.content_type)
    result = FetchResult(
        url=url,
        final_url=raw.final_url,
        status=raw.status,
        content_type=raw.content_type,
        title=_extract_title(html),
        text=_html_to_text(html),
        publication_date=_extract_publication_date(html),
        retrieved_at=retrieved_at,
    )

    if cache_dir is not None:
        write_cache_json(Path(cache_dir) / (cache_key(url) + ".json"), {
            "url": result.url,
            "final_url": result.final_url,
            "text": result.text,
            "title": result.title,
            "content_type": result.content_type,
            "publication_date": result.publication_date,
            "retrieved_at": result.retrieved_at,
        })
    return result
