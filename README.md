# NoteCraft (the Note Studio project)

One desktop app for every kind of notes the team makes. It runs copies of the
four note apps, plus one engine of its own:

| Engine | Makes | Category | Copied from |
|---|---|---|---|
| `shortnotes` | Concise hand-drawn notes from a lecture PDF; the Google Sheet automation | NEET-JEE, Foundation | `Downloads\shortnotes-core` |
| `upsc` | UPSC class notes (DCN, simple, Mains, Prelims) from a class recording; Mains practice; DPP | UPSC | `Downloads\Upsc_Notes_2\upsc_Notes` |
| `pw_books` | PW Books handwritten-style Word and PDF notes from a deck | NEET-JEE | `Downloads\handwritten_notes_automation_complete\...` |
| `notes_qc` | An error report for any finished notes | Every category | `Downloads\upsc_file_QC` |
| `typeset` | Typeset A4 notes in the B.Sc. Wallah layout from a lecture PDF | B.Sc. | none: NoteCraft's own (`note_studio/typeset`) |

- A run is **category × note type × subject × language**. Categories are data,
  added from Settings; a note type is code.
- The original projects are never edited, and they keep working on their own.
- The copies stay byte-identical except for a short list of allowed edits (see
  Tests and gates).
- Status and history are in `HANDOFF.md` (newest first). Standing rules for
  every session are in `CLAUDE.md`.
- The plan is in `NoteStudio_Kit/`: start with `START_HERE.md` and
  `docs/06_PHASE_PLAN.md`.

## Running it

**Installed (the team).** Run `NoteCraft_Setup.exe`. It installs per user,
with no admin rights, into `%LOCALAPPDATA%\Programs\NoteCraft`, and adds a
desktop and Start-menu shortcut.
- `NoteCraft.exe` shows "Starting NoteCraft…" and opens the browser when the
  app is ready. It then sits in the notification area: right-click → Quit.
- Its logs are in `<install>\logs\`.
- Python and every engine come with it. PDFs from Word notes need Microsoft
  Word or LibreOffice on the computer.

**From the project folder (development).** Double-click `Start NoteCraft.bat`,
or run `python run_studio.py`.
- It uses this computer's Python 3.13.
- The child engines run on each original app's own `.venv`, read in place. So
  keep the original folders here.

**Two installer editions.** The normal build includes every engine and keeps
the `NoteCraft_Setup_<version>.exe` name. Build the smaller edition with
`python packaging\build_installer.py --without upsc --without pw_books`; it
leaves out UPSC notes, Mains practice, DPP, PW Books, their libraries, ffmpeg
and the question bank. Hand-drawn and B.Sc. typeset notes, Sheet automation
and Notes check remain. Its files are
`NoteCraft_Setup_NoUPSC_NoPWBooks_<version>.exe` and `.txt` in
`dist/team/<version>-noupsc-nopwbooks/`; each edition only offers its own updates, even
in the same release folder. Building it leaves the source/full release metadata
and existing full installers unchanged. Omitting only `upsc` is also supported,
with `NoteCraft_Setup_NoUPSC_<version>` names and a `-noupsc` folder.
Build, installation and upload are separate actions.
If an original engine environment is unavailable, an explicit
`--engine-libs ENGINE=FOLDER` can copy that engine's libraries from a preserved
package; the builder records the source and reruns all import and mock checks.

**Ports.** NoteCraft uses 3100, or the next free one up to 3110. That never
overlaps the team's other tools: 3000 (the old Short Notes app), 8000-8009
(QBG One), 8501-8510 (the Streamlit tools).
- Anything else on a port, including a NoteCraft with a different data folder,
  is skipped.
- A NoteCraft already running on the same data folder is opened instead of
  started twice: two copies would share one database and one queue.

**Gujarati Short Notes.** In New notes, choose Foundation or NEET-JEE,
Concise hand-drawn notes, then Gujarati (ગુજરાતી). The bundled Gujarati face
handles vowel signs and conjuncts in the same card layout. The default
"Use sheet automation style and quality checks" option invokes the original
automation generator, duplicate audit and final PDF review. Per-group source
review follows the original's setting (off by default). A run retains its
quality report; unresolved or unavailable reviews show Needs attention.
Untick that option to use the legacy Upload workflow. Existing languages
keep their original engine prompts. The Gujarati extension lives in
`note_studio/shortnotes_extensions.py`; engine source files are unchanged.

Gujarati generation also reviews chapter organisation against the source PDF:
late introductions and repeated topic fragments are moved/merged, unsupported
additions require source evidence before removal, and source terminology/date
corrections can reach table cells and diagram labels. This replaces the existing
whole-lecture duplicate call in automation; legacy uploads run one such review.
Incomplete/invalid reviews retain content and are noted in the automation quality
report. `note_studio/gujarati_editorial.py` holds these rules and reference guards.
`note_studio/gujarati_layout.py` keeps prose numerals in the surrounding font,
table rows intact when they fit a page, glossary continuation captions unique,
and process steps connected. Source-backed schematics retain their geometry.
These changes apply to new generation/renders after restarting the source app;
existing delivered PDFs and installed executables are not rewritten.

**B.Sc. typeset notes.** In New notes, choose B.Sc., Typeset notes (B.Sc.
style), then upload the original lecture PDFs. **AI reads and checks every page
by default**, using Google sign-in and PW AI credit. There is no transcription
checkbox or layout-mode choice.
- Word references (`.docx`) are also accepted by B.Sc. style, including custom
  Nursing categories. Every native paragraph, table cell and embedded image is
  retained. AI selects source IDs and formatting only; it cannot rewrite the
  document. Images retain their bytes and sequence; table headers repeat across
  pages. Unsupported Word structures request PDF export before any billing.
  Each uploaded file becomes its own Smart Queue run. A failed retry reuses its
  saved formatting when source and prompt hashes match.
- Final quality review is automatic for Word and default AI PDF references. It
  uses the NEET/JEE review schema and notation/chemistry checks, plus source
  coverage/order, language, terminology, scientific clarity, figures, table
  legibility and every rendered page. Full reference content overrides concise
  content exclusions. Suggestions are reported without rewriting the source.
  A receipt binds the exact PDF/notes/source/prompt hashes; incomplete review
  cannot pass. Identical rerenders and retries reuse valid paid reviews.
- The fixed reference template uses Lato typography, A4 geometry and coloured
  box styles. The source wording, headings, numbers, examples and order are
  retained. The model is instructed to transcribe without paraphrasing or
  correcting source facts.
- Formulas, tables and diagrams use lossless source excerpts. They remain in
  source order and are never redrawn or replaced by AI. The source language
  is retained regardless of the language selector.
- Text highlights and inline formula images are handled as part of the text,
  rather than causing the whole page to be pasted back in its old layout.
  Source definitions and feature lists receive the reference's box styles.
- Digital, scanned and handwritten pages are transcribed and checked separately,
  without paraphrasing; the flat response
  schema is validated and raw responses retained. Formulas/tables/diagrams use
  original crops including captions. Transcription requires source review and
  cannot receive an automatic exact-wording guarantee. Failed-run retries reuse
  saved checked pages for the same source and prompt without charging again;
  an unfinished page proposal receives only its remaining verification call.
- Upload the **original lecture PDF**. Reformatting an incomplete older output
  cannot recover omitted lecture content. A PDF containing LaTeX source code is
  rejected: compile it first and upload the finished lecture PDF.
- `source_map.json` records the source PDF hash, each ordered item, its text
  or excerpt hash, and excluded reference running headers/footers. Content
  changes, omissions, duplication, reordering, missing excerpts or wording
  that cannot be found in the rendered PDF prevent delivery.
- The former rewrite workflow remains compatible with saved historical requests;
  the New notes form uses the reference template and literal AI transcription.
- Each run keeps `quality_report.json`. Header/footer branding still comes
  from the category pack's `branding`, editable in Settings.

The distinction matters: the old reference preview used manually supplied
content and proved only the renderer. The source-preservation tests now start
with actual PDFs and check the resulting PDF, with no stubbed AI content.
The extraction uses [PyMuPDF's positioned text and image data](https://pymupdf.readthedocs.io/en/latest/app1.html);
[structured model JSON](https://ai.google.dev/gemini-api/docs/structured-output)
alone cannot establish that a lecture's content was retained.

## The folders

```
note_studio/            the app: <feature>_routes.py + <feature>_service.py, templates/, static/
  engines/              adapters, one per engine (upsc.py, pw_books.py, notes_qc.py, shortnotes.py, typeset.py, child.py)
  typeset/              the typeset notes engine (prompts, document, ai, formulas, diagrams, render, quality)
  packs/                built-in category packs (upsc, bsc, _template), copied to ~/.note_studio/categories once
  note_types.json       the note type registry
  engine_host.py        starts a child engine (JSON-line events, Stop, job object)
  updater.py            the Update button (QBG One's workflow)
engines/<id>/           the engine copies, each with COPIED_FROM.md and (child engines) engine_main.py
packaging/              the installer build (QBG One's kit, adapted): build_installer.py and its pieces
tests/                  NoteCraft's tests; tests/parity/ drives the original apps to prove the copies match
NoteStudio_Kit/         the plan, the data guard (tools/data_guard.py), copy_engine.py, gate snapshots
dist/team/<version>/    finished installers (the build keeps the last 3; the versioned name is a hard link)
_build/                 build staging and verification.json (safe to delete); each build removes the
                        2.2 GB staged package of every finished build but its own, even with --keep 1000
                        or --keep-package, and never touches the other folders here
```

## Where data lives

- **NoteCraft's own data:** `%USERPROFILE%\.note_studio\` and nowhere else.
  Updates and reinstalls never touch it.
  - `note_studio.db`: runs, the Smart Queue, settings. It is SQLite: the
    `-wal`/`-shm` files beside it come and go by themselves.
  - `runs\<id>\`: each run's input and notes.
  - `categories\`: the packs and their rules. A `.bak` is kept before each
    change.
  - `team_key.json`: the service-account key for updates. Never shown; only its
    email is.
  - `engines\upsc\`: UPSC engine state and the DPP cache.
  - `logs\`: problem reports.
- **Shared with the old Short Notes exe:** `%USERPROFILE%\.shortnotes_bridge\`,
  the sheet list and the Google sign-in. The user agreed to share it in
  Phase 0. NoteCraft keeps unknown keys and writes a `.bak` before saving.
- **The original apps' data** is read in place, never written: older UPSC runs
  appear under Runs → From older apps.
- **The install folder holds no one's work.** It does hold two shared values:
  - the app's Google sign-in registration (no one's sign-in);
  - the team key.

  So share the installer only with the team.

## Adding a category (no code)

An exam category is a pack of data, added by whoever runs NoteCraft:

1. **Settings → Categories → Add category:** the name, subjects, languages, and
   which note types it offers.
2. **Write its rules** in plain sentences: one file for all subjects, plus any
   per subject (for example "Keep every date and article number").
3. **Check category:** it checks the ids, files and languages × types, and
   shows the requirements a run would get.
4. **Make notes for one sample lecture.** This is a paid run, so the page says
   so first. Compare, adjust the rules, repeat.
5. For a sheet, add it on Automation and pick this category on its profile.

The rules are added after the engine's own prompt. Without them, the prompt is
byte-identical to the original app's.

## Adding a note type (code)

Needed only for a new kind of output. Doc 07 and
`NoteStudio_Kit/note_types/README.md` describe it; in this code:

1. **The engine already makes it** (for example a new UPSC format): add an
   entry to `note_studio/note_types.json` with that `engine`, its
   `engine_format`, `options`, `inputs`, `outputs`, `languages` and `models`.
   Offer it in the packs that should have it. Then tests.
2. **A new engine:**
   - **Copy the app:** add a plan to `NoteStudio_Kit/tools/copy_engine.py`
     `PLANS`, run it, and never edit the original.
   - **`engines/<id>/engine_main.py`:** read `request.json`, configure the
     environment before importing the engine, reproduce the original app's own
     call, and print the events (`step`, `cost`, `warning`, `output`, `done`).
     Follow `engines/pw_books/engine_main.py`.
   - **The adapter, `note_studio/engines/<id>.py`:** `ENGINE`,
     `build_request(...)`, and `make_notes(...)` through `child.make_notes`.
   - **Register it:**
     - `make_service.CHILD_ENGINES`;
     - `engine_catalog.ENGINES`;
     - `models.RULES` (stages and fallback slots);
     - `requirements.APPLIED`, if the engine takes only some rule layers;
     - `paths.ORIGINAL_FOLDERS` (the original's `.venv` for project-folder
       runs, and the build's library copy);
     - `packaging/build_installer.py` `CHILD_ENGINES` and
       `packaging/smoke_engines.py` (one mock run);
     - `NoteStudio_Kit/tools/data_guard.py` `ENGINE_COPIES`, for any allowed
       edit to the copy.
3. **Tests:**
   - the engine copy's own suite, matching the original's;
   - a prompt-parity test against the original (see `tests/parity/`);
   - one mock end-to-end child run;
   - a New notes page test.
4. **For the user:** a `whats_new.TOURS` entry and the page guide
   (`note_studio/guides.py`).

## Releasing an update (QBG One's workflow)

1. `python packaging\build_installer.py --notes "what changed; and this"` from
   the project folder.
   - It stamps the version `YYYY.M.D.N` and builds into the release the folder
     already in `note_studio/static/release.json`. To change the folder, use
     `--publish <Drive folder link>`.
   - It copies the code and the libraries: NoteCraft's from this PC's Python,
     each engine's from its original's `.venv`.
   - It then refuses to build if anything fails:
     - every library folder must import everything its code uses;
     - no tests, runs, data, sign-ins or secret-shaped values in the package;
     - a clean start of the packaged copy, with 13 pages;
     - one mock run per engine.
   - It compiles with Inno Setup's `ISCC.exe`: Program Files, this project's
     `_build_cache`, or QBG One's build cache, used where it is. Pass `--iscc`
     to name one.
   - It writes `dist\team\<version>\`.
2. **Put `NoteCraft_Setup_<version>.exe` and `NoteCraft_Setup_<version>.txt`**
   (the notes) into the release folder:
   <https://drive.google.com/drive/folders/1vQXzi3kWd-k_LnVcvF-Izeq2tKEND0zO>.
   - `--publish` uploads them itself only when the folder is in a Shared drive
     with the service account as a member, or the person signed in to NoteCraft
     has edit access.
   - Otherwise drag the two files in by hand, with their names unchanged.
3. **Automatic updates stay on:** installed NoteCraft checks the folder at
   startup and every five minutes, using the team's service account (or the
   person's Google sign-in). This works with the browser closed.
   - A newer installer downloads in the background while notes and queue work
     continue. Progress appears beside Smart Queue.
   - After its size, SHA-256 and Windows executable header are checked, a
     pop-up offers **Install update** or a reminder in ten minutes.
   - Installation requires the person's click. The completed download is
     reused and checked again; active work must finish first, and the queue
     holds while NoteCraft closes, installs silently and opens again.
   - Settings → System → Updates shows the same, plus Check for updates.

The team key: Settings → System → Updates → Add team key. The build carries the
key added there; `--no-team-key` leaves it out. Share the release folder with
`qbg-studio-bot@sage-tribute-501712-e9.iam.gserviceaccount.com` as Viewer.

## Tests and gates

The 8 and 12 October 2026 syncs refresh Short Notes and UPSC from their current
originals, with old copies backed up under `_build/engine_sync_backups/`.
The latest copies include chemistry and notation repairs, current quality/cache
revisions, and the UPSC Drive/supplied-transcript fixes. Gujarati support and
NoteCraft's integration hooks are preserved.
For UPSC notes, More options now accepts the full text from Drive's Transcript
panel when it cannot be read automatically; the engine uses it without fetching
the recording or extracting audio.
`NoteStudio_Kit/tools/sync_engines.py <shortnotes|upsc>` repeats that code/assets
sync while preserving the listed integration hooks. Always recheck those hooks,
prompt/output parity and the data gates afterwards. Credentials and runs are
excluded. Short Notes keeps the original's budget-stop behavior: an exhausted
PW gateway allowance requires renewal by its administrator.

- `python -m pytest` here runs NoteCraft's tests. Each engine copy's own suite
  runs from `engines\<id>\` with that original's `.venv` Python.
- **Every phase or release ends with the gates:**
  - `python NoteStudio_Kit\tools\data_guard.py snapshot NoteStudio_Kit\after_<name>.json`
  - `python NoteStudio_Kit\tools\data_guard.py compare <before>.json <after>.json`
    must show nothing lost and no original touched.
  - `python NoteStudio_Kit\tools\data_guard.py drift` must show only the
    allowed edits to the copies.
- **The name:** it lives in `note_studio/brand.py`, and no template spells it
  out. The package and data folder keep the project name `note_studio`.
