"""Small, strict Vercel Blob REST boundary for web generation jobs.

Authentication is always a fresh Vercel function OIDC token.  Only the HTTP
transport is injectable so tests never weaken the production auth model.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import posixpath
import re
import secrets
import time
from typing import Protocol
from urllib.parse import quote, unquote, urlencode, urlsplit

import requests
import aiohttp
import asyncio

from notesbot.async_dns import AsyncDnsResolver


_PDF_LEADING_WHITESPACE = b" \t\r\n\f\v"
_MAX_PDF_HEADER_OFFSET = 1024
_BLOB_API = "https://vercel.com/api/blob/"
_BLOB_DELETE_API = "https://vercel.com/api/blob/delete"
_MEDIA_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_APP_PREFIX = "shortnotes/v1/"
_EXISTING_BLOB_MESSAGE_PREFIX = "This blob already exists,"
_ASYNC_UPLOAD_CLEANUP_RESERVE_SECONDS = 0.25


class HttpSession(Protocol):
    def request(self, method: str, url: str, **kwargs: object) -> object: ...


class _BlobTooLarge(Exception):
    pass


class _StreamChunkIterator:
    """Single-use async view of a verified, caller-owned seekable stream."""

    def __init__(self, stream: object, content_length: int) -> None:
        self._stream = stream
        self._remaining = content_length
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._closed or self._remaining == 0:
            raise StopAsyncIteration
        piece = self._stream.read(min(64 * 1024, self._remaining))
        if not isinstance(piece, bytes) or not piece or len(piece) > self._remaining:
            raise ValueError("Invalid Blob upload")
        self._remaining -= len(piece)
        return piece

    async def aclose(self) -> None:
        self._closed = True


class BlobNotFoundError(RuntimeError):
    """The requested object does not exist in the configured Blob store."""


class BlobConflictError(RuntimeError):
    """A create or conditional update lost a Blob version race."""


@dataclass(frozen=True)
class BlobRef:
    """The opaque location returned by Vercel Blob."""

    url: str
    pathname: str
    etag: str


@dataclass(frozen=True)
class JsonBlob:
    """A bounded JSON object together with its strong version identifier."""

    value: dict
    etag: str


def _get_oidc_token() -> str:
    """Lazily load the runtime OIDC dependency and return a fresh credential."""
    try:
        from vercel.oidc import get_vercel_oidc_token

        token = get_vercel_oidc_token()
    except Exception:
        raise RuntimeError("Blob authentication is unavailable") from None
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Blob authentication is unavailable")
    return token.strip()


async def _get_oidc_token_async() -> str:
    """Use the provider's native async authority path for remote imports."""
    try:
        from vercel.oidc.aio import get_vercel_oidc_token

        token = await get_vercel_oidc_token()
    except Exception:
        raise RuntimeError("Blob authentication is unavailable") from None
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Blob authentication is unavailable")
    return token.strip()


def _store_key(store_id: str) -> str:
    if not isinstance(store_id, str) or not store_id.startswith("store_"):
        raise ValueError("Invalid Blob store id")
    key = store_id.removeprefix("store_")
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", key):
        raise ValueError("Invalid Blob store id")
    return key


def _normal_prefix(prefix: str) -> str:
    if not isinstance(prefix, str):
        raise ValueError("Invalid Blob prefix")
    clean = prefix.strip("/")
    if clean and ("\\" in clean or any(part in {"", ".", ".."} for part in clean.split("/"))):
        raise ValueError("Invalid Blob prefix")
    return f"{clean}/" if clean else ""


def validate_blob_url(url: str, allowed_store_host: str, prefix: str) -> str:
    """Return a canonical Blob URL or reject host/path ambiguity and SSRF.

    Matching the raw netloc rather than only ``hostname`` intentionally rejects
    ports and user-info; decoding before normalization blocks encoded traversal.
    """
    if not isinstance(url, str) or not isinstance(allowed_store_host, str):
        raise ValueError("Invalid Blob URL")
    host = allowed_store_host.strip().lower()
    if not host or any(char in host for char in "/@:#?[]"):
        raise ValueError("Invalid Blob store host")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ValueError("Invalid Blob URL") from None
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError("Invalid Blob URL")
    try:
        decoded = parsed.path
        # A storage proxy and an HTTP client need not agree on how many times a
        # percent escape is decoded.  Resolve a small bounded chain ourselves
        # so ``%252e%252e`` cannot become traversal downstream.
        for _ in range(8):
            next_path = unquote(decoded, errors="strict")
            if next_path == decoded:
                break
            decoded = next_path
        else:
            raise ValueError("Invalid Blob URL")
    except UnicodeDecodeError:
        raise ValueError("Invalid Blob URL") from None
    if not decoded.startswith("/") or "\\" in decoded or "\x00" in decoded:
        raise ValueError("Invalid Blob URL")
    parts = decoded.split("/")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("Invalid Blob URL")
    normalized = posixpath.normpath(decoded)
    if normalized == "." or not normalized.startswith("/") or normalized != decoded:
        raise ValueError("Invalid Blob URL")
    relative = normalized[1:]
    expected_prefix = _normal_prefix(prefix)
    if not relative or (expected_prefix and not relative.startswith(expected_prefix)):
        raise ValueError("Invalid Blob URL")
    return f"https://{host}/{quote(relative, safe='/-._~')}"


def validate_pdf_bytes(data: bytes) -> None:
    """Require a PDF header after whitespace only, in its allowed leading area."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("Invalid PDF input")
    header_at = bytes(data).find(b"%PDF-")
    if header_at < 0 or header_at > _MAX_PDF_HEADER_OFFSET:
        raise ValueError("Invalid PDF input")
    if any(byte not in _PDF_LEADING_WHITESPACE for byte in data[:header_at]):
        raise ValueError("Invalid PDF input")


def _response_header(headers: object, name: str) -> str | None:
    if not hasattr(headers, "items"):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name.lower():
            return value if isinstance(value, str) else None
    return None


def _is_content_type(value: str | None, expected_media_type: str | None = None) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(";")
    media_type = parts[0].strip().lower()
    if not re.fullmatch(rf"{_MEDIA_TOKEN}/{_MEDIA_TOKEN}", media_type):
        return False
    if expected_media_type is not None and media_type != expected_media_type:
        return False
    parameter = re.compile(
        rf'{_MEDIA_TOKEN}=(?:{_MEDIA_TOKEN}|"[^"\x00-\x1f\x7f]*")'
    )
    return all(part.strip() and parameter.fullmatch(part.strip()) for part in parts[1:])


def _is_strong_etag(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r'"[!#-~]*"', value) is not None


def _is_existing_blob_error(response: object) -> bool:
    """Recognize Vercel's create-only collision without trusting generic 400s."""
    try:
        if not _is_content_type(
            _response_header(getattr(response, "headers", {}), "Content-Type"),
            "application/json",
        ):
            return False
        value = response.json()
        if not isinstance(value, dict):
            return False
        error = value.get("error")
        if not isinstance(error, dict):
            return False
        message = error.get("message")
        return (
            error.get("code") == "bad_request"
            and isinstance(message, str)
            and len(message) <= 4096
            and message.startswith(_EXISTING_BLOB_MESSAGE_PREFIX)
        )
    except Exception:
        return False


def _close_response_quietly(response: object) -> None:
    """Release provider response ownership without leaking close failures."""
    close = getattr(response, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        # Closing is best-effort cleanup.  It must never replace the fixed,
        # redacted result of the Blob operation that already owns the response.
        pass


def safe_blob_path(pathname: str) -> str:
    """Validate a relative Blob path before it is joined into a request URL."""
    if not isinstance(pathname, str) or not pathname or pathname.startswith(("/", "\\")):
        raise ValueError("Invalid Blob pathname")
    if "\\" in pathname or "\x00" in pathname:
        raise ValueError("Invalid Blob pathname")
    decoded = pathname
    for _ in range(8):
        next_path = unquote(decoded)
        if next_path == decoded:
            break
        decoded = next_path
    else:
        raise ValueError("Invalid Blob pathname")
    if any(part in {"", ".", ".."} for part in decoded.split("/")):
        raise ValueError("Invalid Blob pathname")
    return quote(decoded, safe="/-._~")


def random_blob_path(prefix: str, suffix: str) -> str:
    """Create a collision-resistant path with an explicitly controlled suffix."""
    clean_prefix = _normal_prefix(prefix)
    if not isinstance(suffix, str) or not re.fullmatch(r"(?:\.[A-Za-z0-9]{1,12})?", suffix):
        raise ValueError("Invalid Blob suffix")
    return f"{clean_prefix}{secrets.token_urlsafe(24)}{suffix}"


class BlobStore:
    """Authenticated Blob REST operations with bounded, no-redirect reads."""

    def __init__(
        self,
        store_id: str,
        *,
        session: HttpSession | None = None,
        allowed_prefix: str = _APP_PREFIX,
        timeout_seconds: float = 20,
    ) -> None:
        self.store_key = _store_key(store_id)
        self.store_id = f"store_{self.store_key}"
        self.store_host = f"{self.store_key.lower()}.private.blob.vercel-storage.com"
        self.session: HttpSession = session if session is not None else requests.Session()
        self.allowed_prefix = _normal_prefix(allowed_prefix)
        if not self.allowed_prefix.startswith(_APP_PREFIX):
            raise ValueError("Invalid Blob prefix")
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        try:
            token = _get_oidc_token()
        except Exception:
            raise RuntimeError("Blob authentication is unavailable") from None
        return self._headers_for_token(token)

    def _headers_for_token(self, token: str) -> dict[str, str]:
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("Blob authentication is unavailable")
        return {
            "Authorization": f"Bearer {token.strip()}",
            "x-vercel-blob-store-id": self.store_key,
            "x-api-blob-request-id": (
                f"{self.store_key}:{int(time.time() * 1000)}:{secrets.token_hex(8)}"
            ),
            "x-api-blob-request-attempt": "0",
            "x-api-version": "12",
        }

    def prepare_remote_upload_authority(self) -> dict[str, str]:
        """Return the one server-only authority used by a remote import PUT.

        This deliberately returns rather than probes the headers: a remote
        importer must obtain authority before it contacts an untrusted URL and
        must not acquire a fresh credential after downloading that URL.
        """
        return self._headers()

    async def prepare_remote_upload_authority_async(
        self,
        *,
        deadline_at: float,
    ) -> dict[str, str]:
        """Acquire one authority through Vercel's cancellable async API."""
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Blob upload deadline exceeded")
        try:
            token = await asyncio.wait_for(_get_oidc_token_async(), timeout=remaining)
        except (TimeoutError, asyncio.TimeoutError):
            raise TimeoutError("Blob upload deadline exceeded") from None
        except Exception:
            raise RuntimeError("Blob authentication is unavailable") from None
        return self._headers_for_token(token)

    def _request(
        self,
        method: str,
        url: str,
        *,
        expect_create_collision: bool = False,
        **kwargs: object,
    ) -> object:
        try:
            response = self.session.request(
                method, url, allow_redirects=False, timeout=self.timeout_seconds, **kwargs
            )
        except Exception:
            raise RuntimeError("Blob storage is unavailable") from None
        status = getattr(response, "status_code", None)
        if status == 404:
            _close_response_quietly(response)
            raise BlobNotFoundError("Blob object was not found") from None
        if status in {409, 412}:
            _close_response_quietly(response)
            raise BlobConflictError("Blob object changed or already exists") from None
        if (
            status == 400
            and method == "PUT"
            and expect_create_collision
            and _is_existing_blob_error(response)
        ):
            _close_response_quietly(response)
            raise BlobConflictError("Blob object changed or already exists") from None
        if not isinstance(status, int) or status < 200 or status >= 300:
            # Do not call raise_for_status(): providers often include credentials
            # and request URLs in their exception text.
            _close_response_quietly(response)
            if isinstance(status, int) and (status == 429 or status >= 500):
                raise RuntimeError("Blob storage is unavailable") from None
            raise RuntimeError("Blob storage operation failed") from None
        return response

    def _upload_url(self, pathname: str) -> tuple[str, str]:
        clean = safe_blob_path(pathname)
        decoded = unquote(clean)
        if not decoded.startswith(self.allowed_prefix):
            raise ValueError("Invalid Blob pathname")
        query = urlencode({"pathname": decoded})
        return clean, f"{_BLOB_API}?{query}"

    def put_bytes(
        self,
        pathname: str,
        data: bytes,
        content_type: str,
        allow_overwrite: bool = False,
        *,
        if_match: str | None = None,
    ) -> BlobRef:
        if (
            not isinstance(data, bytes)
            or not _is_content_type(content_type)
            or not isinstance(allow_overwrite, bool)
        ):
            raise ValueError("Invalid Blob upload")
        if if_match is not None and (not allow_overwrite or not _is_strong_etag(if_match)):
            raise ValueError("Conditional Blob updates require overwrite and a strong ETag")
        clean, url = self._upload_url(pathname)
        headers = self._headers()
        headers["x-vercel-blob-access"] = "private"
        headers["x-content-type"] = content_type
        headers["x-add-random-suffix"] = "0"
        headers["x-allow-overwrite"] = "1" if allow_overwrite else "0"
        if if_match is not None:
            headers["x-if-match"] = if_match
        response = self._request(
            "PUT",
            url,
            headers=headers,
            data=data,
            expect_create_collision=not allow_overwrite,
        )
        try:
            if not _is_content_type(
                _response_header(getattr(response, "headers", {}), "Content-Type"),
                "application/json",
            ):
                raise ValueError
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
            returned_url = value["url"]
            returned_path = value["pathname"]
            returned_type = value["contentType"]
            returned_etag = value["etag"]
            if not all(isinstance(item, str) for item in (returned_url, returned_path, returned_type)):
                raise ValueError
            expected_path = unquote(clean)
            expected_url = f"https://{self.store_host}/{clean}"
            canonical_url = validate_blob_url(
                returned_url,
                self.store_host,
                expected_path.rsplit("/", 1)[0] + "/" if "/" in expected_path else "",
            )
            if (
                canonical_url != expected_url
                or returned_path != expected_path
                or returned_type.strip().lower() != content_type.strip().lower()
                or not _is_content_type(returned_type)
                or not _is_strong_etag(returned_etag)
            ):
                raise ValueError
        except Exception:
            raise RuntimeError("Blob storage returned an invalid response") from None
        return BlobRef(returned_url, returned_path, returned_etag)

    async def put_stream_async(
        self,
        pathname: str,
        stream: object,
        content_length: int,
        content_type: str,
        *,
        max_bytes: int,
        deadline_at: float,
        authority_headers: dict[str, str] | None = None,
        resolver: aiohttp.abc.AbstractResolver | None = None,
    ) -> BlobRef:
        """Create one private Blob from a verified seekable stream.

        The remote importer supplies its absolute deadline.  This method never
        retries an ambiguous PUT: callers settle their durable reservation and
        can clean the canonical input safely instead.
        """
        if (not hasattr(stream, "seek") or not hasattr(stream, "read") or
                not isinstance(content_length, int) or isinstance(content_length, bool) or
                not 0 < content_length <= max_bytes or not _is_content_type(content_type)):
            raise ValueError("Invalid Blob upload")
        try:
            if stream.tell() != 0:
                raise ValueError
            stream.seek(0, 2)
            if stream.tell() != content_length:
                raise ValueError
            stream.seek(0)
        except Exception:
            raise ValueError("Invalid Blob upload") from None
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Blob upload deadline exceeded")
        clean, url = self._upload_url(pathname)
        # ``authority_headers`` is supplied by the remote-import reservation
        # before DNS.  Copy it so per-request headers cannot mutate a caller's
        # credential record. Other callers retain the normal fresh authority.
        headers = dict(authority_headers) if authority_headers is not None else self._headers()
        if not isinstance(headers.get("Authorization"), str) or not headers["Authorization"].startswith("Bearer "):
            raise ValueError("Invalid Blob upload authority")
        headers.update({
            "x-vercel-blob-access": "private", "x-content-type": content_type,
            "x-add-random-suffix": "0", "x-allow-overwrite": "0",
            "Content-Length": str(content_length),
        })
        cleanup_reserve = min(
            _ASYNC_UPLOAD_CLEANUP_RESERVE_SECONDS,
            remaining / 2,
        )
        operation_remaining = remaining - cleanup_reserve
        if operation_remaining <= 0:
            raise TimeoutError("Blob upload deadline exceeded")
        upload_resolver = resolver or AsyncDnsResolver(deadline_at)
        connector = aiohttp.TCPConnector(
            resolver=upload_resolver,
            use_dns_cache=False,
            ttl_dns_cache=0,
            force_close=True,
        )
        chunks = _StreamChunkIterator(stream, content_length)
        timeout = aiohttp.ClientTimeout(
            total=operation_remaining,
            connect=min(10, operation_remaining),
            sock_connect=min(10, operation_remaining),
            sock_read=min(15, operation_remaining),
        )

        async def upload_once():
            async with aiohttp.ClientSession(
                connector=connector,
                connector_owner=False,
                trust_env=False,
            ) as session:
                async with session.put(
                    url,
                    headers=headers,
                    data=chunks,
                    timeout=timeout,
                    allow_redirects=False,
                ) as response:
                    body = await response.content.read(64 * 1024 + 1)
                    if len(body) > 64 * 1024:
                        raise RuntimeError("Blob storage returned an invalid response")
                    if response.status in {409, 412}:
                        raise BlobConflictError("Blob object changed or already exists")
                    if response.status == 400:
                        class _Response:
                            def __init__(self, headers, raw):
                                self.headers, self.raw = headers, raw

                            def json(self):
                                return json.loads(self.raw.decode("utf-8"))

                        if _is_existing_blob_error(_Response(response.headers, body)):
                            raise BlobConflictError("Blob object changed or already exists")
                    if response.status < 200 or response.status >= 300:
                        raise RuntimeError("Blob storage is unavailable")
                    if not _is_content_type(
                        response.headers.get("Content-Type"), "application/json"
                    ):
                        raise RuntimeError("Blob storage returned an invalid response")
                    return json.loads(body.decode("utf-8"))

        try:
            value = await asyncio.wait_for(upload_once(), timeout=operation_remaining)
        except (BlobConflictError, TimeoutError, ValueError, asyncio.CancelledError):
            raise
        except Exception:
            raise RuntimeError("Blob storage is unavailable") from None
        finally:
            cleanup_remaining = deadline_at - time.monotonic()
            cleanup = asyncio.gather(
                chunks.aclose(),
                connector.close(),
                upload_resolver.close(),
                return_exceptions=True,
            )
            try:
                if cleanup_remaining <= 0:
                    cleanup.cancel()
                    await asyncio.gather(cleanup, return_exceptions=True)
                    raise TimeoutError("Blob upload deadline exceeded")
                cleanup_results = await asyncio.wait_for(
                    cleanup,
                    timeout=cleanup_remaining,
                )
                if (
                    any(
                        isinstance(result, BaseException)
                        for result in cleanup_results
                    )
                    and not asyncio.current_task().cancelling()
                ):
                    raise RuntimeError("Blob storage is unavailable") from None
            except asyncio.CancelledError:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
                raise
            except TimeoutError:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
                if not asyncio.current_task().cancelling():
                    raise TimeoutError("Blob upload deadline exceeded") from None
        try:
            returned_url, returned_path, returned_type, returned_etag = (value["url"], value["pathname"], value["contentType"], value["etag"])
            expected_path = unquote(clean)
            expected_url = f"https://{self.store_host}/{clean}"
            if (not all(isinstance(item, str) for item in (returned_url, returned_path, returned_type, returned_etag)) or
                validate_blob_url(returned_url, self.store_host, expected_path.rsplit("/", 1)[0] + "/") != expected_url or
                returned_path != expected_path or returned_type.strip().lower() != content_type or not _is_strong_etag(returned_etag)):
                raise ValueError
        except Exception:
            raise RuntimeError("Blob storage returned an invalid response") from None
        return BlobRef(returned_url, returned_path, returned_etag)

    def put_json(
        self,
        pathname: str,
        value: dict,
        allow_overwrite: bool = False,
        *,
        if_match: str | None = None,
    ) -> BlobRef:
        if not isinstance(value, dict):
            raise ValueError("JSON Blob value must be an object")
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return self.put_bytes(
            pathname,
            data,
            "application/json",
            allow_overwrite,
            if_match=if_match,
        )

    def _get_bytes_with_headers(
        self,
        url: str,
        max_bytes: int,
        *,
        bypass_cache: bool = False,
    ) -> tuple[bytes, object]:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("Invalid Blob size limit")
        checked_url = validate_blob_url(url, self.store_host, self.allowed_prefix)
        request_url = f"{checked_url}?cache=0" if bypass_cache else checked_url
        response = self._request(
            "GET", request_url, headers=self._headers(), stream=True
        )
        try:
            raw_headers = getattr(response, "headers", {})
            try:
                response_headers = dict(raw_headers.items())
            except Exception:
                raise RuntimeError(
                    "Blob storage returned an invalid response"
                ) from None
            content_length = _response_header(response_headers, "Content-Length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except (TypeError, ValueError):
                    raise RuntimeError(
                        "Blob storage returned an invalid response"
                    ) from None
                if declared < 0:
                    raise RuntimeError("Blob storage returned an invalid response")
                if declared > max_bytes:
                    raise ValueError("Blob content is too large")
            chunks: list[bytes] = []
            total = 0
            try:
                iterator = response.iter_content(
                    chunk_size=min(64 * 1024, max_bytes)
                )
                for chunk in iterator:
                    if not chunk:
                        continue
                    if not isinstance(chunk, bytes) or len(chunk) > max_bytes - total:
                        raise _BlobTooLarge
                    chunks.append(chunk)
                    total += len(chunk)
            except _BlobTooLarge:
                raise ValueError("Blob content is too large") from None
            except Exception:
                raise RuntimeError("Blob storage is unavailable") from None
            return b"".join(chunks), response_headers
        finally:
            _close_response_quietly(response)

    def get_bytes(self, url: str, max_bytes: int) -> bytes:
        data, _headers = self._get_bytes_with_headers(url, max_bytes)
        return data

    def get_pdf(self, url: str, max_bytes: int) -> bytes:
        data, headers = self._get_bytes_with_headers(url, max_bytes)
        if not _is_content_type(_response_header(headers, "Content-Type"), "application/pdf"):
            raise ValueError("Invalid PDF content type")
        validate_pdf_bytes(data)
        return data

    def get_json(self, url: str, max_bytes: int) -> JsonBlob:
        data, headers = self._get_bytes_with_headers(
            url,
            max_bytes,
            bypass_cache=True,
        )
        try:
            if not _is_content_type(
                _response_header(headers, "Content-Type"), "application/json"
            ):
                raise ValueError
            etag = _response_header(headers, "ETag")
            if not _is_strong_etag(etag):
                raise ValueError
            value = json.loads(
                data.decode("utf-8"),
                parse_constant=lambda _constant: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(value, dict):
                raise ValueError
        except Exception:
            raise RuntimeError("Blob storage returned an invalid response") from None
        return JsonBlob(value=value, etag=etag)

    def delete(self, urls: list[str]) -> None:
        if not isinstance(urls, list) or not urls:
            raise ValueError("Blob URLs are required")
        checked_urls = [
            validate_blob_url(url, self.store_host, self.allowed_prefix) for url in urls
        ]
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        self._request(
            "POST",
            _BLOB_DELETE_API,
            headers=headers,
            data=json.dumps({"urls": checked_urls}).encode("utf-8"),
        )
