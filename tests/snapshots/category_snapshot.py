"""Print what the hand-drawn notes engine's category rules produce, as JSON.

    python category_snapshot.py <engine root>      (cwd anywhere; stdout = JSON)

Run in a child process with only <engine root> on sys.path, so the original
project and the NoteCraft copy can be compared without importing both. With
NOTE_STUDIO_CATEGORY_PACKS set, the copy reads its rules from the packs.

Covers, for NEET-JEE and Foundation and every subject id (aliases included):
prompt_block(), commands(), switches(), solved_examples(), fingerprint(), and a
SHA-256 of the full generation prompt in English and Hindi.
"""
from __future__ import annotations

import hashlib
import json
import sys

SUBJECTS = ["", "auto", "other", "chemistry", "physics", "biology", "mathematics", "sanskrit",
            "science", "social_science", "english", "hindi", "maths", "sst"]
CATEGORIES = ["neet_jee", "foundation", "neet", "jee"]


def main(root: str) -> dict:
    sys.path.insert(0, root)
    from notesbot import categories, generation_profile
    from notesbot.prompts import build_prompt

    out = {"categories": list(categories.CATEGORIES), "labels": dict(categories.LABELS),
           "legacy": dict(categories.LEGACY), "default": categories.DEFAULT, "pairs": {}, "fingerprints": {}}
    for category in CATEGORIES:
        out["fingerprints"][category] = categories.fingerprint(category)
        for subject in SUBJECTS:
            key = f"{category}/{subject}"
            with generation_profile.exam_category(category), generation_profile.exam_subject(subject):
                prompts = {lang: hashlib.sha256(build_prompt(lang, "by Sir", subject_hint=subject)
                                                .encode("utf-8")).hexdigest()
                           for lang in ("english", "hindi")}
            out["pairs"][key] = {
                "prompt_block": categories.prompt_block(category, subject),
                "commands": categories.commands(category, subject),
                "switches": categories.switches(category, subject),
                "solved_examples": categories.solved_examples(category, subject),
                "prompt_sha256": prompts,
            }
    for value in ("NEET-JEE", "neet", "Foundation"):
        out.setdefault("normalize", {})[value] = categories.normalize(value)
    try:
        categories.normalize("no-such-category")
        out["normalize_unknown"] = "accepted"
    except ValueError as exc:
        out["normalize_unknown"] = f"ValueError: {exc}"
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(main(sys.argv[1]), ensure_ascii=False, indent=1, sort_keys=True))
