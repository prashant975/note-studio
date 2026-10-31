"""Have the copied QC files drifted from the note-generation project?

This app is a copy of that project's QC, not a reimplementation of it. The value
of that only holds while the copies really are copies, so this script diffs every
file that was taken verbatim and reports any that no longer match.

    python tests/check_copies.py                       # looks for a sibling checkout
    python tests/check_copies.py ../upsc_Notes         # or point it at one

Exit code 0 when everything matches (or the original is not on this machine),
1 when a file has drifted. Run it after pulling changes into either project: a
fix made there is a fix this app has not got yet.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]

#: this app's path -> the note-generation project's path. Every one of these was
#: copied byte for byte and must stay that way.
VERBATIM: dict[str, str] = {
    "pw_access.py": "pw_access.py",
    "src/ai_client.py": "src/ai_client.py",
    "src/auth_session.py": "src/auth_session.py",
    "src/proxy_session.py": "src/proxy_session.py",
    "src/utils.py": "src/utils.py",
    "src/dtp_parser.py": "src/dtp_parser.py",
    "src/language_detect.py": "src/language_detect.py",
    "src/title_utils.py": "src/title_utils.py",
    "src/table_sanitizer.py": "src/table_sanitizer.py",
    "src/subject_detect.py": "src/subject_detect.py",
    "src/quality_checker.py": "src/quality_checker.py",
    "src/upsc_quality_checker.py": "src/upsc_quality_checker.py",
    "src/qc/models.py": "src/qc/models.py",
    "src/qc/paths.py": "src/qc/paths.py",
    "src/qc/qc_config.py": "src/qc/qc_config.py",
    "src/qc/deterministic_checks.py": "src/qc/deterministic_checks.py",
    "src/qc/ai_review.py": "src/qc/ai_review.py",
    "src/qc/format_check.py": "src/qc/format_check.py",
    "config/upsc_qc_rules.yaml": "config/upsc_qc_rules.yaml",
    "config/upsc_qc_sources.yaml": "config/upsc_qc_sources.yaml",
}

#: Copied, then deliberately changed. The reason is recorded here so a diff
#: against the original is expected rather than alarming.
ADAPTED: dict[str, tuple[str, str]] = {
    "src/error_report_writer.py": (
        "src/error_report_writer.py",
        'adds unavailable_rows() and the "Check not run" placeholder type, for '
        "the case the original never had: no model available to ask",
    ),
    "src/pdf_layout_check.py": (
        "src/docx_render_check.py",
        "the DOCX-to-PDF conversion half is removed (this app is handed the "
        "PDF); the geometry, the tolerances and _scan_page_for_overflow are "
        "unchanged",
    ),
    "src/qc_prompts.py": (
        "src/prompt_builder.py",
        "carries error_report_contract_block() and its constants verbatim, plus "
        "one new wrapper prompt for reading a finished PDF",
    ),
    "src/config.py": (
        "src/config.py",
        "trimmed to the constants the QC path reads; every kept value is the "
        "original's value",
    ),
    "src/models.py": (
        "src/models.py",
        "SlideData verbatim; the note-generation models are dropped and "
        "PdfQcResult added",
    ),
}

#: The block of src/prompt_builder.py that src/qc_prompts.py must carry
#: unchanged, identified by its first and last line rather than by line number
#: so an edit above it does not break the check.
CONTRACT_FIRST = "def error_report_contract_block("
CONTRACT_LAST = "covered in less depth than you would like — that is not an error."


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _find_original(argv: list[str]) -> Path | None:
    if len(argv) > 1:
        candidate = Path(argv[1]).expanduser().resolve()
        return candidate if (candidate / "src" / "qc").is_dir() else None
    for guess in (HERE.parent / "upsc_Notes", HERE.parent / "upsc_notes",
                  HERE.parent / "Upsc_Notes"):
        if (guess / "src" / "qc").is_dir():
            return guess
    return None


def _slice(text: str, first: str, last: str) -> str:
    start = text.find(first)
    end = text.find(last)
    if start < 0 or end < 0:
        return ""
    return text[start:end + len(last)]


def main(argv: list[str]) -> int:
    original = _find_original(argv)
    if original is None:
        print("The note-generation project was not found on this machine, so the "
              "copies could not be compared.")
        print("Pass its path:  python tests/check_copies.py ../upsc_Notes")
        return 0

    print(f"Comparing against {original}\n")
    drifted: list[str] = []
    missing: list[str] = []

    for mine, theirs in sorted(VERBATIM.items()):
        a, b = HERE / mine, original / theirs
        if not b.exists():
            missing.append(f"{theirs} (not in the original any more)")
            continue
        if _digest(a) == _digest(b):
            print(f"  ok        {mine}")
        else:
            drifted.append(mine)
            print(f"  DRIFTED   {mine}   {_digest(a)} != {_digest(b)}")

    print()
    contract_ok = True
    theirs = original / "src" / "prompt_builder.py"
    if theirs.exists():
        want = _slice(theirs.read_text(encoding="utf-8"), CONTRACT_FIRST, CONTRACT_LAST)
        got = _slice((HERE / "src" / "qc_prompts.py").read_text(encoding="utf-8"),
                     CONTRACT_FIRST, CONTRACT_LAST)
        contract_ok = bool(want) and want == got
        print(f"  {'ok       ' if contract_ok else 'DRIFTED  '} "
              "error_report_contract_block()  (the Error Report contract)")

    print("\nAdapted on purpose (a diff here is expected):")
    for mine, (theirs_path, why) in sorted(ADAPTED.items()):
        print(f"  {mine}\n      from {theirs_path} — {why}")

    if missing:
        print("\nGone from the original:")
        for m in missing:
            print(f"  {m}")

    if drifted or not contract_ok:
        print(f"\n{len(drifted) + (0 if contract_ok else 1)} file(s) have drifted. "
              "Diff them and decide which side is right:")
        for mine in drifted:
            print(f"  diff \"{HERE / mine}\" \"{original / VERBATIM[mine]}\"")
        return 1

    print("\nEvery verbatim copy still matches the original.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
