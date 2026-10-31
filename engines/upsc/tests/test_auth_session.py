from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.auth_session import SESSION_MAX_AGE_SECONDS, session_expired, session_started_at


DAY = 24 * 60 * 60


def test_session_lasts_seven_days():
    assert SESSION_MAX_AGE_SECONDS == 7 * DAY


def test_session_uses_original_google_auth_time():
    claims = {"auth_time": 100, "iat": 500}
    assert session_started_at(claims) == 100
    assert not session_expired(claims, now=100 + 7 * DAY - 1)
    assert session_expired(claims, now=100 + 7 * DAY)


def test_session_survives_google_token_expiry():
    """An hour-old Google token must NOT end the app session: pw_access rides
    on a 7-day proxy session pass, so only the 6-day limit ends a sign-in."""
    claims = {"auth_time": 100, "iat": 100, "exp": 100 + 3600}
    assert not session_expired(claims, now=100 + 5 * DAY)


def test_session_falls_back_to_token_issue_time():
    assert session_started_at({"iat": "123"}) == 123


# --------------------------------------------------------------------------
# Regression: one process, two users. pw_access._session is a module-level
# global and pw_access._auth_token() reuses whatever pass it finds there
# WITHOUT checking the Google token it was handed. So proxy_session.attach()
# must evict another user's pass, and persist() must refuse to file a pass
# under an email it was not minted for. Otherwise a user who is not on the
# whitelist inherits the previous user's authorisation, and their spend is
# billed to that person.
# --------------------------------------------------------------------------

def _fake_proxy(monkeypatch, whitelist):
    """Minimal PW proxy: mints a pass per Google token, answers the allowlist
    for whoever the presented pass was actually minted for."""
    import json as _json
    import time as _time

    import pw_access

    issued: dict[str, str] = {}

    class _Resp:
        def __init__(self, code, data):
            self.status_code, self._data = code, data

        def json(self):
            return self._data

        @property
        def text(self):
            return _json.dumps(self._data)

    def fake_post(url, headers=None, json=None, timeout=None):
        auth = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        if url.endswith("/api/session"):
            token = "pass:" + auth
            issued[token] = auth
            return _Resp(200, {"session_token": token,
                               "expires_at_ms": (_time.time() + 7 * 86400) * 1000})
        if url.endswith("/api/allowlist"):
            owner = issued.get(auth, auth).replace("google:", "")
            return _Resp(200, {"allowed": owner in whitelist})
        return _Resp(404, {})

    monkeypatch.setattr(pw_access.requests, "post", fake_post)
    return issued


def _sign_in(email):
    """Mirror app.py's sign-in path, including that it ignores attach()."""
    import pw_access
    from src import proxy_session

    proxy_session.attach(email)
    status = pw_access.check_allowed_status(f"google:{email}")
    proxy_session.persist(email)
    return status


def test_second_user_cannot_inherit_first_users_authorisation(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _fake_proxy(monkeypatch, {"alice@pw.live"})

    import pw_access
    from src import proxy_session

    proxy_session.clear()
    assert _sign_in("alice@pw.live") == "allowed"
    # Alice closes the tab without signing out, so clear() never runs.
    assert _sign_in("bob@pw.live") == "denied", "bob must not inherit alice's pass"
    assert pw_access.check_allowed("google:bob@pw.live") is False


def test_denied_user_does_not_lock_out_a_whitelisted_user(monkeypatch, tmp_path):
    """The mirror image: a denied user's leftover pass must not deny the next,
    legitimate user (which would be a multi-day lockout)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _fake_proxy(monkeypatch, {"alice@pw.live"})

    from src import proxy_session

    proxy_session.clear()
    assert _sign_in("bob@pw.live") == "denied"
    assert _sign_in("alice@pw.live") == "allowed"


def test_pass_is_never_stored_under_another_users_email(monkeypatch, tmp_path):
    """A pass filed under the wrong email survives restarts, so the bypass
    would outlive the process that created it."""
    import json as _json

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    issued = _fake_proxy(monkeypatch, {"alice@pw.live"})

    from src import proxy_session

    proxy_session.clear()
    _sign_in("alice@pw.live")
    _sign_in("bob@pw.live")

    stored = _json.loads(proxy_session._store_path().read_text(encoding="utf-8"))
    minted_for = issued.get(stored["token"], "")
    assert minted_for == "google:" + stored["email"], (
        f"stored pass was minted for {minted_for!r} but filed under {stored['email']!r}"
    )
