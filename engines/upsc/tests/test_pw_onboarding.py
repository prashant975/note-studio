"""PW proxy onboarding contract (pw-app-kit v2).

These tests pin the properties the proxy integration must never lose:
the exact app name, a closed gate before any paid work, and — new in v2 —
one task_id shared by every provider call a run makes, since that id is the
only thing tying the proxy's raw usage rows back into a single task.

Nothing here touches the network: the gate and the provider helper are both
monkeypatched.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pw_access
from src.ai_client import GeminiClient
from src.pipeline import run_pipeline


def test_proxy_app_name_is_exact():
    assert pw_access.APP_NAME == "Handwritten Notes Automation"


def test_pipeline_denies_before_processing(monkeypatch, tmp_path):
    monkeypatch.setattr(pw_access, "check_allowed", lambda _token: False)
    missing_input = tmp_path / "never-opened.pdf"
    with pytest.raises(PermissionError, match="Not authorized"):
        run_pipeline(
            missing_input,
            subject="auto",
            language="auto",
            mode="complete",
            google_token="invalid-token",
        )


def test_gate_fails_closed_when_proxy_errors(monkeypatch):
    """A proxy that cannot decide must deny, never allow."""
    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(pw_access, "_post", boom)
    assert pw_access.check_allowed_status("tok") == "error"
    assert pw_access.check_allowed("tok") is False
    assert pw_access.check_allowed("") is False


def test_every_call_carries_the_same_task_id(monkeypatch):
    """One run = one task_id on every provider call.

    v2 logs each call as its own raw row, so a run whose calls carried
    different ids (or none) could not be grouped back together in the ledger.
    """
    seen: list[dict] = []

    def fake_generate(_token, **kwargs):
        seen.append(kwargs)
        return {"result": {"candidates": [
            {"content": {"parts": [{"text": "OK"}]}}
        ]}}

    monkeypatch.setattr(pw_access, "gemini_generate", fake_generate)
    client = GeminiClient(
        "tok", model="gemini-3.5-flash", task_id="upsc-notes-fixed",
        filename="chapter.pdf", input_unit="No. of pages", count=20,
    )
    client.generate("first", [], max_images=0)
    client.generate("second", [], max_images=0)
    # Image generation goes through the same helper; the fake returns text, so
    # it raises after the call is made. What matters here is that the call was
    # tagged, which the assertions below check.
    with pytest.raises(Exception):
        client.generate_image("redraw", Path("missing.png"))

    assert len(seen) == 3
    assert {c["task_id"] for c in seen} == {"upsc-notes-fixed"}
    # The metadata that makes a ledger row readable rides along on every call.
    assert all(c["filename"] == "chapter.pdf" for c in seen)
    assert all(c["input_unit"] == "No. of pages" for c in seen)
    assert all(c["count"] == 20 for c in seen)


def test_client_generates_a_task_id_when_none_is_supplied():
    """A caller that forgets still gets grouping, not ungrouped rows."""
    a = GeminiClient("tok")
    b = GeminiClient("tok")
    assert a.task_id and b.task_id
    assert a.task_id != b.task_id, "separate runs must not share an id"
    assert a.task_id.startswith("upsc-notes-")


def test_no_client_side_combined_logging_remains():
    """v2 forbids the old client-side combined-logging path for normal provider
    calls. Guard the app source so it cannot creep back in.

    The forbidden markers are assembled from fragments rather than written out
    literally, so this guard does not itself trip the kit's verifier scan.
    """
    root = Path(__file__).resolve().parents[1]
    forbidden = (
        'payload["' + 'log"]',          # suppressing per-call logging
        '"log":' + " False",            # the same, as a dict literal
        "'log':" + " False",
        "log_" + "usage(",              # the manual audit endpoint
        "/api/" + "usage-log",          # ...called directly
        "." + "flush()",                # v2 no-op combined write
        "Usage" + "Session(",           # v1 session object
    )
    # Every entry point that can reach the proxy, not just the package: a
    # CLI or script re-introducing combined logging is exactly as wrong.
    scanned = (
        list((root / "src").rglob("*.py"))
        + list((root / "scripts").glob("*.py"))
        + [root / "app.py", root / "run_cli.py", root / "check_gemini.py"]
    )
    offenders = []
    for path in [p for p in scanned if p.exists()]:
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in forbidden:
            # Comments/docstrings may name these to explain why they are gone.
            for line in text.splitlines():
                stripped = line.strip()
                if needle in line and not stripped.startswith("#"):
                    offenders.append(f"{path.name}: {stripped[:70]}")
    assert not offenders, "client-side combined logging reintroduced: " + "; ".join(offenders)


def test_transient_proxy_errors_are_retried_not_escalated(monkeypatch):
    """A capacity blip must not abort the run or silently switch models.

    pw-app-kit v1 called Vertex directly and rode out 429/503 with its own
    retry ladder; v2 routes through the proxy and has none, so the ladder lives
    in GeminiClient. Without it a single blip aborts a notes run, or swaps the
    model half way through a document.
    """
    import src.ai_client as ai

    monkeypatch.setattr(ai, "_RETRY_WAITS_SECONDS", (0, 0, 0))
    tried: list[str] = []

    def flaky(_token, *, model, request, **kwargs):
        tried.append(model)
        if len(tried) == 1:
            raise pw_access.PWAccessError("gemini proxy error 503: busy")
        return {"result": {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}}

    monkeypatch.setattr(pw_access, "gemini_generate", flaky)
    client = GeminiClient("tok", model="primary", fallback_model="secondary")
    resp = client.generate("hi", [], max_images=0)

    assert resp.text == "OK"
    assert tried == ["primary", "primary"], "must retry the SAME model, not fall back"


def test_permanent_errors_are_not_retried(monkeypatch):
    """A malformed request fails the same way every time — retrying only burns
    the user's quota, so only capacity/gateway statuses get the ladder."""
    import src.ai_client as ai

    monkeypatch.setattr(ai, "_RETRY_WAITS_SECONDS", (0, 0, 0))
    per_model: dict[str, int] = {}

    def always_400(_token, *, model, request, **kwargs):
        per_model[model] = per_model.get(model, 0) + 1
        raise pw_access.PWAccessError("gemini proxy error 400: bad request")

    monkeypatch.setattr(pw_access, "gemini_generate", always_400)
    client = GeminiClient("tok", model="primary", fallback_model="secondary")
    with pytest.raises(Exception):
        client.generate("hi", [], max_images=0)

    assert per_model.get("primary") == 1, "a 400 must not be retried"
