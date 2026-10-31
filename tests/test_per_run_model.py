"""The one allowed change in the copy's sheets_bridge/pw_transport.py: each run
keeps its own model, account and fallback order (a context variable), and a
spent budget stops the run just as in the current original."""
from __future__ import annotations

import threading

import pytest

from note_studio import engine_catalog

ASK = {"messages": [{"role": "user", "content": "Say hi."}]}


@pytest.fixture
def kit(monkeypatch):
    """pw_access with both proxy routes faked; returns the list of (thread, route, model, token)."""
    engine_catalog.put_shortnotes_on_path()
    import pw_access
    from sheets_bridge import pw_transport

    seen: list[tuple] = []
    behaviour = {"gemini_budget_spent": False}

    def gemini(token_provider, *, model, request, **_kw):
        seen.append((threading.current_thread().name, "gemini", model, token_provider()))
        if behaviour["gemini_budget_spent"]:
            raise pw_access.PWAccessError("gemini proxy error 429: Budget has been exceeded for this app")
        return {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
                "usageMetadata": {}, "cost_inr": 0.5}

    def gpt(token_provider, *, model, request, **_kw):
        seen.append((threading.current_thread().name, "gpt", model, token_provider()))
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, "cost_inr": 0.7}

    monkeypatch.setattr(pw_access, "gemini_generate", gemini)
    monkeypatch.setattr(pw_access, "gpt_generate", gpt)
    monkeypatch.setattr(pw_transport, "_SPENT", {})
    monkeypatch.setattr(pw_transport, "FALLBACKS", ())
    return seen, behaviour


def _call():
    from notesbot import gemini_client

    return gemini_client._lite_client().chat.completions.create(model=gemini_client._lite_model(), **ASK)


def test_two_runs_at_once_each_use_their_own_model_and_account(kit):
    from sheets_bridge import pw_transport

    seen, _ = kit
    both_installed = threading.Barrier(2)

    def run(token, model):
        pw_transport.install(lambda: token, model=model)
        both_installed.wait(10)            # the second install must not change the first run's model
        for _ in range(3):
            _call()

    threads = [threading.Thread(target=run, args=("token-notes", "gemini-3.7-flash"), name="notes-lane"),
               threading.Thread(target=run, args=("token-sheets", "gpt-5.6-terra-ard-content"), name="sheets-lane")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(set(seen)) == [("notes-lane", "gemini", "gemini-3.7-flash", "token-notes"),
                                 ("sheets-lane", "gpt", "gpt-5.6-terra-ard-content", "token-sheets")]
    assert len(seen) == 6


def test_a_spent_budget_stops_without_trying_another_provider(kit):
    from notesbot import gemini_client
    from sheets_bridge import pw_transport

    seen, behaviour = kit
    behaviour["gemini_budget_spent"] = True
    assert pw_transport.install(lambda: "tok", model="gemini-3.7-flash",
                                fallbacks=("gemini-3.6-flash", "gpt-5.6-terra-ard-content")) == "gemini-3.7-flash"
    from notesbot.errors import ModelBudgetError

    with pytest.raises(ModelBudgetError, match="administrator"):
        _call()
    assert [(route, model) for _t, route, model, _tok in seen] == [("gemini", "gemini-3.7-flash")]
    assert gemini_client._lite_model() == "gemini-3.7-flash"
    # A new run keeps the chosen model; no silent provider/credential rotation.
    assert pw_transport.install(lambda: "tok", model="gemini-3.7-flash",
                                fallbacks=("gpt-5.6-terra-ard-content",)) == "gemini-3.7-flash"


def test_without_fallbacks_a_spent_budget_fails_as_before(kit):
    from sheets_bridge import pw_transport

    _seen, behaviour = kit
    behaviour["gemini_budget_spent"] = True
    pw_transport.install(lambda: "tok", model="gemini-3.7-flash")
    with pytest.raises(Exception) as caught:
        _call()
    assert type(caught.value).__name__ == "ModelBudgetError"


def test_an_install_without_a_run_context_behaves_as_before(kit):
    """A thread that never installed uses the last model installed (the original rule)."""
    from sheets_bridge import pw_transport

    seen, _ = kit
    pw_transport.install(lambda: "main-token", model="gemini-3.6-flash")
    worker = threading.Thread(target=_call, name="plain-thread")
    worker.start()
    worker.join(30)
    assert seen[-1][1:] == ("gemini", "gemini-3.6-flash", "main-token")


def test_settings_give_the_sheet_automation_its_fallbacks(client):
    from note_studio import models
    from sheets_bridge import pw_transport

    response = client.post("/settings/ai", data={"type_id": "concise_handdrawn", "stage": "notes",
                                                "model_0": "gemini-3.6-flash", "model_1": "gpt-5.6-sol-ard-content",
                                                "model_2": "", "model_3": ""}, follow_redirects=False)
    assert "kind=ok" in response.headers["location"]
    assert models.order("concise_handdrawn") == ["gemini-3.6-flash", "gpt-5.6-sol-ard-content"]
    assert pw_transport.FALLBACKS == ("gpt-5.6-sol-ard-content",)
    assert '<option value="gemini-3.6-flash" selected>' in client.get("/automation").text   # sheets start with it too
    bad = client.post("/settings/ai", data={"type_id": "concise_handdrawn", "stage": "notes", "model_0": "made-up"},
                      follow_redirects=False)
    assert "kind=warn" in bad.headers["location"]
    client.post("/settings/ai", data={"type_id": "concise_handdrawn", "stage": "notes", "reset": "1"})
    assert models.order("concise_handdrawn") == ["gemini-3.7-flash", "gpt-5.6-terra-ard-content"]
    assert "GPT-5.6 Terra" in client.get("/settings/ai").text
