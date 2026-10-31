"""Run one lecture upload offline, the way an app does it, and print the result as JSON.

    python upload_driver.py original  <shortnotes-core root> <pdf> <out dir> <settings.json>
    python upload_driver.py notecraft <Note_Studio root>     <pdf> <out dir> <settings.json>

original   the old desktop app's Upload path: notesbot.web_service.run_job (the
           original project's own function, its own JobRecord and LocalBlobStore)
           with sheets_bridge/studio_server.py's _local_generate/_local_render
           (transcribed below; tests/test_parity_upload.py checks that those
           closures in the original are still exactly this text).
notecraft  note_studio.engines.shortnotes.make_notes (the NoteCraft adapter) on
           engines/shortnotes, with the category packs.

Both: the model is tests/parity/fake_proxy.py (pw_access.gemini_generate
replaced), every non-loopback connection is refused, nothing is paid.
Each run is its own process, so the two projects' `notesbot` never meet.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _block_network() -> None:
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(f"network refused in parity run ({host})")
        return real(self, address)

    socket.socket.connect = guarded


def _engine_defaults(engine_root: Path) -> None:
    """The old exe's launcher defaults (identical to the original .env knobs)."""
    spec = importlib.util.spec_from_file_location("_launcher", engine_root / "packaging" / "studio_launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in module.DEFAULTS.items():
        os.environ.setdefault(key, value)


def _fake_proxy():
    spec = importlib.util.spec_from_file_location("fake_proxy", HERE / "fake_proxy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import pw_access

    module.install(pw_access)
    return module


def run_original(root: Path, pdf: bytes, name: str, out: Path, s: dict) -> dict:
    sys.path.insert(0, str(root))
    _engine_defaults(root)
    fake = _fake_proxy()
    from notesbot import web_service as ws
    from notesbot.output_naming import output_name
    from notesbot.web_jobs import JobManifest, JobSettings, new_job_id
    from sheets_bridge import pw_transport
    from sheets_bridge.local_blob import LocalBlobStore

    capture: dict = {}

    def _ensure_proxy():                      # account.access.install_transport(), offline token
        pw_transport.install(lambda: "offline-parity-token")

    # --- sheets_bridge/studio_server.py, serve_studio(): _local_generate / _local_render ---
    def _local_generate(pdf_bytes, edit_instruction="", language=None, subject_hint=""):
        from notesbot.gemini_client import generate_short_notes

        from sheets_bridge.generator import page_count
        from sheets_bridge.pw_gate import new_task_id
        from sheets_bridge.pw_transport import set_task

        _ensure_proxy()
        set_task(
            task_id=new_task_id("studio-manual"),
            filename="studio-upload.pdf",
            input_unit="No. of pages",
            count=page_count(pdf_bytes) or None,
        )
        notes = generate_short_notes(pdf_bytes, edit_instruction, language, subject_hint)
        capture["notes"] = notes
        return notes

    def _local_render(*args, **kwargs):
        from notesbot.notes_pdf import build_notes_pdf

        from sheets_bridge import pdf_branding, pdf_fonts

        pdf_fonts.install()      # Hindi lectures must render as Hindi
        pdf_branding.install()   # PW logo + PW watermark
        kwargs.setdefault("logo_watermark", True)
        return build_notes_pdf(*args, **kwargs)
    # ---------------------------------------------------------------------------------------

    store = LocalBlobStore("store_localdevstore01", root=out / "blob")
    owner, job_id = "a" * 32, new_job_id()
    paths = ws.owned_paths(owner, job_id)
    store.put_bytes(paths.input, pdf, "application/pdf", False)
    now = int(time.time())
    manifest = JobManifest(id=job_id, owner=owner, source_url=store.url_for(paths.input), state="running",
                           created_at=now, updated_at=now, output_url="", error="", dispatch_count=1)
    settings = JobSettings(teacher_name=ws.normalize_teacher_name(s["teacher_name"]),
                           instruction=s.get("instructions", ""), language=s.get("language", "auto"),
                           subject=s.get("subject", "auto"), watermark=s.get("watermark", False),
                           logo_watermark=s.get("logo_watermark", True), category=s.get("category", "neet_jee"))
    record = ws.JobRecord(version=ws.RECORD_VERSION, manifest=manifest, settings=settings,
                          filename=ws.sanitize_pdf_filename(name), store_host=store.store_host,
                          dispatch_claim_id="b" * 32)    # a running job holds one dispatch claim
    terminal = ws.run_job(record, store, _local_generate, _local_render)
    rendered = b""
    filename = ""
    if terminal.state == "complete":
        rendered = store.get_bytes(terminal.output_url, ws.MAX_OUTPUT_BYTES)
        filename = ws.sanitize_pdf_filename(output_name(record.filename, max_stem_length=90))
    return _report(terminal.state, filename, capture, rendered, fake)


def run_notecraft(project: Path, pdf: bytes, name: str, out: Path, s: dict) -> dict:
    sys.path.insert(0, str(project))
    from note_studio import engine_catalog

    engine_root = engine_catalog.put_shortnotes_on_path()
    _engine_defaults(engine_root)
    fake = _fake_proxy()
    from note_studio.engines import shortnotes as adapter
    from sheets_bridge import pw_transport

    capture: dict = {}
    result = adapter.make_notes(pdf, name, out, adapter.UploadSettings(**s),
                                ensure_proxy=lambda: pw_transport.install(lambda: "offline-parity-token"),
                                capture=capture)
    return _report(result.state, result.filename, capture, capture.get("pdf", b""), fake,
                   extra={"failure": result.failure, "detail": result.detail, "cost_inr": result.cost_inr})


def _report(state, filename, capture, rendered: bytes, fake, extra=None) -> dict:
    report = {"state": state, "filename": filename, "calls": fake.CALLS}
    notes = capture.get("notes")
    report["notes"] = notes.model_dump(mode="json") if notes is not None else None
    if rendered:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(rendered))
        report["pages"] = len(reader.pages)
        report["page_text"] = [page.extract_text() or "" for page in reader.pages]
        # fpdf stamps the creation time and a file id: everything else must match.
        normal = re.sub(rb"/CreationDate \(D:[^)]*\)", b"", rendered)
        normal = re.sub(rb"/ID \[<[0-9a-fA-F]+><[0-9a-fA-F]+>\]", b"", normal)
        import hashlib

        report["pdf_sha256_normalized"] = hashlib.sha256(normal).hexdigest()
        report["pdf_bytes"] = len(rendered)
    report.update(extra or {})
    return report


def main(argv: list[str]) -> int:
    mode, root, pdf_path, out_dir, settings_path = argv
    _block_network()
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.dont_write_bytecode = True
    settings = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pdf_file = Path(pdf_path)
    run = run_original if mode == "original" else run_notecraft
    report = run(Path(root), pdf_file.read_bytes(), pdf_file.name, out, settings)
    sys.stdout.reconfigure(encoding="utf-8")
    print("PARITY-REPORT " + json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
