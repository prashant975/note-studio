"""Offline tests for the source-verification stack (claim_verifier,
source_search, evidence_extractor). No network: fetchers are monkeypatched
and the AI client is faked."""
from pathlib import Path
import json
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qc import claim_verifier
from src.qc.claim_verifier import verify_claims
from src.qc.evidence_extractor import (
    evidence_from_chunks,
    evidence_from_page,
    evidence_from_pdf,
)
from src.qc.models import (
    CONTRADICTED,
    NEEDS_HUMAN_REVIEW,
    SOURCE_NOT_FOUND,
    SOURCE_UNAVAILABLE,
    VERIFIED,
    EvidenceChunk,
    FactualClaim,
)
from src.qc.page_fetcher import FetchResult
from src.qc.pdf_fetcher import PdfFetchResult
from src.qc.qc_config import QCLimits
from src.qc.source_router import SubjectSources
from src.qc.source_search import build_queries, candidate_urls


# ------------------------------------------------------------------ fixtures

def make_claim(n=1, text="Article 368 empowers Parliament to amend the Constitution.",
               **kwargs):
    defaults = dict(
        claim_id=f"CLM-{n:03d}", text=text,
        claim_type="constitutional_article", subject="polity",
        entities=["Parliament"],
    )
    defaults.update(kwargs)
    return FactualClaim(**defaults)


def make_sources(domains=()):
    return SubjectSources(
        subject_key="polity", display_name="Indian Polity",
        official_domains=list(domains),
        books=[{"id": "polity:laxmikanth", "name": "Indian Polity",
                "edition": "7th", "publication_year": "2023"}],
        configured=True,
    )


def make_chunk(text="Article 368 empowers Parliament to amend the Constitution.",
               **kwargs):
    defaults = dict(source_id="polity:laxmikanth", source_name="Indian Polity",
                    page_number=101, chapter="Amendment of the Constitution",
                    text=text, score=5.0)
    defaults.update(kwargs)
    return EvidenceChunk(**defaults)


LIMITS = QCLimits()


class FakeRetriever:
    """Local-index stand-in: returns the same chunks for every query."""

    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.calls = []

    def available(self):
        return True

    def search(self, query, subject, top_k=5):
        self.calls.append((query, subject, top_k))
        return self.chunks[:top_k]


class CannedClient:
    """Returns canned response texts in order; the last one repeats."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, prompt, image_paths=None, **kwargs):
        self.calls.append(prompt)
        text = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return SimpleNamespace(text=text)


class EchoClient:
    """Answers VERIFIED for every claim id it sees in the prompt."""

    def __init__(self):
        self.calls = []

    def generate(self, prompt, image_paths=None, **kwargs):
        self.calls.append(prompt)
        ids = list(dict.fromkeys(re.findall(r"CLM-\d{3}", prompt)))
        results = [{"claim_id": i, "status": "VERIFIED", "explanation": "matches",
                    "suggested_text": None, "confidence": 0.9, "evidence_ids": [],
                    "data_period": "", "is_latest": None} for i in ids]
        return SimpleNamespace(text=json.dumps({"results": results}))


def canned(results):
    return json.dumps({"results": results})


def run(claims, *, client, sources=None, retriever=None, allow_web=False,
        cache_dir=None):
    return verify_claims(
        claims, sources if sources is not None else make_sources(),
        client=client, limits=LIMITS, allow_web=allow_web,
        latest_data_check=True, retriever=retriever, cache_dir=cache_dir,
    )


# ------------------------------------------------------- verification basics

def test_status_mapping_verified():
    client = CannedClient([canned([{
        "claim_id": "CLM-001", "status": "VERIFIED", "explanation": "matches book",
        "suggested_text": None, "confidence": 0.9,
        "evidence_ids": ["EV-001-1"], "data_period": "", "is_latest": None,
    }])])
    verifications, evidence, _log = run(
        [make_claim()], client=client, retriever=FakeRetriever([make_chunk()]))
    (v,) = verifications
    assert v.status == VERIFIED
    assert v.claim_id == "CLM-001"
    assert v.evidence_ids == ["EV-001-1"]
    assert not v.requires_human_review
    assert not v.auto_correct_allowed  # VERIFIED never auto-corrects
    assert [e.evidence_id for e in evidence] == ["EV-001-1"]
    assert evidence[0].source_type == "book"


def contradiction_response(confidence):
    return canned([{
        "claim_id": "CLM-001", "status": "CONTRADICTED",
        "explanation": "the book says otherwise",
        "suggested_text": "Article 368 empowers Parliament to amend the Constitution.",
        "confidence": confidence, "evidence_ids": ["EV-001-1"],
        "data_period": "", "is_latest": None,
    }])


def test_auto_correct_allowed_at_096():
    client = CannedClient([contradiction_response(0.96)])
    verifications, _, _ = run([make_claim()], client=client,
                              retriever=FakeRetriever([make_chunk()]))
    assert verifications[0].status == CONTRADICTED
    assert verifications[0].auto_correct_allowed


def test_auto_correct_blocked_at_094():
    client = CannedClient([contradiction_response(0.94)])
    verifications, _, _ = run([make_claim()], client=client,
                              retriever=FakeRetriever([make_chunk()]))
    assert verifications[0].status == CONTRADICTED
    assert not verifications[0].auto_correct_allowed


def test_auto_correct_needs_cited_evidence_and_text():
    no_evidence_cited = canned([{
        "claim_id": "CLM-001", "status": "CONTRADICTED", "explanation": "wrong",
        "suggested_text": "corrected", "confidence": 0.99,
        "evidence_ids": ["EV-999-9"],  # not this claim's evidence
        "data_period": "", "is_latest": None,
    }])
    client = CannedClient([no_evidence_cited])
    verifications, _, _ = run([make_claim()], client=client,
                              retriever=FakeRetriever([make_chunk()]))
    assert verifications[0].evidence_ids == []  # foreign id filtered out
    assert not verifications[0].auto_correct_allowed


def test_unknown_status_becomes_needs_human_review():
    client = CannedClient([canned([{
        "claim_id": "CLM-001", "status": "PROBABLY_FINE", "explanation": "?",
        "suggested_text": None, "confidence": 0.9, "evidence_ids": [],
        "data_period": "", "is_latest": None,
    }])])
    verifications, _, _ = run([make_claim()], client=client,
                              retriever=FakeRetriever([make_chunk()]))
    assert verifications[0].status == NEEDS_HUMAN_REVIEW
    assert verifications[0].requires_human_review


def test_omitted_claim_becomes_needs_human_review():
    client = CannedClient([canned([{
        "claim_id": "CLM-001", "status": "VERIFIED", "explanation": "ok",
        "suggested_text": None, "confidence": 0.9, "evidence_ids": [],
        "data_period": "", "is_latest": None,
    }])])
    claims = [make_claim(1),
              make_claim(2, text="The 42nd Amendment was enacted in 1976.")]
    verifications, _, _ = run(claims, client=client,
                              retriever=FakeRetriever([make_chunk()]))
    assert verifications[0].status == VERIFIED
    assert verifications[1].status == NEEDS_HUMAN_REVIEW
    assert "omitted" in verifications[1].explanation


def test_unparseable_json_both_attempts():
    client = CannedClient(["not json at all", "still } not { json"])
    verifications, _, _ = run([make_claim()], client=client,
                              retriever=FakeRetriever([make_chunk()]))
    assert len(client.calls) == 2  # original + one repair attempt
    assert verifications[0].status == NEEDS_HUMAN_REVIEW
    assert verifications[0].explanation == "verifier response unparseable"


def test_code_fenced_json_is_parsed():
    client = CannedClient(["```json\n" + canned([{
        "claim_id": "CLM-001", "status": "VERIFIED", "explanation": "ok",
        "suggested_text": None, "confidence": 0.9, "evidence_ids": [],
        "data_period": "", "is_latest": None,
    }]) + "\n```"])
    verifications, _, _ = run([make_claim()], client=client,
                              retriever=FakeRetriever([make_chunk()]))
    assert verifications[0].status == VERIFIED
    assert len(client.calls) == 1


# --------------------------------------------------- no-evidence / no-client

def test_no_evidence_is_source_not_found_without_client_call():
    client = CannedClient(["should never be used"])
    verifications, evidence, _ = run([make_claim()], client=client,
                                     retriever=FakeRetriever([]))
    assert client.calls == []
    assert evidence == []
    (v,) = verifications
    assert v.status == SOURCE_NOT_FOUND
    assert "no approved source found" in v.explanation
    assert v.requires_human_review
    assert not v.auto_correct_allowed


def test_client_none_is_source_unavailable():
    verifications, evidence, _ = run([make_claim()], client=None,
                                     retriever=FakeRetriever([make_chunk()]))
    (v,) = verifications
    assert v.status == SOURCE_UNAVAILABLE
    assert v.explanation == "no AI client available for semantic verification"
    assert v.requires_human_review
    assert evidence == []


def test_claim_cap_is_logged():
    claims = [make_claim(n, text=f"Article {300 + n} of the Constitution matters.")
              for n in range(1, LIMITS.max_claims_verified + 6)]
    verifications, _, log = run(claims, client=None)
    assert len(verifications) == LIMITS.max_claims_verified
    assert any(e.get("query") == "claim cap" for e in log)


# ------------------------------------------------------- batching + caching

def test_ten_claims_make_two_batched_calls():
    client = EchoClient()
    claims = [make_claim(n, text=f"Article {300 + n} of the Constitution matters.")
              for n in range(1, 11)]
    verifications, _, _ = run(claims, client=client,
                              retriever=FakeRetriever([make_chunk()]))
    assert len(client.calls) == 2  # 8 + 2
    assert len(verifications) == 10
    assert all(v.status == VERIFIED for v in verifications)


def test_duplicate_claims_verified_once():
    client = EchoClient()
    claims = [make_claim(1), make_claim(2)]  # identical text
    verifications, _, log = run(claims, client=client,
                                retriever=FakeRetriever([make_chunk()]))
    assert len(client.calls) == 1
    assert "CLM-002" not in "".join(client.calls)  # duplicate never sent
    assert [v.claim_id for v in verifications] == ["CLM-001", "CLM-002"]
    assert all(v.status == VERIFIED for v in verifications)
    assert any(e.get("query") == "verification cache" for e in log)


# ------------------------------------------------------------- web fetching

def test_web_fetch_uses_candidate_urls(monkeypatch, tmp_path):
    fetched = []

    def fake_fetch_page(url, domains, **kwargs):
        fetched.append((url, tuple(domains)))
        return FetchResult(
            url=url, final_url=url, status=200, content_type="text/html",
            title="Press Information Bureau",
            text="Article 368 empowers Parliament to amend the Constitution of India.",
            retrieved_at="2026-01-01T00:00:00+00:00",
        )

    monkeypatch.setattr(claim_verifier.page_fetcher, "fetch_page", fake_fetch_page)
    client = EchoClient()
    verifications, evidence, log = run(
        [make_claim()], client=client, sources=make_sources(["pib.gov.in"]),
        retriever=FakeRetriever([]), allow_web=True, cache_dir=tmp_path,
    )
    assert fetched  # something was "fetched", all through the fake
    assert all(u.startswith("https://") for u, _ in fetched)
    assert verifications[0].status == VERIFIED
    assert evidence and all(e.source_type == "official_domain" for e in evidence)
    assert any("url" in entry for entry in log)


def test_all_fetch_errors_become_source_unavailable(monkeypatch, tmp_path):
    def failing_fetch(url, domains, **kwargs):
        return FetchResult(url=url, final_url=url, status=0,
                           error="fetch failed: ConnectionError: boom")

    monkeypatch.setattr(claim_verifier.page_fetcher, "fetch_page", failing_fetch)
    client = EchoClient()
    verifications, evidence, _ = run(
        [make_claim()], client=client, sources=make_sources(["pib.gov.in"]),
        retriever=FakeRetriever([]), allow_web=True, cache_dir=tmp_path,
    )
    assert client.calls == []  # never invented a verdict from thin air
    assert evidence == []
    assert verifications[0].status == SOURCE_UNAVAILABLE
    assert verifications[0].requires_human_review


def test_no_web_when_local_evidence_sufficient(monkeypatch, tmp_path):
    def exploding_fetch(url, domains, **kwargs):  # pragma: no cover
        raise AssertionError("web fetch must not run when local evidence is rich")

    monkeypatch.setattr(claim_verifier.page_fetcher, "fetch_page", exploding_fetch)
    monkeypatch.setattr(claim_verifier.pdf_fetcher, "fetch_pdf", exploding_fetch)
    chunks = [make_chunk(), make_chunk(text="Amendment under Article 368 needs a special majority.")]
    verifications, _, _ = run(
        [make_claim()], client=EchoClient(), sources=make_sources(["pib.gov.in"]),
        retriever=FakeRetriever(chunks), allow_web=True, cache_dir=tmp_path,
    )
    assert verifications[0].status == VERIFIED


# ------------------------------------------------------------ source_search

def test_candidate_urls_roots_and_pib_special_case():
    sources = make_sources(["pib.gov.in", "egazette.gov.in"])
    urls = candidate_urls(make_claim(), sources)
    assert "https://pib.gov.in/" in urls
    assert "https://www.pib.gov.in/Allrel.aspx" in urls
    assert "https://egazette.gov.in/" in urls
    assert len(urls) == len(set(urls))


def test_candidate_urls_no_pib_no_special_case():
    urls = candidate_urls(make_claim(), make_sources(["egazette.gov.in"]))
    assert urls == ["https://egazette.gov.in/"]


def test_candidate_urls_empty_and_malformed_domains():
    assert candidate_urls(make_claim(), make_sources([])) == []
    urls = candidate_urls(make_claim(), make_sources(["bad domain", "a/b", ""]))
    assert urls == []


def test_candidate_urls_respects_max_urls():
    sources = make_sources(["pib.gov.in", "egazette.gov.in"])
    assert len(candidate_urls(make_claim(), sources, max_urls=1)) == 1


def test_build_queries_short_and_bounded():
    claim = make_claim(entities=["Parliament of India"])
    queries = build_queries(claim)
    assert 1 <= len(queries) <= 3
    assert all(q and len(q) <= 120 for q in queries)
    assert any("368" in q for q in queries)


# ------------------------------------------------------- evidence_extractor

def test_evidence_from_chunks_shape():
    (ev,) = evidence_from_chunks(make_claim(), [make_chunk()],
                                 make_sources().books)
    assert ev.evidence_id == "EV-001-1"
    assert ev.claim_id == "CLM-001"
    assert ev.source_type == "book"
    assert ev.approved_source
    assert ev.page_number == 101
    assert ev.retrieved_at  # iso timestamp filled in
    assert ev.edition == "7th"


def test_page_scoring_prefers_number_matching_passages():
    text = ("The Parliament and the Constitution together shape how leaders amend laws.\n"
            "Article 368 sets the special majority requirement.")
    fr = FetchResult(url="https://pib.gov.in/", final_url="https://pib.gov.in/",
                     status=200, text=text, title="PIB",
                     retrieved_at="2026-01-01T00:00:00+00:00")
    # passage_chars=80 keeps the two lines as separate passages
    (ev,) = evidence_from_page(make_claim(), fr, max_passages=1, passage_chars=80)
    assert "368" in ev.passage  # number overlap (x3) beat the word-only passage
    assert ev.source_type == "official_domain"
    assert ev.source_domain == "pib.gov.in"


def test_evidence_from_page_empty_text():
    fr = FetchResult(url="https://pib.gov.in/", final_url="https://pib.gov.in/",
                     status=200, text="")
    assert evidence_from_page(make_claim(), fr, max_passages=3) == []


def test_evidence_from_pdf_preserves_page_number():
    pr = PdfFetchResult(
        url="https://pib.gov.in/x.pdf", final_url="https://pib.gov.in/x.pdf",
        status=200, page_count=2,
        text_pages=["Unrelated preface content.",
                    "Article 368 empowers Parliament to amend the Constitution."],
        retrieved_at="2026-01-01T00:00:00+00:00",
    )
    (ev,) = evidence_from_pdf(make_claim(), pr, max_passages=1)
    assert ev.page_number == 2  # 1-based, second page
    assert ev.source_type == "official_publication"


def test_evidence_from_pdf_skips_near_empty_scanned():
    pr = PdfFetchResult(
        url="https://pib.gov.in/x.pdf", final_url="https://pib.gov.in/x.pdf",
        status=200, page_count=2, text_pages=["", " 368 "], scanned=True,
    )
    assert evidence_from_pdf(make_claim(), pr, max_passages=3) == []
