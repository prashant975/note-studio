# 04 — Target: Note Studio, a new project that copies the note apps into one app

**Note Studio** is a **new project** at `C:\Users\Laptop\Downloads\Note_Studio\`.
It **copies the code** of the existing note apps into its own `engines/` folder
and makes all of them reachable from **one desktop app**, built in the QBG
Studio pattern (`01_QBG_STUDIO_PATTERN.md`). From one app the operator can:

- **make notes** of every type, for every category: NEET-JEE, Foundation,
  UPSC, and any category added later from Settings;
- **start the sheet automation**;
- **make practice** (UPSC Mains practice, DPP);
- **check finished notes**.

The original projects are **never edited**. They keep working exactly as they
do today, and the user retires them whenever they want.

| Original (read-only) | Copied into | Phase |
|---|---|---|
| `Downloads\shortnotes-core` (hand-drawn concise notes + sheet automation) | `engines/shortnotes/` | 1-3 (core) |
| `Downloads\Upsc_Notes_2\upsc_Notes` (UPSC notes, Mains practice, DPP) | `engines/upsc/` | 5-6 (core) |
| `Downloads\handwritten_notes_automation_complete\...` (PW Books handwritten) | `engines/pw_books/` | 7 (optional) |
| `Downloads\upsc_file_QC` (notes check) | `engines/notes_qc/` | 8 (optional) |

## Principles

1. **Copy, never edit the originals.**
   - Each original is copied whole, apart from its runs, outputs, venvs, builds
     and secrets, into `engines/<name>/`, together with a `COPIED_FROM.md`
     noting the source path, date and file count.
   - `data_guard.py drift` shows every difference between a copy and its
     original. Only the listed, allowed files may differ, and prompts and
     rules never may.
2. **The copied engines stay as they are.**
   - Their behaviour, output, cache keys and tests are kept. Note Studio calls
     them; it does not rewrite them.
   - The only edits allowed in a copy are the ones in `ENGINE_COPIES` in
     `data_guard.py`: a few small hooks with tests, plus each engine's new
     `engine_main.py`.
3. **Data is kept, and shared where that saves work and money.**
   - Note Studio's own data lives in `~/.note_studio/`.
   - It **shares** the old notes app's `~/.shortnotes_bridge/` for the sheet
     list (`sheets.json`), the Google sign-in tokens and the notes cache
     (`pending/`). Sheets, sign-in and already-made notes carry over, and the
     old exe and Note Studio see the same sheet list. **Ask in Phase 0**
     (recommended: share).
   - The old projects' runs are **read in place** (Runs → "From older apps").
   - The DPP workbooks are read in place through Settings paths.
4. **Every step from the Studio.** Anything that works today only from a CLI or
   `.env` gets a button or a setting. Whatever cannot be reached from the
   browser is listed in the phase report.
5. **Plain words for content staff:** "Make notes", "Waiting", "Needs
   attention", "Needs LibreOffice". Never stack traces or HTTP codes.

## Project layout

```
C:\Users\Laptop\Downloads\Note_Studio\
  CLAUDE.md                       standing rules for every Claude session here
  NoteStudio_Kit\                 this briefing (docs, tools, reference, examples)
  note_studio\                    THE APP (FastAPI + Jinja2 + HTMX), written new
    app.py                        wiring only: middleware, render(), register() calls
    launch.py                     loopback port 3100 (3101-3110 if busy), single instance, opens the browser
    db.py                         SQLite (WAL) at ~/.note_studio/note_studio.db
    settings_store.py             get/set settings; first-run import of the old .env knobs
    categories.py                 category packs in ~/.note_studio/categories/ (seeded on first start)
    requirements.py               resolve(category, type, subject, language, ...) -> texts, switches, fingerprint
    note_types.py + note_types.json   the note-type registry
    engine_host.py                runs child-process engines (JSON-lines events, Stop, env token, own TEMP)
    engines\                      adapters: shortnotes.py (in process), upsc.py, pw_books.py, notes_qc.py (child)
    runs.py, job_queue.py         run history; Smart Queue (two lanes)
    make_routes.py / make_service.py              New notes
    automation_routes.py / automation_service.py  Sheet automation (wraps the copied sheets_bridge)
    practice_routes.py            Mains practice + DPP on a finished run
    check_routes.py               Check notes
    history_routes.py, settings_routes.py, models.py, whats_new.py, guides.py, updater.py
    templates\, static\           base.html + pages; style.css (tokens), htmx, whats_new.js, release.js
  engines\                        COPIES of the original projects' code
    shortnotes\                   notesbot\ sheets_bridge\ api\ (what the upload path imports) equation_kit\
                                  fonts\ assets\ pw_access.py tests\ COPIED_FROM.md
    upsc\                         src\ prompts\ config\ assets\ reference_documents\ third_party\ffmpeg\
                                  pw_access.py tests\ engine_main.py COPIED_FROM.md
    pw_books\, notes_qc\          (phases 7-8)
  packaging\                      installer build (Phase 9)
  tests\                          Studio tests (engine tests live in engines\<name>\tests\)
```

### How the copies run

- **`engines/shortnotes` runs in the Studio process.** Its folder is put on
  `sys.path`, so `import notesbot` and `import sheets_bridge` work unchanged.
  Its `pw_access.py` is the Studio's PW access as well.
- **Every other engine runs as a child process**, one per run:

  ```
  python engines\<name>\engine_main.py --request <run>\request.json --out <run>
  ```

  It runs with **cwd and `sys.path` = that engine's folder**, so its own
  top-level `src`, `config` and `pw_access` work **without renaming
  anything**. They can never collide with each other or with the Studio,
  because each lives in its own process.
  - The JSON-lines event contract is in doc 07.
  - Stop kills the child.
  - The sign-in token goes through the child's environment, never into
    `request.json`.
  - The engine's settings are set from `request.json` before its config is
    imported. The original's `.env` is never read.
- **A test** imports the Studio and the shortnotes engine in one process and
  checks that no bare `src` / `config` is in `sys.modules`.

## Menu (grouped by job)

```
Overview          running now, waiting, recent notes, today's spend, quick buttons
Smart Queue       everything waiting/running, both lanes, hold/continue, remove
MAKE NOTES
  New notes       Category -> Note type -> Subject -> Language -> PDFs / deck / class-recording link (+ slides) -> Make notes
  Automation      Google sheets (each with a category + note type) -> run / dry run / one row / redo / stop
RESULTS
  Runs            every run of every engine: files, cost, log, Retry, Open in Drive
                  on a finished run: Check these notes . Make Mains practice (UPSC) . Make DPP
                  "From older apps": the original projects' own runs, read-only
CHECK
  Check notes     QC report for any finished notes file (phase 8)
SETTINGS          Sign-in . AI models . Categories (add / edit rules / preview / check) . Note types .
                  Sheets . Values you can change . Check setup . Storage . Updates . About
sidebar footer:   version . Update slot . What's new . signed-in account
```

## The two ways in

| | New notes | Automation (sheet) |
|---|---|---|
| Input | what the category and type accept: PDF upload(s) or link(s), a .pptx deck, a class-recording link (+ slides) | rows of a registered Google Sheet |
| Category + type | chosen on the page (the type list is the category's types) | stored on the sheet profile (`category`, `noteType`). Existing sheets default to what they do today |
| Options | the type's `options`; subject and language from the category | the same, plus the sheet columns that override them today (category column D, faculty columns) |
| Runs through | the type's engine via the queue | the copied `sheets_bridge` run path (`batch.run_targets` / `runner.Runner` / `catalog_flow.CatalogFlow`), unchanged |
| Output | `~/.note_studio/runs/<run_id>/` + Download/Open | Drive filing + sheet write-back exactly as today; links on the run page |

**Parity:** for the hand-drawn type, New notes must make **the same notes**
the old app's Upload page makes for the same PDF and options. That means the
same functions, prompt, cache keys, renderer, fonts and branding
(`pdf_fonts.install()`, `pdf_branding.install()`). Prove it with a
stubbed-model test that compares notes JSON and page count between the
original's upload path and Note Studio's.

**Automation** shows every sheet-profile field (name, sheet + tab or all tabs,
output folder, signature, max spend, also-PDF, enabled, skipped batches,
category, note type) and every CLI run option (dry run, limit, one row, redo,
subject, Sanskrit style, page numbers). Check setup, model test and PDF probe
become buttons.

**Old and new app on the same sheet.** Rows are claimed in the sheet (`claimed
<ts>`), as today. Still, do not run the old exe's automation and Note Studio's
on the same sheet at the same time; the Automation page warns if the old exe
is open.

## Smart Queue (adapted from QBG `job_queue.py`)

- **The request waits, not a run.** Settings and files sit in
  `~/.note_studio/queue/<entry>/`; the run is created when its turn comes.
- **Two lanes:** Notes (New notes, practice, check) and Sheets (automation),
  one running entry per lane.
- **Holds:** Stop, lost sign-in and a Studio restart hold the queue; a failed
  item does not.
- **De-duplication:** a double click does not add the same request twice.
- **Before two lanes may run at once:** the shortnotes engine chooses its model
  through `os.environ["PW_PROXY_MODEL"]`, which is process-wide. Move it into
  a context variable (an allowed change in the copy, with a thread test).
  Until then, run one lane at a time. Child engines are already isolated.

## Run page (one layout for every engine)

- **Header:** category, type, source, options, who started it.
- **Steps:**
  - in-process engines report through `progress.step(...)`;
  - child engines through `step` events;
  - automation through `{row, outcome, position, total, done, failed,
    costInr, sheet}`.
- **Cost so far (INR):** from the proxy-reported cost, and from `cost` events.
- **Live log** in plain words. Technical lines go only into the downloadable
  log, which stays redacted.
- **Buttons:** Stop, Retry, Download / Open, Open in Drive, Start another, and
  the "on a finished run" actions.
- **Polling:** HTMX polls `_run_card.html` every 2 s while the run is active.

## Settings

| Card | Holds |
|---|---|
| Sign-in | PW access status, Google account for Sheets/Drive (the shared token from `~/.shortnotes_bridge/tokens/` if the user agreed), Sign in / Sign out |
| AI models | Order per **note type and stage**. Each engine keeps its own defaults (shortnotes `gemini-3.7-flash` + `SELECTABLE_MODELS`; UPSC `gpt-5.6-luna-ard-content` → `gemini-3.5-flash`; speech-to-text and image models as in the original). A category may override. Budget-exceeded → next model; 429 → retry. A Test button |
| Categories | Every pack. Enable/disable, subjects, languages, offered types, branding. Rule files have **Edit**, which keeps a `.bak`. **Preview requirements**, **Check category**, **Add category** (doc 07) |
| Note types | Each type: engine, status ("Needs LibreOffice" etc.), inputs/outputs, cache revision |
| Sheets | Default output folder, signature, max spend, run-log sheet |
| Values you can change | The knobs the old app kept in `.env` / CLI (chunk size, max chunks, images, mind map, source review, pages per call, workers, reasoning effort...). Applied on the next start; Restart button |
| Check setup | Google/PW sign-in, sheet access, model test, PDF probe, ffmpeg/ffprobe, LibreOffice or Word, PowerPoint for .ppt, fonts (Kalam, Coming Soon), DPP workbooks. Plain words and how to fix each |
| Storage | Data folders and their sizes; optional cleanup of finished upload files older than N days (default: keep everything) |
| Updates / About | Version, Check for updates, Install; data folders, log file, "Copy diagnostics" (redacted) |

**First run:** the old app's `.env` knob values are imported once into
Settings as the starting values; the file itself is not changed.

- The old app's browser preferences (teacher name, default subject) were
  stored by the browser for port 3000. Note Studio uses port **3100** so it
  can run beside the old exe, so those few values are typed once more.
- The Sign-in card remembers the teacher name from then on.

## Look

- QBG's structure: tokens in `:root`, one stylesheet, a sidebar, cards, one
  filled button per screen, "More options" for advanced fields, one notice per
  page.
- A distinct accent colour, so it is never confused with QBG Studio.
- The system font stack, with Nirmala UI for Hindi and Marathi.

## Team distribution (Phase 9)

- **Version** `YYYY.M.D.N`, shown in the sidebar.
- **Per-user Inno Setup install** in `%LOCALAPPDATA%\Programs\NoteStudio`.
  The data in `~/.note_studio` and `~/.shortnotes_bridge` is never touched by
  install, update or uninstall.
- **Packaging (recommended): QBG's embedded Python runtime with the code as
  plain folders**, not a PyInstaller bundle. Several engines have a top-level
  package called `src`. In one PyInstaller archive they would collide; as
  folders, each child process puts only its own engine on `sys.path`.
  Confirm with the user in Phase 9.
- **The installer carries** every engine's Python packages
  (`verify_runtime_imports`), **ffmpeg** (about 194 MB), the fonts and the
  engines' assets. LibreOffice/Word are not bundled; Check setup guides the
  user.
- **Baked in:** the OAuth client and the update folder id. Teammates paste
  nothing.
- **Build checks:** `verify_clean` (no tokens, `.env`, runs or saved work
  packaged), `secret_scan`, `smoke_test` (a clean install opens every page),
  and one mock child run per engine.
- **Update button** (QBG `updater.py`): Drive folder, sha256 check, silent
  install, restart.
- **A what's-new tour per release, and page guides.**

## What is NOT in scope

- Editing, moving or deleting any original project. The Vercel website stays
  in `shortnotes-core`, untouched; Note Studio does not include it.
- Rewriting the copied engines. Only the allowed hooks may change (prompts and
  rules never), plus each engine's `engine_main.py`.
- Changing cache keys, output names, sheet row states and columns, Drive file
  properties, or the shortnotes `PublicJob` / manifest shapes.
- Telemetry, team-data mirroring or remote-support features.
