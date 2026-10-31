# Reference files from QBG Studio

These are copies of QBG Studio files, taken on 2026-10-04, to **read and adapt**.
They are not imports: QBG names, routes and business rules must be replaced with
the notes app's own. `*_excerpt.py` files hold only the named functions from
very large modules and will not run on their own.

The live source is on this computer, read-only. Never edit it from here:
`S = C:\Users\Laptop\Downloads\QBG_Complete_Toolkit\QBG_Studio_Vibecraft\backend\studio`,
`Q = S\qbg_studio`.

Checked before packaging: no keys, tokens, sheet/folder ids or real emails
(one test uses the made-up `me@pw.live`).

| Kit file | Source | Use it for | Phase |
|---|---|---|---|
| `python/app_excerpt.py` | `Q\app.py` | `render()`, `_go()`, `_pw_guard()`, `_nav_context()`, `_same_site_only` middleware, `_prepare()` | 1 |
| `python/db_excerpt.py` | `Q\db.py` | `connect()`/`init()` (WAL), `get_setting`/`set_setting`, `reap_orphan_jobs`, `claim_job` | 1 |
| `python/jobs_excerpt.py` | `Q\jobs.py` | `request_stop`/`stop_requested`, `_run_guarded` → `_finish` (always finishes, redacts) | 1-2 |
| `python/pipeline_bridge_excerpt.py` | `Q\pipeline_bridge.py` | `stream_command`, `stop_process`, `_child_env`, `redact_secrets`, `looks_technical` - **the basis of the child-process engine host** | 2-3, 5 |
| `python/launch.py` | `Q\launch.py` | Free loopback port, single instance via `/api/instance`, `READY|url`, open browser | 1 |
| `python/job_queue.py`, `job_queue_routes.py` | `Q\` | Smart Queue: request waits, two lanes, pump, holds, de-dupe | 4 |
| `docs/QUEUE.md` | `S\3_Docs\QUEUE.md` | The queue's design and rules in prose | 4 |
| `python/autopilot_model.py` | `Q\` | `ROLES`: model chain per stage + saved overrides | 4 |
| `python/ai_transport.py` | `Q\` | One `_send` for every model call; budget-exceeded → next model, pause 30 min | 4 |
| `python/ai_catalog.py` | `Q\` | Settings "Check models" / "Test" | 4 |
| `python/ai_meter.py` | `Q\` | Cost per run and stage in SQLite | 2-4 |
| `python/category_patterns.py` | `Q\` | Example of a registry module (QBG's categories → families) - shape for `note_types.py` | 2 |
| `python/whats_new.py`, `static/whats_new.js` | `Q\` | Release tours (shown 2 starts, "Show me" links) | 9 |
| `python/guides.py`, `templates/_page_guide.html` | `Q\` | 2-4 step guide above every page, foldable | 9 |
| `python/updater.py`, `static/release.js`, `docs/UPDATES.md` | `Q\`, `S\3_Docs` | Update button from a Drive folder (sha256 check, silent install, restart) | 9 |
| `python/celebrations.py`, `static/celebrations.js` | `Q\` | Optional welcome/greeting moments | optional |
| `templates/base.html` | `Q\templates` | Sidebar macro `navlink(...)`, top bar, footer (version, Update slot, What's new) | 1 |
| `templates/home.html` | `Q\templates` | Overview page layout | 1 |
| `templates/settings.html` | `Q\templates` | Settings cards layout | 1-4 |
| `templates/work_queue.html`, `_queue_card.html`, `_queue_strip.html` | `Q\templates` | Queue page, polled card, strip on task pages | 4 |
| `templates/_progress.html`, `_events.html`, `_run_time.html` | `Q\templates` | Polled progress + event list + elapsed time | 2 |
| `templates/error.html`, `_icons.html` | `Q\templates` | Friendly error page, inline SVG icon macro | 1 |
| `static/style.css` | `Q\static` | Tokens in `:root`, cards, buttons, sidebar - change the accent colour | 1 |
| `static/app.js`, `messages.js` | `Q\static` | Small shared behaviours, toasts | 1 |
| `docs/DESIGN.md` | `Q\DESIGN.md` | The design rules (tokens, one filled button, copy style, accessibility) | 1 |
| `docs/STUDIO_README.md` | `Q\README.md` | How QBG Studio presents itself to operators | 10 |
| `build/build_version.py` | `S\` | `YYYY.M.D.N` | 9 |
| `build/build_team_installer.py`, `build_package.py` | `S\` | Installer build: `verify_clean`, `secret_scan`, `verify_upgrade_rules`, `verify_runtime_imports`, `smoke_test` | 9 |
| `build/qbg_setup.iss` | `S\` | Inno Setup: per-user, fixed AppId, data `onlyifdoesntexist uninsneveruninstall` | 9 |
| `build/start_installed.py` | `S\` | Installed start + `--check` (DB integrity) | 9 |
| `build/desktop_launcher.cs` | `S\` | Tray launcher (only if the user picks the QBG packaging) | 9 |
| `tests/conftest.py` | `S\tests` | Blocks network in every test | 1 |
| `tests/test_no_undefined_names.py` | `S\tests` | pyflakes guard | 1 |
| `tests/test_job_queue.py` | `S\tests` | How the queue is tested (TestClient, tmp DB, patched launchers) | 4 |
| `tests/test_whats_new.py` | `S\tests` | Tour tests | 9 |
| `tests/test_updater.py`, `test_clean_install_startup.py` | `S\tests` | Update + clean-install guards | 9 |

Vendored libraries are not copied. Download HTMX (`htmx.min.js`, BSD-2) into
`note_studio/static/vendor/`, or copy it from `Q\static\vendor\`.
