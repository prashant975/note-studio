"""Editable Google Docs in the B.Sc. style, made from a final-notes Google Doc (Nursing, 16-24 Nov 2026).

The source Doc's words are locked: AI only chooses the B.Sc. look of each tab (word_source.style), and every
tab of the new Doc is read back and checked word for word (gdoc_writer.verify). The steps, each saved in the
run's folder so a retry pays nothing twice:

  1 read every tab of the source Doc literally (free)
  2 AI chooses the B.Sc. formatting of each tab, a few tabs at a time (paid; saved per tab)
  3 copy the PW watermark template, keep one tab per source tab, write every tab (free)
  4 read the new Doc back and check every tab word for word (free)
  5 export each tab as a PDF, fix a blank or near-empty last page, check the PW watermark (and logo) on
    every page, then the final NEET/JEE quality check of each tab (paid; saved per tab)
  6 share it so anyone at the account's domain can edit it with the link (optional)

Google's service objects (httplib2) are not thread-safe, so every thread builds its own (Google below).
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import ai, gdoc_source, gdoc_writer, render, source, source_quality, word_source

# The 34-tab PW watermark template (16 Nov 2026): every tab carries Google's own watermark, which the
# API cannot add. A note copies it and keeps one tab per source tab, so a source may have up to 34 tabs.
DEFAULT_TEMPLATE = {"template_id": "1VOEMzskaheh-nPBz7e7oRFIboLkdSb3q20zJQbvgVek", "tabs": 34, "ready": True,
                    "branded": False}
TASK_PREFIX = "studio-typeset-gdoc"
_DOC_ID = re.compile(r"/document/d/([A-Za-z0-9_-]{20,})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{25,}$")


class GdocError(RuntimeError):
    """A plain-words reason the Doc could not be made."""


def doc_id(link: str) -> str:
    """The Google Doc id in a link (or a bare id); '' when it is not a Google Doc."""
    text = str(link or "").strip()
    found = _DOC_ID.search(text)
    if found:
        return found.group(1)
    return text if _BARE_ID.match(text) else ""


def doc_url(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


# ------------------------------------------------------------------ template
def template_path() -> Path:
    from .. import paths

    return paths.home() / "gdoc" / "template.json"


def template() -> dict:
    """The watermark template record (~/.note_studio/gdoc/template.json); the 34-tab one by default."""
    path = template_path()
    if path.is_file():
        try:
            found = json.loads(path.read_text("utf-8"))
            if isinstance(found, dict) and found.get("template_id"):
                return found
        except (OSError, ValueError):
            pass
    found = dict(DEFAULT_TEMPLATE, url=doc_url(DEFAULT_TEMPLATE["template_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(found, indent=2), encoding="utf-8")
    return found


# ------------------------------------------------------------------ Google
class Google:
    """Signed-in Google clients, one set per thread."""

    def __init__(self, creds):
        self.creds = creds
        self._local = threading.local()

    def _client(self, name: str, version: str):
        found = getattr(self._local, name, None)
        if found is None:
            from googleapiclient.discovery import build

            found = build(name, version, credentials=self.creds, cache_discovery=False)
            setattr(self._local, name, found)
        return found

    @property
    def docs(self):
        return self._client("docs", "v1")

    @property
    def drive(self):
        return self._client("drive", "v3")

    @property
    def sheets(self):
        return self._client("sheets", "v4")

    @property
    def authorized(self):
        found = getattr(self._local, "session", None)
        if found is None:
            from google.auth.transport.requests import AuthorizedSession

            found = AuthorizedSession(self.creds)
            self._local.session = found
        return found


def downloader(google: Google, document: dict | None, say, workers: int = 8):
    """Signed-in picture downloads (a plain request to a picture link does not connect). Each picture gets
    a short connect timeout and four tries; with the document given, all are fetched up front."""
    import requests

    def download(uri: str) -> bytes:
        for attempt in range(4):
            try:
                answer = google.authorized.get(uri, timeout=(15, 120))
                if answer.status_code == 200 and answer.content:
                    return answer.content
            except requests.RequestException:
                pass
            time.sleep(2 * (attempt + 1))
        raise source.SourceError("A picture of the source Doc could not be downloaded after four tries.")

    ready: dict[str, bytes] = {}
    if document is not None:
        uris = [u for links in gdoc_source.picture_links(document).values() for u in links.values()]
        if uris:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                ready = dict(zip(uris, pool.map(download, uris)))
            say(f"Downloaded the source's {len(ready)} pictures")
    return lambda uri: ready[uri] if uri in ready else download(uri)


def export_tab(google: Google, document_id: str, tab_id: str, say) -> bytes:
    """One tab as a PDF (this form exports exactly that tab). Google limits exports per minute (429)."""
    import requests

    url = f"https://docs.google.com/document/export?format=pdf&id={document_id}&tab={tab_id}"
    problem = ""
    for attempt in range(8):
        try:
            answer = google.authorized.get(url, timeout=(20, 300))
            if answer.status_code == 200 and answer.content.startswith(b"%PDF"):
                return answer.content
            problem = f"status {answer.status_code}"
        except requests.RequestException as exc:
            problem = type(exc).__name__
        say(f"Google is not ready to export a tab yet; waiting {20 * (attempt + 1)} s")
        time.sleep(20 * (attempt + 1))
    raise GdocError(f"Google did not export a tab of the new Doc as a PDF ({problem}). Press Try again later.")


def bin_file(google: Google, file_id: str) -> None:
    google.drive.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()


def share_with_domain(google: Google, document_id: str, domain: str) -> str:
    """Anyone at `domain` can edit with the link; not searchable for the whole company."""
    existing = google.drive.permissions().list(fileId=document_id, fields="permissions(type,domain,role)",
                                               supportsAllDrives=True).execute().get("permissions", [])
    if any(p.get("type") == "domain" and p.get("domain") == domain and p.get("role") in ("writer", "owner")
           for p in existing):
        return "already editable"
    google.drive.permissions().create(fileId=document_id, supportsAllDrives=True, fields="id", body={
        "type": "domain", "domain": domain, "role": "writer", "allowFileDiscovery": False}).execute()
    return "now editable"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "tab"


# ------------------------------------------------------------------ steps
def read_source(google: Google, source_id: str, folder: Path, say) -> tuple[dict, list[dict]]:
    """Every tab of the source Doc, literally. Content that cannot be copied exactly fails here, before AI."""
    say("Reading every tab of the source Google Doc", 5)
    try:
        document = gdoc_source.fetch(google.docs, source_id)
    except Exception as exc:  # noqa: BLE001
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (403, 404):
            raise GdocError("NoteCraft cannot open that Google Doc. Make sure the signed-in account can view it.") from exc
        raise
    fetch = downloader(google, document, say)
    lists = gdoc_source.list_levels(document)
    tabs = []
    for n, tab in enumerate(gdoc_source.all_tabs(document), start=1):
        tab_folder = folder / "tabs" / f"{n:02d}-{_slug(tab['title'])}"
        doc, manifest = gdoc_source.extract(tab, tab_folder / "source_excerpts", fetch)
        raw = tab_folder / "source_map_raw.json"
        if raw.is_file() and json.loads(raw.read_text("utf-8"))["source_sha256"] != manifest["source_sha256"]:
            # Saved formatting belongs to the earlier words; start this tab afresh.
            for stale in ("word_styles.json", "final_quality_review.json", "tab.pdf"):
                (tab_folder / stale).unlink(missing_ok=True)
        raw.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        tabs.append({"n": n, "tab_id": tab["tab_id"], "title": tab["title"], "depth": tab["depth"],
                     "folder": tab_folder, "doc": doc, "manifest": manifest, "lists": lists.get(tab["tab_id"], {}),
                     "raw_units": json.loads(json.dumps(manifest["units"]))})
    if not tabs:
        raise GdocError("The source Google Doc has no tabs with content.")
    return document, tabs


def style_all(tabs: list[dict], token_provider, task_id: str, models: list[str], workers: int, say,
              label: str) -> list[dict]:
    """AI chooses each tab's B.Sc. look; the words stay the source's. Saved per tab."""

    def one(tab):
        cached = (tab["folder"] / "word_styles.json").is_file()
        session = None if cached else ai.Session(token_provider, task_id, f"{label} - {tab['title']}"[:120], 1,
                                                 log=lambda line: None)
        word_source.style(tab["doc"], tab["manifest"], tab["folder"], session, models, lambda *_: None)
        (tab["folder"] / "notes.json").write_text(json.dumps(tab["doc"], ensure_ascii=False, indent=2), encoding="utf-8")
        return {"tab": tab["n"], "cached": cached, "cost_inr": session.cost if session else 0.0}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, tabs))


def write_doc(google: Google, source_id: str, document: dict, tabs: list[dict], branding: render.Branding,
              found_template: dict, title: str, say) -> dict:
    """The new Doc: a copy of the watermark template with one tab per source tab, every tab written. An
    unfinished Doc is put in the bin when writing fails."""
    fresh = gdoc_source.fetch(google.docs, source_id)
    if fresh.get("revisionId") != document.get("revisionId"):
        raise GdocError("The source Google Doc was edited while the notes were being prepared. Press Try again.")
    links = gdoc_source.picture_links(fresh)                 # picture links last about 30 minutes
    logo_link, ratio, temp = "", 1.0, ""
    if branding.pw_logo:
        from PIL import Image

        logo_path = render.pw_logo_path()
        with Image.open(logo_path) as im:
            ratio = im.size[0] / im.size[1]
        logo_links, temp = gdoc_writer.local_picture_links(google.drive, google.docs, [logo_path.read_bytes()])
        logo_link = logo_links[0]
    document_id = ""
    try:
        document_id, tab_ids = gdoc_writer.create_from_template(google.drive, google.docs, found_template["template_id"],
                                                                title, [{"title": t["title"]} for t in tabs])
        say("New Google Doc made; writing its tabs", 40)
        for tab, new_tab in zip(tabs, tab_ids):
            gdoc_writer.batch(google.docs, document_id, gdoc_writer.page_requests(new_tab))
            source_links = links.get(tab["tab_id"], {})
            gdoc_writer.batch(google.docs, document_id, gdoc_writer.tab_requests(
                tab["doc"]["blocks"], new_tab, lambda block: source_links[block["object_id"]], tab["lists"]))
            gdoc_writer.tidy_end(google.docs, document_id, new_tab)
            if not found_template.get("branded"):
                gdoc_writer.header_footer(google.docs, document_id, new_tab, branding, logo_link, ratio, watermarked=True)
            tab["new_tab_id"] = new_tab
    except Exception:
        if document_id:
            try:
                bin_file(google, document_id)
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        if temp:
            try:
                bin_file(google, temp)
            except Exception:  # noqa: BLE001
                pass
    return {"document_id": document_id, "url": doc_url(document_id), "title": title,
            "tab_ids": [t["new_tab_id"] for t in tabs], "written_at": time.time()}


def verify_doc(google: Google, result: dict, tabs: list[dict]) -> dict:
    """Every tab of the new Doc, read back, against its source: word for word, nothing added."""
    written = gdoc_writer.read_back(google.docs.documents().get(documentId=result["document_id"],
                                                                includeTabsContent=True).execute())
    report = {}
    for tab in tabs:
        found = written.get(tab["new_tab_id"])
        problems = ["Tab missing."] if found is None else \
            gdoc_writer.verify(tab["doc"]["blocks"], tab["raw_units"], found, tab["lists"])
        if found is not None and found["title"] != tab["title"]:
            problems.append(f"Tab title is {found['title']!r}.")
        report[tab["title"]] = {"problems": problems, "paragraphs": len(found["paragraphs"]) if found else 0,
                                "pictures": found["pictures"] if found else 0}
    return report


def fix_last_page(google: Google, result: dict, tab_id: str, pdf: bytes, say) -> bytes:
    """A tab must not end on a blank page, or on a page holding only a table's last row or a box's last line.
    Formatting only; returns the tab's pages after any change."""
    document_id = result["document_id"]
    if gdoc_writer.ends_on_blank_page(pdf):
        gdoc_writer.make_room_at_end(google.docs, document_id, tab_id)
        pdf = export_tab(google, document_id, tab_id, say)
    if gdoc_writer.last_page_nearly_empty(pdf):
        before = gdoc_writer.page_count(pdf)
        if gdoc_writer.break_before_last_table(google.docs, document_id, tab_id):
            moved = export_tab(google, document_id, tab_id, say)
            if (gdoc_writer.page_count(moved) <= before and not gdoc_writer.last_page_nearly_empty(moved)
                    and gdoc_writer.page_fill(moved, -2) >= 0.5):
                return moved
            gdoc_writer.break_before_last_table(google.docs, document_id, tab_id, on=False)
            pdf = export_tab(google, document_id, tab_id, say)
    return pdf


def pages_and_marks(google: Google, result: dict, tabs: list[dict], logo: bool, say) -> dict[int, bytes]:
    """Each tab's final pages (last-page fixes applied), and the watermark / logo found on every page."""
    pdfs = {}
    for tab in tabs:
        pdf = export_tab(google, result["document_id"], tab["new_tab_id"], say)
        pdf = fix_last_page(google, result, tab["new_tab_id"], pdf, say)
        (tab["folder"] / "tab.pdf").write_bytes(pdf)
        pdfs[tab["n"]] = pdf
    if len(tabs) > 1 and len({hash(p) for p in pdfs.values()}) == 1:
        raise GdocError("Google exported the whole Doc for every tab, so the tabs cannot be checked one by one.")
    result["blank_last_page"] = [n for n, pdf in pdfs.items() if gdoc_writer.ends_on_blank_page(pdf)]
    result["pages"] = sum(gdoc_writer.page_count(pdf) for pdf in pdfs.values())
    result["watermark"] = {str(n): gdoc_writer.has_watermark(pdf) for n, pdf in pdfs.items()}
    result["header_logo"] = {str(n): gdoc_writer.has_header_logo(pdf) for n, pdf in pdfs.items()} if logo else {}
    return pdfs


def quality_all(pdfs: dict[int, bytes], tabs: list[dict], token_provider, task_id: str, models: list[str],
                workers: int, label: str) -> list[dict]:
    """The final NEET/JEE quality check of each tab's PDF against its locked source (saved per tab)."""

    def one(tab):
        holder = {}

        def factory():
            holder.setdefault("s", ai.Session(token_provider, task_id, f"{label} quality - {tab['title']}"[:120], 1,
                                              log=lambda line: None))
            return holder["s"]
        found = source_quality.review(pdfs[tab["n"]], tab["doc"], tab["manifest"], tab["folder"], factory, models,
                                      lambda *_: None)
        session = holder.get("s")
        return {"tab": tab["n"], "title": tab["title"], "verdict": found["verdict"],
                "pages": len(found["reviewed_pages"]), "blocking": found["blocking_issues"],
                "advisory": found["advisory_issues"], "problem": found["problem"],
                "cost_inr": session.cost if session else 0.0}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, tabs))


# ------------------------------------------------------------------ one note
def make(source_id: str, folder: Path, *, creds, token_provider, branding: render.Branding,
         style_models: list[str], quality_models: list[str], say: Callable = lambda *a: None,
         share_domain: str = "", workers: int = 4, label: str = "") -> dict:
    """One final-notes Google Doc -> one B.Sc.-style Google Doc. Returns the result (also result.json).

    result["ok"] is True only when every tab passed the word-for-word check and every page shows the PW
    watermark (and the logo, when the category has it). The quality verdicts are in result["quality"]."""
    import pw_access

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    started = time.time()
    google = Google(creds)
    found_template = template()
    if not found_template.get("ready"):
        raise GdocError("The PW watermark template is not ready.")
    document, tabs = read_source(google, source_id, folder, say)
    if len(tabs) > int(found_template.get("tabs") or 0):
        raise GdocError(f"The source Doc has {len(tabs)} tabs, but the PW watermark template has "
                        f"{found_template.get('tabs')}. Ask the owner to add tabs to the template.")
    name = (document.get("title") or "Notes").strip()
    label = label or name
    task_id = pw_access.new_task_id(TASK_PREFIX)
    say(f"AI is choosing the B.Sc. look for {len(tabs)} tab{'s' if len(tabs) != 1 else ''}; every word stays locked", 15)
    styled = style_all(tabs, token_provider, task_id, style_models, workers, say, label)
    spent = round(sum(s["cost_inr"] for s in styled), 4)
    title = f"{name} – {branding.header_right} (B.Sc. style)"
    result = write_doc(google, source_id, document, tabs, branding, found_template, title, say)
    result.update(source_id=source_id, source_url=doc_url(source_id), source_title=name,
                  source_revision=document.get("revisionId"), models={"style": style_models, "quality": quality_models},
                  formatting_cost_inr=spent, template_id=found_template["template_id"])
    say("Checking every tab of the new Doc word for word against the source", 60)
    result["verification"] = verify_doc(google, result, tabs)
    verified = all(not v["problems"] for v in result["verification"].values())
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["quality"] = []
    if verified:
        say("Checking the pages: last page, PW watermark" + (" and logo" if branding.pw_logo else ""), 70)
        pdfs = pages_and_marks(google, result, tabs, branding.pw_logo, say)
        say("Final NEET/JEE quality check of every tab", 80)
        result["quality"] = quality_all(pdfs, tabs, token_provider, task_id, quality_models, workers, label)
    marks = list(result.get("watermark", {}).values()) + list(result.get("header_logo", {}).values())
    result["ok"] = bool(verified and marks and all(marks))
    if result["ok"] and share_domain:
        result["sharing"] = share_with_domain(google, result["document_id"], share_domain)
    result["total_cost_inr"] = round(spent + sum(q["cost_inr"] for q in result["quality"]), 4)
    result["seconds"] = round(time.time() - started)
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def problems(result: dict) -> list[str]:
    """What stops a made Doc from being handed over, in plain words."""
    found = []
    bad = [t for t, v in (result.get("verification") or {}).items() if v["problems"]]
    if bad:
        found.append("The new Doc does not match the source word for word in: " + ", ".join(bad) + ".")
    if result.get("watermark") and not all(result["watermark"].values()):
        found.append("Some pages lost the PW watermark.")
    if result.get("header_logo") and not all(result["header_logo"].values()):
        found.append("Some pages lost the PW logo.")
    return found
