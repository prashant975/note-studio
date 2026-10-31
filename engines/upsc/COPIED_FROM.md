# Copied from `C:\Users\Laptop\Downloads\Upsc_Notes_2\upsc_Notes`

- Copied: 2026-10-04T02:55:55 by `NoteStudio_Kit/tools/copy_engine.py` (byte for byte, `shutil.copy2`; the original was only read).
- Files: 245 (207.9 MB)
- Git commit: `61863cdcadfcfc917f0ced3dd2dfaa0a2c23cb21`

This is a COPY. Only the files listed in `ENGINE_COPIES['upsc']['allowed']` in
`NoteStudio_Kit/tools/data_guard.py` may differ from the original; prompts and rules never.
Check with `python NoteStudio_Kit/tools/data_guard.py drift`.

## Taken

- `src/`
- `prompts/`
- `config/`
- `assets/`
- `reference_documents/`
- `third_party/`
- `tests/`
- `scripts/`
- `dist_assets/`
- `app.py`
- `run_cli.py`
- `pw_access.py`
- `requirements.txt`
- `.gitignore`
- `README.md`
- `TEAM_SETUP.md`
- `TROUBLESHOOTING.md`
- `COLAB_QUICKSTART.md`
- `dist_README_FIRST.md`
- `UpscNotesApp.spec`
- `streamlit_launcher.py`
- `streamlit_launcher_stable.py`
- `check_gemini.py`
- `check_gpt.py`
- `verify_onboarding.py`
- `START_WINDOWS.bat`

## Left out

- `runs/, outputs/, out/, data/`: runs, outputs and the DPP workbooks are read in place
- `.env, .env.example, .streamlit/`: secrets / local config
- `.venv/, build/, dist/, dist_staging/`: environment and build artefacts
- `.agents/, .claude/, .pytest_runtime_*/, notebooks/ (empty), streamlit_run.log`: tool state, test leftovers and logs
- `pw-app-kit/`: the kit's source; the engine uses its own pw_access.py
- caches at any depth: `.git`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.streamlit`, `.venv`, `__pycache__`, `node_modules`, `venv`

## `git status --short` of the original at copy time

```
(clean)
```

## Added 2026-10-04T03:22:01 (`--add-missing`, nothing overwritten)

- Files: 4 (55.3 MB)
- `data/.gitkeep`
- `data/DPP and Infinite Practice Questions.xlsx`
- `data/README.md`
- `data/UPSC QBG Tags.xlsx`

## Synced 2026-10-08T22:28:34+05:30

- Source: `C:\Users\Laptop\Downloads\Upsc_Notes_2\upsc_Notes` (read only).
- Refreshed 47 files with `shutil.copy2`; previous copies: `C:\Users\Laptop\Downloads\Note_Studio\_build\engine_sync_backups\20261008-222834\upsc`.
- Preserved only the listed integration hooks; revalidate them against the original.
- `src/config.py`
- `src/dmp/export.py`
- `src/docx_layout.py`
- `src/docx_pdf.py`
- `src/docx_render_check.py`
- `src/docx_writer.py`
- `src/drive_auth.py`
- `src/image_ai.py`
- `src/image_tools.py`
- `src/notes_markup.py`
- `src/notes_to_dpp/selection.py`
- `src/pdf_exporter.py`
- `src/pipeline.py`
- `src/quality_checker.py`
- `src/script_repair.py`
- `src/selftest.py`
- `src/title_utils.py`
- `src/transcript.py`
- `src/upsc_dcn_docx_writer.py`
- `src/video_sources/google_drive.py`
- `src/visuals/metadata.py`
- `src/youtube_transcript.py`
- `tests/conftest.py`
- `tests/test_app_runtime.py`
- `tests/test_dpp_flow_runtime.py`
- `tests/test_dpp_preview_replacement.py`
- `tests/test_dpp_selection.py`
- `tests/test_drive_auth.py`
- `tests/test_drive_media_access.py`
- `tests/test_drive_transcript.py`
- `tests/test_notes_formatting_regressions.py`
- `tests/test_notes_markup.py`
- `tests/test_page_limit.py`
- `tests/test_pdf_exporter.py`
- `tests/test_report_export_performance.py`
- `tests/test_robustness_fixes.py`
- `tests/test_script_repair.py`
- `tests/test_selftest.py`
- `tests/test_silent_failure_guards.py`
- `tests/test_streamlit_launcher.py`
- `tests/test_transparent_diagrams.py`
- `tests/test_video_sources.py`
- `tests/test_youtube_caption_timeout.py`
- `app.py`
- `README.md`
- `dist_README_FIRST.md`
- `streamlit_launcher_stable.py`

## Synced 2026-10-12T10:51:26+05:30

- Source: `C:\Users\Laptop\Downloads\Upsc_Notes_2\upsc_Notes` (read only).
- Refreshed 11 files with `shutil.copy2`; previous copies: `C:\Users\Laptop\Downloads\Note_Studio\_build\engine_sync_backups\20261012-105126\upsc`.
- Preserved only the listed integration hooks; revalidate them against the original.
- `src/config.py`
- `src/pipeline.py`
- `src/transcript.py`
- `src/video_sources/google_drive.py`
- `tests/test_app_runtime.py`
- `tests/test_drive_transcript.py`
- `tests/test_error_report_pipeline.py`
- `tests/test_video_only_run.py`
- `app.py`
- `README.md`
- `dist_README_FIRST.md`
