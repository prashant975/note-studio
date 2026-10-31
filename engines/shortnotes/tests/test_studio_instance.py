import http.client

import pytest

from sheets_bridge import studio_instance as instance


def test_repeated_launch_opens_existing_studio(monkeypatch):
    calls = []
    monkeypatch.setattr(instance, "_get", lambda port, path, limit:
                        (200, b'{"ok":true}') if "health" in path else
                        (200, b'<title>Short Notes Studio</title>'))
    monkeypatch.setattr(instance.webbrowser, "open", calls.append)
    assert instance.activate_existing_studio(3000)
    assert calls == ["http://localhost:3000/"]


def test_no_open_reuses_studio_without_opening_browser(monkeypatch):
    monkeypatch.setattr(instance, "_get", lambda port, path, limit:
                        (200, b'{"ok":true}') if "health" in path else
                        (200, b'<title>Short Notes Studio</title>'))
    monkeypatch.setattr(instance.webbrowser, "open", lambda url: pytest.fail("browser opened"))
    assert instance.activate_existing_studio(3210, open_browser=False)


@pytest.mark.parametrize("health,page", [
    ((500, b'{"ok":true}'), (200, b'<title>Short Notes Studio</title>')),
    ((200, b'{"ok":false}'), (200, b'<title>Short Notes Studio</title>')),
    ((200, b'not json'), (200, b'<title>Short Notes Studio</title>')),
    ((200, b'{"ok":true}'), (200, b'<title>Another app</title>')),
    ((200, b'{"ok":true}'), (302, b'<title>Short Notes Studio</title>')),
])
def test_other_or_unhealthy_service_is_not_reused(monkeypatch, health, page):
    monkeypatch.setattr(instance, "_get", lambda port, path, limit: health if "health" in path else page)
    monkeypatch.setattr(instance.webbrowser, "open", lambda url: pytest.fail("browser opened"))
    assert not instance.activate_existing_studio(3000)


@pytest.mark.parametrize("error", [ConnectionRefusedError(), TimeoutError(), http.client.BadStatusLine("bad")])
def test_missing_service_starts_normally(monkeypatch, error):
    def missing(*args):
        raise error
    monkeypatch.setattr(instance, "_get", missing)
    assert not instance.activate_existing_studio(3000)


def test_existing_server_returns_before_signin_or_server_creation(monkeypatch):
    from sheets_bridge import studio_server
    monkeypatch.setattr(instance, "activate_existing_studio", lambda port, open_browser: True)
    monkeypatch.setattr(studio_server, "_local_env_defaults", lambda: pytest.fail("second server started"))
    assert studio_server.serve_studio(None, port=3210) == 0
