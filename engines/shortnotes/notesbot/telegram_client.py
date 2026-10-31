"""Minimal Telegram Bot API client (plain HTTPS calls, no heavy framework).

`TG` is a per-bot client (the project can run multiple bots off one codebase).
Module-level helpers handle link downloads, which need no bot token.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import requests

from . import config
from .errors import UserError

_TIMEOUT = 60
_DL_TIMEOUT = 300  # link downloads of big PDFs can take a while

# A bare URL anywhere in the message text (used to accept a PDF link).
URL_RE = re.compile(r"https?://[^\s]+")
_DRIVE_ID_RE = re.compile(
    r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=download&)?id=)([\w-]+)"
)


class TG:
    """Telegram Bot API client bound to one bot token."""

    def __init__(self, token: str):
        self.api = f"https://api.telegram.org/bot{token}"
        self.file_api = f"https://api.telegram.org/file/bot{token}"

    def _call(self, method: str, **params: Any) -> dict:
        resp = requests.post(f"{self.api}/{method}", json=params, timeout=_TIMEOUT)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data}")
        return data["result"]

    def send_message(self, chat_id: int, text: str,
                     reply_to: Optional[int] = None, html: bool = False) -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": reply_to,
            "allow_sending_without_reply": True,
        }
        if html:
            params["parse_mode"] = "HTML"
            params["disable_web_page_preview"] = True
        return self._call(
            "sendMessage", **{k: v for k, v in params.items() if v is not None}
        )

    def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self._call("editMessageText", chat_id=chat_id,
                       message_id=message_id, text=text)
        except RuntimeError:
            pass  # best-effort

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self._call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except RuntimeError:
            pass

    def send_chat_action(self, chat_id: int, action: str = "upload_document") -> None:
        try:
            self._call("sendChatAction", chat_id=chat_id, action=action)
        except RuntimeError:
            pass

    def get_chat(self, chat_id: int) -> dict:
        try:
            return self._call("getChat", chat_id=chat_id)
        except RuntimeError:
            return {}

    def pin_message(self, chat_id: int, message_id: int) -> None:
        try:
            self._call("pinChatMessage", chat_id=chat_id,
                       message_id=message_id, disable_notification=True)
        except RuntimeError:
            pass  # best-effort; name still works for this generation

    def download_file(self, file_id: str) -> bytes:
        """Resolve a Telegram file_id to bytes (bot API allows up to 20 MB)."""
        meta = self._call("getFile", file_id=file_id)
        resp = requests.get(f"{self.file_api}/{meta['file_path']}",
                            timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.content

    def send_document(self, chat_id: int, file_bytes: bytes, filename: str,
                      caption: str = "", reply_to: Optional[int] = None) -> dict:
        files = {"document": (filename, file_bytes, "application/pdf")}
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        if reply_to is not None:
            data["reply_to_message_id"] = str(reply_to)
            data["allow_sending_without_reply"] = "true"
        resp = requests.post(f"{self.api}/sendDocument", data=data,
                             files=files, timeout=_TIMEOUT)
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram sendDocument failed: {payload}")
        return payload["result"]


# --- Link / URL downloads (for PDFs bigger than Telegram's 20 MB cap) -------
def find_url(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(").,]") if m else None


def _read_stream(resp: requests.Response, max_bytes: int, sink=None) -> bytes:
    """Read a response fully; into RAM, or (when sink is given) onto disk."""
    resp.raise_for_status()
    total = 0
    out = bytearray() if sink is None else None
    for chunk in resp.iter_content(chunk_size=1 << 16):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise UserError(
                f"PDF {config.MAX_PDF_MB} MB se badi hai. Kripya chhoti file bhejiye."
            )
        if sink is None:
            out.extend(chunk)
        else:
            sink.write(chunk)
    return bytes(out) if sink is None else b""


def _download_drive(session: requests.Session, file_id: str, max_bytes: int,
                    sink=None) -> bytes:
    base = "https://drive.google.com/uc?export=download"
    resp = session.get(base, params={"id": file_id}, stream=True, timeout=_DL_TIMEOUT)
    if "text/html" in resp.headers.get("content-type", ""):
        # Large files return a virus-scan confirmation page; resend with the token.
        token = next(
            (v for k, v in resp.cookies.items() if k.startswith("download_warning")),
            None,
        )
        m = re.search(r'name="confirm"\s+value="([^"]+)"', resp.text)
        confirm_val = token or (m.group(1) if m else "t")
        uuid_m = re.search(r'name="uuid"\s+value="([^"]+)"', resp.text)
        params = {"id": file_id, "export": "download", "confirm": confirm_val}
        if uuid_m:
            params["uuid"] = uuid_m.group(1)
        resp = session.get(
            "https://drive.usercontent.google.com/download",
            params=params, stream=True, timeout=_DL_TIMEOUT,
        )
    return _read_stream(resp, max_bytes, sink)


def download_from_url(url: str, max_bytes: int, dest_path: "str | None" = None) -> bytes:
    """Download a PDF from a public link (handles Google Drive share links)."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ChemNotesBot/1.0)"})
    drive = _DRIVE_ID_RE.search(url)
    sink = open(dest_path, "wb") if dest_path else None
    try:
        data = (
            _download_drive(session, drive.group(1), max_bytes, sink)
            if drive
            else _read_stream(
                session.get(url, stream=True, timeout=_DL_TIMEOUT), max_bytes, sink
            )
        )
    except UserError:
        raise
    except requests.RequestException as exc:
        raise UserError(
            "Link se PDF download nahi hui. Link sahi + public (anyone with link) hona chahiye."
        ) from exc
    finally:
        if sink is not None:
            sink.close()
    if dest_path:
        with open(dest_path, "rb") as fh:
            data = fh.read()
    if data[:5] != b"%PDF-":
        raise UserError(
            "Link se PDF nahi mili. Link public PDF ka hona chahiye (Drive me 'Anyone "
            "with the link' set karein)."
        )
    return data
