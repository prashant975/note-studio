"""app_harness.py, with the DPP masters replaced by a small synthetic set.

Not a test. The real workbooks are 55 MB and absent on most machines; this
lets the topic filters and hand-picking run in seconds, anywhere. Only the
four dpp_builder entry points that read the masters or match the notes are
replaced — every widget and branch in app.py runs as it does in production.
"""
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import dpp_builder  # noqa: E402
from src.notes_to_dpp import models  # noqa: E402

M = models


def _row(rid, subject, sid, chapter, cid, topic, tid):
    return M.TaxonomyRow(rid, subject, sid, chapter, cid, topic, tid)


ROWS = (
    _row("ca-st-1", "Current Affairs", "CA", "Science and Tech", "ST", "Space Missions", "T1"),
    _row("ca-st-2", "Current Affairs", "CA", "Science and Tech", "ST", "Biotechnology", "T2"),
    _row("ca-pg-1", "Current Affairs", "CA", "Polity and Governance", "PG", "Elections", "T3"),
    _row("ca-si-1", "Current Affairs", "CA", "Social Issues", "SI", "Child Rights", "T4"),
    _row("po-1", "Polity", "PO", "Parliament", "PA", "Money Bill", "T5"),
)
# Everything but the Social Issues row is a strong match; Polity is the
# lecture's minor subject, as a Current Affairs class usually has one.
CANDIDATES = tuple(
    M.CoverageCandidate(row.row_id, conf, row.topic, 1, "exact label", rec)
    for row, conf, rec in zip(ROWS, (0.98, 0.97, 0.95, 0.80, 0.93),
                              (True, True, True, False, True))
)


def _question(qid, row, difficulty):
    localized = M.LocalizedQuestion(
        language=M.Language.ENGLISH,
        prompt=f"Consider the following about {qid}:\n1. One.\n2. Two.\n"
               "Which of the statements given above is/are correct?",
        options=("1 only", "2 only", "Both 1 and 2", "Neither 1 nor 2"),
        correct_option_indexes=(0,),
        solution="Because.",
    )
    return M.QuestionRecord(
        question_id=qid, prompt=localized.prompt, options=localized.options,
        difficulty=difficulty,
        tags=(M.QuestionTag(row.subject_id, row.chapter_id, row.topic_id),),
        source_tags=("DPP",), content_fingerprint=f"fp-{qid}", verified=True,
        localized_content=(localized,),
    )


QUESTIONS = tuple(
    _question(f"{row.row_id}-{d.value.lower()}", row, d)
    for row in ROWS for d in M.Difficulty
)

dpp_builder.masters_ready = lambda: True
dpp_builder.load_masters = lambda **_: (SimpleNamespace(rows=ROWS),
                                        SimpleNamespace(questions=QUESTIONS))
dpp_builder.coverage = lambda *a, **k: CANDIDATES
dpp_builder.note_language = lambda *a, **k: (M.Language.ENGLISH, "en")

runpy.run_path(str(Path(__file__).with_name("app_harness.py")), run_name="__main__")
