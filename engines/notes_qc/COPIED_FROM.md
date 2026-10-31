# Copied from `C:\Users\Laptop\Downloads\upsc_file_QC`

- Copied: 2026-10-04T17:00:40 by `NoteStudio_Kit/tools/copy_engine.py` (byte for byte, `shutil.copy2`; the original was only read).
- Files: 90 (3.4 MB)
- Git commit: `105bf459ba9d5e071b19bc27a835b01041f34d35`

This is a COPY. Only the files listed in `ENGINE_COPIES['notes_qc']['allowed']` in
`NoteStudio_Kit/tools/data_guard.py` may differ from the original; prompts and rules never.
Check with `python NoteStudio_Kit/tools/data_guard.py drift`.

## Taken

- `src/`
- `config/`
- `reference_documents/`
- `tests/`
- `testdata/`
- `app.py`
- `run_cli.py`
- `launcher.py`
- `pw_access.py`
- `requirements.txt`
- `.gitignore`
- `README.md`
- `READ_ME_FIRST.txt`
- `UPSC_QC.spec`
- `verify_onboarding.py`

## Left out

- `runs/, output/`: old runs and reports stay in the original
- `.env.example, .streamlit/`: secrets / local config
- `.venv/, build/, dist/`: environment and builds
- `pw-app-kit/`: the kit's source; the engine uses its own pw_access.py
- caches at any depth: `.git`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.streamlit`, `.venv`, `__pycache__`, `node_modules`, `venv`

## `git status --short` of the original at copy time

```
(clean)
```
