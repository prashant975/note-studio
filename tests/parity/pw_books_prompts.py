"""Build the PW Books engine's notes and merge prompts for every subject, note
type, language and exam from one engine folder and print their SHA-256s (JSON).
Run once with the ORIGINAL's folder and once with the COPY's.

    python pw_books_prompts.py <engine root> [extra instructions]

Only reads the engine folder (the caller sets PYTHONDONTWRITEBYTECODE). Its .env
is never loaded.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
extra = sys.argv[2] if len(sys.argv) > 2 else ""
os.environ["HANDWRITTEN_NOTES_ROOT"] = str(root)
os.environ["HANDWRITTEN_NOTES_ENV"] = str(root / "no-such-folder" / ".env")
sys.path.insert(0, str(root))

from src import prompt_builder as pb  # noqa: E402
from src.config import EXAMS, SUBJECTS  # noqa: E402
from src.models import SlideData  # noqa: E402

if extra:
    pb.EXTRA_INSTRUCTIONS = extra            # the copy's hook; the original has none

SLIDES = [SlideData(slide_no=1, heading="Newton's laws", text="F = ma", cleaned_text="F = ma"),
          SlideData(slide_no=2, heading="Friction", text="f = mu N", cleaned_text="f = mu N")]
MARK = "\n\nADDITIONAL REQUIREMENTS FOR THESE NOTES"
prompts = {}
for subject in SUBJECTS:
    for mode in ("summary", "complete"):
        for lang in ("en", "hi"):
            for exam in ("", *EXAMS.values()):
                for strict in (True, False):
                    key = f"{subject}_{mode}_{lang}_{exam or 'none'}_{strict}"
                    try:
                        prompts["gen_" + key] = pb.build_generation_prompt(
                            subject, mode, lang, SLIDES, chunk_label="1/2", exam=exam, strict_math=strict)
                        prompts["merge_" + key] = pb.build_merge_prompt(
                            subject, mode, lang, ["part one", "part two"], exam=exam, strict_math=strict)
                    except FileNotFoundError:
                        prompts["missing_" + key] = ""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


print(json.dumps({name: {"sha256": digest(text), "tail": text[-160:],
                         "before_extra": digest(text[:text.rfind(MARK)] if MARK in text else text)}
                  for name, text in prompts.items()}, ensure_ascii=False))
