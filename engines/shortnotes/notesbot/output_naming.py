"""Consistent names for saved concise notes, including renamed older outputs.

The output takes the lecture's name with its "Class Notes" label swapped
for "Concise Notes", wherever the label sits in the name:

    पुष्पी पादपों में लैंगिक प्रजनन 02  Class Notes.pdf
    -> पुष्पी पादपों में लैंगिक प्रजनन 02 Concise Notes.pdf
    Thermodynamics 01  Class Notes  Physical Chemistry By Sudhanshu Kumar Sir.pdf
    -> Thermodynamics 01  Concise Notes  Physical Chemistry By Sudhanshu Kumar Sir.pdf

A name without the label gets " Concise Notes" added at the end.
"""
from __future__ import annotations

import os
import re

OUTPUT_SUFFIX = " Concise Notes.pdf"
# The name every output carried before 2026-09-17, kept so a folder that
# already holds one is still recognised as done.
LEGACY_OUTPUT_SUFFIX = " - Concise Notes.pdf"
_OLD_SUFFIX = re.compile(
    r"[\s_]*(?:[-–—_][\s_]*)?(?:shorts?[\s_]*notes?|concise[\s_]*notes?|class[\s_]*notes?)"
    r"\s*(?:\(\d+\))?\s*$", re.IGNORECASE,
)


def _stem(source_name: str) -> str:
    """The lecture's own name: no .pdf, no Class/Short/Concise Notes labels."""
    base = (source_name or "").strip()
    root, ext = os.path.splitext(base)
    if ext.lower() == ".pdf" and root:
        base = root
    while True:
        cleaned = _OLD_SUFFIX.sub("", base).strip()
        if cleaned == base:
            break
        base = cleaned
    return base


_LABEL_INSIDE = re.compile(r"class[\s_]*notes?", re.IGNORECASE)
_DONE_INSIDE = re.compile(r"concise[\s_]*notes?", re.IGNORECASE)


def output_name(source_name: str, *, max_stem_length: int | None = None) -> str:
    base = _stem(source_name)
    if base and _DONE_INSIDE.search(base):
        inside = base                                   # already an output name
    elif base and _LABEL_INSIDE.search(base):
        inside = _LABEL_INSIDE.sub("Concise Notes", base, count=1)
    else:
        inside = ""
    if inside and (max_stem_length is None or len(inside) <= max_stem_length):
        return f"{inside}.pdf"
    if inside:
        # Too long to keep whole: drop the label, shorten, add it at the end.
        base = " ".join(_DONE_INSIDE.sub(" ", _LABEL_INSIDE.sub(" ", base)).split())
    if max_stem_length is not None:
        base = base[:max(1, max_stem_length - len(OUTPUT_SUFFIX[:-4]))].rstrip()
    return f"{base or 'Lecture'}{OUTPUT_SUFFIX}"


def legacy_output_name(source_name: str) -> str:
    """The name the same lecture's output had before the rename:
    '<source without .pdf> - Concise Notes.pdf' with 'Class Notes' kept."""
    base = (source_name or "").strip()
    root, ext = os.path.splitext(base)
    if ext.lower() == ".pdf" and root:
        base = root
    legacy = re.compile(r"\s*(?:[-–—_]\s*)?(?:shorts?\s*notes?|concise\s*notes?)\s*(?:\(\d+\))?\s*$", re.IGNORECASE)
    while True:
        cleaned = legacy.sub("", base).strip()
        if cleaned == base:
            break
        base = cleaned
    return f"{base or 'Lecture'}{LEGACY_OUTPUT_SUFFIX}"


def legacy_output_names(source_name: str) -> tuple[str, ...]:
    """Every older name this lecture's output may already carry, current one excluded.

    - before 2026-09-17: '<source without .pdf> - Concise Notes.pdf';
    - 2026-09-17 builds: ' Concise Notes' added at the end even when
      "Class Notes" sat in the middle of the name.
    """
    names = [legacy_output_name(source_name), f"{_stem(source_name) or 'Lecture'}{OUTPUT_SUFFIX}"]
    current = output_name(source_name)
    return tuple(dict.fromkeys(name for name in names if name != current))
