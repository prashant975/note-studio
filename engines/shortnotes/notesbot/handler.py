"""Turn an incoming Telegram update into a short-notes reply.

Supports multiple bots from one codebase (config.get_bot):
  * 'main' — fixed signature (by PW Faculty).
  * 'ask'  — asks the teacher whose name goes on the notes; the name is stored
    by PINNING a small config message in the chat (no database needed) and read
    back via getChat().pinned_message on every request.
"""
from __future__ import annotations

import os
import re
import time
import traceback
from collections import OrderedDict, deque

from . import config
from . import telegram_client as tgmod
from .errors import UserError, redact
from .gemini_client import generate_short_notes
from .notes_pdf import build_notes_pdf
from .telegram_client import TG
from .workdir import Workspace

_REF_RE = re.compile(r"ref:(\S+)")
_SIG_PIN_RE = re.compile(r"Notes signature:\s*(.+)")
_NAMEREQ_MARK = "#setname"  # legacy visible marker (old messages still work)
# New-style name questions hide the source ref inside an invisible
# zero-width text link, so the user sees a clean message.
_REF_URL_PREFIX = "https://ref.invalid/"

# Best-effort in-memory dedup of Telegram retries (warm instances).
_SEEN: "OrderedDict[tuple, bool]" = OrderedDict()
_SEEN_MAX = 1000

# --- runaway-billing protection: per-chat rate limit + in-flight lock --------
_RATE: "dict[int, deque]" = {}
_INFLIGHT: "dict[int, float]" = {}
_RATE_MAX = 6            # generations allowed per window per chat
_RATE_WINDOW = 600.0     # 10 minutes
_INFLIGHT_TTL = 240.0    # one generation at a time per chat, max 4 min hold
_TRACK_MAX = 500


def _rate_check(chat_id: int) -> "str | None":
    """Return a user-facing refusal message if this chat must wait."""
    now = time.time()
    started = _INFLIGHT.get(chat_id)
    if started and now - started < _INFLIGHT_TTL:
        return ("⏳ Aapke pichhle notes abhi ban rahe hain — unke aane ke baad "
                "agli PDF bhejiye.")
    dq = _RATE.get(chat_id)
    if dq:
        while dq and now - dq[0] > _RATE_WINDOW:
            dq.popleft()
        if len(dq) >= _RATE_MAX:
            return ("🚦 Limit: 10 minute me max 6 notes ban sakte hain. "
                    "Thodi der baad try kijiye.")
    return None


def _rate_mark(chat_id: int) -> None:
    now = time.time()
    _RATE.setdefault(chat_id, deque()).append(now)
    _INFLIGHT[chat_id] = now
    while len(_RATE) > _TRACK_MAX:
        _RATE.pop(next(iter(_RATE)))
    while len(_INFLIGHT) > _TRACK_MAX:
        _INFLIGHT.pop(next(iter(_INFLIGHT)))


def _welcome(cfg: dict) -> str:
    name_part = (
        "👩‍🏫 Pehli PDF par main poochh lunga ki notes par kiska naam jayega "
        "(baad me /teacher <naam> se kabhi bhi badal sakte hain).\n\n"
        if cfg["ask_name"]
        else ""
    )
    return (
        "👋 Namaste! Main Short Notes bot hoon.\n\n"
        "📄 Apne lecture ki PDF bhejiye — main uske short, crisp handwritten notes "
        "bana ke reply kar dunga (formulas, tables, flow-charts, graphs ke saath).\n\n"
        "📦 20 MB se badi PDF? Use Google Drive pe upload karke uska public LINK "
        "bhej dijiye.\n\n"
        f"{name_part}"
        "✏️ Changes chahiye? Notes ya apni PDF/link par REPLY karke likhiye kya "
        "badalna hai.\n\n"
        "⚡ Powered by Gemini"
        + (f" • {cfg['signature']}" if cfg["signature"] else "")
    )


def _is_duplicate(bot_key: str, update_id) -> bool:
    if update_id is None:
        return False
    key = (bot_key, update_id)
    if key in _SEEN:
        return True
    _SEEN[key] = True
    if len(_SEEN) > _SEEN_MAX:
        _SEEN.popitem(last=False)
    return False


def _is_pdf(document: dict) -> bool:
    if not document:
        return False
    if (document.get("mime_type") or "") == "application/pdf":
        return True
    return (document.get("file_name") or "").lower().endswith(".pdf")


def _safe_token(text: str, fallback: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_")
    return token[:40] or fallback


def _filename(notes) -> str:
    subj = _safe_token(notes.subject, "Chemistry")
    tag = _safe_token(notes.lecture_no or notes.title, "Notes")
    return f"{subj}_{tag} - Concise Notes.pdf"


def _normalize_signature(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip().strip('"').strip("'"))[:48]
    if not name:
        return ""
    return name if name.lower().startswith("by ") else f"by {name}"


def _fetch_pdf(tg: TG, source: str, workdir: str) -> bytes:
    if source.startswith("http://") or source.startswith("https://"):
        # Big link downloads stream to the per-update temp dir (auto-deleted).
        dest = os.path.join(workdir, "lecture.pdf")
        return tgmod.download_from_url(source, config.MAX_PDF_BYTES, dest_path=dest)
    return tg.download_file(source)  # Telegram file_id (<= 20 MB)


def _caption(notes, source: str, signature: str) -> str:
    lines = [f"📒 {notes.subject} — Short Notes"]
    if notes.lecture_no:
        lines.append(f"📚 {notes.lecture_no}")
    lines.append(f"✍️ {signature}")
    lines.append("")
    lines.append("✏️ Change chahiye? Is notes ya apni PDF par REPLY karke likhiye.")
    # Stateless edit marker (must survive Telegram's 1024-char caption limit).
    ref = f"ref:{source}"
    head = "\n".join(lines)
    budget = 1024 - len(ref) - 1
    if len(head) > budget:
        head = head[: max(0, budget - 1)] + "…"
    return head + "\n" + ref


# --- per-chat teacher-name storage (pinned config message) -------------------
def _get_chat_signature(tg: TG, chat_id: int) -> str:
    chat = tg.get_chat(chat_id)
    pinned = (chat.get("pinned_message") or {}).get("text") or ""
    m = _SIG_PIN_RE.search(pinned)
    return m.group(1).strip() if m else ""


def _set_chat_signature(tg: TG, chat_id: int, signature: str) -> None:
    msg = tg.send_message(
        chat_id,
        f"✍️ Notes signature: {signature}\n(Badalne ke liye: /teacher <naya naam>)",
    )
    mid = msg.get("message_id")
    if mid:
        tg.pin_message(chat_id, mid)


def _ask_teacher_name(tg: TG, chat_id: int, reply_to: int, source: str) -> None:
    from urllib.parse import quote

    hidden_ref = f'<a href="{_REF_URL_PREFIX}{quote(source, safe="")}">​</a>'
    tg.send_message(
        chat_id,
        "👩‍🏫 Notes par kiska naam jayega?\n\n"
        "Is message par REPLY karke sirf naam likhiye (jaise: Meera ma'am)."
        + hidden_ref,
        reply_to=reply_to,
        html=True,
    )


def _extract_hidden_ref(reply: dict) -> "str | None":
    """Pull the source ref out of the invisible link in our name-question."""
    from urllib.parse import unquote

    entities = (reply.get("entities") or []) + (reply.get("caption_entities") or [])
    for ent in entities:
        url = ent.get("url") or ""
        if ent.get("type") == "text_link" and url.startswith(_REF_URL_PREFIX):
            return unquote(url[len(_REF_URL_PREFIX):])
    return None


# --- generation ---------------------------------------------------------------
def _generate_and_reply(tg: TG, chat_id: int, reply_to: int, source: str,
                        signature: str, edit_instruction: str = "",
                        watermark: bool = True) -> None:
    # Billing guard: one generation at a time per chat + windowed rate limit.
    refusal = _rate_check(chat_id)
    if refusal:
        try:
            tg.send_message(chat_id, refusal, reply_to=reply_to)
        except Exception:  # noqa: BLE001
            pass
        return
    _rate_mark(chat_id)

    note = "⏳ Notes ban rahe hain… AI PDF padh raha hai."
    if edit_instruction:
        note = "⏳ Changes apply ho rahe hain… updated notes bana raha hoon."
    progress_id = None
    try:
        try:
            progress = tg.send_message(chat_id, note, reply_to=reply_to)
            progress_id = progress.get("message_id")
        except Exception:  # noqa: BLE001 - progress message is best-effort only
            print(redact(traceback.format_exc()))
        try:
            tg.send_chat_action(chat_id, "upload_document")
            with Workspace() as workdir:
                pdf_bytes = _fetch_pdf(tg, source, workdir)
                notes = generate_short_notes(pdf_bytes, edit_instruction)
                del pdf_bytes  # release the (possibly ~100 MB) buffer early
                out_pdf = build_notes_pdf(notes, signature,
                                          watermark=watermark)
                tg.send_document(
                    chat_id, out_pdf, _filename(notes),
                    caption=_caption(notes, source, signature), reply_to=reply_to,
                )
            if progress_id:
                tg.delete_message(chat_id, progress_id)
        except Exception as exc:  # noqa: BLE001 - friendly, non-leaky message only
            print(redact(traceback.format_exc()))
            if isinstance(exc, UserError):
                msg = f"⚠️ {exc}"
            else:
                msg = (
                    "❌ Notes nahi ban paaye. Thodi der baad dobara try kijiye, ya "
                    "PDF check karke phir se bhejiye."
                )
            if progress_id:
                tg.edit_message(chat_id, progress_id, msg)
            else:
                tg.send_message(chat_id, msg, reply_to=reply_to)
    finally:
        _INFLIGHT.pop(chat_id, None)


def process_update(update: dict, bot_key: str = "main") -> None:
    """Entry point: handle one Telegram update (best-effort, never raises)."""
    if _is_duplicate(bot_key, update.get("update_id")):
        return
    cfg = config.get_bot(bot_key)
    if not cfg["token"]:
        return
    tg = TG(cfg["token"])

    message = update.get("message") or update.get("channel_post")
    if not isinstance(message, dict):
        return
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return
    message_id = message.get("message_id")
    text = (message.get("text") or message.get("caption") or "").strip()

    # --- commands ---
    if text.startswith("/start") or text.startswith("/help"):
        tg.send_message(chat_id, _welcome(cfg))
        return
    if cfg["ask_name"] and (text.startswith("/teacher") or text.startswith("/name")):
        name = _normalize_signature(text.split(maxsplit=1)[1] if " " in text else "")
        if not name:
            tg.send_message(
                chat_id, "✍️ Aise likhiye:  /teacher PW Faculty", reply_to=message_id
            )
            return
        _set_chat_signature(tg, chat_id, name)
        tg.send_message(
            chat_id, f"✅ Ab notes par jayega: “{name}”", reply_to=message_id
        )
        return

    def resolve_signature() -> str:
        if not cfg["ask_name"]:
            return cfg["signature"]
        return _get_chat_signature(tg, chat_id)

    document = message.get("document")
    reply = message.get("reply_to_message") or {}

    # --- Case 1: a fresh PDF document -> generate (or first ask the name) ---
    if document:
        if not _is_pdf(document):
            tg.send_message(chat_id, "📄 Please ek PDF file bhejiye (lecture notes).",
                            reply_to=message_id)
            return
        if (document.get("file_size") or 0) > config.TELEGRAM_DOWNLOAD_MAX_BYTES:
            tg.send_message(
                chat_id,
                "📦 Ye PDF 20 MB se badi hai — Telegram itni badi file bot ko direct "
                "download nahi karne deta.\n\n"
                "👉 Ise Google Drive pe upload karke, Share → \"Anyone with the link\" "
                "karke uska link bhej dijiye.",
                reply_to=message_id,
            )
            return
        sig = resolve_signature()
        if cfg["ask_name"] and not sig:
            _ask_teacher_name(tg, chat_id, message_id, document["file_id"])
            return
        extra = text if text and not text.startswith("/") else ""
        _generate_and_reply(tg, chat_id, message_id, document["file_id"], sig,
                            extra, watermark=cfg["watermark"])
        return

    # --- Case 2: a reply with text ---
    if reply and text:
        replied_text = (reply.get("text") or reply.get("caption") or "")
        match = _REF_RE.search(replied_text)

        # 2a: reply to OUR "whose name?" question -> set name, then generate.
        name_ref = None
        if cfg["ask_name"]:
            name_ref = _extract_hidden_ref(reply)  # new invisible-link format
            if not name_ref and _NAMEREQ_MARK in replied_text and match:
                name_ref = match.group(1)  # legacy visible-marker format
        if name_ref:
            sig = _normalize_signature(text)
            if not sig:
                tg.send_message(chat_id, "✍️ Sirf naam likhiye, jaise: Meera ma'am",
                                reply_to=message_id)
                return
            _set_chat_signature(tg, chat_id, sig)
            _generate_and_reply(tg, chat_id, message_id, name_ref, sig,
                                watermark=cfg["watermark"])
            return

        # 2b: edit/regeneration request (reply to our notes, their PDF, or a link).
        source = None
        if match:
            source = match.group(1)  # our notes caption -> original source
        elif reply.get("document") and _is_pdf(reply["document"]):
            source = reply["document"]["file_id"]
        else:
            source = tgmod.find_url(reply.get("text") or "")
        if source:
            sig = resolve_signature()
            if cfg["ask_name"] and not sig:
                _ask_teacher_name(tg, chat_id, message_id, source)
                return
            _generate_and_reply(tg, chat_id, message_id, source, sig, text,
                                watermark=cfg["watermark"])
            return
        tg.send_message(
            chat_id,
            "✏️ Edit ke liye apni PDF/link (ya mere bheje notes) par reply karke "
            "change likhiye.",
            reply_to=message_id,
        )
        return

    # --- Case 3: a fresh message containing a link ---
    url = tgmod.find_url(text)
    if url:
        sig = resolve_signature()
        if cfg["ask_name"] and not sig:
            _ask_teacher_name(tg, chat_id, message_id, url)
            return
        _generate_and_reply(tg, chat_id, message_id, url, sig, "",
                            watermark=cfg["watermark"])
        return

    # --- Fallback ---
    tg.send_message(chat_id, _welcome(cfg))
