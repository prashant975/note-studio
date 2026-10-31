# 06 — Phase plan: building Note Studio from copies

All work happens in `C:\Users\Laptop\Downloads\Note_Studio\`. The original
projects are only read and copied.

**The rhythm of every phase:**

1. **Estimate first**: a short table of parts and times.
2. **Build** in small steps, with tests.
3. **Gate:**
   - the Studio tests;
   - each copied engine's own tests, from the phase it arrives;
   - `data_guard.py snapshot` + `compare`;
   - `data_guard.py drift`.
4. **Report:** what changed, test numbers against the Phase 0 baselines, the
   guard results with every CHANGED item explained, and what is still not
   reachable from the Studio.
5. **Wait for the user's go.**

No exe build or install, no commit, push or deploy, and no paid generation
unless the user says so in that conversation. Times assume one focused
Claude Code session. Say so when a phase slips.

---

## Phase 0 — Safety net and facts (≈45-60 min; nothing copied yet)

- **Read** the kit docs 01-08, `note_types/`, `categories/`, and the
  originals' own docs: `shortnotes-core\HANDOFF.md` (newest first),
  `README.md`, `DESIGN.md`, `notesbot\format_rules\README.md`; the UPSC
  `README.md`.
- **`data_guard.py backup`** (about 1.3 GB; `--light` skips the old runs) and
  **`snapshot NoteStudio_Kit\baseline.json`**.
- **Record test baselines.** Run each original's tests **inside the original,
  with its own `.venv`**: record the numbers and the names of failing tests,
  and change nothing. If a suite cannot run, record why; never install into an
  original.

  | Original | Command | Last known |
  |---|---|---|
  | shortnotes-core | `python -m pytest tests -q` | about 1603 passed, 1 known glyph-hash failure |
  | Upsc_Notes_2\upsc_Notes | `python -m pytest tests -q` | about 1171 tests, 42 cached failures |
  | handwritten_notes_automation_complete | `python -m pytest tests -q` | about 164 tests, 4 cached failures |
  | upsc_file_QC | `MAX_PARALLEL_PDFS=1 python -m pytest tests -q` | about 188 tests, 7 cached failures |

- **Show the user:**
  - the two sheet lists (`~/.shortnotes_bridge\sheets.json`, 6 profiles, vs
    `shortnotes-core\sheets.json`, 5): names, ids and fields only;
  - the handwritten project's files that git does not track and its
    uncommitted edits. Suggest they commit them themselves.
- **Ask, each with a recommendation:**
  1. Share `~/.shortnotes_bridge` (sheet list, Google tokens, notes cache) with
     the old exe? Recommended: yes, so there is one sheet list and one
     sign-in.
  2. Port 3100 (runs beside the old exe on 3000)? Recommended: yes.
  3. Run one queue lane at a time until the model choice is per run?
     Recommended: yes.
  4. Make `Note_Studio` a local git repo, with commits only when the user says
     so? Recommended: yes.
- **Check:** the baselines are recorded and the questions answered.

## Phase 1 — Create the project, copy the notes engine, Studio skeleton (≈5-7 h)

- **Copy `shortnotes-core` into `engines\shortnotes\`.** Take only code,
  prompts, rules, fonts, assets and tests (doc 05 lists what is left out). Add
  `COPIED_FROM.md`.
  - Run the copied tests **from the copy**, in Note Studio's own venv. They
    must reach the shortnotes baseline.
  - `drift` must show 0 changes.
- **`note_studio\`:**
  - FastAPI + Jinja2 + HTMX (vendored).
  - `base.html` with the sidebar from doc 04, and `style.css` with tokens.
  - Pages: Overview, a Settings stub (sign-in status), `error.html`.
  - `db.py`: SQLite WAL at `~/.note_studio\note_studio.db` with the
    `settings`, `runs`, `run_items`, `run_events` and `job_queue` tables, and
    startup `recover()`.
  - `launch.py`: port 3100 → 3110, single instance.
  - Middleware `_same_site_only`, plus `render()` and `go()`.
- **Wiring:** a `run_studio.py` (dev) and a `START_NOTE_STUDIO.bat`. The copied
  `sheets_bridge` is pointed at the shared `~/.shortnotes_bridge` files (if
  agreed) through the same environment settings the old launcher sets
  (`SHEETS_BRIDGE_PROFILES`, `SHEETS_BRIDGE_TOKEN`, `SHEETS_BRIDGE_CLIENT_SECRET`).
- **Tests:** `tests\` uses TestClient, puts the DB in `tmp_path`, blocks the
  network, and checks that every page returns 200 with no traceback. Add a
  pyflakes test and the "no bare `src`/`config` in `sys.modules`" test.
- **Check:**
  - the Studio opens;
  - the copied engine's tests match the baseline;
  - `compare` is clean;
  - `drift` shows 0 changes.

## Phase 2 — Categories, note types, requirements, New notes, Runs (≈1.5 days)

- **Category packs** (doc 07) in `~/.note_studio\categories\`:
  - seeded on first start from `engines\shortnotes\notesbot\format_rules\`
    (`neet_jee`, `foundation`) and the `upsc` example (disabled until
    Phase 5);
  - `_template` for new packs.
- **Allowed change:** the copy's `notesbot\categories.py` reads its category
  list and rule texts from the packs. For every existing category × subject,
  `prompt_block()` and `fingerprint()` must be byte-identical before and
  after: snapshot test.
- **Registry and requirements:**
  - `note_types.json` + `note_types.py`: `concise_handdrawn` ready, the rest
    "coming";
  - `requirements.resolve()` (layers, comment lines dropped, fingerprint),
    with tests.
- **Engine adapter** `engines\shortnotes.py` (in process) calls **the same path
  the old Upload page uses**. Parity test with a stubbed model, against the
  original's upload path.
- **New notes:** category → type → subject → language → inputs → options →
  Make notes; one run per file.
- **Run page** with one layout; **Runs** page with filters.
- **First-run import:** the old app's `.env` knob values.
- **Check:** one real lecture made through Note Studio matches the old exe's
  output. This is paid: ask first, and the user picks the lecture.

## Phase 3 — Automation (≈1 day)

- **Sheet list:** the shared `sheets.json`, with every field editable plus
  category + note type. Unknown keys are preserved, and every save writes a
  `.bak`. Test that the old exe still reads the saved file.
- **Run options:** every one, through the copied `sheets_bridge` code.
- **Live rows table:** Stop after the current row, with links to the filed PDFs.
- **History:** saved in the DB and survives a restart; the remote run-log tab
  is still written.
- **Check setup buttons:** setup check, model test, PDF probe.
- **Warning:** the page warns when the old exe is running.
- **Check:** the dry run on the test sheet equals the old CLI dry run. A real
  row needs the user's go.

## Phase 4 — Smart Queue + Settings + Add category (≈1.5 days)

- **Per-run model:** the per-run model becomes a context variable (allowed
  change), with a thread test.
- **Smart Queue:** two lanes, holds on Stop / sign-in / restart, de-duplication,
  "Start another", and the queue strip.
- **Settings:**
  - Sign-in;
  - AI models per type and stage;
  - **Categories:** edit rules with `.bak`, Preview requirements, Check
    category, **Add category**;
  - Note types; Values you can change; Storage; Sheets.
- **Check:**
  - queue 3 PDFs + 1 sheet and confirm Stop, Continue and restart behave;
  - add a test category ("Test CLAT", one rules line) **from the Studio only**,
    make notes with a stubbed model, and see the rule in Preview and in the
    prompt the engine received.

## Phase 5 — Engine host + copy UPSC + UPSC notes (≈2 days)

- **Ask first:** one PW proxy `APP_NAME` for Note Studio, or keep each
  engine's own (separate cost lines and whitelist entries)?
- **`engine_host.py`:**
  - the child-process runner from QBG's `stream_command`, with the doc 07
    contract;
  - Stop kills the process tree;
  - the token goes through the environment, and each child gets its own `TEMP`.
- **Copy `Upsc_Notes_2\upsc_Notes` into `engines\upsc\`.** Take code, prompts,
  config, assets, `reference_documents`, `third_party\ffmpeg` and tests; leave
  out runs, data, `.env`, `.streamlit` and venvs. Add `COPIED_FROM.md`.
- **Add `engines\upsc\engine_main.py`:**
  - it sets `HANDWRITTEN_NOTES_ROOT` (the copy), `HANDWRITTEN_NOTES_ENV`
    (a path that does not exist), `RUNS_DIR`, `OUTPUT_DIR` and the models from
    `request.json`, **before** importing `src`;
  - it calls `run_pipeline` **the way the original app does** (`app.py:3059` +
    `src/app_defaults.py`), not the way `run_cli.py` does;
  - it maps the `stage_callback` labels to `step` events.
- **Allowed change:** the append-only "extra instructions" hook in the copy's
  `src/prompt_builder.py`.
- **Types:** `upsc_dcn`, `upsc_simple`, `upsc_mains`, `upsc_prelims`, with a
  class-recording link + slides input. Enable the `upsc` pack.
- **Runs → "From older apps":** reads the original UPSC `runs\` in place.
- **Check setup:** ffmpeg (the copy's `third_party`), LibreOffice or Word, and
  fonts.
- **Tests:**
  - the copied UPSC tests reach the baseline (Streamlit AppTest UI tests
    excluded);
  - mock-mode prompt parity against the original **app** path;
  - one end-to-end child test.
- **Check:** the user approves one real class run (paid) and compares it with
  the old app.

## Phase 6 — UPSC Mains practice + DPP (+ Edit & rebuild) (≈1.5 days)

- **DMP (Mains practice)** on a finished run, Note Studio's or an older-app UPSC
  run: questions → tick → answers → export. Each step is a queue item. Output
  goes to Note Studio's run folder, never into the old run.
- **DPP:** counts → preview with replace/remove → export. The workbooks are read
  in place via Settings. Marathi shows "English or Hindi only". **Ask** how
  teammates get the 55 MB bank.
- **Optional:** "Edit notes text" + Rebuild (`rebuild_outputs`).
- **Check:** DMP + DPP from an old UPSC run; `compare` shows the old run
  unchanged.

## Phase 7 — (optional) PW Books handwritten (≈1 day)

- Copy into `engines\pw_books\` from the **working tree**, including the files
  git does not track.
- `engine_main` reproduces the original UI's model routing
  (`app.py:1321-1341`).
- Allowed: the token-from-environment change in `src/pw_auth.py`, and the
  extra-instructions hook.
- The type `pw_books_handwritten` is offered by `neet_jee`.
- Tests reach the baseline; one real deck if the user approves (paid).

## Phase 8 — (optional) Check notes (≈1.5 days)

- Copy `upsc_file_QC` into `engines\notes_qc\`.
- **Check these notes** on every finished run, plus a Check notes page for any
  file.
- QC rules per category: UPSC uses the copied yaml; other categories get the
  offline checks plus the AI review.
- **Ask:**
  - remove the direct `GEMINI_API_KEY` fallback (recommended: yes, proxy only);
  - allow web search?
- Tests reach the baseline; one old PDF's report matches the old tool.

## Phase 9 — Team installer + updates + tours (≈1.5 days; build/install only on the user's word)

- **Version:** `YYYY.M.D.N`.
- **Packaging:** recommended is QBG's embedded runtime with the code as
  folders (several engines have a top-level `src`, which collides in one
  PyInstaller archive). Confirm with the user.
- **The installer carries** the packages of all engines
  (`verify_runtime_imports`), ffmpeg and the fonts. **Ask** about installer
  size and the DPP bank.
- **Build checks:** `verify_clean`, `secret_scan`, `smoke_test`, and one mock
  child run per engine.
- **Update button** from a Drive folder.
- **Tours:** "Welcome to Note Studio" plus one per engine; page guides
  everywhere.
- **Check:** install A, update to B through the button, and `compare` shows
  nothing lost.

## Phase 10 — Hand-over (≈2 h)

- `README.md` and `HANDOFF.md` for Note Studio: how to run it, the folders,
  where data lives, how to add a category, how to add a note type.
- Tell the user which old apps they can now retire. They stay on disk
  untouched; Claude never deletes them.

---

## Total

| Phase | Time |
|---|---|
| 0 Safety net | 45-60 min |
| 1 Project + copy shortnotes + skeleton | 5-7 h |
| 2 Categories + types + New notes + Runs | ~1.5 days |
| 3 Automation | ~1 day |
| 4 Queue + Settings + Add category | ~1.5 days |
| 5 Engine host + UPSC notes | ~2 days |
| 6 UPSC practice | ~1.5 days |
| **Core: notes app + UPSC in one app** | **about 9 working days** |
| 7 PW Books (optional) | ~1 day |
| 8 Check notes (optional) | ~1.5 days |
| 9 Installer + updates + tours | ~1.5 days |
| 10 Hand-over | ~2 h |

A future category that fits an existing note type needs **no phase at all**.
It is added from Settings → Categories.
