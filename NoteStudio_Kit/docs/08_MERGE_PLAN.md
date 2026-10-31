# 08 — Copying every note project into Note Studio

Read doc 07 first: categories × note types, the requirement layers, and the
child-process engine contract. This doc gives the concrete copy recipe and the
facts, mapped read-only on 2026-10-04, for each original.

## The universal copy recipe (same for every engine)

1. **Copy, never move or edit the original.**
   - Copy the original's code, prompts, config, assets, fonts, bundled tools
     and tests into `Note_Studio\engines\<engine>\`.
   - Include files git does not track if the code imports them: copy the
     working tree, not `git HEAD`.
   - Leave out runs, outputs, `.env`, `.streamlit`, venvs, builds and caches.
   - Write `COPIED_FROM.md`: source path, date, file count, and the git commit
     plus `git status --short` if the original is a repo.
   - The original stays untouched and working: `compare` treats it as frozen.
     `drift` lists every difference between the copy and the original.
2. **No renaming.** A child engine runs in **its own process, with cwd and
   `sys.path` set to its own folder**. So its top-level `src`, `config` and
   `pw_access` stay exactly as they are, and imports are not rewritten.
   - The Studio process never imports a child engine. A test checks that no
     bare `src` / `config` is in the Studio's `sys.modules`.
   - Only the shortnotes copy is imported in-process. Its packages
     (`notesbot`, `sheets_bridge`, `api`, `pw_access`) do not clash with
     `note_studio`.
3. **One sign-in, passed down.**
   - Each copy keeps its own `pw_access.py` (it is in its own process).
     Whether each engine keeps its own proxy `APP_NAME` (separate cost lines
     and whitelist entries) or all use one is a **Phase 5 question for the
     user**.
   - Several originals refresh Google tokens through Streamlit (`pw_auth.py`
     login hooks, `st.secrets`) or keep their own pass files
     (`%LOCALAPPDATA%\PWConciseNotes\auth`,
     `%LOCALAPPDATA%\UPSC_Notes\proxy_session.json`). None of that is used.
   - The Studio signs in once and passes the child a fresh short-lived token
     **in the child's environment** (`extra_env`), never in `request.json`,
     which stays on disk in the run folder.
   - Where a copy cannot take the token as a plain argument (`google_token=`
     on the entry functions), the smallest change that reads it from the
     environment is an allowed change for that file. List it in
     `ENGINE_COPIES`.
4. **No Streamlit.** The copy's `app.py` is never run; it is copied only as
   reference for "what the app passes". A Streamlit fallback inside engine
   code (`st.secrets`) is never reached when `engine_main` passes the value
   explicitly.
5. **One entry module per engine:** `engine_main.py` (a new file) implements
   the JSON-lines contract in doc 07:
   - reads `request.json`;
   - sets the engine's config environment from it **before** importing the
     engine (not from the original's `.env`);
   - calls the engine's existing entry function;
   - turns its progress callbacks into `step` events;
   - writes outputs into the run folder;
   - ends with `done`.
6. **Stop.** Stop kills the child-process tree (QBG `stop_process`). None of
   these engines can cancel on its own, and with the child-process design they
   don't need to.
7. **Adapter in the Studio:** `note_studio/engines/<engine>.py` builds
   `request.json` from category × type × subject × language plus the options
   and resolved requirements (doc 07), then starts the child with
   `stream_command`. It turns the events into the run page, the cost and the
   history.
8. **Prompts stay byte-identical** (`drift` protects them). The Studio's
   requirement layers 2-7 go in as one extra block at the end. If an engine
   has no hook for extra instructions, add the smallest one possible in the
   copy (a parameter that appends text), listed in `ENGINE_COPIES`, with a
   test that the prompt without it is unchanged.
9. **Tests come along unchanged.** The copied `tests/` run from the copy's
   folder, with no import fixes needed.
   - They must reach the same numbers as the original's Phase 0 baseline,
     *including* its known failures. Don't fix those silently.
   - Streamlit AppTest UI tests are excluded and listed.
   - Add one end-to-end test per engine: a fake `request.json`, a stubbed
     model, and the expected events plus an output file.
10. **Old runs stay where they are.** Runs → "From older apps" lists the
    source project's `runs/` read-only, using its own run metadata files.
11. **Check dependencies in the installed runtime.** Every package the engine
    needs is added to the app's requirements and checked by
    `verify_runtime_imports` at build time. External programs (LibreOffice,
    Word, PowerPoint, ffmpeg, fonts) are checked by **Settings → Check
    setup**, which says in plain words what is missing and how to get it. A
    type whose program is missing shows "Needs LibreOffice" instead of
    failing mid-run.
12. **Tour and guide.** Each merged engine gets a what's-new tour and an
    updated page guide.

## Engine: `pw_books` (PW Books handwritten notes)

Source:
`C:\Users\Laptop\Downloads\handwritten_notes_automation_complete\handwritten_notes_automation_complete`
(v2.6.1, git HEAD `eee632a`).

| Item | Fact |
|---|---|
| Entry | `src/pipeline.py:95` `run_pipeline(input_path, *, subject, language, mode, google_token, model, ..., exam="", ..., notes_fallbacks, vision_fallbacks, qc_model, qc_level, routing_summary) -> PipelineResult`; batch `run_batch(...)` at `:518`. No Streamlit inside, except `src/pw_auth.py:419,473,491` (`st.secrets`), which is replaced by the Studio sign-in |
| Model routing | The UI runs `src/model_router.py`: `load_routing_config` → `check_models` → `recommend_mode` → `resolve` → `available_chain` (copy the order from `app.py:1321-1341`). The CLI skips it. **`engine_main` must do what the UI does**, using the model order from `request.json` |
| Inputs / outputs | .pdf / .pptx / .ppt → `<stem>_Concise_Notes.docx` + `.pdf` + run zip; `PipelineResult` has the paths, warnings and metadata |
| Requirements | 19 prompts in `prompts/`: `{biology,chemistry,physics,mathematics}_{complete,summary}_{en,hi}` + `jee_concise_en`, `neet_concise_en`, `jee_neet_concise_en`, chosen by `prompt_builder._candidate_names` (`:42`). Hard-coded rules in `prompt_builder.py:117-209`. Style in `src/style_config.py`: Kalam 16 pt, title #FF0000, body #000099, accent #E97132, green heading highlight, footer "Master NCERT with PW Books APP", logo `assets/pw_logo.png` |
| Progress | `retry_callback(str)` + `progress_callback` per file → `step` events. No cancel |
| Errors | A failed chunk becomes a placeholder + warning (→ `needs_attention`). All chunks failed → `GeminiError`. Others write `error.json` and re-raise (→ `failed`) |
| External needs | LibreOffice **or** Word (DOCX→PDF, `pdf_exporter.py:90-98`); PowerPoint or LibreOffice for .pptx (`extract_pptx.py`); **the Kalam font installed** (`docx_writer.py:845`); `pywin32`. Mathpix goes through the proxy |
| Models | notes and vision: `gemini-3.5-flash` → `3.6-flash` → `gemini-2.5-pro`; diagrams: `gemini-3.1-flash-image` → `gemini-3-pro-image` |
| Process globals | `ai_client._REQUEST_GATE` (6 concurrent), `model_router._cache`, `pw_auth` caches, `pw_access` token provider. All fine in a child process |
| Tests | 11 files, about 164 tests. Last cached run: 4 failures (3 `test_gemini_retry`, 1 `test_pw_kit_contract`) |
| ⚠ Before merging | Files git does not track are imported by the pipeline: `src/image_filter.py`, `src/style_config.py` (+ their tests); the modified files include the 3 exam prompts (13 Aug, current). **Ask the user to commit the source project first** (Claude does not commit for them), or at least copy the working-tree files, not `git HEAD` |
| Becomes | Type `pw_books_handwritten`. Offered by categories `neet_jee` (subjects physics/chemistry/biology/maths) and, if the user wants, `foundation`. Options: mode (complete / summary), language (en / hi), exam (jee / neet / jee_neet) |

## Engine: `notes_qc` (Check notes; a tool, not a note type)

Source: `C:\Users\Laptop\Downloads\upsc_file_QC`.

| Item | Fact |
|---|---|
| Entry | `src/pipeline.py:106` `run_pdf_qc_batch(pdf_paths, *, google_token, output_dir, runs_dir, strictness="strict", write_individual_reports=True, keep_run_files=True, progress_callback, stage_callback) -> QcRunResult`; per file `src/qc/engine.py:1286` `run_pdf_qc(...)`. Rebuild without AI: `rebuild_report.rebuild_run` |
| Output | `qc_report_<ts>.xlsx` + `individual_reports/` |
| Progress | `progress_callback(n, total, name, finished)`, plus `stage_callback(label)` (serial only) → `step` events. Errors are never raised: each file gets a status, error, stages and warnings |
| Requirements | Prompts in code: `src/qc_prompts.py` (`build_pdf_error_report_prompt:383`, lenses `:259`, `:336`), `qc/ai_review.py`, `qc/verify.py`, `qc/fact_sweep.py`. Rules: `config/upsc_qc_rules.yaml`, `config/upsc_qc_sources.yaml`. Framework: `reference_documents/notes_qc_framework.md`. DCN style constants: `config.py:243-264` |
| Stages | 0 text_recovery (AI, only when the text is scrambled) · 1 integrity · 2 layout · 3 page_analysis · 4 deterministic + spelling · 5 structure · 6 legacy_checks · 7 visual (off) · 8 ai_review · 9 error_report (+ fact_sweep and grounded_lookup **with web search**, skipped quietly when offline) |
| External needs | Office through `powershell.exe` COM for non-PDF inputs (`input_normalize.py:177`); `pyspellchecker`, `PyYAML`, `openpyxl` |
| Models | Every stage: `gemini-3.5-flash` → `3.6-flash`; when the Gemini budget is spent → `claude-haiku-4-5` → `claude-sonnet-4-6`; grounded lookup uses Claude with web search |
| ⚠ Decisions | (1) If grounded lookup fails, it calls Gemini **directly with `GEMINI_API_KEY`, bypassing the proxy** (`grounded_lookup.py`). In the Studio: proxy only, and drop the direct call? **Ask the user.** (2) `GeminiClient._gemini_budget_spent` is a class-wide flag: once set, every later QC in that process uses Claude until restart. A child process per run makes this harmless |
| Tests | 11 files, about 188 tests; run with `MAX_PARALLEL_PDFS=1` (PyMuPDF `find_tables` is not thread-safe). Last cached run: 7 failures (`test_pdf_qc` ×5, `test_regression` ×2) |
| Today it is UPSC-only | Rules and style constants are UPSC DCN. In the Studio, **Check notes** is offered on every finished run, but the QC rules become per-category requirement files (doc 07, layer 3). Categories without QC rules show "Check notes: basic checks only" (stages 1-6) until rules are written |

## Engine: `upsc` (UPSC notes, practice, DPP)

Source: `C:\Users\Laptop\Downloads\Upsc_Notes_2\upsc_Notes` (v2.12.3). Nothing
under `src/` imports Streamlit; only `app.py` (3236 lines) and the launchers
do. The engine is the top-level package `src` plus a top-level `pw_access`, so
it runs as a child process from its own folder, unchanged (recipe step 2).

### Notes: `upsc_dcn`, `upsc_simple`, `upsc_mains`, `upsc_prelims`

| Item | Fact |
|---|---|
| Entry | `src/pipeline.py:494` `run_pipeline(input_path, *, subject, language, mode, google_token, model, ..., video_url, ..., workflow="default", note_type="", area="auto", quality_level="strict", ..., stage_callback, upsc_output_format="dcn", note_depth="detailed", fact_mode="verify", dcn_options, template_path, logo_path, generate_error_report=True, error_report_strictness="standard", ..., lecture_no, dcn_no, lecture_title, ..., source_options, transcript_text_override, require_transcript=True, youtube_url) -> PipelineResult` (`src/models.py:124`). Batch: `run_batch` (`:2228`). Rebuild after an edit: `rebuild_outputs(run_dir, edited_notes_text, ...)` (`:142`) |
| ⚠ CLI ≠ app | `run_cli.py` does **not** pass `workflow="upsc"`, so it makes the legacy `base_*` notes. **`engine_main` must pass what the app passes:** `app.py:3059` (`workflow="upsc"`, `upsc_output_format=<format>`, `note_type="complete"`, `area="auto"`) and the defaults in `app.py:2729-2758` + `src/app_defaults.py`. Test this: the prompt file a DCN run writes (`prompt_dcn_part_NN.txt` / `prompt_upsc.txt`) must equal the one the old app writes for the same inputs (mock mode) |
| Formats | `UPSC_OUTPUT_FORMATS` (`config.py:295`): `dcn` (DCN prompt + DCN writer + visuals + page budget + render check), `simple`→concise, `mains`→mains_ready, `prelims`→prelims_ready (`build_upsc_prompt` + the legacy writer). `UPSC_NOTE_TYPES` (`:217`) also has `complete` and `revision`: offer them only if the user wants them |
| Options (the type's `options`) | depth `detailed/concise/revision` (DCN), fact mode `source_only/verify/mark_uncertain`, quality `off/basic/strict` (+13 toggles, under "More options"), error-report strictness `basic/standard/strict`, lecture no / DCN no / lecture title, optional reference DOCX template, logo (from the category's branding) |
| Languages | `en`, `hi`, `mr` (`src/languages.py:23`); required. Map them to the Studio's `english/hindi/marathi` |
| Subjects | polity, economy, geography, ancient, medieval, modern, culture, environment, science, ir, society, security, ethics (+ history, governance, current_affairs), each with EN/HI/MR labels. **These become the `upsc` category pack's subject list** (ids unchanged) |
| Requirements | `prompts/upsc_dcn_{en,hi,mr}.txt`, `upsc_{complete,concise,mains_ready,prelims_ready,revision}_en.txt` (format × language, falling back to `_en`); subject emphasis from `upsc_subjects.subject_focus`; a language lock added in code. Studio layers 2-7 go in as one appended block |
| Page budget | `src/note_budget.py`: 10-15 pages (hard 15); en 295 / hi 355 / mr 355 words per page; condense if more than 8% over; at most 2 compress passes. Becomes a `page_budget` switch the category can override |
| Progress | `stage_callback(label)` only (labels such as "Transcribing the lecture (n of m)", "Generating draft notes", "Creating DOCX/PDF", "Building error report"). Map them to steps the way `app.py:3003-3055` does. No cancel: Stop kills the child |
| Errors | Raises `PermissionError`, `ValueError`, `RuntimeError`, `GeminiError`, and `VideoSourceError` subclasses carrying `.user_message` (show that text). Writes `error.json` + zip first. Non-fatal problems go into `warnings`, `pdf_error`, `error_report_status` (→ `needs_attention`) |
| Inputs | Video: YouTube (captions via youtube-transcript-api; audio via yt-dlp), Google Drive (captions; service account or public), direct URL. Slides optional (.pdf / .pptx / .ppt). Transcript cache `RUNS_DIR/_transcript_cache/` |
| External needs | **ffmpeg + ffprobe** (bundled in `third_party/ffmpeg`, 194 MB); **LibreOffice or Word** for PDF (`pdf_exporter.py:112`); fonts **Coming Soon + Kalam** (embedded from `assets/fonts`, but installed fonts give a faithful PDF). Optional and heavy: faster-whisper, rapidocr |
| Models | `MODEL_NAME` `gpt-5.6-luna-ard-content` → `FALLBACK_MODEL_NAME` `gemini-3.5-flash`; image `gemini-2.5-flash-image`; speech-to-text `AUDIO_MODEL_NAME` `gemini-3.5-flash` → `gemini-3.6-flash`. Keep these as the type's default model order; the operator may reorder them in Settings |
| Config at import | `src/config.py` calls `load_dotenv` on import and reads everything once. `ROOT_DIR` comes from `HANDWRITTEN_NOTES_ROOT`; `RUNS_DIR` / `OUTPUT_DIR` are relative paths in `.env`. In the child: set `HANDWRITTEN_NOTES_ROOT` = the engine copy folder (prompts, assets), `HANDWRITTEN_NOTES_ENV` = a non-existent path (so the source `.env` is never loaded), and `RUNS_DIR` / `OUTPUT_DIR` / `MODEL_NAME`... from `request.json`, **before** importing the engine |
| Process globals | `pw_access._session`, `proxy_session._owner_email`, `table_sanitizer` mutating module globals (not thread-safe), a fixed temp logo name `%TEMP%/dcn_logo_WxH.png`, `lru_cache`d DPP masters and OCR/whisper models. All harmless in a child process. Two UPSC runs at once still share the temp logo name, so give each child its own `TEMP` |
| Per-user state | `%LOCALAPPDATA%\UPSC_Notes\` (`proxy_session.json`, `qc_cache/`, `reference_index/`): leave it. The copy uses its own folder under `~/.note_studio/engines/upsc/` |
| Tests | 76 files, about 1171 tests; run from the repo root. Last cached run: **42 failures**; record them in Phase 0. Tests that drive `app.py` through Streamlit AppTest (`test_app_runtime`, `*_panel*`, `*_flow_runtime`) are **not** ported. Their behaviour is covered by the Studio's own page tests |
| Old runs | `runs/`: 57 run folders + zips (225 MB) with `run_metadata.json` (paths, settings, task_id, api_calls). Listed read-only in Runs → From older apps. "Make practice from this run" (DMP / DPP) works on them too, reading in place and writing into the Studio's run folder |

### Practice: `upsc_mains_practice` (DMP)

- **Entry:** `src/dmp/service.py`.
  - `generate_questions(note_id, client, *, levels, runs_dir)` (`:349`): 3
    parallel calls giving 4 easy, 3 moderate and 3 applied questions.
  - `generate_answers(note_id, session_id, question_ids, client, *,
    regenerate, runs_dir)` (`:407`): one call per question, saved as it goes.
- **Export:** `src/dmp/export.py:251` `export(session, "questions"|"answers",
  output_dir, *, logo_path, only_answered, ...)` gives a DOCX plus an optional
  PDF.
- **Storage:** `runs/<run>/dmp/`. Errors are raised as `DmpError`.
- **In the Studio:** "Make Mains practice" sits on a finished UPSC run (and on
  an older-app UPSC run).
  - Step 1 makes the questions and shows them with tick boxes.
  - Step 2 writes answers for the ticked ones.
  - Step 3 exports.
  - Each step is a queue item on the same run.

### Practice: `dpp_from_notes` (DPP, no AI)

- **Entry:** the facade `src/dpp_builder.py`.
  - `note_document(note_id)`, `extract_uploaded_document(...)`.
  - `load_masters(verified_only=True)`: `lru_cache`d, about 23 s to parse the
    bank.
  - `coverage(note_id, taxonomy_rows)`.
- **`src/notes_to_dpp`:** `match_coverage`, `generate_selection(questions,
  rows, requested, ...)`, `manual_selection`, `replace_question`,
  `remove_question`, `export_practice_pdfs` (ReportLab) and
  `export_ids_csv` / `export_ids_text`. Errors are raised as `DppError`.
- **Needs two workbooks:**
  - `data/DPP and Infinite Practice Questions.xlsx` (55 MB);
  - `data/UPSC QBG Tags.xlsx`.
  - Overrides: `QUESTION_BANK_PATH` / `QBG_TAGS_PATH`.
  - Settings → Categories → UPSC → "DPP question bank" and "DPP tags" point at
    the existing files **in place** by default.
  - **Ask the user** how teammates get the 55 MB bank: in the installer, on a
    shared Drive, or each person picks it.
- **Languages:** the bank has en and hi only, so Marathi notes show "DPP:
  English or Hindi only".
- **In the Studio:** "Make DPP" on a finished run (any category whose pack
  names a bank).
  - Pick the counts (Easy / Medium / Hard).
  - Preview with replace and remove.
  - Export (ids CSV/TXT + question and solution PDFs).
- **The standalone `notes_to_dpp_taging` folders are older copies.** Use this
  one.

### Edit & rebuild (optional, UPSC first)

`rebuild_outputs(run_dir, edited_notes_text, ...)` remakes the DOCX/PDF from
edited notes text with no AI call. In the Studio this is "Edit notes text" on a
finished UPSC run: a text area, then Rebuild. Add it only if the user wants it.
