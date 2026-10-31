# 03 — The other note projects, and what each becomes

Mapped 2026-10-04 (read-only; nothing was run). None of these folders is a copy
of `shortnotes-core`: they are separate codebases that share only the PW access
helper (`pw_access.py`). All paths are under `C:\Users\Laptop\Downloads\`.

## Summary

| Project | What it makes | In the common app | Why |
|---|---|---|---|
| `shortnotes-core` | Lecture PDF -> hand-drawn concise theory notes PDF; sheet automation; desktop exe | **The base.** Type `concise_handdrawn`; categories `neet_jee`, `foundation` (phases 1-4) | Most complete: tests, automation, Drive filing, cost, caching |
| `handwritten_notes_automation_complete\handwritten_notes_automation_complete` | Lecture PPT/PDF -> PW Books "handwritten" DOCX + PDF (Kalam font, slide images) | **Merged**: engine `pw_books`, type `pw_books_handwritten` (phase 7) | Same audience, different layout and output format |
| `Upsc_Notes_2\upsc_Notes` (v2.12.3, newest) | Class recording link + slides -> UPSC DCN typed notes DOCX/PDF + error report; DMP tab; DPP tab | **Merged**: engine `upsc`, category `upsc`, types `upsc_dcn`, `upsc_simple`, `upsc_mains`, `upsc_prelims`, `upsc_mains_practice`, `dpp_from_notes` (phases 5-6) | Different exam, video input, layout and QC |
| `upsc_file_QC` | Any finished notes file -> `qc_report.xlsx` (10 checks, verification) | **Merged**: engine `notes_qc`, the "Check notes" tool for every category (phase 8) | Not a note type: checks the output of every type |
| `upsc_notes\upsc_Notes` | Older UPSC notes | Not ported | Ancestor of `Upsc_Notes_2` |
| `Upsc_Notes_2\upsc_pdf_qc` | UPSC PDF QC | Not ported | Superseded by `upsc_file_QC` |
| `notes_to_dpp_taging` (both copies, identical) | Notes -> DPP question set | Not ported separately | The newer copy lives inside `Upsc_Notes_2\upsc_Notes\src\notes_to_dpp` |
| `upsc_exmple` | One sample DCN PDF | Reference file only | No code |

## Details per project to port

### PW Books handwritten notes (`handwritten_notes_automation_complete`)
- **Tech:** Streamlit `app.py`, CLI `run_cli.py`, PyInstaller `dist/HandwrittenNotesAppTeam`.
  Gemini through the PW proxy (`gemini-3.5-flash` text, `gemini-3.1-flash-image`
  diagram redraw); Mathpix for maths OCR.
- **Its requirements (copy unchanged):**
  - `prompts/`: 19 files by subject × mode × language, such as
    `physics_complete_en.txt` and `jee_neet_concise_en.txt`.
  - `src/style_config.py`: Kalam font, red titles, #000099 body text, orange
    accent, green heading highlight, logo watermark, footer.
  - Slide images: `docx_writer.py`, `dtp_parser.py` (yellow "Note to DTP"
    markers) and `image_filter.py`.
  - Checks: `quality_checker.py` and `equation_quality*.py`.
- **Output path:** DOCX first, then PDF through Word/LibreOffice. The Note
  Studio installer cannot carry LibreOffice or Word, so Check setup looks for
  them and the type shows "Needs LibreOffice or Word" until one is found.
- **Data:** `runs/`, `outputs/`, `logs/`, `.env`, `.streamlit/secrets.toml`.
  These stay in that folder; History can list the old runs read-only.
- **State:** v2.6.1, 181 tests, last commit 2026-08-11, with some prompt edits
  not yet committed. Ask the owner which prompt version is current before
  copying.

### UPSC notes (`Upsc_Notes_2\upsc_Notes`)
- **Tech:** Streamlit `app.py` (151 KB, three tabs), CLI, exe `dist/UpscNotesApp`.
  - Text: GPT `gpt-5.6-luna-ard-content`, falling back to Gemini.
  - Images and speech-to-text: Gemini; `faster-whisper` is optional.
  - All calls go through the PW proxy.
- **Its requirements:**
  - Notes prompts: `prompts/upsc_dcn_{en,hi,mr}.txt`, plus the legacy
    `upsc_complete`, `concise`, `mains_ready`, `prelims_ready` and `revision`.
  - Note types and the four formats (dcn, simple, mains, prelims):
    `src/config.py:217-309`.
  - Layout: `src/upsc_dcn_docx_writer.py`.
  - Mains practice prompts: `src/dmp/prompts.py`.
  - Fact checks: `src/qc/*`, with rules in `config/upsc_qc_rules.yaml` and
    `config/upsc_qc_sources.yaml`.
  - Video input: `src/video_sources/` and `src/transcription/`.
  - Page budgets: `src/note_budget.py`.
- **Data:**
  - Runs: 114 in `runs/`; transcript cache in `runs/_transcript_cache/`;
    output in `outputs/`.
  - Workbooks: `data/UPSC QBG Tags.xlsx` and
    `data/DPP and Infinite Practice Questions.xlsx`.
  - Secrets and settings: `.env`, `.streamlit/secrets.toml`.
  - All of this stays where it is.
- **State:** v2.12.3, 82 tests, last commit 2026-09-19. It is the most active
  of the projects. `src/qbg_bridge/` is not wired into its app; leave it out.

### Notes QC (`upsc_file_QC`)
- **Input and output:** takes PDF, DOCX, PPTX, XLSX, images or text, and
  returns `qc_report_<date>.xlsx`.
- **Checks:** ten checks, plus:
  - `fact_sweep.py`
  - `spelling_check.py`
  - `verify.py` (asks a model to disprove each error it found)
  - `grounded_lookup.py` (web lookups)
- **Models:** Gemini first, then Claude once the Gemini budget is spent, all
  through the proxy (proxy name "upsc_file_QC").
- **History:** 57 QC runs and 56 reports kept in its folder.
- **In Note Studio:** a "Check notes" tool, offered both on any finished run
  (the "Check these notes" button) and as its own page.

## Porting rules (short version; the full recipe and facts are in `08_MERGE_PLAN.md`)

1. **One engine per phase** (phases 5-8 in `06_PHASE_PLAN.md`), each with the
   user's go-ahead, after the Studio core (phases 1-4) is done. Engines run as
   child processes (doc 07), because every project has its own top-level
   `src` / `pw_access` and loads its `.env` at import.
2. **Copy the engine; never move or edit the source project.** It keeps working
   as it is until the user retires it.
   - Copy the modules into `Note_Studio/engines/<name>/`, keep their prompt
     files byte-identical, and write a `NoteEngine` adapter
     (`note_types/README.md`).
   - Streamlit UI code is not ported. Its form fields become the type's
     `options` in `note_types.json`.
3. **Bring their tests.** A port is done when the project's own tests reach
   their Phase 0 numbers inside `Note_Studio/engines/<name>/`, plus one
   end-to-end child-process test through the queue.
4. **Old runs are read, not moved.** History gets a "Runs from older apps" list
   that reads the old `runs/` folders. Nothing is copied or rewritten.
5. **Secrets.** Read each project's `.env` and `secrets.toml` key *names* only.
   The Studio uses its own sign-in and its own model access (the PW proxy
   through `pw_access.py`, already shared by all the projects).
6. **Dependencies are checked in the INSTALLED runtime, not only the dev Python.**
   Heavy ones (ffmpeg, `faster-whisper`, LibreOffice/Word, RapidOCR) are each a
   decision for the user: carry them in the installer, or have Check setup
   guide the user, with the type showing "Needs ..." until the item is
   present.
