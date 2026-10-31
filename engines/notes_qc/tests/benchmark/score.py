"""Run QC over the benchmark and score it. This is the accuracy loop's ruler.

    python tests/benchmark/score.py            # uses the stored proxy session
    python tests/benchmark/score.py --no-ai    # offline stages only (recall ~0)

Reports, per run:

``recall``      seeded errors found / seeded errors present
``precision``   findings that name a seeded error / findings reported
``F1``          the two together, so one cannot be bought with the other

Precision needs care. A finding that is not on the answer sheet is not
automatically wrong — a real checker will notice things the benchmark did not
seed. So findings are put in three buckets: MATCHED (names a seeded error),
UNSEEDED (plausible but not on the sheet) and FALSE (fires on a sentence known
to be correct). Only FALSE counts against precision; UNSEEDED is reported
separately for a human to glance at, because scoring it either way would be a
guess.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ground_truth import CLEAN_SENTENCES, SEEDED  # noqa: E402

PDF = Path(__file__).resolve().parent / "benchmark_notes.pdf"
RESULTS = Path(__file__).resolve().parent / "results"


def _row_text(row: dict) -> str:
    """Everything a row says, as one searchable string."""
    return " ".join(str(row.get(k, "") or "") for k in (
        "original_statement", "error_type", "error_concern",
        "correct_information_correction"))


#: Distinctive fragments of the CORRECT sentences. A finding quoting one of
#: these, and matching no seeded error, is firing on something known to be right.
_CLEAN_MARKERS = tuple(
    " ".join(s.split()[:6]).lower() for s in CLEAN_SENTENCES
)


def classify(rows: list[dict]) -> dict:
    """Bucket every reported row and compute the score."""
    placeholder_types = {"No issue", "Report parsing", "Check not run",
                         "Incomplete scan"}
    real_rows = [r for r in rows
                 if str(r.get("error_type", "")) not in placeholder_types]

    found: dict[str, dict] = {}
    matched, unseeded, false_pos = [], [], []

    for row in real_rows:
        text = _row_text(row)
        hit = next((e for e in SEEDED if e.matched_by(text)), None)
        if hit is not None:
            matched.append((hit.key, row))
            found.setdefault(hit.key, row)
            continue
        low = " ".join(text.split()).lower()
        if any(marker in low for marker in _CLEAN_MARKERS):
            false_pos.append(row)
        else:
            unseeded.append(row)

    recall = len(found) / len(SEEDED) if SEEDED else 0.0
    judged = len(found) + len(false_pos)
    precision = len(found) / judged if judged else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return {
        "seeded": len(SEEDED),
        "found_keys": sorted(found),
        "missed_keys": sorted(e.key for e in SEEDED if e.key not in found),
        "rows_reported": len(real_rows),
        "matched": len(found),
        "unseeded": len(unseeded),
        "false_positives": len(false_pos),
        "recall": recall,
        "precision": precision,
        "f1": f1,
        "unseeded_rows": [_row_text(r)[:160] for r in unseeded],
        "false_positive_rows": [_row_text(r)[:160] for r in false_pos],
    }


def run(no_ai: bool = False, strictness: str = "strict", label: str = "",
        pdf: Path | None = None) -> dict:
    from src.pipeline import run_pdf_qc_batch

    token = ""
    if not no_ai:
        from src import proxy_session
        import os

        store = Path(os.environ["LOCALAPPDATA"]) / "UPSC_Notes" / "proxy_session.json"
        email = json.loads(store.read_text(encoding="utf-8")).get("email", "")
        if not proxy_session.attach(email):
            raise SystemExit("the stored proxy session is not usable; sign in again")
        token = "stored-session"   # same sentinel run_cli.py uses; the pass is attached

    run_result = run_pdf_qc_batch(
        [pdf or PDF], google_token=token, strictness=strictness,
        write_individual_reports=False,
    )
    result = run_result.results[0]
    score = classify(result.error_rows or [])
    score["status"] = result.status
    score["error_report_status"] = result.error_report_status
    score["complete"] = getattr(result, "error_report_complete", True)
    score["warnings"] = [w[:150] for w in result.warnings]
    score["run_dir"] = str(run_result.run_dir)
    score["label"] = label or ("offline" if no_ai else strictness)
    return score


def report(score: dict) -> None:
    print()
    print(f"=== BENCHMARK: {score['label']} ===")
    print(f"  status        : {score['status']} / {score['error_report_status']}"
          f" / complete={score['complete']}")
    print(f"  rows reported : {score['rows_reported']}")
    print()
    print(f"  RECALL        : {score['recall']:6.1%}   "
          f"({score['matched']}/{score['seeded']} seeded errors found)")
    print(f"  PRECISION     : {score['precision']:6.1%}   "
          f"({score['false_positives']} fired on known-correct sentences)")
    print(f"  F1            : {score['f1']:6.1%}")
    print()
    if score["missed_keys"]:
        print(f"  MISSED ({len(score['missed_keys'])}):")
        for key in score["missed_keys"]:
            note = next(e.note for e in SEEDED if e.key == key)
            print(f"    - {key:24} {note}")
    if score["false_positive_rows"]:
        print(f"  FALSE POSITIVES ({len(score['false_positive_rows'])}):")
        for text in score["false_positive_rows"][:6]:
            print(f"    ! {text}")
    if score["unseeded_rows"]:
        print(f"  unseeded findings ({len(score['unseeded_rows'])}) "
              f"— not on the answer sheet, may still be real:")
        for text in score["unseeded_rows"][:6]:
            print(f"    ? {text}")
    for warning in score["warnings"]:
        if "INCOMPLETE" in warning:
            print(f"  !! {warning}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--strictness", default="strict")
    ap.add_argument("--label", default="")
    ap.add_argument("--long", action="store_true",
                    help="score the long, multi-chunk document")
    args = ap.parse_args()

    target = (Path(__file__).resolve().parent / "benchmark_notes_long.pdf"
              if args.long else PDF)
    if not target.exists():
        raise SystemExit(f"{target} is missing — run tests/benchmark/build.py first")

    score = run(no_ai=args.no_ai, strictness=args.strictness, label=args.label,
                pdf=target)
    report(score)

    RESULTS.mkdir(exist_ok=True)
    stamp = score["label"].replace(" ", "_")
    (RESULTS / f"score_{stamp}.json").write_text(
        json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
