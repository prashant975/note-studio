"""Download a lecture PDF from a plain web link (static.pw.live, CloudFront ...).

A catalog sheet points straight at the PDF, not at a Drive folder. The file is
fetched with the same limits the Drive path applies - a size cap, a PDF
signature check, a bounded number of repeats on a dropped connection - and
identified by a digest of its URL, so one PDF listed under several batches is
generated once.
"""
from __future__ import annotations

import hashlib
import os
import socket
import ssl
import time
from urllib.parse import unquote, urlsplit

from .errors import BridgeError

_ATTEMPTS = 4
_CHUNK = 1024 * 1024
_TIMEOUT = (20, 120)            # connect, read (seconds)
_USER_AGENT = "shortnotes-sheets-bridge/1.0"


class _RetryableStatus(Exception):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(str(status))


def is_web_link(text: object) -> bool:
    """An http(s) URL with a host: anything the Drive parser does not take."""
    if not isinstance(text, str):
        return False
    try:
        parts = urlsplit(text.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def source_key(url: str) -> str:
    """Stable identity of one web PDF (cache key, dedupe, Drive appProperties)."""
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:32]


def source_basename(url: str) -> str:
    """The file name at the end of the URL, for logs."""
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        return "lecture.pdf"
    return os.path.basename(path) or "lecture.pdf"


_TRANSIENT_NAMES = frozenset({
    "ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout", "ChunkedEncodingError",
    "ContentDecodingError", "SSLError", "ProtocolError",
})


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, _RetryableStatus):
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, ssl.SSLError, OSError)):
        return True
    return type(exc).__name__ in _TRANSIENT_NAMES      # the requests library's own classes


def _classify(exc: BaseException) -> BridgeError:
    if isinstance(exc, _RetryableStatus):
        return BridgeError(f"download:http_{exc.status}")
    if _is_transient(exc):
        return BridgeError("download:unreachable")
    return BridgeError(f"download:{type(exc).__name__}")


def download_pdf(url: str, max_bytes: int, *, session=None, sleep=time.sleep) -> bytes:
    """The PDF bytes behind ``url``.

    ``too_large`` when the file exceeds ``max_bytes`` (by header or on the
    wire), ``not_pdf`` when the bytes are not a PDF, ``download:http_<code>``
    for a refusal, ``download:unreachable`` when the connection never held.
    """
    if not is_web_link(url):
        raise BridgeError("invalid_link")
    own_session = session is None
    if own_session:
        import requests

        session = requests.Session()
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/pdf,*/*"}
    last: BaseException | None = None
    try:
        for attempt in range(_ATTEMPTS):
            try:
                with session.get(url.strip(), stream=True, timeout=_TIMEOUT, headers=headers,
                                 allow_redirects=True) as response:
                    status = int(getattr(response, "status_code", 0) or 0)
                    if status == 429 or status >= 500:
                        raise _RetryableStatus(status)
                    if status != 200:
                        raise BridgeError(f"download:http_{status}")
                    length = str((getattr(response, "headers", None) or {}).get("Content-Length", "") or "")
                    if length.isdigit() and int(length) > max_bytes:
                        raise BridgeError("too_large")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_content(_CHUNK):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise BridgeError("too_large")
                        chunks.append(chunk)
                data = b"".join(chunks)
            except BridgeError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not _is_transient(exc) or attempt == _ATTEMPTS - 1:
                    raise _classify(exc) from None
                sleep(2.0 * (attempt + 1))
                continue
            if not data.startswith(b"%PDF"):
                raise BridgeError("not_pdf")
            return data
    finally:
        if own_session:
            session.close()
    raise _classify(last or RuntimeError("download failed"))
