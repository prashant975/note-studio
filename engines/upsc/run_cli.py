from __future__ import annotations

import argparse
import os
from pathlib import Path
from src.config import DEFAULT_MODEL, SUBJECTS, SUPPORTED_EXTENSIONS
from src.pipeline import run_batch, run_pipeline


def _collect_inputs(input_path: Path) -> list[Path]:
    if input_path.is_dir():
        files = sorted(p for p in input_path.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS)
        if not files:
            raise SystemExit(f"No supported files ({sorted(SUPPORTED_EXTENSIONS)}) found in folder: {input_path}")
        return files
    return [input_path]


def main():
    parser = argparse.ArgumentParser(description="Generate detailed UPSC study notes from a lecture PPT/PDF.")
    parser.add_argument("--input", required=True, help="Path to a lecture PDF/PPTX/PPT, or a folder of them (batch mode)")
    parser.add_argument("--video-url", "--youtube", dest="video_url", default="", help="Link to the class recording: a Google Drive share link, a YouTube link, or a direct video URL. The transcript is produced from it automatically. Single-file mode only.")
    parser.add_argument("--transcription-engine", default="", choices=["", "gemini", "faster_whisper"], help="Override TRANSCRIPTION_ENGINE for this run. faster_whisper transcribes locally (needs: pip install faster-whisper) and uses no proxy quota, but on CPU it runs at roughly realtime.")
    parser.add_argument("--force-audio-transcript", action="store_true", help="For YouTube links, skip the captions and transcribe the audio instead. Slower, but avoids auto-caption defects (English transliterated into Devanagari, corrupted technical terms).")
    parser.add_argument("--no-transcript", action="store_true", help="Build notes from the slides alone, without a lecture video. Debugging aid: the notes will not contain any of the faculty's explanation.")
    parser.add_argument("--subject", default="auto", choices=["auto", *SUBJECTS], help="UPSC subject key, or 'auto' to detect from the file")
    parser.add_argument("--language", default="auto", choices=["auto", "English", "Hindi", "en", "hi"], help="Notes language. 'auto' matches the language the class is taught in (detected from the video transcript, falling back to the slides); pass English/Hindi to force it.")
    parser.add_argument("--mode", default="complete", choices=["complete", "summary"], help="complete = detailed Prelims+Mains; summary = revision notes")
    parser.add_argument("--google-token", default=os.getenv("PW_GOOGLE_TOKEN", ""), help="Signed-in @pw.live Google access/id token (or set PW_GOOGLE_TOKEN). Required unless --allow-mock is used; the PW proxy holds the Gemini key.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--no-images", action="store_true", help="Do not send rendered slide images to AI")
    parser.add_argument("--no-strict-filter", action="store_true")
    parser.add_argument("--allow-mock", action="store_true")
    parser.add_argument("--ai-redraw-diagrams", action="store_true", help="AI-redraw inserted diagrams in handwritten blue-on-white style (extra API quota)")
    parser.add_argument("--image-model", default="gemini-2.5-flash-image", help="Gemini image model for diagram redraw")
    parser.add_argument("--dtp-note-policy", default="hide_note_insert_image", choices=["hide_note_insert_image", "keep_note_and_insert_image", "keep_note_only"], help="hide_note_insert_image removes the yellow DTP note and inserts only the image")
    args = parser.parse_args()
    inputs = _collect_inputs(Path(args.input))
    if not args.google_token and not args.allow_mock:
        raise SystemExit("No Google token. Pass --google-token (a signed-in @pw.live token) or set PW_GOOGLE_TOKEN. The PW proxy needs it to run Gemini. Use --allow-mock for a no-AI dry run.")
    common = dict(subject=args.subject, language=args.language, mode=args.mode, google_token=args.google_token.strip(), model=args.model, send_images_to_ai=not args.no_images, strict_filter=not args.no_strict_filter, allow_mock=args.allow_mock, ai_redraw_diagrams=args.ai_redraw_diagrams, image_model=args.image_model, dtp_note_policy=args.dtp_note_policy)

    # A video link names ONE class, so it can't be applied across a folder.
    if args.video_url and len(inputs) > 1:
        raise SystemExit("--video-url applies to a single class. Pass one file with --input, not a folder.")
    if not args.video_url and not args.no_transcript:
        raise SystemExit(
            "No lecture video. Pass --video-url with a Google Drive, YouTube or direct "
            "video link — the transcript is produced from it. Use --no-transcript to "
            "build notes from the slides alone.")

    if len(inputs) == 1:
        result = run_pipeline(
            inputs[0],
            video_url=args.video_url.strip(),
            force_audio_transcript=args.force_audio_transcript,
            transcription_engine=args.transcription_engine,
            require_transcript=not args.no_transcript,
            **common,
        )
        print(f"Run folder: {result.run_dir}")
        print(f"DOCX: {result.docx_path}")
        print(f"PDF: {result.pdf_path}")
        print(f"ZIP: {result.zip_path}")
        if result.warnings:
            print("Warnings:")
            for w in result.warnings:
                print(f"- {w}")
        return

    # Batch mode takes a folder, and a folder has no single class recording, so
    # every file in it is necessarily slides-only. Said out loud rather than
    # left for the reader to notice in the notes.
    print(f"Batch mode: {len(inputs)} file(s) — slides only, no lecture transcript\n")

    def _progress(i, total, path):
        print(f"[{i}/{total}] Processing {path.name} ...")

    results = run_batch(inputs, progress_callback=_progress,
                        require_transcript=False, **common)
    ok = sum(1 for _, r, _ in results if r)
    print(f"\nCompleted: {ok}/{len(results)} succeeded")
    for path, result, error in results:
        if result:
            print(f"- OK   {path.name} -> {result.docx_path}")
        else:
            print(f"- FAIL {path.name}: {error}")


if __name__ == "__main__":
    main()
