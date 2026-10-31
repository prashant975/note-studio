# 02 — Short Notes Studio today (`shortnotes-core`)

Mapped read-only on 2026-10-04. Root: `C:\Users\Laptop\Downloads\shortnotes-core`.

> **In the Note Studio plan this is an ORIGINAL.** Its code is copied into
> `Note_Studio\engines\shortnotes\` and the original is never edited. It keeps
> working, together with its website and exe. Where this doc says "keep",
> "wrap" or "fix", it applies to **the copy**. Its desktop shell (the
> `studio_server`, `web/app.js` views and the legacy `/automation` page) is
> **not** copied into the app: Note Studio's own pages replace it. Those files
> are copied only if the copied tests need them.

Read `HANDOFF.md` (newest section first), `README.md`, `DESIGN.md` and
`notesbot/format_rules/README.md` before copying anything.

## What it is

The app takes a lecture PDF and produces a hand-drawn concise theory notes PDF.
It runs in three ways from one codebase:

1. **Desktop exe:** `dist/ShortNotesStudio.exe` (PyInstaller one-file, about 89 MB).
2. **Vercel website:** `public/` + `api/`, password login, Vercel Blob storage.
3. **CLI:** `python -m sheets_bridge ...`.

## Desktop shell (replaced by Note Studio's own pages; not copied into the app)

| Piece | Where | Notes |
|---|---|---|
| Build | `packaging/ShortNotesStudio.spec` | Bundles `fonts/ assets/ public/ notesbot/format_rules/ client_secret.json sheets.json` |
| Entry | `packaging/studio_launcher.py` | `prepare_environment()`: `.env` beside the exe, then `DEFAULTS` (`PW_PROXY_MODEL=gemini-3.7-flash`, `SHEETS_BRIDGE_CACHE=off`, ...). Seeds `~/.shortnotes_bridge/sheets.json` once. `--self-test`. No args → `sheets_bridge.cli.main(["--serve"])` |
| Server | `sheets_bridge/studio_server.py::serve_studio()` | stdlib `ThreadingHTTPServer`, `127.0.0.1:3000`, opens the system browser. `Handler` subclasses `api.web.handler` and **monkeypatches** `BlobStore`→`LocalBlobStore`, the dispatcher→`_LocalDispatcher` thread, `_lazy_generate`→PW proxy, `_lazy_render`→fonts+branding |
| Page | `public/index.html` (hand-edited, 440 lines) + `public/app.js` (esbuild bundle of `web/app.js`) | Two views: Automation (home) and Upload PDF |
| Legacy page | `/automation` → `sheets_bridge/webui.py::render_page()` | HTML/JS inside a Python string; a second copy of the run options and the confirm dialog |
| Upload API | `/api/web?action=health|session|status|auth|start|import|run|downloaded|logout` → `api/web.py::WebApplication._route` | `import` is 503 locally |
| Automation API | `/api/automation/{status,config,session,signin,signout,tabs,preview,profiles,profiles/delete,run,stop}` → `webui.api_response` | Needs the Studio session; POSTs need header `X-Requested-With: sheets-bridge` |
| Local blob | `/api/blob-upload?action=prepare`, `PUT /api/local-blob/upload`, `POST /api/blob-download`, `GET /api/local-blob/get` | Desktop stand-ins for Vercel Blob |

### Front end (`web/`, vanilla JS ES modules, no framework)

- `web/app.js` (1839 lines) holds every view: login, Automation dashboard and
  Upload. It switches between them with `showView()`.
- `web/state.js` (899 lines) is pure logic: validation, localStorage prefs,
  `pollJob`, `startGeneration` and downloads.
- `web/session.js` and `web/styles.css`.
- Built with `npm run build` (esbuild) into `public/`. `rules.json` is imported
  at build time.
- Polling only, no SSE:
  - Upload job: 2.5 s, with a 720 s deadline.
  - Automation status: 1.5 s.
  - Sign-in: 1.5 s, up to 6 minutes.

## Backend (copied into engines/shortnotes; Note Studio wraps the copy, does not rewrite it)

### `notesbot/` (the product)

**Pipeline**
- `pdf_utils.py`: chunks the PDF and sets `page_render_scale`.
- `prompts.py`: the master prompt and the `ShortNotes` schema. Its text order
  is deliberate, for implicit caching; do not reorder it.
- `gemini_client.py`: the generation chain `generate_short_notes` →
  `_generate_sequence_note_bundle` → `_generate_sequence_chunk` →
  `_generate_pdf_schema`.
- `latex_repair.py`, `math_render.py`, `notes_pdf.py::build_notes_pdf`,
  `doodles.py`: repair the maths, then draw the PDF.

**Policy and quality:** `concise_policy.py`, `generation_policy.py`,
`input_limits.py`, `source_coverage.py`, `repetition.py`, `final_quality.py`,
`note_quality.py`.

**Visuals:** `visual_inventory.py`, `visual_manifest.py`, `visual_assets.py`,
`snapshot.py`, `image_gen.py`, `svg_gen.py`.

**Requirements:** `categories.py` reads `format_rules/rules.json` and the
`format_rules/<category>/<subject>.txt` files. `generation_profile.py` carries
the per-run context variables: category, subject, Sanskrit style and fixed
signature.

**Cost and config:** `cost_meter.py` and `config.py`, which loads `.env` and
holds every knob.

**Web jobs:** `web_service.py`, `web_jobs.py` (states `importing → queued →
running → complete|failed|expired`), `web_auth.py`, `blob_store.py`.

**Legacy Telegram:** `handler.py`, `telegram_client.py`, `workdir.py`.

### `sheets_bridge/` (the automation)

- **Pipeline:** sheet row → source PDF (Drive or a catalog link) → notes →
  Drive upload → write-back to the sheet.
- **Run path:**
  1. `batch.run_targets`
  2. `runner.Runner` (one row at a time), or `catalog_flow.CatalogFlow`
  3. `coverage.generate_complete_notes`
  4. `generator.generate_notes_pdf`
  5. `drive.py`
- **Row states (`columns.py`):**
  - Column Q, Auto Status: empty, `claimed <ts>`, `review`, `failed: <code>` or `skipped`.
  - Column R, Review Status: Review, Done or Regenerate.
  - Catalog sheets also get "Concise Notes Link" and "Lecture Folder Link".
- **Paying once:** Drive files carry the properties `shortnotesSource` and
  `shortnotesRender`, so a lecture that was already made is reused or copied
  rather than generated again.
- **Model access:** `pw_transport.install()` swaps the LLM client for the PW
  proxy (Gemini) or `pw_openai.OpenAIProxyClient` (GPT). `SELECTABLE_MODELS`
  feeds the model dropdown.
- **Run control:** `webui.RunController` is one daemon thread.
  - `status()` reports state, the last 2000 log lines, progress `{row, outcome,
    position, total, done, failed, costInr, sheet}` and a summary.
  - `stop` stops after the current row.
- **Run log:** one summary row per run in the remote tab "Handwritten Notes Runs"
  (`run_log.py`).
- **CLI flags:**
  - Run: `--dry-run --limit --row --redo --subject --max-cost --spreadsheet-id
    --gid --profile --all-profiles --list-profiles --list-tabs --all-tabs`
  - Output: `--signature --output-folder --category --sanskrit-style
    --no-page-numbers`
  - Server: `--serve --serve-page --port --no-browser --no-open`
  - Checks: `--check-setup --test-model --probe-*`

### Auth

- **Studio password:** `APP_PASSWORD` (default `localdev`) → HMAC cookie
  `shortnotes_session`, valid 12 hours. `derive_owner()` names the blob folder.
  Without `.env`, `APP_SESSION_SECRET` is random on every launch, so the user
  must sign in again after each restart and older local-blob jobs are orphaned.
- **Google:** `sheets_bridge/google_auth.py` (installed-app OAuth, scopes drive,
  spreadsheets, openid, email). `auth_session.AutomationAuth` uses one
  process-wide account.
- **PW access:**
  - `pw_access.py` (the kit copy, `APP_NAME="Handwritten Notes Automation"`) +
    `sheets_bridge/pw_gate.ProxyAccess`: requires an `@pw.live` account on the
    whitelist.
  - The root `pw_access.py` differs from `pw-app-kit/pw_access.py`.

## Tests

- Python: `python -m pytest tests -q`, 33 files, about 1600 cases.
- Web: `npm run test:web`, 92 tests. `web/app.test.mjs` reads
  `public/index.html`, and `web/automation.test.mjs` renders the legacy page.
- Known machine-dependent failure:
  `tests/test_visual_pipeline.py::test_exact_source_label_layers_survive_generation_and_pdf_render`.
  The last full run was 1603 passed, 1 failed.

## What is missing compared with QBG Studio

| QBG Studio has | Short Notes Studio today |
|---|---|
| One menu with a page per job | Two views in one page + a separate legacy `/automation` page |
| Smart Queue (many papers, one by one, own batch each) | One upload job or one automation run at a time; a second is refused |
| Run history in a local DB, survives restart | Run log lives in memory: lost on restart. History only on the sheet + remote log tab |
| Settings page (models per stage, keys, folders, updates) | No settings; model is a per-run dropdown written into `os.environ`; many knobs are `.env`/CLI-only |
| Model order per stage + fallback on budget/429 | One model per run; the Upload page uses whatever the last automation run set |
| Team installer (Inno Setup, per-user install, config baked in) | PyInstaller one-file exe copied by hand |
| Update button (Drive folder, checksum, silent install) | Builds replaced by renaming `dist/*.exe`; `__version__ = "0.1.0"` |
| What's new tour + page guides | None |
| Live run page with steps, cost, Stop, Retry | Status card + log polling |
| Note types / requirement registry | `format_rules/` exists for one note style (good base) |

## Pain points Note Studio solves (without editing the copy beyond its allowed list)

- **Huge files.** `notes_pdf.py` (2989 lines), `gemini_client.py` (2616) and
  `visual_inventory.py` (2080) are too big to work in safely. Note Studio does
  not split them; it only calls them. `web/app.js` and `webui.py` are not used
  by Note Studio at all.
- **Duplicated automation UI.** The run options exist twice, in `webui._PAGE`
  and in the Automation view. Note Studio has one Automation page and calls
  the run code directly.
- **Runtime monkeypatching.** `studio_server` swaps `BlobStore` /
  dispatcher / `_lazy_generate` / `_lazy_render`, and `pw_transport.install()`
  swaps the client. The New notes adapter must reproduce exactly what the old
  desktop does: PW proxy transport, local storage, `pdf_fonts.install()` and
  `pdf_branding.install()`. Do it from Note Studio's adapter, in the same
  order, not by editing the copy. The parity test proves it.
- **Features reachable only from the CLI or `.env`.** These are `--row`,
  `--check-setup`, `--test-model`, `--probe-pdf`, the profile fields
  (`signature`, `maxCostInr`, `alsoPdf`, `enabled`, `skipBatches`) and these
  env knobs:
  - `SHEETS_BRIDGE_PAGES_PER_CALL`, `GROUP_WORKERS`, `SOURCE_REVIEW`
  - `MINDMAP`, `NAME_WATERMARK`, `WATERMARK_OPACITY`
  - `LOG_SHEET`, `CACHE`, `PW_PROXY_REASONING_EFFORT`
- **Diverged sheet lists.** `~/.shortnotes_bridge/sheets.json` has 6 profiles
  and the repo's `sheets.json` has 5.
- **Local-blob clutter.** Local-blob files are never cleaned up, and about
  1.6 GB of old exes sit in `dist/`.

## Hard constraints from HANDOFF.md (do not relearn these)

1. Never pipe values to `vercel env add` from PowerShell (it adds a BOM and can
   take production down).
2. Logs are redacted by contract: exception class and file:line only, never
   user content.
3. The `PublicJob` and manifest JSON shapes are strict in both Python and JS.
   Never add keys to them; put extra data in a sidecar.
4. Repair first: model wobble is salvaged or retried, not turned into a failed job.
5. `GENERATION_MAX_OUTPUT_TOKENS=24576`; do not raise it.
6. The gateway's prices are not Gemini list prices (see the `config.py` rates).
7. The pydantic models keep legacy fields so old stored notes still parse.
8. New code that renders a source page uses `pdf_utils.page_render_scale`.
9. Saved PDFs are named ` - Concise Notes.pdf` (`output_naming.py`); the cache
   keys `concise-theory-only-v6` and `concise-theory-quality-v2` must not
   change just because the shell changed.
