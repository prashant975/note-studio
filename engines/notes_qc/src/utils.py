from __future__ import annotations

import json
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Iterable


def make_run_id(prefix: str = "run") -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"


def safe_name(name: str, default: str = "file") -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or default


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


#: Formats that are already compressed. Running DEFLATE over them again costs
#: real time and saves nothing: a 92-page PDF lecture's run folder is 245 MB of
#: slide PNGs, and compressing them took 8.5 s to shrink the archive by 0.4%.
_ALREADY_COMPRESSED = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".zip", ".docx", ".xlsx", ".pptx", ".pdf",
    ".ogg", ".opus", ".mp3", ".m4a", ".mp4", ".webm",
})


def zip_dir(src_dir: Path, zip_path: Path) -> Path:
    """Zip a run folder, storing (not deflating) files that cannot compress.

    Text — the notes, the prompts, the JSON logs — still compresses well and is
    what makes the archive worth having, so it keeps DEFLATE.
    """
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src_dir.rglob("*"):
            if not p.is_file():
                continue
            method = (zipfile.ZIP_STORED if p.suffix.lower() in _ALREADY_COMPRESSED
                      else zipfile.ZIP_DEFLATED)
            zf.write(p, p.relative_to(src_dir), compress_type=method)
    return zip_path


def copy_input(input_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / safe_name(input_path.name)
    shutil.copy2(input_path, dst)
    return dst


def image_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def chunked(items: list, size: int) -> Iterable[list]:
    if size <= 0:
        size = len(items) or 1
    for i in range(0, len(items), size):
        yield items[i:i + size]


def first_nonempty_line(text: str, max_len: int = 120) -> str:
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if len(s) <= max_len:
            return s
        return s[:max_len].rstrip() + "..."
    return ""
