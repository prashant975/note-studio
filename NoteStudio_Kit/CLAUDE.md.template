# Note Studio — standing rules for every Claude session in this project

Note Studio is a **new project**. It copies the code of the existing note apps
into `engines\` and runs them all from one desktop app (QBG Studio
architecture) with a category × note type model, so new categories are added
from Settings without code. The plan and briefing are in `NoteStudio_Kit\`.
Read `NoteStudio_Kit\docs\06_PHASE_PLAN.md` for the phases and
`NoteStudio_Kit\docs\04_TARGET_DESIGN.md` + `07_CATEGORIES_AND_TYPES.md` for the
design, then check the latest `HANDOFF.md` section (once it exists) to see
which phase is done.

- **Never edit the originals.** They are:
  - `Downloads\shortnotes-core`
  - `Downloads\Upsc_Notes_2\upsc_Notes`
  - `Downloads\handwritten_notes_automation_complete\...`
  - `Downloads\upsc_file_QC`

  Copy them into `engines\<name>\` (working tree, with a `COPIED_FROM.md`).
  They keep working as they are.
- **Copies stay as they are.**
  - Only the files in `ENGINE_COPIES` in `NoteStudio_Kit\tools\data_guard.py`
    may differ from the originals; prompts and rules never.
  - Never change cache keys, output names, sheet row states or columns,
    Drive file properties, or the shortnotes `PublicJob` / manifest shapes.
  - `engines\shortnotes` runs in process. The other engines run as child
    processes from their own folder, unchanged, through `engine_main.py`.
- **Data.**
  - Note Studio's own data lives only in `~/.note_studio\`.
  - The originals' data is read in place.
  - `~/.shortnotes_bridge\` is shared with the old exe only if the user agreed
    in Phase 0. Then preserve unknown keys and write a `.bak` before saving.
- **Gates.** Every phase ends with `data_guard.py snapshot` + `compare` +
  `drift`. A STOP result stops the work.
- **Categories are data, not code.** A new exam category is a pack added from
  Settings → Categories. Code is needed only for a new kind of output.
- **Every step from the app.** Operators are content staff: no terminal, no
  `.env` edits, no pasted keys. Use plain words in the UI.
- **Estimate first, one phase at a time.** Report the tests (against the
  Phase 0 baselines) and the guard results, then wait for the user's go.
- **No build, install, commit, push, deploy or paid generation** unless the
  user says so in the current conversation.
- **Releases.** Every release with a user-facing feature adds a
  `whats_new.TOURS` entry and updates the page guide.
- **File safety.**
  - Glob before creating a file; grep for a URL before adding a route.
  - Use Write/Edit, not heredocs.
  - Copy with `shutil.copy2`.
  - Never kill a process you did not start.
