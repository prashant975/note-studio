# Copied from `C:\Users\Laptop\Downloads\shortnotes-core`

- Copied: 2026-10-04T02:55:54 by `NoteStudio_Kit/tools/copy_engine.py` (byte for byte, `shutil.copy2`; the original was only read).
- Files: 167 (5.8 MB)
- Git: the original is not a git repository.

This is a COPY. Only the files listed in `ENGINE_COPIES['shortnotes']['allowed']` in
`NoteStudio_Kit/tools/data_guard.py` may differ from the original; prompts and rules never.
Check with `python NoteStudio_Kit/tools/data_guard.py drift`.

## Taken

- `notesbot/`
- `sheets_bridge/`
- `api/`
- `equation_kit/`
- `fonts/`
- `assets/`
- `tests/`
- `scripts/`
- `docs/`
- `packaging/`
- `pw_access.py`
- `requirements.txt`
- `pyproject.toml`
- `.python-version`
- `.gitignore`
- `README.md`
- `HANDOFF.md`
- `DESIGN.md`
- `verify_onboarding.py`
- `web/state.js`

## Left out

- `output/, build/, dist/, tmp/`: runs, outputs and builds stay in the original
- `.env, .env.example, client_secret.json, client_secret.personal.bak`: secrets / local config
- `sheets.json, sheets.json.example`: data: the sheet list is read in place (~/.shortnotes_bridge)
- `web/ (except state.js), public/, vercel.json, .vercelignore, package*.json, node_modules/`: the website and its JS build; Note Studio has its own pages
- `pw-app-kit/, equation_kit.zip`: the kit's source and a zip of a folder that is copied
- caches at any depth: `.git`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.streamlit`, `.venv`, `__pycache__`, `node_modules`, `venv`

## Synced 2026-10-08T22:28:33+05:30

- Source: `C:\Users\Laptop\Downloads\shortnotes-core` (read only).
- Refreshed 26 files with `shutil.copy2`; previous copies: `C:\Users\Laptop\Downloads\Note_Studio\_build\engine_sync_backups\20261008-222833\shortnotes`.
- Preserved only the listed integration hooks; revalidate them against the original.
- `notesbot/errors.py`
- `notesbot/final_quality.py`
- `notesbot/gemini_client.py`
- `notesbot/graph_render.py`
- `notesbot/latex_repair.py`
- `notesbot/math_render.py`
- `notesbot/notes_pdf.py`
- `notesbot/prompts.py`
- `notesbot/sanskrit.py`
- `notesbot/schematic_render.py`
- `notesbot/source_coverage.py`
- `sheets_bridge/coverage.py`
- `sheets_bridge/model_check.py`
- `sheets_bridge/output_cache.py`
- `sheets_bridge/runner.py`
- `sheets_bridge/studio_instance.py`
- `sheets_bridge/studio_server.py`
- `tests/test_final_quality.py`
- `tests/test_generation_options.py`
- `tests/test_model_budget.py`
- `tests/test_science_notes.py`
- `tests/test_sheets_bridge.py`
- `tests/test_studio_instance.py`
- `packaging/ShortNotesStudio-launch-rebuild.spec`
- `README.md`
- `HANDOFF.md`

## Synced 2026-10-12T10:51:26+05:30

- Source: `C:\Users\Laptop\Downloads\shortnotes-core` (read only).
- Refreshed 13 files with `shutil.copy2`; previous copies: `C:\Users\Laptop\Downloads\Note_Studio\_build\engine_sync_backups\20261012-105126\shortnotes`.
- Preserved only the listed integration hooks; revalidate them against the original.
- `notesbot/chemistry_repair.py`
- `notesbot/final_quality.py`
- `notesbot/latex_repair.py`
- `notesbot/math_render.py`
- `notesbot/notes_pdf.py`
- `notesbot/prompts.py`
- `notesbot/source_coverage.py`
- `sheets_bridge/output_cache.py`
- `tests/test_chemistry_repair.py`
- `tests/test_notation_quality.py`
- `packaging/studio_launcher.py`
- `README.md`
- `HANDOFF.md`
