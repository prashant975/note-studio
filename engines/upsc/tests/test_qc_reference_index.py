"""ReferenceRetriever tests: local book index build, search and degradation.

Everything runs offline against temp directories — no network, no shared
state. The FTS5 path and the LIKE fallback are both exercised against the
same fixture corpus so the two code paths can never drift apart silently.
"""
import sqlite3
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.qc.models import EvidenceChunk
from src.qc.reference_index import ReferenceRetriever

POLITY_TEXT = """Chapter 1: Constitutional Framework

The Constitution of India was adopted on 26 November 1949 and came into
force on 26 January 1950. Article 368 lays down the procedure for the
amendment of the Constitution by Parliament.

The President of India is elected by an electoral college consisting of the
elected members of both Houses of Parliament and of the Legislative
Assemblies of the States.
"""

ECONOMY_TEXT = """Chapter 3: Inflation and Monetary Policy

The Reserve Bank of India uses the repo rate as its main policy instrument.
Flexible inflation targeting was formally adopted in 2016 under the amended
RBI Act.

Gross Domestic Product measures the value of goods and services produced
within the country in a given period.
"""


@pytest.fixture()
def retriever(tmp_path):
    r = ReferenceRetriever(index_root=tmp_path / "idx")
    yield r
    r.close()  # release the sqlite handle so Windows can clean the temp dir


def _write_corpus(tmp_path):
    polity = tmp_path / "polity.txt"
    economy = tmp_path / "economy.txt"
    polity.write_text(POLITY_TEXT, encoding="utf-8")
    economy.write_text(ECONOMY_TEXT, encoding="utf-8")
    return polity, economy


def _index_corpus(retriever, tmp_path):
    polity, economy = _write_corpus(tmp_path)
    n1 = retriever.index_document(
        polity, subject="polity", source_id="laxmikanth", source_name="Indian Polity",
        edition="7th", publication_year="2023",
    )
    n2 = retriever.index_document(
        economy, subject="economy", source_id="ramesh_singh", source_name="Indian Economy",
    )
    return n1, n2


# ------------------------------------------------------------ empty index


def test_empty_index_is_unavailable_and_search_is_safe(retriever):
    assert retriever.available() is False
    assert retriever.search("Article 368 amendment", subject="polity") == []
    # A read-only miss must not leave a db file behind.
    assert not retriever.db_path.exists()


# --------------------------------------------------------- indexing + search


def test_index_txt_documents_and_search(retriever, tmp_path):
    n1, n2 = _index_corpus(retriever, tmp_path)
    assert n1 > 0 and n2 > 0
    assert retriever.available() is True

    results = retriever.search("procedure for amendment under Article 368", subject="polity")
    assert results, "expected at least one polity hit"
    assert all(isinstance(c, EvidenceChunk) for c in results)
    top = results[0]
    assert top.source_id == "laxmikanth"
    assert top.source_name == "Indian Polity"
    assert top.edition == "7th"
    assert top.publication_year == "2023"
    assert top.page_number is None  # whole-file txt chunks carry no page
    assert "368" in top.text


def test_search_filters_by_subject(retriever, tmp_path):
    _index_corpus(retriever, tmp_path)
    for chunk in retriever.search("repo rate inflation targeting", subject="polity"):
        assert chunk.source_id != "ramesh_singh"
    econ = retriever.search("repo rate inflation targeting", subject="economy")
    assert econ and econ[0].source_id == "ramesh_singh"


def test_ranking_puts_matching_chunk_first(retriever, tmp_path):
    _index_corpus(retriever, tmp_path)
    results = retriever.search("repo rate policy instrument", subject="economy", top_k=3)
    assert results
    assert "repo rate" in results[0].text.lower()
    scores = [c.score for c in results]
    assert scores == sorted(scores, reverse=True)
    assert len(results) <= 3


def test_chapter_comes_from_nearest_heading(retriever, tmp_path):
    _index_corpus(retriever, tmp_path)
    results = retriever.search("electoral college President", subject="polity")
    assert results
    assert "Constitutional Framework" in results[0].chapter


def test_stopword_only_query_returns_empty(retriever, tmp_path):
    _index_corpus(retriever, tmp_path)
    assert retriever.search("of the and by which", subject="polity") == []
    assert retriever.search("", subject="polity") == []


# ------------------------------------------------------------- re-indexing


def test_reindex_same_document_replaces_rows(retriever, tmp_path):
    polity, _ = _write_corpus(tmp_path)
    first = retriever.index_document(
        polity, subject="polity", source_id="laxmikanth", source_name="Indian Polity"
    )
    second = retriever.index_document(
        polity, subject="polity", source_id="laxmikanth", source_name="Indian Polity"
    )
    assert first == second > 0

    with sqlite3.connect(str(retriever.db_path)) as db:
        total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        fts_total = db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    assert total == first, "re-indexing must replace, not append"
    assert fts_total == total, "FTS rows must stay in lockstep with chunk rows"


# ------------------------------------------------------------ LIKE fallback


def test_like_fallback_matches_fts_behaviour(retriever, tmp_path):
    _index_corpus(retriever, tmp_path)
    retriever.fts5_enabled = False  # force the degraded path on the same data

    results = retriever.search("repo rate policy instrument", subject="economy", top_k=3)
    assert results
    assert "repo rate" in results[0].text.lower()
    scores = [c.score for c in results]
    assert scores == sorted(scores, reverse=True)

    for chunk in retriever.search("repo rate", subject="polity"):
        assert chunk.source_id != "ramesh_singh"
    assert retriever.search("of the and", subject="economy") == []


# ------------------------------------------------------- degraded documents


def test_missing_document_warns_and_returns_zero(retriever, tmp_path):
    count = retriever.index_document(
        tmp_path / "no_such_book.txt",
        subject="polity", source_id="ghost", source_name="Ghost Book",
    )
    assert count == 0
    assert any("no_such_book" in w for w in retriever.warnings)


def test_unsupported_extension_warns_and_returns_zero(retriever, tmp_path):
    weird = tmp_path / "book.docx"
    weird.write_text("not indexable", encoding="utf-8")
    count = retriever.index_document(
        weird, subject="polity", source_id="weird", source_name="Weird"
    )
    assert count == 0
    assert any("book.docx" in w for w in retriever.warnings)
    assert retriever.available() is False


# -------------------------------------------------------------- PDF path


def test_pdf_indexing_records_page_numbers(retriever, tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "polity.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Article 356 deals with President's Rule in the States.")
    doc.save(str(pdf_path))
    doc.close()

    count = retriever.index_document(
        pdf_path, subject="polity", source_id="pdf_book", source_name="PDF Polity"
    )
    assert count > 0
    results = retriever.search("Article 356 President Rule", subject="polity")
    assert results
    assert results[0].page_number == 1
    assert "356" in results[0].text


# ------------------------------------------------------------ index_library


def _fake_registry():
    return types.SimpleNamespace(
        subject_aliases={"ancient india": "ancient_history"},
        subjects={"polity": {}, "economy": {}},
    )


def _make_library(tmp_path):
    root = tmp_path / "reference_library"
    (root / "Polity").mkdir(parents=True)
    (root / "Economy").mkdir(parents=True)
    (root / "Polity" / "laxmikanth_notes.txt").write_text(POLITY_TEXT, encoding="utf-8")
    (root / "Economy" / "ramesh_singh_notes.txt").write_text(ECONOMY_TEXT, encoding="utf-8")
    return root


def test_index_library_routes_folders_via_source_router(retriever, tmp_path, monkeypatch):
    stub = types.ModuleType("src.qc.source_router")

    class _StubRouter:
        def __init__(self, registry=None):
            self.registry = registry

        def normalise(self, raw):
            return {"polity": "polity", "economy": "economy"}.get(str(raw).strip().lower())

    stub.SourceRouter = _StubRouter
    monkeypatch.setitem(sys.modules, "src.qc.source_router", stub)

    result = retriever.index_library(_make_library(tmp_path), registry=_fake_registry())
    assert result["files"] == 2
    assert result["chunks"] > 0
    assert set(result["by_subject"]) == {"polity", "economy"}

    hits = retriever.search("electoral college President", subject="polity")
    assert hits and hits[0].source_id.startswith("polity:")


def test_index_library_falls_back_without_source_router(retriever, tmp_path, monkeypatch):
    # None in sys.modules makes the lazy import raise, exercising the
    # registry-alias and slug fallbacks deterministically even after the real
    # source_router module lands in the repo.
    monkeypatch.setitem(sys.modules, "src.qc.source_router", None)

    root = _make_library(tmp_path)
    (root / "Ancient India").mkdir()
    (root / "Ancient India" / "rs_sharma.txt").write_text(
        "Chapter 2: Harappan Culture\n\nThe Harappan civilisation flourished along the Indus.",
        encoding="utf-8",
    )
    (root / "Map Work").mkdir()
    (root / "Map Work" / "maps.txt").write_text("Rivers of India and their tributaries.", encoding="utf-8")

    result = retriever.index_library(root, registry=_fake_registry())
    by_subject = result["by_subject"]
    assert "ancient_history" in by_subject      # via registry alias table
    assert "polity" in by_subject               # exact registry subject key
    assert "map_work" in by_subject             # slug fallback for unknown folder
    assert retriever.search("Harappan civilisation Indus", subject="ancient_history")


def test_index_library_missing_root_returns_empty_dict(retriever, tmp_path):
    assert retriever.index_library(tmp_path / "does_not_exist") == {}
    assert retriever.warnings == []
