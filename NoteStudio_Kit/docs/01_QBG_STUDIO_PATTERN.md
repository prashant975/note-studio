# 01 — The QBG Studio pattern (what "like this project" means)

QBG Studio is the model to copy. Its live source, read-only (never edit it from
the notes project), is at:

```
R = C:\Users\Laptop\Downloads\QBG_Complete_Toolkit\QBG_Studio_Vibecraft
S = R\backend\studio            (the desktop app + build scripts)
Q = S\qbg_studio                (the app package)
```

The kit's `reference/qbg_studio/` folder holds copies and excerpts of the
pieces worth reusing; `reference/README.md` maps each file back to its source.

## 1. Stack

| Layer | QBG Studio uses | Why it works for non-developer operators |
|---|---|---|
| Server | **FastAPI + uvicorn**, one process, `127.0.0.1` | Simple, typed forms, background threads |
| Pages | **Jinja2 templates**, server-rendered, `base.html` + one template per page | Each page is one small file; no bundler |
| Interactivity | **HTMX** (vendored `static/vendor/htmx.min.js`) + a little plain JS | Live run cards are just `hx-get ... hx-trigger="every 3s"` |
| Styling | **One stylesheet** `static/style.css`, design tokens in `:root` | Every page looks the same; one place to change |
| State | **SQLite** (WAL) via `db.py`: `settings(key,value)`, `jobs`, per-feature `*_runs` / `*_items`, `job_queue` | Settings and history survive restarts |
| Long work | Threads for in-process work; `pipeline_bridge.stream_command` for scripts in a child process | Crash-safe, stoppable, log streamed line by line |
| Desktop | `launch.py` picks a free loopback port, opens the browser; `desktop_launcher.cs` = tray exe | Double-click to start; one instance per install |
| Distribution | Embedded Python runtime + Inno Setup per-user installer; self-update from a Drive folder | Teammates install once and stay current |

## 2. Code layout per feature

Every feature is the same three files. The notes app copies this shape exactly.

```
<feature>_routes.py    register(app, render, go, guard): the HTTP routes, small
<feature>_service.py   initialize() (own tables), recover() (after a crash),
                       create_run(), run(): the work, no HTTP
<feature>_*.py         model / generation / portal helpers
templates/<feature>.html, templates/_<feature>_run.html (polled fragment)
```

The bottom of `app.py` calls each `register(...)`. `_prepare()` calls each
`initialize()` at startup, then each `recover()`.

App-wide helpers (see `reference/qbg_studio/python/app_excerpt.py`):

- `render(request, name, **ctx)` injects the nav, badges and preflight
  warnings, and sends no-store headers.
- `_go(path, toast, kind)` redirects with a toast message.
- `_pw_guard(request, action)` shows a "sign in first" page instead of failing.
- Middleware `_same_site_only` refuses cross-site POSTs. This is the CSRF
  protection; there is no token juggling.
- `asset()` busts the cache by file mtime.

## 3. Navigation

- The sidebar is hard-coded in `templates/base.html` through a
  `navlink(href, label, icon, match, tag, tagcls, hint)` macro.
- Menu items are grouped by **job**, not by technology:
  - Overview
  - Smart Queue
  - Add papers
  - Work on QBG questions
  - Make papers
  - System / Settings
- The sidebar footer holds the version, the Update slot, "What's new" and the
  signed-in user.
- The top bar holds a breadcrumb, page actions and a "running now" chip.
- Each page fills `{% block body %}`.
- `templates/_page_guide.html` sits above every page body: a foldable 2-4
  step guide from `guides.py`.

## 4. Long-running work

- **Jobs (`jobs.py`, `db.jobs` table):**
  - Columns: `state`, `pct`, `message`, `detail`, `error`, `pid`, `done_n`,
    `total_n`, `errors_n`, `parent_job_id`.
  - `_run_guarded()` always ends in `_finish()`, which redacts secrets.
  - `request_stop(job_id)` / `stop_requested()` stop work that has no child
    process yet.
- **Child processes:** `pipeline_bridge.stream_command(argv, on_line, cwd,
  on_started, extra_env)` starts a Popen with a new process group and UTF-8
  env, streams each line and records the pid.
  - `stop_process(pid)` sends CTRL_BREAK, waits 10 s, then runs
    `taskkill /T /F`.
- **Crash safety:**
  - `db.reap_orphan_jobs()` at startup marks dead jobs failed.
  - Each `service.recover()` resumes or ends its own runs.
  - Spreadsheets are saved atomically, and `release.json` is written through a
    `.pending` file.
- **Smart Queue (`job_queue.py`, design in `reference/qbg_studio/docs/QUEUE.md`):**
  - What waits is the **request** (settings plus files in
    `data/queue/<entry>/`), not a half-made run. Remove leaves nothing behind.
  - Every Start button calls `job_queue.submit_task(...)`. If nothing is ahead,
    it starts at once; otherwise it waits, never refused.
  - "Each file is a separate paper" makes one entry per file with the same
    settings.
  - **Two lanes** (`lane(kind)`): one running entry per lane, each lane in
    order. A pump thread (`tick()`, every 4 s) starts each free lane's head.
  - The queue **holds** (the card says why, with a "Continue the queue" button)
    when Stop is pressed, when sign-in is lost, and after a Studio restart. A
    failed item does not hold it.
  - `submit(..., key=)` de-duplicates double clicks. "Start another" sits on
    every run page, and `_queue_strip.html` sits on task pages.
- **Live progress:** no SSE. HTMX polls the run fragment every 1.2-5 s.
  `looks_technical()` hides stack-trace-looking lines from operators; the full
  log stays downloadable.

## 5. Settings and models

- **Settings page:** `settings.html`, `GET/POST /settings` plus one sub-route
  per card. It covers connection, values you can change, sign-in, AI models,
  updates, usage sheet, team data and celebrations.
- **Storage:** `db.get_setting(key, default)` / `set_setting(key, value)`.
  Nothing lives in `os.environ` at runtime.
- **Model order per stage:** `autopilot_model.ROLES` (copied whole) gives each
  stage (read, check, solutions, translation, tagging) a chain: Gemini first,
  then fallbacks. The operator can reorder it in Settings (setting
  `autopilot_model_chains`).
  - `ai_catalog.py` powers the "Check models" and "Test" buttons.
- **Budget:** `ai_transport._send` routes every model call.
  - "Budget exceeded" switches to the next model at once and pauses that model
    for 30 minutes.
  - A 429 is retried, not switched.
- **Cost:**
  - `ai_meter.py` keeps cost per run and stage in SQLite.
  - `usage_sheet.py` sends one row per finished task to the shared
    "Usage Cost 2.0" sheet.

## 6. Sign-in

- **Desktop:** `pw_auth.py` runs loopback Google OAuth, accepts the pw.live
  domain only, and stores a DPAPI-encrypted refresh token.
- **AI calls:** `pw_access.py` (PW App Kit) checks the whitelist and calls the
  PW proxy (`gemini_generate` / `gpt_generate`, with a `UsageSession` per task).
- **Missing sign-in:** pages show `pw_blocked.html` with a Sign in button and
  never crash.
- **Team installer:** carries the OAuth client registration
  (`deployment.json`), so teammates only press Sign in.

## 7. Team distribution

- **Version:** `build_version.next_release()` gives `YYYY.M.D.N`
  (e.g. 2026.10.4.1). It is shown in the sidebar and written to
  `static/release.json`.
- **Installer:** `build_team_installer.py` takes `git ls-files`, then:
  1. compiles to `.pyc` and copies the embedded Python 3.13 runtime;
  2. compiles the C# launcher and runs Inno Setup on `qbg_setup.iss`. This is
     a per-user install in `%LOCALAPPDATA%\Programs\<App>` with a fixed AppId;
     data folders are marked `onlyifdoesntexist uninsneveruninstall`, so an
     update never touches saved work;
  3. runs the checks: `verify_clean` (no saved work or tokens packaged),
     `secret_scan`, `verify_upgrade_rules`, `verify_runtime_imports` (every
     import works in the bundled runtime) and `smoke_test` (installs a copy
     with empty state, starts it, opens the main pages and fails on any
     traceback or leaked secret).
- **What the build bakes in:** everything a teammate needs. That means the
  OAuth client, the release folder id and the team sheet id; a service-account
  key only with `onlyifdoesntexist`. Teammates never paste anything.
- **Update button (`updater.py`):**
  1. Lists the Drive release folder with a read-only service account.
  2. Picks the newest `<App>_Setup_<version>.exe`.
  3. Downloads it and checks Drive's sha256.
  4. Writes a detached PowerShell helper that waits for Studio to quit, runs the
     setup with `/VERYSILENT` and restarts Studio.
  5. Calls `os._exit(0)`.
  - `static/release.js` polls `/update/status` and shows the sidebar Update
    slot.
  - The release folder is checked every 30 minutes.
  - A service account can READ but not OWN Drive files, so publishing means
    the owner drops the setup exe into the folder by hand.
- **What's new + guides:**
  - `whats_new.TOURS` holds the newest tour first, each with its own id. A tour
    shows on the first page of a Studio start, at most 2 starts, has "Show me"
    links, and can be reopened from the sidebar.
  - `guides.GUIDES` maps each path to 2-4 short steps, rendered by
    `_page_guide.html`; a folded guide stays folded (localStorage).
  - **Every release that adds a feature adds a tour and updates its guide.**
- **Celebrations** (optional): welcome and greeting moments, `celebrations.py`
  and `.js`, with an off switch in Settings.

## 8. Tests

- `tests/conftest.py` blocks network access for every test.
- Page tests use `TestClient(app.app)` with `db.DB_PATH` pointed at `tmp_path`.
  Examples: `reference/qbg_studio/tests/test_job_queue.py` and
  `test_whats_new.py`.
- `test_no_undefined_names.py` runs pyflakes, so deleted names cannot hide in
  best-effort code.
- `test_clean_install_startup.py` and `test_updater.py` guard install and
  update.
- Build self-checks: `start_installed.py --check` (DB integrity check) and the
  installer's `smoke_test`.

## 9. Do not copy

- **`team_sync_*`** (the covert teammate sync): declined, never shipped. Do not
  port it.
- **`team_support.py`** (signed remote fixes): dormant in QBG and needs explicit
  consent. Out of scope.
- **`team_data.py`** (mirrors every table to a shared sheet): only with the
  user's explicit go and visible to the operator. Out of scope for Note
  Studio.
- **QBG business modules** (`paper_*`, `tagging_*`, `autopilot_*`, portal and
  Chrome code). They do not belong in a notes app.
- **The size of `pipeline_bridge.py`** (185 KB) and `app.py` (139 KB). They are
  big because features piled into them; keep Note Studio's `app.py` to wiring
  only.
