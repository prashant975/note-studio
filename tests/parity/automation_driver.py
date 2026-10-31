"""Compare the real original Sheets workflow and NoteCraft with identical model responses."""
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

from upload_driver import _block_network, _engine_defaults, _fake_proxy, _report


def main():
    mode, root_arg, pdf_arg, out_arg, settings_arg = sys.argv[1:]
    _block_network()
    sys.dont_write_bytecode = True
    root, source, out = Path(root_arg), Path(pdf_arg), Path(out_arg)
    out.mkdir(parents=True, exist_ok=True)
    settings = json.loads(Path(settings_arg).read_text(encoding="utf-8"))
    if mode == "original":
        sys.path.insert(0, str(root))
        engine = root
    else:
        sys.path.insert(0, str(root))
        from note_studio import engine_catalog
        engine = engine_catalog.put_shortnotes_on_path()
    _engine_defaults(engine)
    # The final-quality request includes the generated PDF. Fix its creation
    # date in both processes so hashing those requests compares engine behavior.
    from fpdf import FPDF
    original_init = FPDF.__init__
    def fixed_pdf_init(pdf, *args, **kwargs):
        original_init(pdf, *args, **kwargs)
        pdf.set_creation_date(datetime(2026, 10, 9, tzinfo=timezone.utc))
    FPDF.__init__ = fixed_pdf_init
    fake = _fake_proxy()
    from notesbot import generation_profile
    from sheets_bridge import pw_transport
    from sheets_bridge.generator import generate_notes_pdf, notes_from_json
    from notesbot.web_service import normalize_signature
    proxy = lambda: pw_transport.install(lambda: "offline-parity-token")
    capture = {}
    if mode == "original":
        proxy()
        with generation_profile.exam_category(settings["category"]), \
                generation_profile.exam_subject(settings["subject"]), generation_profile.fixed_signature():
            made = generate_notes_pdf(source.read_bytes(), language=settings["language"], subject=settings["subject"],
                                      signature=normalize_signature(settings["teacher_name"]), filename=source.name,
                                      task_id="offline-original")
        capture.update(notes=notes_from_json(made.notes_json), pdf=made.pdf)
        quality, cost, state = made.quality_report, made.cost_inr, "complete"
    else:
        from note_studio.engines import shortnotes
        result = shortnotes.make_notes(source.read_bytes(), source.name, out,
            shortnotes.UploadSettings(**settings, automation_quality=True), ensure_proxy=proxy, capture=capture)
        quality, cost, state = result.quality_report, result.cost_inr, result.state
        if state != "complete":
            raise RuntimeError((result.message, result.detail))
    # PDF time stamps differ between processes; every other receipt field is comparable.
    quality = {key: value for key, value in quality.items() if key != "pdf_sha256"}
    report = _report(state, "lecture Concise Notes.pdf", capture, capture["pdf"], fake,
                      extra={"quality_report": quality, "cost_inr": cost})
    print("AUTOMATION-PARITY " + json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
