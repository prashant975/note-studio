# Note Studio — start here

**Note Studio** is a **new app in its own folder**:
`C:\Users\Laptop\Downloads\Note_Studio\`. It **copies the code** of your note
apps into itself and gives you **one app** for everything:

- make notes of every type, for every category: NEET-JEE and Foundation
  hand-drawn notes, UPSC class notes from a recording, and later PW Books
  handwritten notes;
- start the Google Sheet automation;
- make UPSC Mains practice and DPP sets;
- check finished notes.

**Your original projects are never touched.** `shortnotes-core`, the UPSC app,
the handwritten app and the QC app keep working exactly as today; you retire
them whenever you like. New categories (CLAT, SSC, Banking, CUET...) are added
later **from Settings, with no new code**.

## How to start (2 steps)

The folder `C:\Users\Laptop\Downloads\Note_Studio\` already holds this kit
(`NoteStudio_Kit\`) and the standing rules (`CLAUDE.md`).

1. **Open Claude Code in `C:\Users\Laptop\Downloads\Note_Studio`.**
2. **Paste** everything below the line in `NoteStudio_Kit\PROMPT.md`.

Claude does **Phase 0 only**:

- reads everything;
- backs up your data;
- records the test numbers of the four originals;
- shows you two things to check;
- asks you four questions.

Then it waits.

For each later phase, open Claude Code in the same folder and say:

> Continue with Phase N of `NoteStudio_Kit/docs/06_PHASE_PLAN.md`. Estimate first, then build, then report and wait.

(If you ever need a fresh copy, unzip `NoteStudio_Kit.zip` into
`Note_Studio\`. **Not** into any of the original projects.)

## The plan

| Phases | What you get | Time |
|---|---|---|
| 0 | Backup, test baselines, your four decisions | ~1 h |
| 1-4 | Note Studio with the hand-drawn notes engine copied in: New notes, Automation, Runs, Smart Queue, Settings, **Add category** | ~5 days |
| 5-6 | UPSC copied in: DCN / simple / mains / prelims notes from a class recording, Mains practice, DPP | ~3.5 days |
| 7-8 (optional) | PW Books handwritten notes; Check notes (QC) | ~2.5 days |
| 9-10 | Team installer, Update button, tours; hand-over docs | ~2 days |

## What is inside the kit

| Path | What |
|---|---|
| `PROMPT.md` | The prompt to paste (Phase 0, plus the rules for every phase) |
| `CLAUDE.md.template` | The same rules, short; already copied to `Note_Studio\CLAUDE.md` |
| `docs\01_QBG_STUDIO_PATTERN.md` | How QBG Studio is built: the pattern to copy |
| `docs\02_NOTE_APP_TODAY.md` | How the hand-drawn notes app is built (the first original copied in) |
| `docs\03_OTHER_NOTE_PROJECTS.md` | All your note folders: which are copied in, which are old duplicates |
| `docs\04_TARGET_DESIGN.md` | **Note Studio:** folders, how the copies run, menu, pages, queue, settings, installer |
| `docs\05_DATA_TO_KEEP.md` | Every place your data lives, what is shared with the old app, what is new |
| `docs\06_PHASE_PLAN.md` | Phases 0-10 with time and checks |
| `docs\07_CATEGORIES_AND_TYPES.md` | How one app serves every exam: engines → note types → categories; adding a category with no code |
| `docs\08_MERGE_PLAN.md` | How UPSC, PW Books and QC are copied in: exact functions, tools, pitfalls |
| `note_types\`, `categories\` | The note-type registry and category pack examples (`_template`, `neet_jee`, `foundation`, `upsc`) |
| `reference\qbg_studio\` | QBG Studio files to adapt |
| `tools\data_guard.py` | Backup; proves after every phase that no data was lost, no original was edited, and each copied engine differs from its original only where allowed |

## Safety

- **Nothing in your original projects is edited, moved or deleted.** Their code
  is copied.
- **Your data stays where it is.** That covers `~/.shortnotes_bridge`, sheets,
  Drive, and the old runs and workbooks.
- **Note Studio's own data** goes in `~/.note_studio\`.
- **Phase 0 makes a backup zip** in `~/NoteStudio_backups\` (about 1.3 GB;
  `--light` skips the old runs). It contains your `.env` files and Google
  tokens: keep it on this computer and never share it.
- **Claude asks before** any build, install, commit, push, deploy or paid
  generation.
- **This kit** contains no keys, tokens, sheet ids or folder ids.
