"""Drive v3 client: metadata, download, duplicate check, upload.

Every call passes ``supportsAllDrives=True`` and every list call also passes
``includeItemsFromAllDrives=True``. Without these, files on a Shared Drive
return 404 as if they did not exist - a silent failure that looks like a
permissions bug.
"""
from __future__ import annotations

import io
import socket
import ssl
import time
from dataclasses import dataclass, field

from .errors import BridgeError

_RETRIES = 3
_TRANSFER_ATTEMPTS = 4          # whole-transfer attempts on a broken connection
_SIMPLE_UPLOAD_LIMIT = 5 * 1024 * 1024   # below this, one multipart request


def _is_transient(exc: BaseException) -> bool:
    """Connection-level failures worth repeating the whole transfer for.

    Windows aborts long-lived upload sockets with WinError 10053
    (ConnectionAbortedError); proxies and Drive itself drop them too.
    """
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, ssl.SSLError, OSError)):
        return True
    status = _http_status(exc)
    return status == 429 or (status is not None and 500 <= status < 600)
_FILE_FIELDS = "id,name,mimeType,size,parents,driveId,webViewLink,shortcutDetails,md5Checksum,modifiedTime,properties"
_LIST_FIELDS = "id,name,mimeType,size,parents,driveId,webViewLink,md5Checksum,modifiedTime,properties"
_GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
_FOLDER_MIME = "application/vnd.google-apps.folder"


def escape_query_value(text: str) -> str:
    """Escape a value for use inside single quotes in a Drive ``q`` string.

    Backslashes first, then apostrophes - source names are Devanagari and can
    contain ' (e.g. L'Hospital) which would otherwise break the query.
    """
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _http_status(exc: BaseException) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def classify_http_error(exc: BaseException, stage: str) -> BridgeError:
    status = _http_status(exc)
    if status == 404:
        return BridgeError("drive_not_found")
    if status == 403:
        return BridgeError("drive_forbidden")
    if status == 401:
        return BridgeError("drive_unauthorized")
    if status == 429 or (status is not None and status >= 500):
        return BridgeError(f"{stage}:drive_unavailable")
    return BridgeError(f"{stage}:{type(exc).__name__}")


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int | None
    parents: tuple = field(default_factory=tuple)
    drive_id: str = ""
    web_view_link: str = ""
    # Content checksum and last change, used to tell duplicate uploads of one
    # lecture apart from genuinely different files. Empty when Drive has none.
    md5: str = ""
    modified_time: str = ""
    # Drive's public key/value ``properties``: the automation stamps the notes
    # it files with the source they were made from (see runner, catalog rows).
    properties: tuple = field(default_factory=tuple)

    def prop(self, name: str) -> str:
        return next((value for key, value in self.properties if key == name), "")

    @property
    def is_google_native(self) -> bool:
        return self.mime_type.startswith(_GOOGLE_NATIVE_PREFIX)

    @property
    def is_folder(self) -> bool:
        return self.mime_type == _FOLDER_MIME


def _properties(raw: dict) -> tuple:
    value = raw.get("properties") or {}
    return tuple(sorted((str(k), str(v)) for k, v in value.items())) if isinstance(value, dict) else ()


def drive_file(raw: dict, default_parent: str = "", drive_id: str = "") -> DriveFile:
    """A files.* API resource -> DriveFile."""
    size_text = raw.get("size")
    size = int(size_text) if isinstance(size_text, (str, int)) and str(size_text).isdigit() else None
    return DriveFile(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        mime_type=str(raw.get("mimeType", "")),
        size=size,
        parents=tuple(raw.get("parents") or ((default_parent,) if default_parent else ())),
        drive_id=str(raw.get("driveId") or drive_id or ""),
        web_view_link=str(raw.get("webViewLink") or ""),
        md5=str(raw.get("md5Checksum") or ""),
        modified_time=str(raw.get("modifiedTime") or ""),
        properties=_properties(raw),
    )


class DriveClient:
    def __init__(self, service) -> None:
        self.service = service

    # -- metadata -----------------------------------------------------------
    def get_file(self, file_id: str) -> DriveFile:
        """files.get with the ``parents`` field; follows one level of shortcut."""
        try:
            raw = (
                self.service.files()
                .get(fileId=file_id, fields=_FILE_FIELDS, supportsAllDrives=True)
                .execute(num_retries=_RETRIES)
            )
        except BridgeError:
            raise
        except Exception as exc:  # googleapiclient.errors.HttpError etc.
            raise classify_http_error(exc, "drive_get") from None
        if raw.get("mimeType") == _SHORTCUT_MIME:
            target = (raw.get("shortcutDetails") or {}).get("targetId")
            if not target:
                raise BridgeError("drive_shortcut_unresolvable")
            try:
                raw = (
                    self.service.files()
                    .get(fileId=target, fields=_FILE_FIELDS, supportsAllDrives=True)
                    .execute(num_retries=_RETRIES)
                )
            except Exception as exc:
                raise classify_http_error(exc, "drive_get") from None
        size_text = raw.get("size")
        size = int(size_text) if isinstance(size_text, (str, int)) and str(size_text).isdigit() else None
        return DriveFile(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            mime_type=str(raw.get("mimeType", "")),
            size=size,
            parents=tuple(raw.get("parents") or ()),
            drive_id=str(raw.get("driveId") or ""),
            web_view_link=str(raw.get("webViewLink") or ""),
            properties=_properties(raw),
        )

    # -- download -----------------------------------------------------------
    def download_pdf(self, file: DriveFile, max_bytes: int) -> bytes:
        if file.is_google_native:
            raise BridgeError("not_pdf")
        if file.size is not None and file.size > max_bytes:
            raise BridgeError("too_large")
        from googleapiclient.http import MediaIoBaseDownload

        last: BaseException | None = None
        for attempt in range(_TRANSFER_ATTEMPTS):
            buffer = io.BytesIO()
            try:
                request = self.service.files().get_media(fileId=file.id, supportsAllDrives=True)
                downloader = MediaIoBaseDownload(buffer, request, chunksize=4 * 1024 * 1024)
                done = False
                while not done:
                    _status, done = downloader.next_chunk(num_retries=_RETRIES)
                    if buffer.tell() > max_bytes:
                        raise BridgeError("too_large")
            except BridgeError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not _is_transient(exc) or attempt == _TRANSFER_ATTEMPTS - 1:
                    raise classify_http_error(exc, "download") from None
                time.sleep(2.0 * (attempt + 1))
                continue
            data = buffer.getvalue()
            if not data.startswith(b"%PDF"):
                raise BridgeError("not_pdf")
            return data
        raise classify_http_error(last or RuntimeError("download failed"), "download")

    # -- duplicate check ----------------------------------------------------
    def find_in_folder(self, folder_id: str, name: str, drive_id: str = "") -> list[dict]:
        """Non-trashed files named exactly ``name`` inside ``folder_id``."""
        query = (
            f"name = '{escape_query_value(name)}' and "
            f"'{escape_query_value(folder_id)}' in parents and trashed = false"
        )
        params = {
            "q": query,
            "fields": "files(id,name,webViewLink)",
            "pageSize": 10,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if drive_id:
            params["corpora"] = "drive"
            params["driveId"] = drive_id
        else:
            params["corpora"] = "allDrives"
        try:
            result = self.service.files().list(**params).execute(num_retries=_RETRIES)
        except Exception as exc:
            raise classify_http_error(exc, "drive_list") from None
        return list(result.get("files", []))

    def list_folder(self, folder_id: str, drive_id: str = "") -> list[DriveFile]:
        """All non-trashed children of a folder (paginated), as DriveFile."""
        params = {
            "q": f"'{escape_query_value(folder_id)}' in parents and trashed = false",
            "fields": f"nextPageToken,files({_LIST_FIELDS})",
            "pageSize": 200,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if drive_id:
            params["corpora"] = "drive"
            params["driveId"] = drive_id
        else:
            params["corpora"] = "allDrives"
        files: list[DriveFile] = []
        token = None
        try:
            while True:
                if token:
                    params["pageToken"] = token
                result = self.service.files().list(**params).execute(num_retries=_RETRIES)
                for raw in result.get("files", []):
                    size_text = raw.get("size")
                    size = int(size_text) if isinstance(size_text, (str, int)) and str(size_text).isdigit() else None
                    files.append(
                        DriveFile(
                            id=str(raw.get("id", "")),
                            name=str(raw.get("name", "")),
                            mime_type=str(raw.get("mimeType", "")),
                            size=size,
                            parents=tuple(raw.get("parents") or (folder_id,)),
                            drive_id=str(raw.get("driveId") or drive_id or ""),
                            web_view_link=str(raw.get("webViewLink") or ""),
                            md5=str(raw.get("md5Checksum") or ""),
                            modified_time=str(raw.get("modifiedTime") or ""),
                            properties=_properties(raw),
                        )
                    )
                token = result.get("nextPageToken")
                if not token:
                    break
        except Exception as exc:
            raise classify_http_error(exc, "drive_list") from None
        return files

    def upload_doc(self, folder_id: str, name: str, docx_bytes: bytes, drive_id: str = "") -> dict:
        """Create an editable **Google Doc** from a .docx, converted by Drive."""
        from googleapiclient.http import MediaIoBaseUpload

        from .notes_doc import DOCX_MIME, GOOGLE_DOC_MIME

        body = {"name": name, "parents": [folder_id], "mimeType": GOOGLE_DOC_MIME}
        last: BaseException | None = None
        for attempt in range(_TRANSFER_ATTEMPTS):
            if attempt:
                existing = self.find_in_folder(folder_id, name, drive_id)
                if existing:
                    return existing[0]
            media = MediaIoBaseUpload(io.BytesIO(docx_bytes), mimetype=DOCX_MIME, resumable=False)
            try:
                created = (
                    self.service.files()
                    .create(body=body, media_body=media, fields="id,name,webViewLink,mimeType", supportsAllDrives=True)
                    .execute(num_retries=_RETRIES)
                )
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not _is_transient(exc) or attempt == _TRANSFER_ATTEMPTS - 1:
                    raise classify_http_error(exc, "upload_doc") from None
                time.sleep(2.0 * (attempt + 1))
                continue
            if not created.get("id"):
                raise BridgeError("upload_doc:missing_id")
            return created
        raise classify_http_error(last or RuntimeError("doc upload failed"), "upload_doc")

    # -- upload -------------------------------------------------------------
    def upload_pdf(self, folder_id: str, name: str, data: bytes, drive_id: str = "", *,
                   properties: dict | None = None) -> dict:
        """Create the PDF in ``folder_id``, repeating a transfer whose
        connection was aborted.

        Before each repeat the folder is re-checked: if the previous attempt
        actually landed the file, that existing file is returned instead of
        creating a duplicate.
        """
        from googleapiclient.http import MediaIoBaseUpload

        body = {"name": name, "parents": [folder_id], "mimeType": "application/pdf"}
        if properties:
            body["properties"] = dict(properties)
        last: BaseException | None = None
        for attempt in range(_TRANSFER_ATTEMPTS):
            if attempt:
                existing = self.find_in_folder(folder_id, name, drive_id)
                if existing:
                    return existing[0]
            resumable = len(data) > _SIMPLE_UPLOAD_LIMIT
            media = MediaIoBaseUpload(
                io.BytesIO(data),
                mimetype="application/pdf",
                chunksize=4 * 1024 * 1024 if resumable else -1,
                resumable=resumable,
            )
            try:
                created = (
                    self.service.files()
                    .create(body=body, media_body=media, fields="id,name,webViewLink", supportsAllDrives=True)
                    .execute(num_retries=_RETRIES)
                )
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not _is_transient(exc) or attempt == _TRANSFER_ATTEMPTS - 1:
                    raise classify_http_error(exc, "upload") from None
                time.sleep(2.0 * (attempt + 1))
                continue
            if not created.get("id"):
                raise BridgeError("upload:missing_id")
            return created
        raise classify_http_error(last or RuntimeError("upload failed"), "upload")

    def update_pdf(self, file_id: str, data: bytes, *, name: str = "",
                   properties: dict | None = None) -> dict:
        """Replace an existing PDF's content: a new version of the same file,
        so its link and sharing stay as they are (Drive keeps the old version
        in the file's history). Repeating the same content is harmless, so a
        dropped connection is simply tried again."""
        from googleapiclient.http import MediaIoBaseUpload

        body: dict = {}
        if name:
            body["name"] = name
        if properties:
            body["properties"] = dict(properties)
        last: BaseException | None = None
        for attempt in range(_TRANSFER_ATTEMPTS):
            resumable = len(data) > _SIMPLE_UPLOAD_LIMIT
            media = MediaIoBaseUpload(
                io.BytesIO(data),
                mimetype="application/pdf",
                chunksize=4 * 1024 * 1024 if resumable else -1,
                resumable=resumable,
            )
            try:
                updated = (
                    self.service.files()
                    .update(fileId=file_id, body=body, media_body=media, fields="id,name,webViewLink",
                            supportsAllDrives=True)
                    .execute(num_retries=_RETRIES)
                )
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not _is_transient(exc) or attempt == _TRANSFER_ATTEMPTS - 1:
                    raise classify_http_error(exc, "upload") from None
                time.sleep(2.0 * (attempt + 1))
                continue
            if not updated.get("id"):
                raise BridgeError("upload:missing_id")
            return updated
        raise classify_http_error(last or RuntimeError("upload failed"), "upload")

    # -- folders and copies (catalog sheets file their notes in a tree) ---------
    def _list(self, query: str, drive_id: str = "", page_size: int = 50) -> list[DriveFile]:
        params = {
            "q": query,
            "fields": f"files({_LIST_FIELDS})",
            "pageSize": page_size,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if drive_id:
            params["corpora"] = "drive"
            params["driveId"] = drive_id
        else:
            params["corpora"] = "allDrives"
        try:
            result = self.service.files().list(**params).execute(num_retries=_RETRIES)
        except Exception as exc:
            raise classify_http_error(exc, "drive_list") from None
        return [drive_file(raw, drive_id=drive_id) for raw in result.get("files", [])]

    def folders_named(self, parent_id: str, name: str, drive_id: str = "") -> list[DriveFile]:
        """Non-trashed folders called exactly ``name`` inside ``parent_id``."""
        return self._list(
            f"name = '{escape_query_value(name)}' and '{escape_query_value(parent_id)}' in parents "
            f"and mimeType = '{_FOLDER_MIME}' and trashed = false",
            drive_id,
        )

    def create_folder(self, parent_id: str, name: str, drive_id: str = "") -> DriveFile:
        """A new folder ``name`` inside ``parent_id``.

        A repeat after a dropped connection first looks for the folder the
        previous attempt may already have made, so a flaky network never
        leaves two folders of one name.
        """
        body = {"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]}
        last: BaseException | None = None
        for attempt in range(_TRANSFER_ATTEMPTS):
            if attempt:
                existing = self.folders_named(parent_id, name, drive_id)
                if existing:
                    return existing[0]
            try:
                raw = (
                    self.service.files()
                    .create(body=body, fields=_LIST_FIELDS, supportsAllDrives=True)
                    .execute(num_retries=_RETRIES)
                )
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not _is_transient(exc) or attempt == _TRANSFER_ATTEMPTS - 1:
                    raise classify_http_error(exc, "create_folder") from None
                time.sleep(2.0 * (attempt + 1))
                continue
            if not raw.get("id"):
                raise BridgeError("create_folder:missing_id")
            return drive_file(raw, parent_id, drive_id)
        raise classify_http_error(last or RuntimeError("folder creation failed"), "create_folder")

    def copy_file(self, file_id: str, folder_id: str, name: str, drive_id: str = "", *,
                  properties: dict | None = None) -> dict:
        """Copy an existing file (notes already made for the same lecture PDF)
        into ``folder_id`` as ``name``; a repeat adopts a copy that landed."""
        body: dict = {"name": name, "parents": [folder_id]}
        if properties:
            body["properties"] = dict(properties)
        last: BaseException | None = None
        for attempt in range(_TRANSFER_ATTEMPTS):
            if attempt:
                existing = self.find_in_folder(folder_id, name, drive_id)
                if existing:
                    return existing[0]
            try:
                created = (
                    self.service.files()
                    .copy(fileId=file_id, body=body, fields="id,name,webViewLink", supportsAllDrives=True)
                    .execute(num_retries=_RETRIES)
                )
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not _is_transient(exc) or attempt == _TRANSFER_ATTEMPTS - 1:
                    raise classify_http_error(exc, "copy") from None
                time.sleep(2.0 * (attempt + 1))
                continue
            if not created.get("id"):
                raise BridgeError("copy:missing_id")
            return created
        raise classify_http_error(last or RuntimeError("copy failed"), "copy")

    def find_by_property(self, key: str, value: str, drive_id: str = "") -> list[DriveFile]:
        """Non-trashed files carrying ``properties[key] == value`` (a PDF the
        automation already made from the same source, filed elsewhere)."""
        return self._list(
            f"properties has {{ key='{escape_query_value(key)}' and value='{escape_query_value(value)}' }} "
            "and trashed = false",
            drive_id,
            page_size=10,
        )
