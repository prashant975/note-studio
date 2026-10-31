"""Build the UPSC engine's four notes prompts from one engine folder and print
their SHA-256s (JSON). Run once with the ORIGINAL's folder and once with the
COPY's: two packages named `src` cannot share one process.

    python upsc_prompts.py <engine root> [extra instructions]

Only reads the engine folder (bytecode writing is off; the caller sets
PYTHONDONTWRITEBYTECODE). Its .env is never loaded.
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
from src.models import SlideData  # noqa: E402

if extra:
    pb.EXTRA_INSTRUCTIONS = extra            # the copy's hook; the original has none

TRANSCRIPT = ("Namaskar students, aaj hum Fundamental Rights, Article 21 aur Article 32 par baat karenge. "
              "Writs paanch types ke hote hain: habeas corpus, mandamus, prohibition, certiorari, quo warranto.")
SLIDES = [SlideData(slide_no=1, heading="Writs", text="Article 32 and Article 226", cleaned_text="Article 32 and 226")]
prompts = {}
for lang in ("en", "hi", "mr"):
    prompts[f"dcn_{lang}"] = pb.build_upsc_dcn_prompt(
        subject="polity", transcript=TRANSCRIPT, slides=SLIDES, language_code=lang, lecture_title="Writs",
        glossary=["Article 21", "habeas corpus"])
    prompts[f"chunk_{lang}"] = pb.build_dcn_chunk_prompt(
        subject="polity", depth="detailed", chunk_index=1, chunk_total=2, transcript_chunk=TRANSCRIPT,
        slides=SLIDES, language_code=lang, glossary=["Article 21"])
    prompts[f"merge_{lang}"] = pb.build_dcn_merge_prompt(
        subject="polity", depth="detailed", parts=["#H1: Writs\n- one", "#H1: Writs\n- two"], lecture_title="Writs",
        language_code=lang)
    for note_type in ("concise", "mains_ready", "prelims_ready"):
        prompts[f"{note_type}_{lang}"] = pb.build_upsc_prompt(
            note_type=note_type, area="polity", language_code=lang, slides=SLIDES, transcript=TRANSCRIPT)
MARK = "\n\nADDITIONAL REQUIREMENTS FOR THIS CLASS"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


print(json.dumps({name: {"sha256": digest(text), "tail": text[-200:],
                         "before_extra": digest(text[:text.rfind(MARK)] if MARK in text else text)}
                  for name, text in prompts.items()}, ensure_ascii=False))
