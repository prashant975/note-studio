"""Google Docs with tabs (B.Sc. style) made from the app: New notes (Doc links) and Sheet automation (a
sheet's rows), with Google and PW AI replaced by fakes. The maker itself (typeset/gdoc_make.py) is tested
for its decisions; its Google writing is tested in test_typeset_gdoc.py."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from note_studio import (categories, category_admin, db, gdoc_service, gdoc_sheet, job_queue, make_service,
                         note_types)
from note_studio.typeset import gdoc_make

DOC_A = "1AAAAtestDocAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
DOC_B = "1BBBBtestDocBbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
DOC_C = "1CCCCtestDocCcccccccccccccccccccccccccccccccc"
SHEET = "1SSSStestSheetSsssssssssssssssssssssssssssss"
HEADER = ["exam", "batch", "subject", "faculty_id", "faculty_name", "faculty_email", "chapter", "Lecture_Date",
          "lecture_topic", "downloadable_link", "Auto Status", "Review Status\n(by ARD Team)", "Concise Notes Link",
          "Uploading status\n(by BM Team)", "Lecture Folder Link"]


# ------------------------------------------------------------------ fakes
class FakeSheets:
    """Enough of the Sheets API: one tab, cells as dicts, values().batchUpdate writes formattedValue."""

    def __init__(self, header, rows, tab="Bsc Nursing Notes", gid=689523849):
        self.tab, self.gid = tab, gid
        self.grid = [[{"formattedValue": h} for h in header]] + rows
        self.writes = []

    # spreadsheets()
    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, fields="", ranges=None, includeGridData=False):
        assert spreadsheetId == SHEET
        if ranges:
            assert ranges == [f"'{self.tab}'"]
            return _Call({"sheets": [{"data": [{"rowData": [{"values": list(r)} for r in self.grid]}]}]})
        return _Call({"sheets": [{"properties": {"title": "Sheet1", "sheetId": 0, "index": 0}},
                                 {"properties": {"title": self.tab, "sheetId": self.gid, "index": 1}}]})

    def batchUpdate(self, spreadsheetId, body):
        for change in body["data"]:
            match = re.fullmatch(r"'(.+)'!([A-Z]+)(\d+)", change["range"])
            assert match and match.group(1) == self.tab
            column = sum((ord(c) - 64) * 26 ** i for i, c in enumerate(reversed(match.group(2)))) - 1
            row = int(match.group(3)) - 1
            while len(self.grid) <= row:
                self.grid.append([])
            while len(self.grid[row]) <= column:
                self.grid[row].append({})
            self.grid[row][column] = {"formattedValue": change["values"][0][0]}
            self.writes.append((change["range"], change["values"][0][0]))
        return _Call({})


class _Call:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def row(topic, link_cell, subject="Applied Nutrition and Dietetics", extra=None):
    cells = [{"formattedValue": "Bsc Nursing"}, {}, {"formattedValue": subject}, {}, {}, {},
             {"formattedValue": "1st Year"}, {}, {"formattedValue": topic}, link_cell]
    return cells + (extra or [])


def hyperlink(doc):
    url = f"https://docs.google.com/document/d/{doc}/edit?usp=sharing"
    return {"formattedValue": url, "hyperlink": url}


def chip(doc):
    return {"formattedValue": "￼", "chipRuns": [{"chip": {"richLinkProperties": {
        "uri": f"https://docs.google.com/document/d/{doc}/edit"}}}]}


def fake_result(source_id, folder, ok=True, verdict="passed", tabs=1):
    document_id = "1NEW" + source_id[4:]
    result = {"document_id": document_id, "url": gdoc_make.doc_url(document_id),
              "title": f"Unit {source_id[:4]} – Nursing Notes (B.Sc. style)", "source_id": source_id,
              "source_url": gdoc_make.doc_url(source_id), "source_title": f"Unit {source_id[:4]}",
              "tab_ids": [f"t.{i}" for i in range(tabs)], "pages": 9,
              "verification": {"Tab 1": {"problems": [] if ok else ["Paragraph 3 differs."]}},
              "watermark": {"1": True}, "header_logo": {"1": True}, "ok": ok,
              "quality": [{"tab": 1, "title": "Tab 1", "verdict": verdict, "pages": 9, "blocking": [], "advisory": [],
                           "problem": "", "cost_inr": 1.5}] if ok else [],
              "total_cost_inr": 3.0 if ok else 1.5}
    Path(folder).mkdir(parents=True, exist_ok=True)
    (Path(folder) / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return result


@pytest.fixture
def signed_in(monkeypatch):
    from note_studio import signin

    monkeypatch.setattr(gdoc_service, "google_creds", lambda: object())
    monkeypatch.setattr(gdoc_service, "account_domain", lambda: "pw.live")
    monkeypatch.setattr(make_service, "typeset_token", lambda: (lambda: "test-token"))
    monkeypatch.setattr(signin, "snapshot", lambda: {"state": "ready", "email": "someone@pw.live", "error": "",
                                                     "configured": True})
    yield
    job_queue.wait_idle(60)


@pytest.fixture
def maker(monkeypatch):
    """gdoc_make.make replaced: records each call, returns a result (per source id, if set)."""
    calls, outcomes = [], {}

    def make(source_id, folder, **kwargs):
        calls.append({"source_id": source_id, "folder": Path(folder), **kwargs})
        return fake_result(source_id, folder, **outcomes.get(source_id, {}))
    monkeypatch.setattr(gdoc_service, "make_doc", make)
    make.calls, make.outcomes = calls, outcomes
    return make


def nursing(client) -> str:
    added = client.post("/settings/categories/add", data={
        "label": "Nursing", "subjects": "Applied Anatomy\nApplied Nutrition and Dietetics\n",
        "languages": ["english", "hindi", "hinglish"], "note_types": ["typeset_notes"]}, follow_redirects=False)
    assert "kind=ok" in added.headers["location"]
    return "nursing"


# ------------------------------------------------------------------ registry and settings
def test_the_note_type_is_ready_gemini_37_only_and_made_from_a_doc_link():
    entry = note_types.get("typeset_gdoc")
    assert entry["status"] == "ready" and entry["engine"] == "typeset" and note_types.available("typeset_gdoc")
    assert entry["inputs"] == ["gdoc_link"] and entry["outputs"] == ["gdoc"]
    assert entry["models"] == {"notes": ["gemini-3.7-flash"], "qc": ["gemini-3.7-flash"]}     # 3.6 fails (16 Nov)
    assert note_types.option_defaults("typeset_gdoc") == {"share_domain": True}


def test_a_category_with_the_pw_logo_offers_google_docs_without_a_settings_step(client):
    """Nursing (added from Settings, so it has the PW logo) offers Google Docs straight away, on New notes and
    Sheet automation; Settings can switch it off. The built-in B.Sc. Wallah pack keeps its own look until
    switched on there."""
    cid = nursing(client)
    assert categories.get(cid)["note_types"] == ["typeset_notes"]
    assert categories.gdoc_offered(categories.get(cid)) and not categories.gdoc_offered(categories.get("bsc"))
    assert [t["id"] for t in categories.offered_types(categories.get(cid))] == ["typeset_notes", "typeset_gdoc"]
    assert "Google Doc with tabs (B.Sc. style)" in client.get(f"/new?category={cid}").text
    auto = client.get("/automation").text
    assert 'href="#gdocs"' in auto and f'<option value="{cid}">Nursing</option>' in auto
    assert "No category makes Google Docs yet" not in auto
    page = client.get("/settings/categories").text
    assert f'action="/settings/categories/{cid}/gdoc"' in page and "Stop offering Google Docs" in page
    assert 'action="/settings/categories/neet_jee/gdoc"' not in page        # not a B.Sc.-style category

    off = client.post(f"/settings/categories/{cid}/gdoc", data={"on": "0"}, follow_redirects=False)
    assert "kind=ok" in off.headers["location"]
    folder = Path(categories.get(cid)["folder"])
    meta = json.loads((folder / "category.json").read_text("utf-8"))
    assert meta["note_types"] == ["typeset_notes"] and meta["offer_gdoc"] is False
    assert "offer_gdoc" not in json.loads((folder / "category.json.bak").read_text("utf-8"))
    assert not categories.gdoc_offered(categories.get(cid))
    assert 'value="typeset_gdoc"' not in client.get(f"/new?category={cid}").text
    client.post(f"/settings/categories/{cid}/gdoc", data={"on": "1"})
    meta = json.loads((folder / "category.json").read_text("utf-8"))
    assert meta["note_types"] == ["typeset_notes", "typeset_gdoc"] and "offer_gdoc" not in meta
    client.post(f"/settings/categories/{cid}/gdoc", data={"on": "1"})                 # twice: still once
    assert categories.get(cid)["note_types"] == ["typeset_notes", "typeset_gdoc"]
    assert not [c for c in category_admin.check(cid) if c["kind"] == "bad"]

    client.post("/settings/categories/bsc/gdoc", data={"on": "1"})                    # B.Sc. Wallah: on request
    assert categories.gdoc_offered(categories.get("bsc"))
    refused = client.post("/settings/categories/neet_jee/gdoc", data={"on": "1"}, follow_redirects=False)
    assert "kind=warn" in refused.headers["location"]
    assert not categories.gdoc_offered(categories.get("neet_jee"))


# ------------------------------------------------------------------ links and rows
def test_pasted_links_become_doc_ids_once_each():
    text = (f"https://docs.google.com/document/d/{DOC_A}/edit?usp=sharing\n\n"
            f"  https://docs.google.com/document/d/{DOC_B}/edit?tab=t.0  \n{DOC_A}\n")
    assert gdoc_service.link_ids(text) == [(DOC_A, gdoc_make.doc_url(DOC_A)), (DOC_B, gdoc_make.doc_url(DOC_B))]
    with pytest.raises(make_service.FormError, match="not a Google Doc link"):
        gdoc_service.link_ids(f"https://docs.google.com/spreadsheets/d/{SHEET}/edit")
    with pytest.raises(make_service.FormError, match="at least one"):
        gdoc_service.link_ids("  \n ")


def test_sheet_links_and_row_choices_in_plain_words():
    assert gdoc_sheet.parse_link(f"https://docs.google.com/spreadsheets/d/{SHEET}/edit?gid=689523849#gid=689523849") \
        == (SHEET, 689523849)
    assert gdoc_sheet.parse_link(f"https://docs.google.com/spreadsheets/d/{SHEET}/edit?usp=sharing") == (SHEET, None)
    with pytest.raises(gdoc_sheet.SheetError, match="not a Google Sheets link"):
        gdoc_sheet.parse_link(gdoc_make.doc_url(DOC_A))
    assert gdoc_sheet.parse_rows("") is None
    assert gdoc_sheet.parse_rows("16-18, 21") == {16, 17, 18, 21}
    for bad in ("1-3", "9-4", "abc"):
        with pytest.raises(gdoc_sheet.SheetError):
            gdoc_sheet.parse_rows(bad)
    assert [gdoc_sheet.letter(i) for i in (0, 15, 25, 26)] == ["A", "P", "Z", "AA"]


def test_rows_are_read_by_hyperlink_chip_or_plain_link():
    sheets = FakeSheets(HEADER + [gdoc_sheet.HEADER], [
        row("Clinical Enzymology", hyperlink(DOC_A)),
        row("Introduction to Nutrition", chip(DOC_B)),
        [{"formattedValue": "Bsc Nursing"}],                                       # a row with no link
        row("Carbohydrates & Energy", {"formattedValue": gdoc_make.doc_url(DOC_C)},
            extra=[{}] * 5 + [{"formattedValue": "https://docs.google.com/document/d/1DONE/edit"}]),
    ])
    found = gdoc_sheet.read(sheets, SHEET, "Bsc Nursing Notes")
    assert found["column"] == 15 and found["link_column"] == 9
    assert [(r["row"], r["topic"], r["source_id"], bool(r["link"])) for r in found["rows"]] == [
        (2, "Clinical Enzymology", DOC_A, False), (3, "Introduction to Nutrition", DOC_B, False),
        (5, "Carbohydrates & Energy", DOC_C, True)]


def test_a_link_is_written_only_into_an_empty_cell_of_the_same_row():
    sheets = FakeSheets(HEADER + [gdoc_sheet.HEADER], [
        row("A", hyperlink(DOC_A)), row("B", hyperlink(DOC_B), extra=[{}] * 5 + [{"formattedValue": "kept"}])])
    url = gdoc_make.doc_url("1NEWDOC" + "x" * 30)
    assert gdoc_sheet.write_link(sheets, SHEET, sheets.tab, 2, DOC_A, url) == "written to P2"
    assert gdoc_sheet.write_link(sheets, SHEET, sheets.tab, 2, DOC_A, url) == "already there"
    assert gdoc_sheet.write_link(sheets, SHEET, sheets.tab, 3, DOC_B, url) == "not written: P3 holds something else"
    assert gdoc_sheet.write_link(sheets, SHEET, sheets.tab, 2, DOC_B, url).startswith("not written: the row's")
    assert sheets.writes == [("'Bsc Nursing Notes'!P2", url)]


def test_a_missing_column_is_added_after_the_last_header_in_use():
    sheets = FakeSheets(HEADER, [row("A", hyperlink(DOC_A))])
    url = gdoc_make.doc_url("1NEWDOC" + "y" * 30)
    assert gdoc_sheet.write_link(sheets, SHEET, sheets.tab, 2, DOC_A, url) == "written to P2"
    assert sheets.writes == [("'Bsc Nursing Notes'!P1", gdoc_sheet.HEADER), ("'Bsc Nursing Notes'!P2", url)]
    assert gdoc_sheet.same_reading(HEADER, HEADER + [gdoc_sheet.HEADER])


# ------------------------------------------------------------------ the maker's decisions
def test_the_template_record_is_seeded_once_in_the_data_folder():
    found = gdoc_make.template()
    assert found["template_id"] == gdoc_make.DEFAULT_TEMPLATE["template_id"] and found["tabs"] == 34
    assert gdoc_make.template_path().is_file() and str(gdoc_make.template_path()).startswith(str(db.paths.home()))
    gdoc_make.template_path().write_text(json.dumps({"template_id": "1OTHER", "tabs": 40, "ready": True}), "utf-8")
    assert gdoc_make.template()["template_id"] == "1OTHER"


@pytest.mark.parametrize("verified, marks, shared", [(True, True, True), (False, True, False), (True, False, False)])
def test_quality_runs_only_on_a_verified_doc_and_only_a_good_doc_is_shared(monkeypatch, tmp_path, verified, marks, shared):
    import sys
    import types

    from note_studio.typeset import render

    monkeypatch.setitem(sys.modules, "pw_access", types.SimpleNamespace(new_task_id=lambda prefix: prefix + "-1"))
    tabs = [{"n": 1, "title": "Tab 1", "new_tab_id": "", "folder": tmp_path}]
    steps = []
    monkeypatch.setattr(gdoc_make, "read_source", lambda g, sid, folder, say: ({"title": "Clinical Enzymology"}, tabs))
    monkeypatch.setattr(gdoc_make, "style_all", lambda *a, **k: [{"tab": 1, "cost_inr": 2.0}])
    monkeypatch.setattr(gdoc_make, "write_doc", lambda g, sid, d, t, b, tpl, title, say: (
        steps.append(("write", title)) or {"document_id": "1NEW", "url": gdoc_make.doc_url("1NEW"), "title": title,
                                            "tab_ids": ["t.0"], "written_at": 1.0}))
    monkeypatch.setattr(gdoc_make, "verify_doc", lambda g, r, t: {"Tab 1": {"problems": [] if verified else ["x"]}})

    def pages(g, result, t, logo, say):
        steps.append(("pages", logo))
        result.update(watermark={"1": marks}, header_logo={"1": True}, pages=13)
        return {1: b"%PDF"}
    monkeypatch.setattr(gdoc_make, "pages_and_marks", pages)
    monkeypatch.setattr(gdoc_make, "quality_all", lambda *a, **k: steps.append(("quality",)) or [
        {"tab": 1, "verdict": "passed", "cost_inr": 1.5}])
    monkeypatch.setattr(gdoc_make, "share_with_domain", lambda g, doc, domain: steps.append(("share", domain)) or "now editable")
    branding = render.Branding(header_left="Nursing", header_right="Nursing Notes", footer_left="Nursing", pw_logo=True)
    result = gdoc_make.make(DOC_A, tmp_path / "run", creds=object(), token_provider=lambda: "t", branding=branding,
                            style_models=["gemini-3.7-flash"], quality_models=["gemini-3.7-flash"], share_domain="pw.live")
    assert result["title"] == "Clinical Enzymology – Nursing Notes (B.Sc. style)"
    assert result["ok"] is (verified and marks)
    assert (("quality",) in steps) is verified and (("share", "pw.live") in steps) is shared
    assert result["total_cost_inr"] == (3.5 if verified else 2.0)
    assert json.loads((tmp_path / "run" / "result.json").read_text("utf-8"))["ok"] is result["ok"]
    if not verified:
        assert gdoc_make.problems(result) == ["The new Doc does not match the source word for word in: Tab 1."]


# ------------------------------------------------------------------ New notes
def test_new_notes_takes_google_doc_links_and_each_becomes_a_doc(client, signed_in, maker):
    cid = nursing(client)
    page = client.get(f"/new?category={cid}&type=typeset_gdoc").text
    assert 'name="doc_links"' in page and "Google Doc links" in page and 'name="files"' not in page
    assert "Anyone at PW can edit it (with the link)" in page and "for each Google Doc" in page

    bad = client.post("/new", data={"category": cid, "note_type": "typeset_gdoc", "subject": "auto",
                                    "language": "english", "doc_links": "not a link"})
    assert bad.status_code == 400 and "This is not a Google Doc link" in bad.text and "not a link" in bad.text

    links = f"https://docs.google.com/document/d/{DOC_A}/edit\nhttps://docs.google.com/document/d/{DOC_B}/edit"
    form = {"category": cid, "note_type": "typeset_gdoc", "subject": "auto", "language": "english",
            "doc_links": links, "share_domain": "on"}
    added = client.post("/new", data=form, follow_redirects=False)
    assert added.status_code == 303 and "2 Google Docs" in unquote(added.headers["location"])
    again = client.post("/new", data=form, follow_redirects=False)
    assert "not added twice" in unquote(again.headers["location"])
    assert job_queue.wait_idle(60)

    runs = db.query("SELECT * FROM runs WHERE note_type='typeset_gdoc' ORDER BY id")
    assert [(r["state"], r["source"], r["title"]) for r in runs] == [
        ("complete", "gdoc", f"Unit {DOC_A[:4]}"), ("complete", "gdoc", f"Unit {DOC_B[:4]}")]
    call = maker.calls[0]
    assert call["source_id"] == DOC_A and call["share_domain"] == "pw.live" and call["token_provider"]() == "test-token"
    assert call["style_models"] == ["gemini-3.7-flash"] and call["quality_models"] == ["gemini-3.7-flash"]
    look = call["branding"]
    assert (look.header_left, look.header_right, look.pw_logo) == ("Nursing", "Nursing Notes", True)
    assert runs[0]["cost_inr"] == 3.0 and runs[0]["message"] == "Done: Google Doc with 1 tab, 9 pages."

    run_page = client.get(f"/runs/{runs[0]['id']}").text
    new_url = gdoc_make.doc_url("1NEW" + DOC_A[4:])
    assert "Open the Google Doc" in run_page and new_url in run_page and "Source Google Doc" in run_page
    assert "Traceback" not in run_page and "Check these notes" not in run_page
    item = db.query("SELECT id FROM run_items WHERE run_id=?", (runs[0]["id"],))[0]["id"]
    opened = client.get(f"/runs/{runs[0]['id']}/files/{item}", follow_redirects=False)
    assert opened.status_code == 303 and opened.headers["location"] == new_url
    report = json.loads((Path(runs[0]["run_dir"]) / "quality_report.json").read_text("utf-8"))
    assert report["url"] == new_url and report["quality"][0]["verdict"] == "passed"

    retried = client.post(f"/runs/{runs[0]['id']}/again", follow_redirects=False)
    assert retried.headers["location"].startswith("/queue")
    assert job_queue.wait_idle(60)
    assert [c["source_id"] for c in maker.calls] == [DOC_A, DOC_B, DOC_A]


def test_a_doc_that_fails_its_checks_is_not_handed_over(client, signed_in, maker):
    cid = nursing(client)
    maker.outcomes[DOC_A] = {"ok": False}
    maker.outcomes[DOC_B] = {"verdict": "needs_attention"}
    client.post("/new", data={"category": cid, "note_type": "typeset_gdoc", "subject": "auto", "language": "english",
                              "doc_links": f"{DOC_A}\n{DOC_B}"})
    assert job_queue.wait_idle(60)
    failed, attention = db.query("SELECT * FROM runs WHERE note_type='typeset_gdoc' ORDER BY id")
    assert failed["state"] == "failed" and "word for word" in failed["error"]
    assert not db.query("SELECT * FROM run_items WHERE run_id=?", (failed["id"],))
    assert attention["state"] == "needs_attention" and "Review the quality report for: Tab 1" in attention["message"]
    assert db.query("SELECT * FROM run_items WHERE run_id=?", (attention["id"],))


def test_a_missing_sign_in_holds_the_queue(client, monkeypatch, maker):
    from note_studio import signin

    cid = nursing(client)
    monkeypatch.setattr(gdoc_service, "google_creds", lambda: (_ for _ in ()).throw(
        signin.SignInNeeded("Sign in with Google in Settings, then press Try again.")))
    client.post("/new", data={"category": cid, "note_type": "typeset_gdoc", "subject": "auto", "language": "english",
                              "doc_links": DOC_A})
    assert job_queue.wait_idle(60)
    run = db.query("SELECT * FROM runs WHERE note_type='typeset_gdoc'")[0]
    assert run["state"] == "failed" and "Sign in" in run["error"] and not maker.calls
    assert job_queue.held() == "signin"
    job_queue.resume()


# ------------------------------------------------------------------ Sheet automation
def test_a_sheet_run_makes_the_rows_without_a_doc_and_writes_their_links(client, signed_in, maker, monkeypatch):
    cid = nursing(client)
    sheets = FakeSheets(HEADER + [gdoc_sheet.HEADER], [
        row("Clinical Enzymology", hyperlink(DOC_A)),
        row("Introduction to Nutrition", hyperlink(DOC_B), extra=[{}] * 5 + [{"formattedValue": "https://x/done"}]),
        row("Carbohydrates & Energy", chip(DOC_C)),
    ])
    monkeypatch.setattr(gdoc_service, "sheets_client", lambda: sheets)
    monkeypatch.setattr(gdoc_make.Google, "sheets", property(lambda self: sheets))
    maker.outcomes[DOC_C] = {"ok": False}

    page = client.get("/automation").text
    assert 'id="gdocs"' in page and 'action="/automation/docs/run"' in page and f'value="{cid}"' in page
    link = f"https://docs.google.com/spreadsheets/d/{SHEET}/edit#gid={sheets.gid}"
    preview = client.post("/automation/docs/check", data={"sheet_link": link, "rows": ""}).text
    assert "Bsc Nursing Notes" in preview and "2 rows would be made" in preview and "Clinical Enzymology" in preview
    assert "1 already has one in column P" in preview and not sheets.writes

    added = client.post("/automation/docs/run", data={"sheet_link": link, "rows": "2-4", "category": cid,
                                                      "share": "on"}, follow_redirects=False)
    assert added.headers["location"].startswith("/queue") and "2 rows" in unquote(added.headers["location"])
    assert job_queue.wait_idle(60)
    run = db.query("SELECT * FROM runs WHERE source='gdoc_sheet'")[0]
    assert run["state"] == "needs_attention" and run["message"] == "Made 1 Google Doc; 1 did not finish."
    assert sorted(c["source_id"] for c in maker.calls) == sorted([DOC_A, DOC_C])
    assert all(c["share_domain"] == "pw.live" for c in maker.calls)
    assert sheets.writes == [("'Bsc Nursing Notes'!P2", gdoc_make.doc_url("1NEW" + DOC_A[4:]))]
    items = db.query("SELECT label, state, output_path FROM run_items WHERE run_id=? ORDER BY position", (run["id"],))
    assert [(i["label"], i["state"]) for i in items] == [("Row 2 · Clinical Enzymology", "done"),
                                                         ("Row 4 · Carbohydrates & Energy", "failed")]
    card = client.get(f"/runs/{run['id']}").text
    assert "Open the Google Doc" in card and "Did not finish" not in card.split("Row 2")[0] and "Traceback" not in card

    nothing = client.post("/automation/docs/run", data={"sheet_link": link, "rows": "2-3", "category": cid},
                          follow_redirects=False)
    assert "kind=warn" in nothing.headers["location"]           # row 2 has its Doc now, row 3 had one


def test_a_sheet_run_needs_a_category_that_makes_google_docs(client, signed_in):
    with pytest.raises(gdoc_sheet.SheetError, match="Choose a category"):
        gdoc_service.enqueue_sheet("bsc", f"https://docs.google.com/spreadsheets/d/{SHEET}/edit", "")
    page = client.get("/automation").text
    assert 'id="gdocs"' in page and "No category makes Google Docs yet" in page


def test_the_guides_and_the_tour_name_it():
    from note_studio import guides, whats_new

    assert any("Google Doc with tabs" in s for s in guides.for_path("/new")["steps"])
    assert any("Google Docs from a sheet" in s for s in guides.GUIDES["/automation"]["steps"])
    assert whats_new.TOURS[0]["id"] == "2026-11-24-gdoc-bsc"
    assert {s["href"] for s in whats_new.TOURS[0]["steps"]} == {"/settings/categories", "/new", "/automation#gdocs"}
