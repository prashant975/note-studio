"""Bounded, private-server acquisition of one public PDF.

This module intentionally never retains or reports the submitted URL.  Its
errors are fixed categories so provider HTML, signed query strings and Drive
identifiers cannot escape the server boundary.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
import ipaddress
import re
import socket
import tempfile
import time
from typing import BinaryIO, Callable, Literal
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
import idna

from notesbot.blob_store import (
    BlobConflictError,
    BlobStore,
    validate_blob_url,
    validate_pdf_bytes,
)
from notesbot.async_dns import AsyncDnsResolver
from notesbot.errors import UserError
from notesbot.input_limits import MAX_INPUT_BYTES

MAX_REMOTE_URL_CHARS = 2_048
MAX_REMOTE_REQUESTS = 5
MAX_REMOTE_REDIRECTS = 3
MAX_DRIVE_CONFIRMATIONS = 1
MAX_REMOTE_CONFIRM_HTML_BYTES = 512 * 1024
MAX_DRIVE_RESOURCE_KEY_CHARS = 256
REMOTE_FETCH_MAX_SECONDS = 60.0
REMOTE_ACQUISITION_TOTAL_SECONDS = 85.0
REMOTE_BLOB_UPLOAD_RESERVE_SECONDS = 20.0
REMOTE_IMPORT_RECOVERY_TTL_SECONDS = 120
REMOTE_SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,128}\Z")
_RESOURCE_KEY = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_CONTROL = re.compile(r"[\x00-\x20\x7f\\]")

# `ipaddress.is_global` is not a public-unicast policy: on supported Python
# runtimes it can be true for multicast, transition, translation, and some
# IANA special-purpose ranges.  Keep an explicit registry snapshot so the
# network boundary is stable across Python's changing embedded registry data.
_IPV4_SPECIAL_USE = tuple(ipaddress.ip_network(value) for value in (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.31.196.0/24",
    "192.52.193.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "192.175.48.0/24",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "240.0.0.0/4",
))
_IPV6_SPECIAL_USE = tuple(ipaddress.ip_network(value) for value in (
    "::/96",
    "::ffff:0:0/96",
    "64:ff9b::/96",
    "64:ff9b:1::/48",
    "100::/64",
    "100:0:0:1::/64",
    "2001::/23",
    "2001:db8::/32",
    "2002::/16",
    "2620:4f:8000::/48",
    "3fff::/20",
    "5f00::/16",
    "fc00::/7",
    "fe80::/10",
    "fec0::/10",
))


def _is_ordinary_public_unicast(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        not address.is_global
        or address.is_private
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or getattr(address, "is_site_local", False)
        or getattr(address, "scope_id", None) is not None
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped is not None
        or address.sixtofour is not None
        or address.teredo is not None
    ):
        return False
    denied = _IPV4_SPECIAL_USE if address.version == 4 else _IPV6_SPECIAL_USE
    return not any(address in network for network in denied)

@dataclass(frozen=True)
class RemotePdfMetadata:
    size: int
    filename: str

@dataclass(frozen=True)
class ImportedBlob:
    source_url: str
    source_size: int
    filename: str

class RemotePdfImportError(UserError):
    _MESSAGES = {
        "invalid_link": "PDF link valid nahi hai.", "private_destination": "Private link allowed nahi hai.",
        "not_public": "PDF public nahi hai.", "not_pdf": "Link PDF file nahi hai.",
        "too_large": "PDF 100 MB se badi hai.", "deadline_exceeded": "PDF download time limit poora ho gaya.",
        "unavailable": "PDF abhi available nahi hai.",
    }
    def __init__(self, code: Literal["invalid_link", "private_destination", "not_public", "not_pdf", "too_large", "deadline_exceeded", "unavailable"]):
        self.code = code
        super().__init__(self._MESSAGES[code])

@dataclass(frozen=True)
class _DriveFile:
    file_id: str
    resource_key: str = ""

def _exact_query(query: str) -> dict[str, str]:
    try: pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError: raise RemotePdfImportError("invalid_link") from None
    result: dict[str, str] = {}
    for key, value in pairs:
        if key in result: raise RemotePdfImportError("invalid_link")
        result[key] = value
    return result

def _drive_file(url: str) -> _DriveFile | None:
    """Accept exactly the three documented, public Drive file link shapes."""
    p = urlsplit(url)
    if p.hostname != "drive.google.com": return None
    query = _exact_query(p.query)
    file_id = ""
    # A share URL has no query id; open/uc have exactly id plus allowed extras.
    match = re.fullmatch(r"/file/d/([A-Za-z0-9_-]{10,128})/view", p.path)
    if match:
        if any(key not in {"usp", "resourcekey"} for key in query): raise RemotePdfImportError("invalid_link")
        file_id = match.group(1)
    elif p.path in {"/open", "/uc"}:
        if any(key not in {"id", "export", "usp", "resourcekey"} for key in query) or "id" not in query:
            raise RemotePdfImportError("invalid_link")
        if p.path == "/uc" and query.get("export", "download") != "download": raise RemotePdfImportError("invalid_link")
        file_id = query["id"]
    else:
        # Explicitly reject folder/editor families before a resolver can run.
        raise RemotePdfImportError("invalid_link")
    if not _DRIVE_ID.fullmatch(file_id): raise RemotePdfImportError("invalid_link")
    if "usp" in query and query["usp"] not in {"sharing", "drive_link", "drivesdk"}: raise RemotePdfImportError("invalid_link")
    key = query.get("resourcekey", "")
    if key and not _RESOURCE_KEY.fullmatch(key): raise RemotePdfImportError("invalid_link")
    return _DriveFile(file_id, key)

def _drive_download_url(info: _DriveFile, *, confirm: dict[str, str] | None = None) -> str:
    values = {"id": info.file_id, "export": "download"}
    if info.resource_key: values["resourcekey"] = info.resource_key
    if confirm: values.update(confirm)
    return "https://drive.usercontent.google.com/download?" + urlencode(values)

class _ConfirmationParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.action = ""; self.method = ""; self.fields: dict[str, str] = {}; self.forms = 0
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag.lower() == "form":
            self.forms += 1
            if self.forms == 1: self.action = values.get("action", ""); self.method = values.get("method", "get").lower()
        elif tag.lower() == "input" and self.forms == 1:
            name, value = values.get("name"), values.get("value", "")
            if not name or name in self.fields: self.forms = 2
            else: self.fields[name] = value

def _confirmation_url(html: bytes, info: _DriveFile) -> str:
    if len(html) > MAX_REMOTE_CONFIRM_HTML_BYTES: raise RemotePdfImportError("not_public")
    try: text = html.decode("utf-8", "strict")
    except UnicodeDecodeError: raise RemotePdfImportError("not_public") from None
    parser = _ConfirmationParser()
    try: parser.feed(text); parser.close()
    except Exception: raise RemotePdfImportError("not_public") from None
    if parser.forms != 1 or parser.method != "get": raise RemotePdfImportError("not_public")
    try: action = _safe_url(parser.action)
    except RemotePdfImportError: raise RemotePdfImportError("not_public") from None
    action_parts = urlsplit(action)
    if action_parts.hostname != "drive.usercontent.google.com" or action_parts.path != "/download" or action_parts.query or action_parts.fragment:
        raise RemotePdfImportError("not_public")
    fields = parser.fields
    if set(fields) - {"id", "export", "confirm", "uuid", "resourcekey"} or fields.get("id") != info.file_id or fields.get("export") != "download" or not fields.get("confirm"):
        raise RemotePdfImportError("not_public")
    if "resourcekey" in fields and fields["resourcekey"] != info.resource_key: raise RemotePdfImportError("not_public")
    extras = {key: value for key, value in fields.items() if key in {"confirm", "uuid"}}
    if any(not value or len(value) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in extras.values()): raise RemotePdfImportError("not_public")
    return _drive_download_url(info, confirm=extras)

def _safe_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_REMOTE_URL_CHARS or _CONTROL.search(value):
        raise RemotePdfImportError("invalid_link")
    try: p = urlsplit(value); port = p.port
    except ValueError: raise RemotePdfImportError("invalid_link") from None
    if p.scheme != "https" or not p.hostname or p.username or p.password or port is not None or p.fragment or "%" in p.netloc or p.hostname.endswith(".") or "@" in p.netloc:
        raise RemotePdfImportError("invalid_link")
    try:
        ipaddress.ip_address(p.hostname)
        raise RemotePdfImportError("private_destination")
    except ValueError: pass
    except RemotePdfImportError: raise
    try:
        canonical_host = idna.encode(
            p.hostname,
            uts46=True,
            std3_rules=True,
        ).decode("ascii").lower()
    except (idna.IDNAError, UnicodeError):
        raise RemotePdfImportError("invalid_link") from None
    if not canonical_host or canonical_host.endswith(".") or len(canonical_host) > 253:
        raise RemotePdfImportError("invalid_link")
    # aiohttp treats legacy all-numeric forms (single integer, short dotted,
    # and octal dotted) as IP addresses and bypasses custom DNS resolution.
    # Reject the whole numeric-host family before handing the URL to it.
    if re.fullmatch(r"[0-9.]+", canonical_host):
        raise RemotePdfImportError("private_destination")
    try:
        ipaddress.ip_address(canonical_host)
        raise RemotePdfImportError("private_destination")
    except ValueError: pass
    except RemotePdfImportError: raise
    return urlunsplit(("https", canonical_host, p.path or "/", p.query, ""))

class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """Return newly built public numeric rows, never dependency metadata."""

    def __init__(
        self,
        base=None,
        *,
        deadline_at: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if base is None:
            if deadline_at is None:
                raise ValueError("A DNS deadline is required")
            base = AsyncDnsResolver(deadline_at, monotonic=monotonic)
        self.base = base
        self._closed = False

    async def resolve(self, host, port=0, family=0):
        if self._closed:
            raise RuntimeError("DNS resolver is closed")
        try:
            if (
                not isinstance(host, str)
                or not host
                or not isinstance(port, int)
                or isinstance(port, bool)
                or not 0 <= port <= 65535
                or family not in {socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6}
            ):
                raise ValueError
        except Exception:
            raise RemotePdfImportError("private_destination") from None
        # Transport, timeout, and cancellation failures are not evidence that
        # the destination is private.  Let the fetch boundary classify them.
        rows = await self.base.resolve(host, port, family)
        try:
            if not isinstance(rows, (list, tuple)) or not rows:
                raise ValueError
            normalized = []
            seen: set[tuple[int, str]] = set()
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError
                address = row.get("host")
                row_family = row.get("family")
                if not isinstance(address, str) or row_family not in {
                    socket.AF_INET,
                    socket.AF_INET6,
                }:
                    raise ValueError
                parsed = ipaddress.ip_address(address)
                expected_family = (
                    socket.AF_INET if parsed.version == 4 else socket.AF_INET6
                )
                if (
                    row_family != expected_family
                    or (family != socket.AF_UNSPEC and row_family != family)
                    or not _is_ordinary_public_unicast(parsed)
                ):
                    raise ValueError
                canonical = str(parsed)
                key = (expected_family, canonical)
                if key in seen:
                    continue
                seen.add(key)
                normalized.append({
                    "hostname": host,
                    "host": canonical,
                    "port": port,
                    "family": expected_family,
                    "proto": socket.IPPROTO_TCP,
                    "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                })
            if not normalized:
                raise ValueError
            return normalized
        except Exception:
            raise RemotePdfImportError("private_destination") from None

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await self.base.close()

def _remaining(deadline: float, monotonic: Callable[[], float], reserve: float = 0) -> float:
    value = deadline - monotonic() - reserve
    if value <= 0: raise RemotePdfImportError("deadline_exceeded")
    return value


def _drive_redirect_allowed(url: str, info: _DriveFile) -> bool:
    """Allow only the same anonymous Drive download identity after a redirect."""
    parts = urlsplit(url)
    if not (
        (parts.hostname == "drive.google.com" and parts.path == "/uc")
        or (parts.hostname == "drive.usercontent.google.com" and parts.path == "/download")
    ):
        return False
    try:
        query = _exact_query(parts.query)
    except RemotePdfImportError:
        return False
    if set(query) - {"id", "export", "confirm", "uuid", "resourcekey"}:
        return False
    if query.get("id") != info.file_id or query.get("export") != "download":
        return False
    # A key-bearing import must keep its key across every redirected request;
    # a key-free import must reject a server-injected key.
    if query.get("resourcekey", "") != info.resource_key:
        return False
    return True


def _filename(headers, url: str) -> str:
    cd = headers.get("Content-Disposition", "")
    match = re.search(r'filename="?([^";]{1,90})', cd, re.I)
    name = match.group(1) if match else urlsplit(url).path.rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._ -]", "", name or "").strip(" .")[:90].rstrip(" .")
    return name if name.lower().endswith(".pdf") else "linked-lecture.pdf"

async def fetch_public_pdf(
    url: str,
    sink: BinaryIO,
    *,
    deadline_at: float,
    max_bytes: int = MAX_INPUT_BYTES,
    resolver: aiohttp.abc.AbstractResolver | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> RemotePdfMetadata:
    """Fetch one public PDF while re-resolving and pinning every GET."""
    current = _safe_url(url)
    if not hasattr(sink, "write") or max_bytes != MAX_INPUT_BYTES:
        raise RemotePdfImportError("invalid_link")
    drive = _drive_file(current)
    if drive is not None:
        current = _drive_download_url(drive)
    pinned = _PinnedResolver(
        resolver,
        deadline_at=deadline_at,
        monotonic=monotonic,
    )
    connector = aiohttp.TCPConnector(
        resolver=pinned,
        use_dns_cache=False,
        ttl_dns_cache=0,
        force_close=True,
    )
    fetch_deadline = min(
        deadline_at - REMOTE_BLOB_UPLOAD_RESERVE_SECONDS,
        monotonic() + REMOTE_FETCH_MAX_SECONDS,
    )

    async def fetch_with_session() -> RemotePdfMetadata:
        nonlocal current
        jar = aiohttp.CookieJar() if drive is not None else aiohttp.DummyCookieJar()
        async with aiohttp.ClientSession(
            connector=connector,
            connector_owner=False,
            trust_env=False,
            cookie_jar=jar,
            auto_decompress=False,
            headers={"Accept-Encoding": "identity"},
        ) as session:
            confirmations = 0
            for request_number in range(MAX_REMOTE_REQUESTS):
                remaining = _remaining(fetch_deadline, monotonic)
                timeout = aiohttp.ClientTimeout(
                    total=remaining,
                    connect=min(10, remaining),
                    sock_connect=min(10, remaining),
                    sock_read=min(15, remaining),
                )
                drive_headers = (
                    {
                        "X-Goog-Drive-Resource-Keys": (
                            f"{drive.file_id}/{drive.resource_key}"
                        )
                    }
                    if drive is not None and drive.resource_key
                    else {}
                )
                async with session.get(
                    current,
                    allow_redirects=False,
                    timeout=timeout,
                    headers=drive_headers,
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        locations = (
                            response.headers.getall("Location", [])
                            if hasattr(response.headers, "getall")
                            else [response.headers.get("Location")]
                            if response.headers.get("Location")
                            else []
                        )
                        if (
                            len(locations) != 1
                            or not locations[0]
                            or len(locations[0]) > MAX_REMOTE_URL_CHARS
                            or request_number >= MAX_REMOTE_REDIRECTS
                        ):
                            raise RemotePdfImportError("not_public")
                        current_url = _safe_url(urljoin(current, locations[0]))
                        if drive is not None and not _drive_redirect_allowed(
                            current_url, drive
                        ):
                            raise RemotePdfImportError("not_public")
                        current = current_url
                        continue
                    if response.status != 200 or response.headers.get("Content-Range"):
                        code = (
                            "not_public"
                            if response.status in {401, 403, 404}
                            else "unavailable"
                        )
                        raise RemotePdfImportError(code)
                    content_type = (
                        response.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if drive is not None and content_type in {
                        "text/html",
                        "application/xhtml+xml",
                    }:
                        html = await response.content.read(
                            MAX_REMOTE_CONFIRM_HTML_BYTES + 1
                        )
                        if confirmations >= MAX_DRIVE_CONFIRMATIONS:
                            raise RemotePdfImportError("not_public")
                        current = _confirmation_url(html, drive)
                        confirmations += 1
                        continue
                    if content_type not in {
                        "application/pdf",
                        "application/octet-stream",
                    }:
                        raise RemotePdfImportError("not_pdf")
                    if (
                        response.headers.get("Content-Encoding", "identity").lower()
                        != "identity"
                    ):
                        raise RemotePdfImportError("not_pdf")
                    raw_length = response.headers.get("Content-Length")
                    if raw_length is not None:
                        if not raw_length.isdigit():
                            raise RemotePdfImportError("not_pdf")
                        declared = int(raw_length)
                        if declared > max_bytes:
                            raise RemotePdfImportError("too_large")
                    else:
                        declared = None
                    total = 0
                    head = bytearray()
                    tail = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if len(chunk) > max_bytes - total:
                            raise RemotePdfImportError("too_large")
                        sink.write(chunk)
                        total += len(chunk)
                        if len(head) < 1029:
                            head.extend(chunk[: 1029 - len(head)])
                        tail.extend(chunk)
                        del tail[:-8192]
                    if not total or (declared is not None and total != declared):
                        raise RemotePdfImportError("not_pdf")
                    try:
                        validate_pdf_bytes(bytes(head))
                    except ValueError:
                        raise RemotePdfImportError("not_pdf") from None
                    if b"%%EOF" not in tail:
                        raise RemotePdfImportError("not_pdf")
                    return RemotePdfMetadata(
                        total,
                        _filename(response.headers, current),
                    )
            raise RemotePdfImportError("unavailable")

    try:
        remaining = _remaining(fetch_deadline, monotonic)
        return await asyncio.wait_for(fetch_with_session(), timeout=remaining)
    except asyncio.CancelledError:
        raise
    except RemotePdfImportError:
        raise
    except (TimeoutError, asyncio.TimeoutError):
        raise RemotePdfImportError("deadline_exceeded") from None
    except Exception:
        raise RemotePdfImportError("unavailable") from None
    finally:
        cleanup_remaining = deadline_at - monotonic()
        cleanup = asyncio.gather(
            connector.close(),
            pinned.close(),
            return_exceptions=True,
        )
        try:
            if cleanup_remaining <= 0:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
                raise RemotePdfImportError("deadline_exceeded")
            cleanup_results = await asyncio.wait_for(
                cleanup,
                timeout=cleanup_remaining,
            )
            if (
                any(isinstance(result, BaseException) for result in cleanup_results)
                and not asyncio.current_task().cancelling()
            ):
                raise RemotePdfImportError("unavailable") from None
        except asyncio.CancelledError:
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
            raise
        except (TimeoutError, asyncio.TimeoutError):
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
            if not asyncio.current_task().cancelling():
                raise RemotePdfImportError("deadline_exceeded") from None

async def _acquire_public_pdf_to_blob_until(
    url: str,
    blob_store: BlobStore,
    pathname: str,
    *,
    max_bytes: int,
    deadline_at: float,
) -> ImportedBlob:
    """Own authority, fetch scratch, one PUT, and all cleanup in one task."""
    authority = await blob_store.prepare_remote_upload_authority_async(
        deadline_at=deadline_at
    )
    if not isinstance(authority, dict):
        raise RuntimeError("invalid authority")
    with tempfile.SpooledTemporaryFile(
        max_size=REMOTE_SPOOL_MEMORY_BYTES,
        mode="w+b",
        dir=tempfile.gettempdir(),
    ) as spool:
        meta = await fetch_public_pdf(
            url,
            spool,
            deadline_at=deadline_at,
            max_bytes=max_bytes,
        )
        spool.seek(0)
        ref = await blob_store.put_stream_async(
            pathname,
            spool,
            meta.size,
            "application/pdf",
            max_bytes=max_bytes,
            deadline_at=deadline_at,
            authority_headers=authority,
        )
        imported = _validated_imported_blob(
            ref.url,
            meta.size,
            meta.filename,
            blob_store,
            pathname,
            max_bytes,
        )
    return imported


def _validated_imported_blob(
    source_url: object,
    source_size: object,
    filename: object,
    blob_store: BlobStore,
    pathname: str,
    max_bytes: int,
) -> ImportedBlob:
    try:
        prefix = pathname.rsplit("/", 1)[0] + "/"
        canonical_source_url = validate_blob_url(
            source_url,
            blob_store.store_host,
            prefix,
        )
        returned_path = unquote(urlsplit(canonical_source_url).path.lstrip("/"))
    except Exception:
        raise RemotePdfImportError("unavailable") from None
    if not (
        isinstance(source_url, str)
        and canonical_source_url == source_url
        and returned_path == pathname
        and isinstance(source_size, int)
        and not isinstance(source_size, bool)
        and 0 < source_size <= max_bytes
        and isinstance(filename, str)
        and 0 < len(filename) <= 90
        and filename == filename.strip(" .")
        and filename.lower().endswith(".pdf")
        and re.fullmatch(r"[A-Za-z0-9._ -]+", filename)
    ):
        raise RemotePdfImportError("unavailable")
    return ImportedBlob(source_url, source_size, filename)


async def acquire_public_pdf_to_blob(
    url: str,
    blob_store: BlobStore,
    pathname: str,
    *,
    max_bytes: int = MAX_INPUT_BYTES,
) -> ImportedBlob:
    """Acquire in one cancellable in-process task with one absolute deadline."""
    if not isinstance(blob_store, BlobStore) or max_bytes != MAX_INPUT_BYTES:
        raise RemotePdfImportError("invalid_link")
    canonical_url = _safe_url(url)
    total_seconds = REMOTE_ACQUISITION_TOTAL_SECONDS
    if (
        not isinstance(total_seconds, (int, float))
        or isinstance(total_seconds, bool)
        or total_seconds <= 0
    ):
        raise RemotePdfImportError("deadline_exceeded")
    deadline_at = time.monotonic() + float(total_seconds)
    try:
        return await asyncio.wait_for(
            _acquire_public_pdf_to_blob_until(
                canonical_url,
                blob_store,
                pathname,
                max_bytes=max_bytes,
                deadline_at=deadline_at,
            ),
            timeout=_remaining(deadline_at, time.monotonic),
        )
    except asyncio.CancelledError:
        raise
    except BlobConflictError:
        raise
    except RemotePdfImportError:
        raise
    except (TimeoutError, asyncio.TimeoutError):
        raise RemotePdfImportError("deadline_exceeded") from None
    except Exception:
        raise RemotePdfImportError("unavailable") from None


def _new_no_executor_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()

    def forbidden_executor(*_args, **_kwargs):
        raise RuntimeError("Remote PDF import forbids executor work")

    loop.run_in_executor = forbidden_executor
    return loop


async def _acquire_and_assert_clean(
    url: str,
    blob_store: BlobStore,
    pathname: str,
    *,
    max_bytes: int,
) -> ImportedBlob:
    try:
        return await acquire_public_pdf_to_blob(
            url,
            blob_store,
            pathname,
            max_bytes=max_bytes,
        )
    finally:
        loop = asyncio.get_running_loop()
        current = asyncio.current_task()
        pending = [
            task
            for task in asyncio.all_tasks(loop)
            if task is not current and not task.done()
        ]
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise RuntimeError("Remote PDF import left pending work")
        if getattr(loop, "_default_executor", None) is not None:
            raise RuntimeError("Remote PDF import created an executor")
        if getattr(loop, "_asyncgens", None):
            raise RuntimeError("Remote PDF import left an async generator")


def acquire_public_pdf_to_blob_sync(
    url: str,
    blob_store: BlobStore,
    pathname: str,
    *,
    max_bytes: int,
) -> ImportedBlob:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        result: ImportedBlob
        with asyncio.Runner(loop_factory=_new_no_executor_loop) as runner:
            result = runner.run(
                _acquire_and_assert_clean(
                    url,
                    blob_store,
                    pathname,
                    max_bytes=max_bytes,
                )
            )
            loop = runner.get_loop()
            if getattr(loop, "_default_executor", None) is not None:
                raise RuntimeError("Remote PDF import created an executor")
        return result
    raise RuntimeError("Remote PDF import cannot run inside an event loop")
