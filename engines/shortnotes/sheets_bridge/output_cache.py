"""Keep a generated notes PDF on disk until its upload is confirmed.

Generation costs money; an upload can fail on a dropped connection. Caching the
result means the retry uploads the same bytes instead of paying to generate
them again. The entry is deleted as soon as the upload succeeds.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import time
from pathlib import Path

DEFAULT_DIR = Path.home() / ".shortnotes_bridge" / "pending"
MAX_AGE_SECONDS = 7 * 24 * 3600
GENERATION_REVISION = "concise-theory-v13-chemistry-bonds-states"


# ``SHEETS_BRIDGE_CACHE`` names the directory, or one of these to switch the
# reuse off entirely - useful when the notes themselves were wrong and every
# lecture must be generated again from scratch.
_OFF = ("off", "none", "no", "0", "false")


def enabled() -> bool:
    return os.environ.get("SHEETS_BRIDGE_CACHE", "").strip().lower() not in _OFF


def cache_dir() -> Path:
    override = os.environ.get("SHEETS_BRIDGE_CACHE", "").strip()
    if not override or override.lower() in _OFF:
        return DEFAULT_DIR
    return Path(override).expanduser()


def key(*, file_id: str, out_name: str, language: str, subject: str, signature: str, header: str = "",
        show_page_numbers: bool = True, category: str = "neet_jee", sanskrit_style: str = "hindi",
        signature_fixed: bool = False) -> str:
    # ``header`` = the page-1 subject/chapter/lecture from the sheet: a fixed
    # cell must re-render, never reuse a PDF that shows the old header.
    # ``signature_fixed``: typed in the Studio, so the slides' teacher never
    # replaced it - not the same PDF as the sheet naming that teacher.
    digest = hashlib.sha256(
        "\x00".join([GENERATION_REVISION, os.environ.get("PW_PROXY_MODEL", ""), file_id, out_name, language, subject, signature, header]
                    + ([] if show_page_numbers else ["page-numbers:off"])
                    + _category_marker(category)
                    + (["sanskrit:strict"] if sanskrit_style == "strict" else [])
                    + (["signature:fixed"] if signature_fixed else [])).encode("utf-8")
    ).hexdigest()
    return f"{digest[:32]}.json"


def _category_marker(category: str) -> list[str]:
    """Nothing for NEET without rules, so the keys that existed before
    categories stay valid; otherwise the category and a digest of all its
    format rules (notesbot/format_rules), so editing any of them never
    reuses older notes."""
    from notesbot import categories

    category = categories.LEGACY.get(category, category)
    fingerprint = categories.fingerprint(category)
    if category == categories.DEFAULT and not fingerprint:
        return []
    return [f"category:{category}:{fingerprint}"]


def path_for(cache_key: str) -> Path:
    return cache_dir() / cache_key


def load(cache_key: str) -> bytes | None:
    """Cached bytes (the model's notes JSON), or None when absent/stale."""
    if not enabled():
        return None
    target = path_for(cache_key)
    try:
        if not target.is_file():
            return None
        if time.time() - target.stat().st_mtime > MAX_AGE_SECONDS:
            target.unlink(missing_ok=True)
            return None
        data = target.read_bytes()
    except OSError:
        return None
    return data or None


def store(cache_key: str, data: bytes) -> bool:
    if not enabled() or not isinstance(data, bytes) or not data:
        return False
    target = path_for(cache_key)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return True
    except OSError:
        return False


def touch(cache_key: str) -> None:
    """Keep the entry alive after a successful upload (its TTL still applies)."""
    target = path_for(cache_key)
    try:
        if target.is_file():
            os.utime(target, None)
    except OSError:
        pass
    approved = path_for(cache_key).with_suffix(".final.json")
    try:
        if approved.is_file():
            os.utime(approved, None)
    except OSError:
        pass


def store_finalized(cache_key: str, notes_json: bytes, pdf: bytes, receipt: dict) -> bool:
    """Cache the exact approved PDF; never regenerate different bytes on retry."""
    from notesbot.final_quality import valid_receipt
    if not valid_receipt(notes_json, pdf, receipt):
        return False
    if not store(cache_key, notes_json):
        return False
    bundle = json.dumps({"pdf": base64.b64encode(pdf).decode("ascii"), "receipt": receipt}).encode("utf-8")
    return store(str(Path(cache_key).with_suffix(".final.json")), bundle)


def load_finalized(cache_key: str, notes_json: bytes):
    from notesbot.final_quality import valid_receipt
    raw = load(str(Path(cache_key).with_suffix(".final.json")))
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        pdf = base64.b64decode(value["pdf"], validate=True)
        receipt = value["receipt"]
        return (pdf, receipt) if valid_receipt(notes_json, pdf, receipt) else None
    except (ValueError, KeyError, TypeError):
        return None


def clear() -> int:
    """Drop every cached lecture. Returns how many were removed."""
    removed = 0
    try:
        for entry in cache_dir().glob("*.json"):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def sweep(max_age_seconds: float = MAX_AGE_SECONDS) -> int:
    """Delete entries older than the TTL. ``load`` only prunes the key it is
    asked for, and a finished row is never asked for again, so without this
    the directory grows for ever. Returns the number removed."""
    try:
        entries = list(cache_dir().iterdir())
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for entry in entries:
        if entry.suffix not in (".json", ".tmp"):
            continue
        try:
            if now - entry.stat().st_mtime > max_age_seconds:
                entry.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def discard(cache_key: str) -> None:
    try:
        path_for(cache_key).unlink(missing_ok=True)
        path_for(cache_key).with_suffix(".final.json").unlink(missing_ok=True)
    except OSError:
        pass
