"""The one-time import of the old app's settings: knobs only, never secrets,
once, and the right precedence when the engine starts."""
from __future__ import annotations

import os

from note_studio import db, paths, settings_store


def _old_env(text: str) -> None:
    folder = paths.shortnotes_original()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".env").write_text(text, encoding="utf-8")


def test_only_knobs_are_imported_never_secrets():
    db.init()
    _old_env("APP_PASSWORD=hunter2\nGEMINI_API_KEY=sk-abc\nSHEETS_BRIDGE_TOKEN=C:/x/token.json\n"
             "# a comment\nPW_PROXY_MODEL=gemini-3.6-flash\nMAX_CHUNKS=8\nSHEETS_BRIDGE_SPREADSHEET_ID=1abc\n")
    assert settings_store.import_old_env_once() == {"PW_PROXY_MODEL": "gemini-3.6-flash", "MAX_CHUNKS": "8"}
    stored = " ".join(r["value"] for r in db.query("SELECT value FROM settings"))
    for secret in ("hunter2", "sk-abc", "token.json", "1abc"):
        assert secret not in stored
    assert settings_store.import_record()["count"] == 2


def test_the_import_happens_once():
    db.init()
    _old_env("MAX_CHUNKS=8\n")
    settings_store.import_old_env_once()
    _old_env("MAX_CHUNKS=4\n")
    assert settings_store.import_old_env_once() == {"MAX_CHUNKS": "8"}


def test_without_the_old_file_the_old_apps_defaults_are_used(monkeypatch):
    db.init()
    assert settings_store.import_old_env_once() == {}
    assert settings_store.import_record()["found"] is False
    monkeypatch.delenv("PW_PROXY_MODEL", raising=False)
    settings_store.apply_engine_environment()
    assert os.environ["PW_PROXY_MODEL"] == "gemini-3.7-flash" and os.environ["SHEETS_BRIDGE_CACHE"] == "off"


def test_precedence_outside_value_then_imported_then_default(monkeypatch):
    db.init()
    _old_env("PW_PROXY_MODEL=gemini-3.6-flash\nMAX_CHUNKS=8\n")
    settings_store.import_old_env_once()
    monkeypatch.delenv("PW_PROXY_MODEL", raising=False)
    monkeypatch.setenv("MAX_CHUNKS", "3")
    monkeypatch.delenv("MAX_IMAGES", raising=False)
    settings_store.apply_engine_environment()
    assert os.environ["PW_PROXY_MODEL"] == "gemini-3.6-flash"     # imported beats the default
    assert os.environ["MAX_CHUNKS"] == "3"                        # set outside wins
    assert os.environ["MAX_IMAGES"] == "2"                        # the old app's default


def test_settings_shows_the_values_and_the_sign_in_card(client):
    page = client.get("/settings").text
    assert "Sign-in" in page and "Choose sign-in storage" in page
    for tab in ("AI models", "Categories", "Values", "System"):
        assert tab in page, tab
    values = client.get("/settings/values").text
    assert "Values you can change" in values and "MAX_CHUNKS" in values
    assert 'name="PW_PROXY_MODEL"' not in values                 # chosen in Settings -> AI models
    response = client.post("/settings/signin", follow_redirects=False)
    assert response.status_code == 303 and "kind=warn" in response.headers["location"]


def test_changing_values_from_the_page(client):
    response = client.post("/settings/values", data={"MAX_CHUNKS": "12", "SHEETS_BRIDGE_CACHE": "on"},
                           follow_redirects=False)
    assert "kind=ok" in response.headers["location"]
    assert settings_store.imported_values() == {"MAX_CHUNKS": "12", "SHEETS_BRIDGE_CACHE": "on"}
    bad = client.post("/settings/values", data={"MAX_CHUNKS": "lots"}, follow_redirects=False)
    assert "kind=warn" in bad.headers["location"] and settings_store.imported_values()["MAX_CHUNKS"] == "12"
    client.post("/settings/values", data={"MAX_CHUNKS": "16"})               # back to the old app's value
    assert "MAX_CHUNKS" not in settings_store.imported_values()
