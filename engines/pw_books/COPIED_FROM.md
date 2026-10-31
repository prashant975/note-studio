# Copied from `C:\Users\Laptop\Downloads\handwritten_notes_automation_complete\handwritten_notes_automation_complete`

- Copied: 2026-10-04T17:00:00 by `NoteStudio_Kit/tools/copy_engine.py` (byte for byte, `shutil.copy2`; the original was only read).
- Files: 88 (1.5 MB)
- Git commit: `eee632abb519e5cadfc7e7a032a5cfbb5ceec359`

This is a COPY. Only the files listed in `ENGINE_COPIES['pw_books']['allowed']` in
`NoteStudio_Kit/tools/data_guard.py` may differ from the original; prompts and rules never.
Check with `python NoteStudio_Kit/tools/data_guard.py drift`.

## Taken

- `src/`
- `prompts/`
- `assets/`
- `equation_kit/`
- `tests/`
- `scripts/`
- `app.py`
- `run_cli.py`
- `pw_access.py`
- `requirements.txt`
- `.gitignore`
- `README.md`
- `TROUBLESHOOTING.md`
- `HOW_TO_USE.txt`
- `TEAM_RELEASE_NOTES.txt`
- `COLAB_QUICKSTART.md`
- `HandwrittenNotesAppTeam.spec`
- `streamlit_launcher_stable.py`
- `START_WINDOWS.bat`
- `check_gemini.py`

## Left out

- `runs/, outputs/, logs/`: old runs and outputs stay in the original
- `.env, .env.example, .streamlit/`: secrets / local config
- `.venv/, build/, dist/, .test-tmp*/, notebooks/`: environment, builds and test leftovers
- `.agents/, HOW_TO_USE_saved.txt, streamlit_run*.log`: tool state, a duplicate and logs
- `pw-app-kit/, pw-app-kit.zip, equation_kit.zip`: the kit's source and a zip of a folder that is copied
- caches at any depth: `.git`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.streamlit`, `.venv`, `__pycache__`, `node_modules`, `venv`

## `git status --short` of the original at copy time

```
M TEAM_RELEASE_NOTES.txt
 M prompts/jee_concise_en.txt
 M prompts/jee_neet_concise_en.txt
 M prompts/neet_concise_en.txt
 M src/ai_client.py
 M src/config.py
 M src/docx_layout.py
 M src/docx_writer.py
 M src/image_ai.py
 M src/math_renderer.py
 M src/pipeline.py
 M src/prompt_builder.py
 M src/quality_checker.py
 M src/version.py
 M tests/test_image_ai.py
?? equation_kit/
?? src/image_filter.py
?? src/style_config.py
?? tests/test_image_filter.py
?? tests/test_notes_formatting.py
```

## Added 2026-10-04T17:02:09 (`--add-missing`, nothing overwritten)

- Files: 7 (0.0 MB)
- `pw-app-kit/CONNECT-TO-PW-PROXY.md`
- `pw-app-kit/ONBOARD.md`
- `pw-app-kit/pw_access.js`
- `pw-app-kit/pw_access.py`
- `pw-app-kit/README.md`
- `pw-app-kit/verify_onboarding.mjs`
- `pw-app-kit/verify_onboarding.py`
