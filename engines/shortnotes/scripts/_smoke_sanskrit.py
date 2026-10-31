"""Generate a Sanskrit PDF locally through the existing signed-in model route."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/pdf/sanskrit"))
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    notes_path = args.output / "sanskrit-notes.json"
    from notesbot.prompts import ShortNotes
    from notesbot.notes_pdf import build_notes_pdf
    from notesbot.sanskrit import require_sanskrit, resolve_language

    if args.render_only:
        notes = ShortNotes.model_validate_json(notes_path.read_bytes())
    else:
        from sheets_bridge.settings import Settings
        from sheets_bridge.google_auth import load_any_cached, token_provider, user_email
        from sheets_bridge.pw_gate import ProxyAccess
        from notesbot.gemini_client import generate_short_notes
        from notesbot.generation_profile import lean_requests
        settings = Settings.from_env()
        creds = load_any_cached(settings.client_secret_path, settings.token_path)
        if creds is None:
            raise RuntimeError("Sign into the notes app before running this smoke test.")
        access = ProxyAccess(token_provider(creds), email=user_email(creds))
        if not access.domain_ok():
            raise RuntimeError("The signed-in account cannot use this model route.")
        access.ensure_allowed()
        access.install_transport()
        source = args.source.read_bytes()
        print("Detected source language:", resolve_language("auto", pdf_bytes=source), flush=True)
        with lean_requests():
            notes = generate_short_notes(source, language="sanskrit", subject_hint="sanskrit")
        notes_path.write_text(notes.model_dump_json(indent=2), encoding="utf-8")
    require_sanskrit(notes)
    data = build_notes_pdf(notes, "संस्कृताध्यापकः", watermark=False, logo_watermark=False)
    target = args.output / "sanskrit-notes.pdf"
    target.write_bytes(data)
    print(f"Written {target}; sections={len(notes.sections)}; bytes={len(data)}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Auth and provider exceptions may include credentials or source payloads.
        print(f"Sanskrit smoke failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
