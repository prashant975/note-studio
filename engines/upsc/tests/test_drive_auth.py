"""The signed-in person's own Drive access: kept at sign-in, renewed per run.

Streamlit keeps only the id and access tokens from a sign-in, and the access
token dies after an hour of a seven-day sign-in — so without the refresh token
captured here, Drive links "shared inside PW" would work only in the first hour.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import tomllib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from src import drive_auth

ROOT = Path(__file__).resolve().parents[1]
EMAIL = "teacher@pw.live"
SCOPE = f"openid email profile {drive_auth.DRIVE_SCOPE}"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = tmp_path / "drive_access.json"
    monkeypatch.setattr(drive_auth, "_store_path", lambda: path)
    drive_auth._cache.clear()
    yield path
    drive_auth._cache.clear()


def _sign_in(scope=SCOPE, refresh="1//refresh-token", expires_in=3599):
    return {"access_token": "ya29.first", "expires_at": time.time() + expires_in,
            "refresh_token": refresh, "scope": scope, "userinfo": {"email": EMAIL}}


class Reply:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body or {}
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body


def test_a_sign_in_with_drive_access_is_kept_and_sealed(store):
    assert drive_auth.capture(_sign_in())
    assert drive_auth.is_connected(EMAIL)
    saved = store.read_text(encoding="utf-8")
    assert "1//refresh-token" not in saved or os.name != "nt"     # DPAPI on Windows
    drive_auth._cache.clear()
    assert drive_auth.is_connected(EMAIL)                          # from disk alone


def test_a_sign_in_without_drive_access_keeps_nothing(store):
    assert not drive_auth.capture(_sign_in(scope="openid email profile"))
    assert not drive_auth.is_connected(EMAIL) and not store.exists()


def test_the_sign_in_token_is_used_while_it_has_life_left():
    drive_auth.capture(_sign_in())
    with patch.object(drive_auth.requests, "post") as post:
        assert drive_auth.access_token(EMAIL, "id", "secret") == ("ya29.first", "")
    post.assert_not_called()


def test_a_token_too_close_to_expiry_is_renewed_from_the_refresh_token():
    drive_auth.capture(_sign_in(expires_in=600))      # 10 minutes: too little for a run
    with patch.object(drive_auth.requests, "post",
                      return_value=Reply(200, {"access_token": "ya29.fresh",
                                               "expires_in": 3599})) as post:
        assert drive_auth.access_token(EMAIL, "id", "secret") == ("ya29.fresh", "")
    sent = post.call_args.kwargs["data"]
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "1//refresh-token"
    assert sent["client_id"] == "id" and sent["client_secret"] == "secret"


def test_a_revoked_refresh_token_is_dropped():
    drive_auth.capture(_sign_in(expires_in=0))
    with patch.object(drive_auth.requests, "post",
                      return_value=Reply(400, text='{"error": "invalid_grant"}')):
        assert drive_auth.access_token(EMAIL, "id", "secret") == ("", "expired")
    assert not drive_auth.is_connected(EMAIL)


def test_google_unreachable_is_not_mistaken_for_a_revoked_sign_in():
    drive_auth.capture(_sign_in(expires_in=0))
    with patch.object(drive_auth.requests, "post",
                      side_effect=drive_auth.requests.ConnectionError("offline")):
        assert drive_auth.access_token(EMAIL, "id", "secret") == ("", "unavailable")
    assert drive_auth.is_connected(EMAIL)


def test_a_non_json_refresh_response_does_not_crash_or_forget_the_account():
    drive_auth.capture(_sign_in(expires_in=0))
    with patch.object(drive_auth.requests, "post", return_value=Reply()) as post:
        post.return_value.json = lambda: json.loads("<html>Bad gateway</html>")
        assert drive_auth.access_token(EMAIL, "id", "secret") == ("", "unavailable")
    assert drive_auth.is_connected(EMAIL)


@pytest.mark.parametrize("body", [
    ["not an object"],
    {"access_token": ["not a token"]},
    {"access_token": " "},
    {"access_token": "ya29.fresh", "expires_in": "not a number"},
    {"access_token": "ya29.fresh", "expires_in": -1},
    {"access_token": "ya29.fresh", "expires_in": 0},
    {"access_token": "ya29.fresh", "expires_in": "nan"},
    {"access_token": "ya29.fresh", "expires_in": "inf"},
    {"access_token": "ya29.fresh", "expires_in": 60},
])
def test_malformed_refresh_fields_are_unavailable_not_cached(body):
    drive_auth.capture(_sign_in(expires_in=0))
    old_cache = dict(drive_auth._cache)
    with patch.object(drive_auth.requests, "post", return_value=Reply(body=body)):
        assert drive_auth.access_token(EMAIL, "id", "secret") == ("", "unavailable")
    assert drive_auth._cache == old_cache
    assert drive_auth.is_connected(EMAIL)


@pytest.mark.parametrize("accounts", [[], "broken", {EMAIL: []}, {EMAIL: "broken"}])
def test_a_corrupted_account_store_does_not_crash_the_profile_or_generation(store, accounts):
    store.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
    assert not drive_auth.is_connected(EMAIL)
    assert drive_auth.connected_emails() == []
    assert drive_auth.access_token(EMAIL, "id", "secret") == ("", "not_connected")


def test_no_drive_sign_in_means_not_connected():
    assert drive_auth.access_token(EMAIL, "id", "secret") == ("", "not_connected")


def test_sign_out_forgets_only_that_person():
    drive_auth.capture(_sign_in())
    other = dict(_sign_in(), userinfo={"email": "other@pw.live"})
    drive_auth.capture(other)
    drive_auth.forget(EMAIL)
    assert not drive_auth.is_connected(EMAIL)
    assert drive_auth.is_connected("other@pw.live")


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_dpapi_round_trip():
    sealed = drive_auth._seal("1//secret")
    assert sealed["protection"] == "dpapi" and "1//secret" not in sealed["value"]
    assert drive_auth._open(sealed) == "1//secret"


def test_the_capture_wraps_the_sign_in_exchange_once(monkeypatch):
    from authlib.integrations.starlette_client import StarletteOAuth2App

    async def exchange(self, request, **kwargs):
        return _sign_in()

    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", exchange)
    assert drive_auth.install_signin_capture()
    wrapped = StarletteOAuth2App.authorize_access_token
    assert drive_auth.install_signin_capture()
    assert StarletteOAuth2App.authorize_access_token is wrapped      # not wrapped twice

    token = asyncio.run(StarletteOAuth2App.authorize_access_token(object(), object()))
    assert token["access_token"] == "ya29.first"                       # sign-in unchanged
    assert drive_auth.is_connected(EMAIL)


def test_a_failing_capture_never_breaks_the_sign_in(monkeypatch):
    from authlib.integrations.starlette_client import StarletteOAuth2App

    async def exchange(self, request, **kwargs):
        return _sign_in()

    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", exchange)
    monkeypatch.setattr(drive_auth, "capture", lambda token: 1 / 0)
    drive_auth.install_signin_capture()
    token = asyncio.run(StarletteOAuth2App.authorize_access_token(object(), object()))
    assert token["refresh_token"]


@pytest.mark.skipif(not (ROOT / ".streamlit" / "secrets.toml.example").is_file(),
                    reason="no sign-in template here (Note Studio's engine copy signs in its own way)")
def test_the_sign_in_asks_google_for_offline_read_only_drive_access():
    """What Streamlit + Authlib actually put in the sign-in URL, from the shipped
    secrets template (Google's metadata stubbed: no network)."""
    from authlib.integrations import starlette_client
    from streamlit.web.server.starlette.starlette_auth_routes import (
        _AuthlibConfig,
        _create_streamlit_oauth_class,
    )

    auth = tomllib.loads((ROOT / ".streamlit" / "secrets.toml.example").read_text(encoding="utf-8"))
    oauth = _create_streamlit_oauth_class(starlette_client)(
        config=_AuthlibConfig({"google": dict(auth["auth"]["google"])}))
    oauth.register("google")
    client = oauth.create_client("google")

    async def metadata():
        return {"authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth"}

    client.load_server_metadata = metadata
    url = asyncio.run(client.create_authorization_url("http://localhost:8501/oauth2callback"))["url"]
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert query["access_type"] == "offline"
    assert "consent" in query["prompt"].split()
    assert drive_auth.DRIVE_SCOPE in query["scope"].split()
    assert "https://www.googleapis.com/auth/drive" not in query["scope"].split()   # read-only
