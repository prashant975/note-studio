# Prompt — paste everything below the line into Claude Code, opened in `C:\Users\Laptop\Downloads\Note_Studio`

---

You are building **Note Studio**, a **new project** in this folder (`C:\Users\Laptop\Downloads\Note_Studio`). It **copies the code** of my existing note apps into `engines\` and makes all of them reachable from **one desktop app**. From that one app I can:

- **make notes** of every type, for every category: NEET-JEE, Foundation, UPSC now, and new categories such as CLAT or SSC later from Settings, without new code;
- **start the Google Sheet automation**;
- **make practice**;
- **check finished notes**.

Build it in the same architecture as my other app, **QBG Studio**. **Never edit my original projects:** they keep working as they are. **Keep all saved data.**

## The originals (read and copy; never edit, move or delete)

| Original | What it does | Copy into | Phases |
|---|---|---|---|
| `C:\Users\Laptop\Downloads\shortnotes-core` | Lecture PDF → hand-drawn concise notes PDF; Google Sheet automation | `engines\shortnotes\` | 1-4 (core) |
| `C:\Users\Laptop\Downloads\Upsc_Notes_2\upsc_Notes` | Class recording (+ slides) → UPSC DCN / simple / mains / prelims notes; Mains practice (DMP); DPP from notes | `engines\upsc\` | 5-6 (core) |
| `C:\Users\Laptop\Downloads\handwritten_notes_automation_complete\handwritten_notes_automation_complete` | PW Books handwritten DOCX/PDF | `engines\pw_books\` | 7 (optional) |
| `C:\Users\Laptop\Downloads\upsc_file_QC` | QC report for finished notes | `engines\notes_qc\` | 8 (optional) |

The older copies (`Downloads\upsc_notes`, `Upsc_Notes_2\upsc_pdf_qc`, both `notes_to_dpp_taging` folders) are superseded. Do not use them.

## Your briefing pack: `NoteStudio_Kit\` in this folder. Read all of it first

- `docs\01_QBG_STUDIO_PATTERN.md`: the architecture to copy (FastAPI + Jinja2 + HTMX, SQLite, feature = routes + service, Smart Queue, run pages, Settings, model order, installer, Update button, tours, guides, tests).
- `docs\02_NOTE_APP_TODAY.md`: the shortnotes original as it is. Verify it against the code.
- `docs\03_OTHER_NOTE_PROJECTS.md`: all my note folders and what each becomes.
- `docs\04_TARGET_DESIGN.md`: **the target.** Project layout, how the copies run, menu, New notes + Automation, queue, run page, Settings, distribution.
- `docs\05_DATA_TO_KEEP.md`: every data location, what is shared with the old app, what is new, and what the copies must keep.
- `docs\06_PHASE_PLAN.md`: phases 0-10 with checks.
- `docs\07_CATEGORIES_AND_TYPES.md`: **the core model.**
  - Engines → note types → category packs.
  - Requirements are layered text files.
  - Engines run as child processes over a JSON-lines contract.
  - An operator adds a category with no code.
- `docs\08_MERGE_PLAN.md`: the copy recipe, plus exact facts and pitfalls for the UPSC, PW Books and QC engines.
- `note_types\` (registry design + example) and `categories\` (pack examples: `_template`, `neet_jee`, `foundation`, `upsc`).
- `reference\qbg_studio\`: QBG Studio files to adapt (`reference\README.md` maps each to its source). The full QBG source at `C:\Users\Laptop\Downloads\QBG_Complete_Toolkit\QBG_Studio_Vibecraft\backend\studio\qbg_studio` is **read-only**.
- `tools\data_guard.py`:
  - `backup`, plus `snapshot` / `compare`, which prove no data was lost and no original was edited;
  - `drift`, which shows exactly how each `engines\<name>\` copy differs from its original. Only listed, allowed files may differ, and prompts and rules never may.

## What I want, in short

1. **One app, one menu:** Overview, Smart Queue, **New notes**, **Automation**, Runs, Check notes, Settings.
2. **How the copies run:**
   - `engines\shortnotes` runs **in the Studio process** (its folder on `sys.path`; `notesbot` / `sheets_bridge` imported unchanged).
   - Every other engine runs as a **child process** from its own folder (cwd and `sys.path` = that folder). Their own `src` / `config` / `pw_access` therefore work **without renaming anything** and never collide.
   - One new `engine_main.py` per engine speaks the doc 07 JSON-lines contract. Stop kills the child.
3. **Categories × note types:** every run is category × note type × subject × language plus options.
   - **Note types** (`note_types.json`):
     - `concise_handdrawn`;
     - `upsc_dcn`, `upsc_simple`, `upsc_mains`, `upsc_prelims`;
     - `upsc_mains_practice`;
     - `dpp_from_notes`;
     - `pw_books_handwritten`;
     - the `notes_qc` tool.
   - **Category packs** live in `~/.note_studio\categories\`:
     - `neet_jee` and `foundation` are seeded byte-for-byte from the copy's `notesbot\format_rules\`;
     - `upsc` uses the UPSC engine's own subject ids;
     - `_template` is for new categories.
4. **Requirements are layered text** (doc 07): engine prompt → type → category → subject → category×type → sheet profile → this run.
   - `requirements.resolve()` builds the stack and a fingerprint for the cache key.
   - Settings → Categories edits rules (with `.bak`), previews the stack, checks a pack, and **adds a new category from a form**.
5. **New notes:** category → type → subject → language → inputs (PDFs, a deck, or a class-recording link + slides) → options → Make notes. One run per file, through the Smart Queue.
   - For `concise_handdrawn` it must make **the same notes** as the old app's Upload page: same functions, prompt, cache keys, renderer, fonts and branding. Prove it with a stubbed-model test against the original.
6. **Automation:**
   - The sheet runs live in Note Studio through the copied `sheets_bridge`, with no second copy of the run logic.
   - Every sheet-profile field and every CLI option becomes a field or button, and each sheet gets a category + note type.
   - History survives a restart, and the remote run-log tab is still written.
7. **Runs:**
   - Every run of every engine.
   - On a finished run: Check these notes, Make Mains practice (UPSC), Make DPP.
   - "From older apps" lists the originals' own `runs\` read-only.
8. **Smart Queue:** many items, each its own run; two lanes (Notes / Sheets); holds on Stop, lost sign-in and restart (QBG `job_queue.py`).
9. **Settings, instead of `.env` and CLI-only knobs:**
   - Sign-in.
   - AI model order per note type and stage (each engine keeps its own defaults; budget-exceeded switches to the next model).
   - Categories, Note types, Sheets.
   - Values you can change.
   - **Check setup** (ffmpeg, LibreOffice/Word, fonts, the DPP workbooks), in plain words.
   - Storage, Updates.
10. **Later (Phase 9):** a team installer carrying everything the engines need, version `YYYY.M.D.N`, an Update button from a Drive folder, a what's-new tour per release, and a short guide on every page.

## Rules that are not negotiable

**Originals, copies and data**
- **Never edit an original.**
  - Copy its code, prompts, rules, assets, fonts, bundled tools (UPSC `third_party\ffmpeg`) and tests from the **working tree** into `engines\<name>\`, with a `COPIED_FROM.md`.
  - Do not copy runs, outputs, `.env`, `.streamlit`, `client_secret*`, venvs, builds, `dist`, `node_modules` or caches.
  - If an original's tests need a file, copy it.
- **Keep the copies as they are.**
  - Only the files listed in `ENGINE_COPIES` in `data_guard.py` may differ from their originals, each for its stated reason:
    - shortnotes: `categories.py` reads the packs, and the per-run model moves into a context variable;
    - UPSC and PW Books: the append-only extra-instructions hook;
    - PW Books: the token from the environment.
  - Prompts and rules stay byte-identical.
  - Never change cache keys (`concise-theory-only-v6`, `concise-theory-quality-v2`, per-category fingerprints), output naming, sheet row states and columns, Drive file properties (`shortnotesSource` / `shortnotesRender`), or the strict `PublicJob` / manifest shapes.
  - For NEET-JEE and Foundation, `prompt_block()` and `fingerprint()` must stay byte-identical when rules come from the packs: snapshot-test it.
- **UPSC is called the way its app calls it** (`app.py:3059` + `src\app_defaults.py`). Its `run_cli.py` takes a different, legacy path. Prove it with a mock-mode prompt-parity test.
- **Keep the data.**
  - Read the originals' data in place.
  - Note Studio's own data goes only in `~/.note_studio\`.
  - Share `~/.shortnotes_bridge\` (sheet list, Google tokens, notes cache) with the old exe only if I agree in Phase 0. Then preserve unknown keys, write a `.bak` before every save, and test that the old exe still reads the file.
  - The two sheet lists have diverged (6 vs 5 profiles). Show me; never drop a profile.
- **Gates.**
  - Phase 0 starts with `python NoteStudio_Kit\tools\data_guard.py backup` and `snapshot NoteStudio_Kit\baseline.json`.
  - Every phase ends with `snapshot` + `compare` + `drift`. A STOP result stops the work until it is fixed.
- **Port 3100**, so Note Studio can run beside the old exe on 3000.

**How you work with me**
- **Every step from the app.** I and my teammates are content staff, not developers. A step that needs a terminal, a `.env` edit or a pasted key is a step that does not get used. At the end of each phase, list what is still not reachable from the app.
- **Plain words in the UI:** "Make notes", "Waiting", "Needs attention", "Needs LibreOffice", "Stopped after row 12". Never stack traces or HTTP codes; those go in the downloadable log. Logs stay redacted.
- **Estimate before you build.** Start every phase with a short table of parts and time. Tell me if it slips.
- **One phase at a time.** After each phase, report:
  - what changed;
  - test results against the Phase 0 baselines (Studio tests, plus each copied engine's own suite, including the failures it already had);
  - the `compare` and `drift` results, with every change explained;
  - open questions.

  Then **stop and wait for my go.**
- **No build, install, commit, push or deploy unless I say so** in this conversation.
- **No paid AI generation** (a real lecture, class recording, sheet row or QC run) without asking me first. Use stubbed or mock models in tests.
- **Ask me about real decisions** and recommend an answer. Doc 06 lists them per phase:
  - sharing `~/.shortnotes_bridge`;
  - git for this folder;
  - one or several proxy app names;
  - QC's direct `GEMINI_API_KEY` fallback and web search;
  - how teammates get the 55 MB DPP bank;
  - packaging and installer size;
  - the update Drive folder.

  Pick conventional defaults for everything else and tell me what you chose.

**Code habits**
- Before creating any file, Glob for it. Before adding a route, grep for the URL.
- Use the Write/Edit tools, not shell heredocs (they mangle backslashes on this Windows machine).
- Copy folders with a script that preserves bytes (`shutil.copy2`), never by re-writing text.
- Never kill a process you did not start. The old exe or the old Streamlit apps may be running; ask me to close them.
- Do not copy QBG's `team_sync_*`, `team_support.py`, `team_data.py` or any QBG/portal business module.
- Keep `note_studio\app.py` to wiring. Each feature is `<feature>_routes.py` + `<feature>_service.py` + templates, as in QBG.
- Tests:
  - `tests\` uses TestClient, the DB in `tmp_path`, and blocks the network. Every page returns 200 without a traceback.
  - Add a pyflakes test, and a test that no bare `src` / `config` is imported in the Studio process.
  - Each copied engine's own tests run from its folder.
  - Known failures recorded in Phase 0 are not regressions; anything else is.

## Start now with Phase 0 only

1. Read the kit docs (01-08, `note_types\`, `categories\`) and the shortnotes original's `HANDOFF.md` (newest first), `README.md`, `DESIGN.md` and `notesbot\format_rules\README.md`.
2. Check doc 02 against the shortnotes code, and spot-check doc 08's entry points in the UPSC original. Tell me anything wrong or out of date.
3. Run the data-guard `backup` and the baseline `snapshot`. Record the test baselines of all four originals, each run inside the original with its own `.venv`, changing nothing.
4. Show me:
   - the sheet-list difference (names, ids and fields only, never tokens);
   - the handwritten project's untracked and uncommitted files. I may commit them myself.
5. Ask me the Phase 0 questions, each with your recommendation:
   - share `~/.shortnotes_bridge`;
   - port 3100;
   - one queue lane at a time until the model choice is per run;
   - make this folder a local git repo.
6. Give me the time estimate for Phase 1, then wait.
