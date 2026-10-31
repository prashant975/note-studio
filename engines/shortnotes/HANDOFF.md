# Shortnotes — Core Handoff

Studygram-style short-notes generator: PDF lecture deck in → hand-drawn-style
notes PDF out. Runs as a Vercel Python function with a web UI (and an optional
Telegram bot handler). This zip is the complete core — everything that decides
*how notes are made* lives here.

## Alloy percentages and temperature notation — 9 October 2026

The reported mixtures notes printed `80%text Cu`, `textFe`, `25 degC`
and `ge 25text circtextC`. Offline reproduction confirmed three causes:
math normalization unescaped percent signs (mathtext treats bare `%` as a
comment), mathtext rejected the TeX aliases `\ge`/`\le` and empty text
wrappers, and the prose sanitizer replaced a supported degree glyph with
`deg`. The fallback then stripped braces/backslashes while leaving command
names visible.

- `math_render.py` preserves escaped percentages, normalizes comparisons and
  empty wrappers, and keeps TeX text-range dashes readable. `notes_pdf.py`
  retains the real degree glyph and unwraps text commands in its fallback.
  Long alloy compositions wrap at top-level commas without splitting
  parenthesized arguments or braced text.
- The additional local notation check inspects every actual PDF page before
  delivery and rejects these command/temperature artifacts. It applies to
  desktop, manual uploads and cached re-renders even with optional model QC
  disabled; it adds no model call. Final review also receives the findings
  and explicit percentage, degree and comparison instructions.
- Quality receipts use `concise-theory-quality-v3-notation`; generation cache
  keys use `concise-theory-v12-percent-temperature-notation`.
- Validation: 164 final rendering checks, 41 final quality/budget/launch
  checks, 579 broader integration checks and 92 web checks passed. Web build
  and the packaged self-test passed. The six modified application modules
  inside the executable match the current source bytecode.
- `dist/ShortNotesStudio.exe` now matches the verified build at
  `dist/build-2026-10-09-notation/ShortNotesStudio.exe`. This rebuild reused
  the verified dependency inventory through the existing cached build spec;
  all changed application and launcher sources were recompiled. The previous
  executable is preserved at `dist/ShortNotesStudio.old-2026-10-09-notation.exe`.
- Visual QA used Poppler to inspect both normal and forced-fallback samples
  at `output/pdf/notation-quality/mixtures-notation-preview.pdf` and
  `mixtures-notation-fallback-preview.pdf`. These reproduce the screenshot
  cases; they are not a regeneration of the original 11-page lecture notes.
  Existing PDFs need regeneration or a re-render from their stored notes.
  Build evidence is in `output/pdf/notation-quality/desktop-build.json` and
  the `tmp/notation-*.log` files. No paid generation or Drive/sheet writes
  were performed for this code repair.

## Double-click opens the running desktop app — 8 October 2026

After the budget fix, the app had been restarted hidden with `--no-open`.
Double-clicking another copy hit port 3000's existing server and exited with
a port-conflict error before opening the browser. The server itself was
healthy; this was a launch/handoff problem.

- `sheets_bridge/studio_instance.py` recognizes the existing Studio by both
  its health response and its exact page title, with bounded local requests.
  `serve_studio` opens that instance and returns success before sign-in or
  creating another server. The bind-conflict path also handles simultaneous
  launches. Other services on the port are still refused.
- Validation: 279 launch/bridge/budget tests passed; packaged self-test passed;
  a real no-argument EXE launch returned success and kept the same server.
- Updated `dist/ShortNotesStudio.exe` from
  `dist/build-2026-10-08-launch/ShortNotesStudio.exe`. The normal full build
  spec collects the new module; `ShortNotesStudio-launch-rebuild.spec` reused
  this machine's verified dependency inventory for the immediate rebuild.
  The previous budget-fix exe is preserved as
  `dist/ShortNotesStudio.old-2026-10-08-launch.exe`.
- The UI was opened in the browser. The gateway budget remains an external
  blocker for the 26 pending science chapters; no generation was attempted
  during this launch repair.

## Exhausted PW gateway budget stops regeneration — 8 October 2026

The 26 science chapters marked Regenerate on spreadsheet
`1uO2CKhioaEaaLLEoC5w2_tVhh2dt8SNo9DUTgmyspc0`, gid `1561509839`,
all failed with `VisualFidelityError` / `generate:link_invalid`. A live probe
identified HTTP 422 with the gateway's `budget_exceeded` category. The model
allowance is exhausted; this is not a source-PDF or diagram error. Only the
gateway administrator can restore the allowance. Do not switch credentials
or models to work around the budget.

- `ModelBudgetError` carries a fixed, safe explanation and the code
  `model_budget_exceeded`; no gateway body or credential information is logged.
- Gemini transport recognizes this permanent failure. Generation, page-group
  retries/splitting, language retries, source checks and final quality checks
  preserve it. The runner stops the current batch and later tabs, preserving
  Regenerate on the affected row and leaving other chapter rows pending.
- Tests: 368 relevant checks passed across the bridge, budget regressions,
  source coverage, final quality, generation policy and GPT transport. An
  additional 322 rendering/schema/science checks passed; the existing
  machine-dependent `EXPECTED_FIDELITY_GLYPH_HASHES` test still fails, as
  documented in earlier handoffs. No new lint findings versus the backup.
- Live rerun: `20261008-032447-b83aed`, Foundation / Science, 26 selected,
  0 generated, first row blocked, 25 stopped, INR 0.00. All 26 chapter rows
  still say Regenerate. Row 61 is a class heading without a source, excluded
  by the existing selection rules. Evidence is in
  `output/science-regenerate-2026-10-08/result.json` and `rerun.log`.
- Desktop: rebuilt `dist/ShortNotesStudio.exe` from the verified
  `dist/build-2026-10-08/ShortNotesStudio.exe`; packaged self-test passed and
  archive inspection confirms the new exception/classifier are included.
  The finished old desktop process was replaced and restarted hidden at
  `http://localhost:3000`; its authenticated automation status is idle.
  Original exe: `dist/ShortNotesStudio.old-2026-10-08-budget.exe`.
  Build evidence: `output/science-regenerate-2026-10-08/desktop-build.json`.

After the administrator restores the gateway budget, resume with:

```powershell
python -m sheets_bridge --spreadsheet-id 1uO2CKhioaEaaLLEoC5w2_tVhh2dt8SNo9DUTgmyspc0 --gid 1561509839 --category foundation --subject science --no-browser
```

## Giant slide pages no longer fail with crop_failed — 3 October 2026

A Physics lecture (Arjuna NEET 2027, Circular Motion, 26 slides) failed on the
Studio's Upload page with `generate:crop_failed` twice, in about a second and
at no AI cost. Its slides are **7200 x 3600 pt** (100 x 50 inches; an ordinary
slide is about 960 x 540). Every page is measured for a raster at the fixed
2.2x render scale before any model call: 15840 x 7920 px here, over
`MAX_VISUAL_PAGE_DIMENSION_PX` (5000) and `MAX_VISUAL_PAGE_PIXELS` (25 MP), so
`pdf_utils._page_geometry` rejected the whole lecture. This is a different
cause from the 30 September PDFium threading failure, which shows the same
code on every row.

- **Fix:** `pdf_utils.page_render_scale(width, height)` gives the configured
  scale for an ordinary page and, for an oversized one, the largest scale that
  fits both limits (this deck: 5000 x 2500 px). The three places that measure
  or rasterise a page take their scale from it, so the recorded geometry and
  the rendered image agree: `pdf_utils._page_geometry`,
  `visual_inventory._rendered_pages`, `snapshot._original_geometry` /
  `extract_source_crops` (and the legacy `extract_snapshots`). Ordinary pages
  are unchanged; the limits themselves are unchanged.
- Any new code that renders a source page must use `page_render_scale`, not
  `config.VISUAL_PAGE_RENDER_SCALE` directly.
- Tests: `tests/test_oversized_pages.py`; the old "giant mediabox fails" test
  in `tests/test_visual_manifest.py` now asserts the page is fitted to the
  caps without a preflight raster.
- The lecture itself: generated through the Upload path with the failed job's
  settings (NEET-JEE, "Saleem Ahmad Sir", Gemini 3.7 Flash): 5 sections,
  4 pages, 1 model call, INR 5.60. The sheet automation's page groups for the
  same PDF also split cleanly (checked offline).
- Full suite: 1603 passed, 1 failed (the known machine-dependent glyph-hash
  test). No `web/` change, so `public/` was not rebuilt.
- Desktop build: `dist/ShortNotesStudio.exe` (3 October 2026, 18:49, self-test
  passes, same file as `dist/build-2026-10-03/ShortNotesStudio.exe`); the
  1 October build is kept as `dist/ShortNotesStudio.old-2026-10-03-1850.exe`.

## Catalog faculty columns; the slides' teacher wins — 1 October 2026

The content team's catalog (`1kb3bAMX1TfI4entO976WW4aIRGJ9zLTzQR5_3Rx3ziM`)
gained `faculty_id`, `faculty_name`, `faculty_email` after `subject`, moving
chapter/date/topic/link right (now G/H/I/J, our columns K:M and O, the BM
team's upload column N). Columns are found by header, so the layout resolved
unchanged; what was wrong is that catalog rows never read a faculty column -
only "Subject By Name" - so this sheet's notes were signed "by PW Faculty"
unless the slides named the teacher.

- **Signature order** (`sheets_bridge/mapping.choose_signature`, both sheet
  types): Studio/profile signature used as is (`generation_profile.fixed_signature`;
  the cache key carries `signature:fixed`) > `faculty_name` / Lecture Faculty
  > subject-cell teacher. The teacher the notes model read on the slides then
  replaces the sheet's name when it is a different person; the same person
  spelt differently (titles ignored, Hindi and English compared by consonant
  sound) keeps the sheet's spelling. Brand words (PW, Physics Wallah) never
  count as a name. The row log prints `signature: '<name>' - <why>`.
- `faculty_id` / `faculty_email` are never taken for the name (`columns._fuzzy`).
- Offline plan of the sheet's 279 lecture rows: 269 signed from the sheet,
  10 with no name anywhere on the sheet (slides' teacher, else PW Faculty).
  The 146 rows already filed keep their PDFs; mark Review Status
  **Regenerate** to remake one (a paid generation).
- Studio text now reads "signature from the faculty column (the slides'
  teacher if different)"; `public/` rebuilt.
- Tests: `test_the_faculty_name_column_signs_the_notes_and_the_shifted_columns_are_read`,
  `test_a_signature_typed_in_the_studio_is_fixed_for_the_whole_run`
  (`tests/test_catalog_sheet.py`); `test_the_slides_teacher_wins_when_the_sheet_names_someone_else`
  and the `same_teacher` table (`tests/test_sheets_bridge.py`). Full suite:
  1595 passed, 1 failed (the known glyph-hash test); web tests 92/92.
- Desktop build: `dist/ShortNotesStudio.exe` (1 October 2026, 14:57, self-test
  passes); the morning's build is kept as `dist/ShortNotesStudio.old-2026-10-01-1457.exe`.

## Faculty name from the lecture deck — 1 October 2026

A NEET-JEE run shipped notes with the generic "by PW Faculty" because the
teacher's name was not on the literal first page of the lecture PPT's PDF.
The extraction rule now reads the whole deck: the first chunk looks at the
front page, then intro slides, slide footers and the caption beside the
teacher's photo; later chunks may also copy a name a slide in their part
prints (the merge keeps the earliest part's name). A printed name only —
guessing stays forbidden. (Superseded the same day: the slides' teacher now
replaces a different sheet name - see the section above.) Changed: `_METADATA_FIRST_BLOCK` /
`_METADATA_LATER_BLOCK` and the `faculty` schema description in
`notesbot/prompts.py`, the lean description in `notesbot/gemini_client.py`,
`docs/PROMPTS-DUMP.md`; test
`test_faculty_rule_reads_the_whole_deck_not_only_the_front_page` in
`tests/test_generation_options.py`. Full suite: 1581 passed, 1 failed (the
known machine-dependent glyph-hash test). Desktop build:
`dist/ShortNotesStudio.exe` (1 October 2026, self-test passes); the replaced
30 September build is kept as `dist/ShortNotesStudio.old-2026-10-01-1108.exe`.

## Catalog sheets and the notes Drive folder — 28 September 2026

The automation now also runs the content team's **catalog sheet** (`exam,
batch, subject, chapter, Lecture_Date, lecture_topic, downloadable_link`: one
row per lecture, a direct static.pw.live PDF link, no status columns). Its
notes are filed in a Drive folder named on the profile:
`<exam category>/<batch>/<subject>/<chapter>/<lecture title> Concise Notes.pdf`.
The sheet gets `Auto Status`, `Review Status`, **`Concise Notes Link`**
(the filed PDF's Drive link) and **`Lecture Folder Link`** (the chapter folder
it was filed in; added 30 September 2026) after its own columns. Full rules: the
"Catalog sheets" section of `sheets_bridge/README.md`.

- **Adding one.** The Studio's **+ Add sheet** has a *Notes Drive folder*
  field; a catalog tab is refused without it. `sheets.json` key:
  `outputFolder` (a folder link or id), optional `skipBatches`. CLI:
  `--output-folder LINK`. The test sheet ("Copy of Class_Notes") is registered
  as **Class Notes Catalog (Concise Notes)**, output folder "notes "
  (`1T6H3Iir9amxkTkYpD6jVHRCiDXpu3doD`), in both the repo `sheets.json` and
  `~/.shortnotes_bridge/sheets.json` (the desktop app's list).
- **Paying once.** Filed PDFs carry Drive properties `shortnotesSource` /
  `shortnotesRender`. A row already filed is adopted at no cost; the same PDF
  under another batch is copied, within a run or from an earlier run; a PDF
  that failed is not retried on its repeats in the same run. On the test sheet
  (785 rows) the dry run plans 655 generations, 106 copies and 24 skips.
- **Rows that are not lectures** (batches containing Pi Lens, UXCC, VP LIVE,
  Smart DPP) are marked `skipped: not_a_lecture`.
- **Languages.** Hindi batches -> Hindi notes; Gujarati, Telugu, Tinglish,
  Tamil, Kannada, Bangla batches -> English notes (the PDF font draws Latin
  and Devanagari only).
- Code: `sheets_bridge/catalog.py` (names, numbers, categories),
  `catalog_flow.py` (per row), `destination.py` (folder tree),
  `http_source.py` (web PDF download); `columns.Layout.kind` tells the two
  sheet shapes apart. Batch sheets behave exactly as before.
- Tests: `tests/test_catalog_sheet.py` (33); the 237 bridge tests and 91 web
  tests still pass.
- Live test on the test sheet (rows 9, 14, 62, 104): two lectures generated
  (INR 12.31), the repeat of row 62 in another batch copied at no AI cost,
  the Pi Lens row skipped; H:J written and the tree created as planned.
  Full suite: 1558 passed, 1 failed (the known machine-dependent glyph-hash test).
- Desktop build: `dist/ShortNotesStudio.exe` (same as
  `dist/catalog/ShortNotesStudio.exe`); the previous build is kept as
  `dist/ShortNotesStudio.old-2026-09-28-0140.exe`. Its self-test passes.

## Categories, subjects and format rules — 25 September 2026

The Studio makes notes per exam category (NEET-JEE / Foundation; NEET and JEE
were merged into one NEET-JEE choice on 30 September 2026). Foundation
also has a Subject dropdown (Science, Maths, Physics, Chemistry, Biology, SST,
English, Hindi, Sanskrit) on both the Automation and the Upload page. On the
automation page, "From sheet" keeps each row's column D. NEET-JEE always
uses column D.

What differs per category and subject lives in `notesbot/format_rules/`: plain
command files per `<category>/<subject>.txt` (plus `<category>/all-subjects.txt`)
and `rules.json` switches. See its README. Loaded by `notesbot/categories.py`,
cached until a file changes. The category and subject travel as context
variables (`generation_profile.exam_category` / `exam_subject`).

- **Foundation Maths keeps solved examples** (`solved_examples: true`). The
  concise policy becomes `SOLVED_EXAMPLES_POLICY`, the schema keeps
  `worked_examples`, the Q&A guard accepts them, and the reviews use
  `SOLVED_EXAMPLES_REVIEW_NOTE`. Every other pair stays theory-only.
- **Foundation Sanskrit:** lesson verses only, one शब्दार्थ table, no mind map.
- **Sanskrit lectures default to श्लोक + हिन्दी अर्थ notes;** strict Sanskrit-only is
  an explicit choice.
- **Default model is Gemini 3.7 Flash** again. GPT is in the AI model dropdown.

The rule files ship in the .exe (spec `datas`) and on Vercel (`includeFiles`).
The cache key carries `categories.fingerprint(category)`.

## GPT latency fix — 22 September 2026

The 22 September run spent 23m33s generating the second PDF's pages 9–15,
including a failed attempt and retry. The GPT adapter accepted the generator's
180-second request timeout but did not pass it to `pw_access.gpt_generate`,
which used 300 seconds. Forward that timeout through the kit helper to HTTP,
preserving fractional remaining deadlines and per-call overrides. Timeout and
connection failures now get one internal retry; rate-limit backoff retains its
existing bound. The outer page-group recovery and quality checks remain enabled.
GPT calls now log start, completion duration, and safe failure type/HTTP status
in the row log, including failed calls without a returned cost.

Updated desktop executable: `dist/latency-fix/ShortNotesStudio.exe`. Source
backups are in `tmp/latency-fix-backup-20260922/`. The request timeout applies
to the GPT generation HTTP call, not a hard wall-clock deadline for an entire
lecture or its uploads/authentication. No paid production rerun was performed.
Validation: 485 targeted Python tests passed across GPT transport, generation
options/policy, Sheets automation, source coverage, duplicate removal, and final
quality checks.

## Automation settings — 21 September 2026

The Studio's **Open automation** button opens `/automation` in a new tab and
passes the selected sheet(s); it never dispatches a batch. Choose settings there,
then click **Run automation now**. **Include page numbers** starts checked;
unchecking it applies to every generated PDF in that run. The option is passed
through RunOptions, generation and rendering, and separates PDF cache entries.
CLI automation accepts `--no-page-numbers`. Sanskrit labels and strict language
validation remain supported with either setting.

The updated executable is `dist/page-options/ShortNotesStudio.exe`; the older
`dist/ShortNotesStudio.exe` does not contain these changes. Close a running copy
before launching the updated executable. Targeted verification: 368 Python tests,
87 web tests, plus a six-page Sanskrit PDF rendered without page-number labels.

## Current user preference — 14 September 2026

Generate **concise theory notes** without questions, MCQs, answers, worked
examples or solution/proof/derivation steps. Extract unique general teaching
from those blocks into theory; retain formulas, conditions and actual scientific
processes. Do not restore excluded Q&A as a coverage repair. The shared policy
and explicit-content guards live in `notesbot/concise_policy.py`; coverage and
final PDF quality checks apply the same selection rules. Revision maps are
omitted after recovering any unique legacy map content into the main theory.

Saved PDFs use ` - Concise Notes.pdf` through `notesbot/output_naming.py`.
Older saved output suffixes are replaced instead of stacked. New cache keys use
`concise-theory-only-v6`; approved PDFs require `concise-theory-quality-v2`.
Legacy worked-example parsing and low-level rendering remain available for
older files, but new generation constrains the list to empty and the automation
refuses to finalize explicit Q&A.

The user's biology PDF correction and validation records are under
`output/pdf/concise-notes-2026-09-14/`. The deliverable is also copied alongside
the original in Downloads. Use `dist/ShortNotesStudio.exe` (rebuilt 19 September 2026: stricter
source-only prompt rules against facts the lecture does not contain; 18 September:
page-1 subject, chapter and lecture number from sheet columns D/G/H; earlier: GPT route,
coverage/quality fixes, `gpt-5.6-terra-ard-content` default). The 17 September
build is kept in `tmp/exe-backup-20260918/`. The 14 September build stays
at `dist/concise/ShortNotesStudio.exe` as a fallback. Close the running app
before launching a new build — Windows locks the executable file.

## The core pipeline (what makes shortnotes)

```
PDF upload/link
  → notesbot/pdf_utils.py      split_pdf_chunks(): ~9 MB size-based chunks (cap 16)
  → notesbot/prompts.py        build_prompt(): the master prompt (see below)
  → notesbot/gemini_client.py  _generate_sequence_chunk(): one structured-output
                               LLM call per chunk (2 parallel workers, bounded
                               retries for 429/timeout/invalid responses)
  → notesbot/latex_repair.py   deterministic repair of JSON-mangled LaTeX
  → notesbot/math_render.py    $...$ spans → matplotlib mathtext PNGs
  → notesbot/notes_pdf.py      fpdf2 renderer: handwritten-style pages, tables,
                               flows, graphs, mind map, doodles, watermarks
```

- `notesbot/web_service.py` — job orchestration, session auth, Vercel Blob
  storage, cost blob sidecar, redacted telemetry.
- `notesbot/handler.py` — Telegram entrypoint (optional).
- `notesbot/config.py` — every knob, env-overridable. Reads `.env` locally.
- `web/` (source) → built to `public/` with `npm run build` (esbuild).
- `api/` — JS endpoints (blob presign, cleanup cron).

## Prompts (the heart — "prompt and style" files)

| What | Where |
|---|---|
| Master notes prompt + structured-output schema (pydantic) | `notesbot/prompts.py` |
| Prompt assembly per chunk (first vs later chunk metadata gating) | `build_prompt(..., first_chunk=)` |
| Image house style (colored-pencil sketch look) | `VISUAL_STYLE` in `notesbot/prompts.py` |
| Retry/salvage + schema pruning around the LLM call | `notesbot/gemini_client.py` (`_generate_pdf_schema`, `_generation_schema_json`) |
| QC / design passes (off by default) | `_QC_PROMPT`, `_DESIGN_PROMPT` in `gemini_client.py` |
| Rendered dump of all of the above (regenerate after edits) | `docs/PROMPTS-DUMP.md` |

Prompt cost note: the prompt + schema (~5.7k tokens) is resent with EVERY chunk
call (LLM API is stateless). The prompt text is deliberately ordered
[prompt, PDF] with variable parts at the END so Gemini implicit context caching
can kick in across chunk calls. Don't casually reorder it.

## Setup

```bash
npm ci                         # JS deps
pip install -r requirements.txt  # or use a venv; Python 3.12
cp .env.example .env           # then fill values (see below)
npm run build                  # web/ → public/
```

Env you must provide (in `.env` locally, Vercel encrypted env in prod):
- `GEMINI_API_KEY` — the LiteLLM gateway key (sk-...). All model traffic goes
  through the gateway configured in config.py (`LITELLM_*` vars).
- `APP_PASSWORD`, `APP_SESSION_SECRET` — web app login.
- `BLOB_STORE_ID` (+ OIDC blob access on Vercel) — private PDF storage.
- `CRON_SECRET` — cleanup cron auth.
- Optional: `MATHPIX_APP_ID/KEY` (extra visual evidence), `TEACHER_SIGNATURE`
  (header signature; also settable per-job from the UI).

## Commands

```bash
# Python tests (984+ tests; test_notes_pdf_layout.py needs pdfplumber)
python -m pytest tests -q

# JS tests (82)
npm run test:web

# Build web bundle
npm run build

# Deploy (Vercel CLI, linked project)
vercel deploy --prod --yes
```

One Python test (`test_exact_source_label_layers_survive_generation_and_pdf_render`)
compares font-glyph hashes and is machine-dependent — it may fail on a new
machine; that is a known environment quirk, not a code bug.

## Hard-won constraints (do not relearn these the expensive way)

1. **Never pipe env values to `vercel env add` from PowerShell** — it injects a
   UTF-8 BOM and can take prod down. Use `cmd /c 'echo VALUE|vercel env add ...'`.
   `config._get` has a BOM guard as a second line of defence.
2. **Logs are redacted by contract**: exception CLASS names + file:line only,
   never message content, never user content. Keep it that way.
3. **PublicJob / manifest JSON schemas are strict cross-runtime** (Python and
   JS both enforce exact keys). Never add keys to job records; use a sidecar
   blob (see the cost blob pattern in `web_service.py` + `api/cleanup.js`).
4. **Repair-first philosophy**: model wobble (bad page numbers, empty chunks,
   truncated JSON, missing backslashes, runaway generations) is salvaged or
   retried, not turned into a failed job. See `_validate_and_order_chunk_notes`,
   `latex_repair.py`, and the retry loop in `_generate_pdf_schema`.
5. **Output cap** `GENERATION_MAX_OUTPUT_TOKENS=24576`: the model occasionally
   runs away to the 65k output cap (250+ s, job-killing). Don't raise this
   without a reason.
6. **Gateway pricing**: the LiteLLM gateway bills ~$1.50/$7.50 per 1M tokens
   (NOT Gemini list price). `LLM_COST_*_INR_PER_1M` display rates in config.py
   match the gateway; update them if gateway pricing changes.
7. The pydantic models keep legacy fields (`Section.diagram`, `visual_ids`,
   `Visual.prompt`) so old stored notes parse, but `_generation_schema_json`
   prunes them from what the model is allowed to emit. Manifest mode (legacy)
   still needs the unpruned schema.

## Where to start reading

1. `notesbot/prompts.py` end to end (30 min) — the product IS this prompt.
2. `notesbot/gemini_client.py`: `generate_short_notes` → `_generate_sequence_note_bundle`
   → `_generate_sequence_chunk` → `_generate_pdf_schema`.
3. `notesbot/notes_pdf.py`: `build_notes_pdf` for the visual identity.
4. `DESIGN.md` and `docs/PROMPTS-DUMP.md` for the rendered view.
