"""Score a QC run against a labelled document.

    python tests/benchmark/score_run.py runs/qc_2026...  [ground_truth.json]

Grading by hand is what made the last two rounds slow and hard to compare, so
this does it the same way every time: a seeded error counts as FOUND when some
row in the report contains one of its keyword sets, and every row that matches
no seeded error is a candidate FALSE POSITIVE for a human to confirm.

It reports recall and precision — precision twice, because the report has two
tiers: confirmed FINDINGS a reviewer acts on, and LEADS the verification pass
could not settle, which are labelled as such. It then lists the misses, which
are the useful half, because they say what to fix next.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# The rows quote the notes, which carry bullets and Devanagari; a cp1252
# console dies on the first one, taking the whole scorecard with it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PLACEHOLDER_TYPES = {"No issue", "Report parsing", "Check not run", "Incomplete scan"}

#: A row the verification pass could not settle is printed as a LEAD, at Low
#: severity, with the caveat leading the cell. Scoring it as a false positive
#: punishes the report for being honest; scoring it as a finding overstates
#: what was confirmed. So the two tiers are counted separately.
LEAD_MARKERS = ("lead —", "lead -", "treat as a lead",
                "not independently verified", "rests on the checker's memory")


def is_lead(row: dict) -> bool:
    text = str(row.get("error_concern", "")).lower()
    return any(m in text for m in LEAD_MARKERS)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def load_rows(run_dir: Path, document: str = "") -> list[dict]:
    """Every real error row the run produced for ``document``, placeholders excluded.

    A batch run holds one sub-folder per file. Without the document filter a
    two-file run would be scored as one report, and the second file's rows
    would all read as false positives of the first.
    """
    rows: list[dict] = []
    want = _slug(Path(document).stem) if document else ""
    for result in run_dir.glob("*/qc_result.json"):
        if want and not (_slug(result.parent.name).startswith(want)
                         or want.startswith(_slug(result.parent.name))):
            continue
        data = json.loads(result.read_text(encoding="utf-8"))
        for row in data.get("error_rows") or []:
            if row.get("error_type") not in PLACEHOLDER_TYPES:
                rows.append(row)
    if rows:
        return rows
    # Older runs kept the rows only in the merged block.
    for raw in run_dir.glob("*/error_report_raw.json"):
        try:
            for row in json.loads(raw.read_text(encoding="utf-8")):
                if isinstance(row, dict) and row.get("error_type") not in PLACEHOLDER_TYPES:
                    rows.append(row)
        except Exception:
            pass
    return rows


def row_text(row: dict) -> str:
    return " ".join(str(row.get(k, "")) for k in (
        "original_statement", "error_type", "error_concern",
        "correct_information_correction")).lower()


def matches(spec: dict, text: str) -> bool:
    """True when any one of the error's keyword sets is fully present."""
    for keyword_set in spec.get("any", []):
        if all(k.lower() in text for k in keyword_set):
            return True
    return False


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    run_dir = Path(sys.argv[1])
    truth_path = Path(sys.argv[2]) if len(sys.argv) > 2 else (
        Path(__file__).parent / "trail_ground_truth.json")

    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    seeded = truth["errors"]
    rows = load_rows(run_dir, str(truth.get("document") or ""))
    texts = [row_text(r) for r in rows]

    found, missed = [], []
    matched_rows: set[int] = set()
    for spec in seeded:
        # Prefer a row not yet credited to another seeded error. Without this,
        # one row whose text happens to satisfy two specs (the Prambanan rows
        # both contain "indonesia") absorbs both credits, and the report's
        # OTHER, genuinely-matching row is then printed as a false positive —
        # under-counting precision for finding the error properly.
        hit = next((i for i, text in enumerate(texts)
                    if i not in matched_rows and matches(spec, text)), None)
        if hit is None:
            # Fall back to an already-credited row: the spec still counts as
            # found (recall is "did the report say it anywhere"), it just adds
            # no new matched row.
            hit = next((i for i, text in enumerate(texts)
                        if matches(spec, text)), None)
        if hit is None:
            missed.append(spec)
        else:
            found.append(spec)
            matched_rows.add(hit)

    unmatched = [rows[i] for i in range(len(rows)) if i not in matched_rows]

    total = len(seeded)
    recall = len(found) / total if total else 0.0
    precision = len(matched_rows) / len(rows) if rows else 0.0

    print(f"document : {truth.get('document')}")
    print(f"run      : {run_dir.name}")
    print(f"rows      : {len(rows)}")
    print()
    findings = [r for r in rows if not is_lead(r)]
    f_matched = {i for i in matched_rows if not is_lead(rows[i])}
    only_lead = sum(1 for i in matched_rows if is_lead(rows[i]))

    print(f"RECALL    : {len(found)}/{total}  = {recall:6.1%}"
          f"   (stated as a finding: {len(found) - only_lead}, "
          f"only as a lead: {only_lead})")
    print(f"PRECISION : {len(f_matched)}/{len(findings)} = "
          f"{(len(f_matched) / len(findings) if findings else 0):6.1%}"
          f"   over the {len(findings)} FINDINGS a reviewer is asked to act on")
    print(f"            {len(matched_rows)}/{len(rows)} = {precision:6.1%}"
          f"   over all rows, including {len(rows) - len(findings)} leads")
    print()

    if missed:
        print(f"--- MISSED {len(missed)} (this is the work list) ---")
        for spec in missed:
            print(f"  p{spec['page']:<3} #{spec['id']:<3} {spec['topic']}")
        print()
    if unmatched:
        print(f"--- {len(unmatched)} rows matching no seeded error ---")
        print("    (some are real errors the list does not cover; the rest are false positives)")
        for row in unmatched[:25]:
            stmt = " ".join(str(row.get("original_statement", "")).split())[:58]
            concern = " ".join(str(row.get("error_concern", "")).split())[:70]
            print(f"  [{row.get('severity','?'):6}] {stmt}")
            print(f"           {concern}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
