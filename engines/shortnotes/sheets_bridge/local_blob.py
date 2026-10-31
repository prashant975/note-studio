"""A disk-backed stand-in for Vercel Blob, so the Studio's own upload →
generate → download flow runs on this machine.

It implements the slice of ``notesbot.blob_store.BlobStore`` the job pipeline
uses, with the same semantics the service relies on:

* ``allow_overwrite=False`` collides with ``BlobConflictError`` (immutable
  inputs, one-shot dispatch claim),
* ``if_match`` is a real compare-and-set on the object's ETag,
* missing objects raise ``BlobNotFoundError``,
* URLs keep the ``https://<store>.private.blob.vercel-storage.com/<path>``
  shape the service validates, but never leave this process.

Nothing in ``notesbot`` or ``api`` is modified: the local server swaps this in
at runtime.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

from notesbot.blob_store import (
    BlobConflictError,
    BlobNotFoundError,
    BlobRef,
    JsonBlob,
    _APP_PREFIX,
    _store_key,
    validate_pdf_bytes,
)

DEFAULT_ROOT = Path.home() / ".shortnotes_bridge" / "local-blob"
_LOCK = threading.Lock()


def _replace(tmp, target) -> None:
    """os.replace that tolerates Windows sharing violations.

    A status poll or an antivirus scanner holding the destination open makes
    MoveFileEx fail with 'Access is denied'; the handle is released within
    milliseconds, so a short retry succeeds where a single attempt fails.
    """
    import time

    for attempt in range(10):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.02 * (attempt + 1))


def _etag(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest()[:32] + '"'


class LocalBlobStore:
    """Files under ``root`` addressed by Blob-shaped URLs."""

    def __init__(self, store_id: str, *, root: Path | None = None, allowed_prefix: str = _APP_PREFIX) -> None:
        self.store_key = _store_key(store_id)
        self.store_id = f"store_{self.store_key}"
        self.store_host = f"{self.store_key.lower()}.private.blob.vercel-storage.com"
        self.allowed_prefix = allowed_prefix
        self.root = Path(root) if root else DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    # -- addressing ---------------------------------------------------------
    def url_for(self, pathname: str) -> str:
        return f"https://{self.store_host}/{self._clean(pathname)}"

    def _clean(self, pathname: object) -> str:
        text = str(pathname or "").strip().lstrip("/")
        if not text.startswith(self.allowed_prefix):
            raise ValueError("Blob path outside the app prefix")
        if "\\" in text or any(part in {"", ".", ".."} for part in text.split("/")):
            raise ValueError("Invalid Blob path")
        return text

    def path_of(self, pathname: str) -> Path:
        target = (self.root / self._clean(pathname)).resolve()
        if self.root.resolve() not in target.parents:
            raise ValueError("Invalid Blob path")
        return target

    def _path_from_url(self, url: str) -> Path:
        parts = urlsplit(str(url or ""))
        if parts.scheme != "https" or parts.hostname != self.store_host:
            raise ValueError("Invalid Blob URL")
        return self.path_of(unquote(parts.path.lstrip("/")))

    # -- writes -------------------------------------------------------------
    def _write(self, pathname: str, data: bytes, content_type: str, allow_overwrite: bool, if_match: str | None) -> BlobRef:
        clean = self._clean(pathname)
        target = self.path_of(clean)
        meta = target.with_suffix(target.suffix + ".meta")
        with _LOCK:
            exists = target.is_file()
            if exists and not allow_overwrite and if_match is None:
                raise BlobConflictError("Blob already exists")
            if if_match is not None:
                if not exists:
                    raise BlobConflictError("Blob version mismatch")
                if _etag(target.read_bytes()) != if_match:
                    raise BlobConflictError("Blob version mismatch")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            _replace(tmp, target)
            meta.write_text(json.dumps({"contentType": content_type}), encoding="utf-8")
        return BlobRef(url=self.url_for(clean), pathname=clean, etag=_etag(data))

    def put_bytes(self, pathname: str, data: bytes, content_type: str, allow_overwrite: bool = False, *, if_match: str | None = None) -> BlobRef:
        if not isinstance(data, bytes):
            raise ValueError("Blob data must be bytes")
        return self._write(pathname, data, content_type, allow_overwrite, if_match)

    def put_json(self, pathname: str, value: dict, allow_overwrite: bool = False, *, if_match: str | None = None) -> BlobRef:
        if not isinstance(value, dict):
            raise ValueError("JSON Blob value must be an object")
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return self._write(pathname, data, "application/json", allow_overwrite, if_match)

    # -- reads --------------------------------------------------------------
    def _read(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        target = self._path_from_url(url)
        if not target.is_file():
            raise BlobNotFoundError("Blob not found")
        with _LOCK:
            data = target.read_bytes()
        if len(data) > max_bytes:
            raise ValueError("Blob exceeds the allowed size")
        meta = target.with_suffix(target.suffix + ".meta")
        content_type = "application/octet-stream"
        if meta.is_file():
            try:
                content_type = str(json.loads(meta.read_text(encoding="utf-8")).get("contentType") or content_type)
            except (OSError, ValueError):
                pass
        return data, content_type

    def get_bytes(self, url: str, max_bytes: int) -> bytes:
        return self._read(url, max_bytes)[0]

    def get_pdf(self, url: str, max_bytes: int) -> bytes:
        data, content_type = self._read(url, max_bytes)
        if not content_type.startswith("application/pdf"):
            raise ValueError("Invalid PDF content type")
        validate_pdf_bytes(data)
        return data

    def get_json(self, url: str, max_bytes: int) -> JsonBlob:
        data, content_type = self._read(url, max_bytes)
        if not content_type.startswith("application/json"):
            raise ValueError("Invalid JSON content type")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("Invalid JSON blob") from None
        if not isinstance(value, dict):
            raise ValueError("Invalid JSON blob")
        return JsonBlob(value=value, etag=_etag(data))

    # -- deletes ------------------------------------------------------------
    def delete(self, urls: list[str]) -> None:
        for url in urls or ():
            try:
                target = self._path_from_url(url)
            except ValueError:
                continue
            with _LOCK:
                try:
                    target.unlink(missing_ok=True)
                    target.with_suffix(target.suffix + ".meta").unlink(missing_ok=True)
                except OSError:
                    pass

    # -- housekeeping -------------------------------------------------------
    def clear(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
