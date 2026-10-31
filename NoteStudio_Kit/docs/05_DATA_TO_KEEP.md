# 05 — Data to keep

Note Studio is a new project that **copies code** from four original
projects. "Keep the backend data" means:

1. **The originals are read-only.** Their code is frozen, so any change stops
   the phase. Their data is kept: nothing may disappear.
2. **Shared data stays readable by the old apps.** That is
   `~/.shortnotes_bridge` (if the user agrees to share it) and the sheets and
   Drive. The old exe must keep working on the same files.
3. **Note Studio's own data** lives only in `~/.note_studio/`.
4. **Anything that writes to shared data gets a backup first.** That includes
   saving the sheet list and the one-time import of old `.env` values.
5. **A gate after every phase:**
   - `data_guard.py snapshot` + `compare` against the baseline, which covers
     the originals and all data;
   - `data_guard.py drift`, which compares the engine copies with their
     originals.

## The originals (frozen code, kept data)

| Original | Data (kept in place; may grow if the old app is still used) | Code (frozen; copied, never edited) |
|---|---|---|
| `Downloads\shortnotes-core` | `sheets.json` (the repo's sheet list, 5 profiles), `.env`, `client_secret.json` (+ `.personal.bak`), `output/` | `notesbot/` (incl. `format_rules/`), `sheets_bridge/`, `api/`, `web/`, `equation_kit/`, `packaging/`, `fonts/`, `assets/`, `tests/`, `pw_access.py`, `requirements.txt`, `package.json`, `vercel.json` |
| `Downloads\Upsc_Notes_2\upsc_Notes` | `runs/` (57 runs + zips, 225 MB, each with `run_metadata.json`; DMP in `runs/<run>/dmp/`, DPP in `runs/<run>/dpp/`), `outputs/`, `out/`, `data/` (**`DPP and Infinite Practice Questions.xlsx` 55 MB**, `UPSC QBG Tags.xlsx`), `.env`, `.streamlit/` | `src/`, `prompts/`, `config/` (QC rules yaml), `reference_documents/`, `assets/`, `tests/`, `app.py`, `run_cli.py`, `requirements.txt`, `pw_access.py` |
| `Downloads\handwritten_notes_automation_complete\handwritten_notes_automation_complete` | `runs/` (79 MB), `outputs/`, `logs/`, `.env`, `.streamlit/` | `src/` (**including files git does not track:** `image_filter.py`, `style_config.py`), `prompts/`, `assets/`, `equation_kit/`, `tests/`, `app.py`, `run_cli.py`, `requirements.txt`, `pw_access.py` |
| `Downloads\upsc_file_QC` | `runs/` (57 runs, about 870 MB), `output/` (65 reports), `.streamlit/` | `src/`, `config/`, `reference_documents/`, `tests/`, `app.py`, `run_cli.py`, `requirements.txt`, `pw_access.py` |

What is **not** copied into `engines/<name>/`:

- run folders and outputs;
- `.env`, `.streamlit/` and `client_secret*`;
- `.venv`, `build/`, `dist/`, `node_modules/`, `tmp/`, caches;
- the shortnotes website files (`web/`, `public/`, `vercel.json`).

Copy the source code, prompts, rules, assets, fonts, `third_party/ffmpeg`
(UPSC) and tests. Each copy gets a `COPIED_FROM.md` with the source path, date,
file count, and the git commit if the source is a repo.

## Data on this computer outside the projects

| Location | What | Rule |
|---|---|---|
| `~/.shortnotes_bridge/sheets.json` | The old exe's sheet list (6 profiles, written by "+ Add sheet") | **Phase 0 decision.** Recommended: Note Studio **shares** this file, so there is one sheet list for both apps. It reads and writes the same format (`profiles[]`, with `name`, `spreadsheetId`, `gid` or `allTabs`, and the optional `outputFolder`, `signature`, `maxCostInr`, `alsoPdf`, `enabled`, `skipBatches`). It adds `category` / `noteType` only where the user sets them, preserves unknown keys, and writes a `.bak` before each save. The old exe ignores keys it does not know: test that. The repo `sheets.json` (5 profiles) has **diverged**. Show the difference; never drop a profile |
| `~/.shortnotes_bridge/tokens/`, `token.json` | Google OAuth tokens | Shared through the copied `google_auth.py` only (same OAuth client). Never copy, log or package them |
| `~/.shortnotes_bridge/pending/` | Notes cache (+ approval receipts), 7-day TTL; switched off in the old exe | Shared, with the same cache keys, so notes already made are not paid for again. The on/off switch is in Settings |
| `~/.shortnotes_bridge/local-blob/...`, `probe/`, `self-test.pdf`, `last_account.json` | The old exe's upload jobs and its files | Not used by Note Studio (it has its own runs). Runs → "From older apps" may list the old uploads read-only |
| `~/.note_studio/` | **NEW.** `note_studio.db` (settings, runs, queue, usage), `runs/<id>/`, `queue/<entry>/`, `categories/<id>/` (packs + rule files), `engines/<name>/` (per-engine state such as the UPSC transcript cache and QC cache) | The only place Note Studio writes its own data. Never packaged; never touched by install, update or uninstall |
| `%LOCALAPPDATA%\UPSC_Notes\` | Originals' UPSC + QC proxy pass, `qc_cache/`, `reference_index/` | Leave it. The copies use `~/.note_studio/engines/<name>/` instead |
| `%LOCALAPPDATA%\PWConciseNotes\auth\` | PW Books' encrypted Google refresh token | Leave it. The copy gets its token from the Studio |
| Old browser preferences (port 3000) | Teacher name, default subject... | Not carried over (Note Studio uses port 3100); typed once in Note Studio |

## On Google's side (never change the shape)

| Location | Rule |
|---|---|
| Sheet columns | Q "Auto Status" (`claimed <ts>` / `review` / `failed: <code>` / `skipped`), R "Review Status" (Review / Done / Regenerate), M = Done, the P timestamp, and the catalog columns "Concise Notes Link" and "Lecture Folder Link". These stay the same values in the same columns |
| Drive files | Batch sheets put the PDF beside its source. Catalog sheets use the `outputFolder` tree `<category>/<batch>/<subject>/<chapter>/<title> Concise Notes.pdf`. The properties `shortnotesSource` / `shortnotesRender` mean "already made, reuse or copy": never re-pay |
| Remote run log | One row per run in "Handwritten Notes Runs" (`run_log.py`). Keep writing it; the local history is in addition |
| Usage sheet | The proxy writes per-call usage to "Usage Cost 2.0". Unchanged. Which proxy `APP_NAME` Note Studio uses is a Phase 5 question |

## Promises the copies must keep

- **shortnotes:**
  - cache keys `concise-theory-only-v6` and `concise-theory-quality-v2`, plus
    the per-category fingerprint;
  - output name ` - Concise Notes.pdf`;
  - the strict `PublicJob` / manifest shapes;
  - redacted logs;
  - for every existing category × subject, `prompt_block()` and
    `fingerprint()` stay byte-identical when its rules are read from the packs
    instead of `format_rules/`. Snapshot-test that.
- **UPSC:** prompt files byte-identical. A mock-mode run through Note Studio
  writes the same prompt files as the original app (not the original CLI) for
  the same inputs.
- **Every engine:** its own tests reach the numbers recorded in Phase 0,
  including the failures it already had.

## Gate commands (run from `C:\Users\Laptop\Downloads\Note_Studio`)

```powershell
# Phase 0, once, before any change:
python NoteStudio_Kit\tools\data_guard.py backup            # about 1.3 GB; --light skips the old runs/
python NoteStudio_Kit\tools\data_guard.py snapshot NoteStudio_Kit\baseline.json

# After every phase:
python NoteStudio_Kit\tools\data_guard.py snapshot NoteStudio_Kit\after_phase_N.json
python NoteStudio_Kit\tools\data_guard.py compare NoteStudio_Kit\baseline.json NoteStudio_Kit\after_phase_N.json
python NoteStudio_Kit\tools\data_guard.py drift
```

- **`compare` STOP** (data gone, JSON entries lost, or an original edited):
  stop, explain, restore from `~/NoteStudio_backups/` or undo.
- **`drift` STOP** (a copy changed outside its allowed list, or a prompt/rules
  file changed): undo it. Only if the plan needs it, add the file to
  `ENGINE_COPIES[...]["allowed"]` with a reason and tell the user.
- **`DATA CHANGED` / `DATA NEW`:** must be explained. In shared and original
  data this is expected only from the old apps still in use, or from a save
  the user made in Note Studio.
