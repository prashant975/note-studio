"""Every page answers in plain words, the menu has no dead links, and the
same-site guard and friendly errors work."""
from __future__ import annotations

import os
import re

from note_studio import brand, db, paths

PAGES = ["/", "/settings", "/settings/ai", "/settings/categories", "/settings/values", "/settings/system",
         "/new", "/automation", "/runs", "/practice", "/check", "/queue", "/runs/older", "/new?category=upsc"]


def test_every_page_answers_without_a_traceback(client):
    for path in PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Traceback" not in response.text, path
        assert brand.APP_NAME in response.text, path
        assert "no-store" in response.headers["cache-control"], path


def test_every_menu_link_answers(client):
    page = client.get("/").text
    links = {h for h in re.findall(r'href="(/[^"?#]*)', page) if not h.startswith("/static")}
    assert {"/new", "/automation", "/runs", "/practice", "/check", "/queue", "/settings"} <= links
    for href in links:
        assert client.get(href).status_code == 200, href


def test_static_files_and_htmx_are_served_locally(client):
    page = client.get("/").text
    assert "/static/vendor/htmx.min.js" in page and "cdn" not in page.lower()
    for path in re.findall(r'(?:href|src)="(/static/[^"]+)"', page):
        assert client.get(path).status_code == 200, path


def test_an_unknown_page_is_a_friendly_404(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "This page does not exist" in response.text and "Traceback" not in response.text


def test_a_crash_shows_a_reference_and_writes_the_log():
    from fastapi.testclient import TestClient

    from note_studio import app as app_module

    if not any(getattr(r, "path", "") == "/__crash-for-test" for r in app_module.app.routes):
        @app_module.app.get("/__crash-for-test")
        def _boom():
            raise ValueError("boom")
    app_module._prepare()
    with TestClient(app_module.app, raise_server_exceptions=False) as c:
        response = c.get("/__crash-for-test")
    assert response.status_code == 500
    assert "Something went wrong" in response.text and "Traceback" not in response.text
    ref = re.search(r'<span class="mono">([0-9A-F]{6})</span>', response.text).group(1)
    log = (paths.logs_dir() / "studio.log").read_text(encoding="utf-8")
    assert ref in log and "ValueError" in log


def test_a_post_from_another_site_is_refused(client):
    for headers in ({"sec-fetch-site": "cross-site"}, {"origin": "http://evil.example"}):
        response = client.post("/settings/old-app", data={"choice": "yes"}, headers=headers,
                               follow_redirects=False)
        assert response.status_code == 403, headers
    assert db.get_setting("share_old_app") == ""
    same = client.post("/settings/old-app", data={"choice": "yes"}, follow_redirects=False,
                       headers={"origin": "http://127.0.0.1:3100", "sec-fetch-site": "same-origin"})
    assert same.status_code == 303


def test_instance_names_this_copy(client):
    data = client.get("/api/instance").json()
    assert data["app"] == brand.APP_NAME and data["instance"] == paths.instance_id()


def test_overview_asks_for_the_old_app_choice_until_it_is_made(client):
    assert "Choose whether NoteCraft uses the old app" in client.get("/").text
    client.post("/settings/old-app", data={"choice": "no"})
    assert "Choose whether NoteCraft uses the old app" not in client.get("/").text
    assert "Nothing needs you right now" in client.get("/").text


def test_sharing_points_the_sheet_automation_at_the_old_app(client, old_app_folder):
    response = client.post("/settings/old-app", data={"choice": "yes"}, follow_redirects=False)
    assert response.status_code == 303 and "toast=" in response.headers["location"]
    assert db.get_setting("share_old_app") == "yes"
    assert os.environ["SHEETS_BRIDGE_PROFILES"] == str(old_app_folder / "sheets.json")
    assert os.environ["SHEETS_BRIDGE_TOKEN"] == str(old_app_folder / "token.json")
    assert os.environ["PW_PROXY_MODEL"]          # the engine's own launcher default
    page = client.get("/settings").text
    assert "Using the old app" in page and "someone@pw.live" in page
    assert "SECRET-TOKEN-VALUE" not in page      # counts only, never contents
    assert re.search(r'<span class="n">3</span><span class="l">sheets', page)


def test_keeping_separate_uses_notecrafts_own_folder(client, old_app_folder):
    client.post("/settings/old-app", data={"choice": "no"})
    assert os.environ["SHEETS_BRIDGE_PROFILES"] == str(paths.home() / "sheets" / "sheets.json")
    page = client.get("/settings").text
    assert "Kept separate" in page and "someone@pw.live" not in page


def test_a_made_up_choice_changes_nothing(client):
    response = client.post("/settings/old-app", data={"choice": "maybe"}, follow_redirects=False)
    assert response.status_code == 303 and "kind=warn" in response.headers["location"]
    assert db.get_setting("share_old_app") == ""


def test_the_shared_folder_is_never_written(client, old_app_folder):
    before = {p: p.read_bytes() for p in old_app_folder.rglob("*") if p.is_file()}
    client.post("/settings/old-app", data={"choice": "yes"})
    for path in PAGES:
        client.get(path)
    after = {p: p.read_bytes() for p in old_app_folder.rglob("*") if p.is_file()}
    assert before == after


def test_settings_lists_the_engines_on_board(client):
    page = client.get("/settings/system").text
    assert page.count("On board") >= 4            # shortnotes, upsc, pw_books, notes_qc are copied
    assert "PW Books handwritten" in page and "Notes check" in page
