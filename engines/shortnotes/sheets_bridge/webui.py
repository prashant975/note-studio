"""One-button local web UI for the sheet automation.

    python -m sheets_bridge --serve            # opens http://127.0.0.1:8765

The page has a single "Run automation now" button. Clicking it runs the whole
batch (sheet -> Drive -> notes -> Drive -> sheet) in a background thread on
this machine and streams progress back to the page. It binds to loopback
only; state-changing requests must be same-origin.
"""
from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .errors import BridgeError, describe_exception
from .mapping import SUBJECTS

_MAX_LOG = 2000

try:  # the kit client lives at the app root
    from pw_access import APP_NAME as _APP_NAME
except Exception:  # noqa: BLE001
    _APP_NAME = ""


class RunController:
    """Owns the single in-flight run and its observable state."""

    def __init__(self, settings, *, auth=None) -> None:
        self.settings = settings
        # When an AutomationAuth is supplied the operator has already signed in
        # and passed the allowlist, so the run uses those credentials instead of
        # opening a browser tab part-way through the batch.
        self.auth = auth
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.log: deque[str] = deque(maxlen=_MAX_LOG)
        self.state = "idle"          # idle | auth | running | finished | error
        self.error = ""
        self.summary: dict | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.options: dict = {}
        # Live counters: the summary only exists once a run has finished, and a
        # lecture takes minutes, so the page would otherwise show nothing.
        self.progress: dict = {}
        # Who pressed the button, so the log and the summary row name a person.
        self.started_by = ""
        self.account = ""

    # -- observation ----------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "error": self.error,
                "options": self.options,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "log": list(self.log),
                "summary": self.summary,
                "progress": dict(self.progress),
                "startedBy": self.started_by,
                "account": self.account,
                "app": _APP_NAME,
                "stopRequested": self._stop.is_set(),
            }

    def _note_progress(self, event: dict) -> None:
        with self._lock:
            self.progress = event

    def _emit(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.log.append(f"{stamp}  {line}")

    # -- control -----------------------------------------------------------------
    def start(self, options: dict, account=None) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise BridgeError("already_running")
            self._stop.clear()
            self.log.clear()
            self.state = "auth"
            self.error = ""
            self.summary = None
            self.progress = {}
            self.started_at = time.time()
            self.finished_at = None
            self.options = options
            self.started_by = getattr(account, "email", "") or ""
            self._thread = threading.Thread(
                target=self._work, args=(options, account), name="sheets-bridge-run", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._emit("stop requested - finishing the current row, then halting")

    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the run thread ends (or ``timeout``); True when it has."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # -- worker ---------------------------------------------------------------------
    def _services(self, options: dict, account=None):
        """(sheets, drive, access, email) for this run.

        With an ``AutomationAuth`` the account is already verified against the
        allowlist, so its clients are reused; ``account`` is the one the run
        was started with, falling back to the server's current account.
        Without an auth (the standalone page) the original behaviour is kept:
        sign in here, opening a browser tab if no cached token can be refreshed.
        """
        if self.auth is not None:
            account = account or self.auth.require()
            self._emit(f"signed in as {account.email}")
            if not options.get("dryRun"):
                model = account.access.install_transport(self._chosen_model(options))
                self._emit(f"PW proxy: app '{_APP_NAME}' - model {model} - allowlist already verified")
                self._check_model(model)
                return account.sheets_service, account.drive_service, account.access, account.email
            return account.sheets_service, account.drive_service, None, account.email

        from .google_auth import build_services, load_credentials

        self._emit("signing in to Google (a browser tab opens the first time only) ...")
        creds = load_credentials(self.settings.client_secret_path, self.settings.token_path, interactive=True)
        sheets_service, drive_service = build_services(creds)

        access = None
        email = ""
        if not options.get("dryRun"):
            from .google_auth import token_provider, user_email
            from .pw_gate import ProxyAccess

            access = ProxyAccess(token_provider(creds), email=user_email(creds))
            email = access.email
            self._emit(f"signed in as {email or 'unknown account'}")
            if email and not access.domain_ok():
                raise BridgeError("not_pw_account", f"{email} is not an @pw.live account")
            model = access.install_transport(self._chosen_model(options))
            self._emit(f"PW proxy: app '{_APP_NAME}' - model {model} - checking allowlist ...")
            access.ensure_allowed()
            self._emit("allowlist: allowed")
            self._check_model(model)
        return sheets_service, drive_service, access, email

    def _chosen_model(self, options: dict) -> str:
        """The dropdown's model for this run, or "" to keep the configured one.

        The choice also goes into the environment: the notes cache keys on
        PW_PROXY_MODEL, so without this a run could reuse notes another model
        generated.
        """
        model = str(options.get("model") or "").strip()
        if model:
            os.environ["PW_PROXY_MODEL"] = model
        return model

    def _check_model(self, model: str) -> None:
        """Stop before the first row when a newly selected model is unusable."""
        from .model_check import preflight

        problem = preflight(model)
        if problem:
            raise BridgeError("model_unavailable", problem)

    def _work(self, options: dict, account=None) -> None:
        try:
            from notesbot import categories

            from . import batch, profiles
            from .runner import RunOptions

            sheets_service, drive_service, access, email = self._services(options, account)
            with self._lock:
                self.state = "running"
                self.account = email

            targets = profiles.resolve_targets(self.settings)
            chosen = profiles.select(targets, options.get("profiles") or None)
            self._emit(
                "profile(s): " + ", ".join(t.name for t in chosen)
                if len(targets) > 1 else f"sheet: {chosen[0].spreadsheet_id} gid {chosen[0].gid}"
            )

            run_options = RunOptions(
                dry_run=bool(options.get("dryRun")),
                limit=options.get("limit"),
                row=options.get("row"),
                redo=bool(options.get("redo")),
                show_page_numbers=options.get("showPageNumbers", True),
                subject_mode=options.get("subject") or "sheet",
                category=options.get("category") or categories.DEFAULT,
                sanskrit_style=options.get("sanskritStyle") or "hindi",
                max_cost_inr=options.get("maxCost"),
                signature_override=self.settings.signature,
                max_input_bytes=self.settings.max_input_bytes,
            )
            result = batch.run_targets(
                chosen,
                sheets_service,
                drive_service,
                run_options,
                access=access,
                out=self._emit,
                stop_check=self._stop.is_set,
                operator=email,
                on_progress=self._note_progress,
            )
            with self._lock:
                self.summary = _batch_payload(result)
                self.state = "finished"
        except BridgeError as exc:
            self._emit(f"error: {exc}")
            with self._lock:
                self.state = "error"
                self.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - class + location only, never content
            text = describe_exception(exc)
            self._emit(f"error: {text}")
            with self._lock:
                self.state = "error"
                self.error = text
        finally:
            with self._lock:
                self.finished_at = time.time()


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Short Notes - Sheet Automation</title>
<style>
  :root { --ink:#1f2937; --muted:#6b7280; --line:#e5e7eb; --bg:#faf7f2; --card:#fff; --go:#8b2942; --go2:#a4324f; --ok:#0f766e; --bad:#b91c1c; --warn:#b45309; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 system-ui, Segoe UI, Roboto, sans-serif; color:var(--ink); background:var(--bg); }
  main { max-width:960px; margin:0 auto; padding:28px 18px 60px; }
  h1 { font-size:26px; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:18px; margin-bottom:16px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
  .row { display:flex; gap:14px; flex-wrap:wrap; align-items:center; }
  button.go { font-size:20px; font-weight:700; color:#fff; background:var(--go); border:0; border-radius:12px; padding:16px 28px; cursor:pointer; box-shadow:0 2px 0 #5c1a2b; }
  button.go:hover { background:var(--go2); }
  button.go:disabled { background:#c9a2ad; box-shadow:none; cursor:not-allowed; }
  button.sec { font-size:14px; background:#fff; color:var(--ink); border:1px solid var(--line); border-radius:10px; padding:10px 14px; cursor:pointer; }
  button.sec:disabled { color:#9ca3af; cursor:not-allowed; }
  label.opt { display:inline-flex; gap:6px; align-items:center; color:var(--muted); font-size:14px; }
  input[type=number] { width:80px; padding:6px 8px; border:1px solid var(--line); border-radius:8px; }
  .status { display:flex; gap:18px; flex-wrap:wrap; align-items:baseline; }
  .pill { display:inline-block; padding:3px 10px; border-radius:999px; font-size:13px; font-weight:600; background:#eef2ff; color:#3730a3; }
  .pill.running { background:#fef3c7; color:var(--warn); }
  .pill.finished { background:#d1fae5; color:var(--ok); }
  .pill.error { background:#fee2e2; color:var(--bad); }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; margin-top:12px; }
  .stat { border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
  .stat b { display:block; font-size:22px; }
  .stat span { color:var(--muted); font-size:13px; }
  pre.log { background:#111827; color:#e5e7eb; border-radius:10px; padding:12px; height:320px; overflow:auto; font:12.5px/1.45 ui-monospace, Consolas, monospace; white-space:pre-wrap; word-break:break-word; margin:0; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th, td { text-align:left; padding:8px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:600; font-size:13px; }
  td.done { color:var(--ok); font-weight:600; } td.failed { color:var(--bad); font-weight:600; } td.planned { color:#3730a3; } td.stopped { color:var(--warn); }
  .hint { color:var(--muted); font-size:13px; }
  code { background:#f3f4f6; padding:1px 5px; border-radius:5px; }
</style></head>
<body><main>
  <h1>Short Notes - Sheet Automation</h1>
  <p class="sub">Reads the sheet, takes every row with <b>J = Done</b> and <b>M &ne; Done</b>, finds the lecture PDF in its Drive folder, generates the notes, uploads them beside the source, and marks <b>M = Done</b>.</p>

  <div class="card">
    <fieldset id="runOptions" class="row" style="margin-top:12px;border:0;padding:0" disabled>
      <legend>Choose settings before running</legend>
      <label class="opt"><input type="checkbox" id="showPageNumbers" checked> Include page numbers</label>
      <label class="opt"><input type="checkbox" id="useLimit"> limit to <input type="number" id="limit" min="1" value="5" disabled> rows</label>
      <label class="opt"><input type="checkbox" id="redo"> redo rows already marked Done</label>
      <label class="opt">max spend &#8377; <input type="number" id="maxCost" min="0" step="10" placeholder="none"></label>
      <label class="opt" id="profileBox" hidden>sheet <select id="profile"></select></label>
      <label class="opt">category <select id="category"><option value="neet_jee">NEET-JEE</option><option value="foundation">Foundation</option></select></label>
      <label class="opt">model <select id="model"></select></label>
    </fieldset>
    <p class="hint">Page numbers are included by default. Uncheck the option to omit them from this run's notes.</p>
    <p class="hint" id="target"></p>
    <div class="row">
      <button id="run" class="go" disabled>&#9654; Run automation now</button>
      <button id="dry" class="sec" disabled>Preview only (dry run)</button>
      <button id="stop" class="sec" disabled>Stop after current row</button>
    </div>
  </div>

  <div class="card">
    <div class="status">
      <span>Status: <span id="state" class="pill">idle</span></span>
      <span id="runId" class="hint"></span>
      <span id="elapsed" class="hint"></span>
    </div>
    <p class="hint" id="proxyLine">Model calls go through the PW shared proxy - this app holds no provider keys.</p>
    <div class="stats">
      <div class="stat"><b id="cDone">0</b><span>done</span></div>
      <div class="stat"><b id="cFailed">0</b><span>failed</span></div>
      <div class="stat"><b id="cPlanned">0</b><span>planned</span></div>
      <div class="stat"><b id="cStopped">0</b><span>not reached</span></div>
      <div class="stat"><b id="cSkipped">0</b><span>skipped (not eligible)</span></div>
      <div class="stat"><b id="cCost">&#8377;0.00</b><span>total cost</span></div>
    </div>
  </div>

  <div class="card">
    <h3 style="margin:0 0 8px">Rows</h3>
    <div style="overflow-x:auto"><table><thead><tr><th>Row</th><th>Outcome</th><th>Code</th><th>Output</th><th>&#8377;</th></tr></thead><tbody id="rows"><tr><td colspan="5" class="hint">nothing yet</td></tr></tbody></table></div>
  </div>

  <div class="card">
    <h3 style="margin:0 0 8px">Log</h3>
    <pre id="log" class="log"></pre>
  </div>
</main>
<script>
(function(){
  const $ = id => document.getElementById(id);
  const API = '__API_BASE__';
  const H = {'Content-Type':'application/json','X-Requested-With':'sheets-bridge'};
  let polling = null, lastLen = -1, configReady = false, starting = false, profileNames = [];
  $('useLimit').onchange = () => { $('limit').disabled = !$('useLimit').checked; };
  function opts(dry){
    return { dryRun: !!dry,
      limit: $('useLimit').checked ? Math.max(1, parseInt($('limit').value||'1',10)) : null,
      redo: $('redo').checked,
      showPageNumbers: $('showPageNumbers').checked,
      maxCost: $('maxCost').value ? parseFloat($('maxCost').value) : null,
      model: $('model').value || '',
      category: $('category').value || 'neet_jee',
      profiles: $('profile').value === '__all__' ? profileNames : (($('profileBox').hidden || !$('profile').value) ? [] : [$('profile').value]) };
  }
  async function start(dry){
    if (!configReady || starting || $('run').disabled) return;
    const selected = opts(dry);
    starting = true;
    $('runOptions').disabled = $('run').disabled = $('dry').disabled = true;
    try {
      if (!dry) {
        const mode = selected.showPageNumbers ? 'included' : 'omitted';
        const cat = ({neet_jee:'NEET-JEE', foundation:'Foundation'})[selected.category] || 'NEET-JEE';
        let message = 'Run the automation now? Category: ' + cat + '. Page numbers will be ' + mode + '. This generates notes and updates the sheet.';
        try {
          const r = await fetch(API + '/preview', {method:'POST', headers:H, body: JSON.stringify({profiles:selected.profiles})});
          if (r.ok) {
            const preview = await r.json();
            if (!preview.totalReady) { alert('Nothing is ready to run.'); return; }
            message = (preview.items || []).map(item => item.tab + ': ' + item.ready + ' ready').join('\\n') + '\\n\\n' + message;
          }
        } catch (_) {}
        if (!confirm(message)) return;
      }
      const r = await fetch(API + '/run', {method:'POST', headers:H, body: JSON.stringify(selected)});
      const j = await r.json();
      if (!r.ok) alert(j.error || 'could not start');
    } catch (_) { alert('Could not reach the automation. Please try again.'); }
    finally { starting = false; await poll(true); }
  }
  $('run').onclick = () => start(false);
  $('dry').onclick = () => start(true);
  $('stop').onclick = async () => { await fetch(API + '/stop', {method:'POST', headers:H, body:'{}'}); };
  function fmtElapsed(s){ s = Math.max(0, Math.round(s)); const m = Math.floor(s/60); return (m? m+'m ':'') + (s%60) + 's'; }
  function render(st){
    const running = st.state === 'running' || st.state === 'auth';
    $('runOptions').disabled = $('run').disabled = $('dry').disabled = running || starting || !configReady;
    if (running && st.options) { $('showPageNumbers').checked = st.options.showPageNumbers !== false; if (st.options.category) $('category').value = st.options.category; }
    $('stop').disabled = !running || st.stopRequested;
    const pill = $('state'); pill.textContent = st.state === 'auth' ? 'signing in' : st.state; pill.className = 'pill ' + (running ? 'running' : st.state);
    if (st.startedAt) { const end = st.finishedAt || (Date.now()/1000); $('elapsed').textContent = 'elapsed ' + fmtElapsed(end - st.startedAt); }
    const s = st.summary; const c = (s && s.counts) || {};
    $('cDone').textContent = c.done||0; $('cFailed').textContent = c.failed||0; $('cPlanned').textContent = c.planned||0; $('cStopped').textContent = c.stopped||0;
    $('cSkipped').textContent = s ? Object.values(s.skipped||{}).reduce((a,b)=>a+b,0) : 0;
    $('cCost').textContent = '\\u20b9' + ((s && s.totalCostInr) || 0).toFixed(2);
    $('runId').textContent = s ? ('run ' + s.runId) : '';
    $('proxyLine').textContent = 'PW proxy' + (st.app ? ' - app "' + st.app + '"' : '') + (st.account ? ' - signed in as ' + st.account : '') + ' - no provider keys in this app';
    if (st.log.length !== lastLen) {
      const log = $('log'); const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
      log.textContent = st.log.join('\\n'); if (stick || lastLen < 0) log.scrollTop = log.scrollHeight; lastLen = st.log.length;
    }
    const tb = $('rows');
    if (s && s.outcomes && s.outcomes.length) {
      tb.innerHTML = s.outcomes.map(o => '<tr><td>' + o.row + '</td><td class="' + o.outcome + '">' + o.outcome + (o.note? ' <span class="hint">('+o.note+')</span>':'') + '</td><td><code>' + (o.code||'') + '</code></td><td>' + (o.link ? '<a href="' + o.link + '" target="_blank" rel="noopener">open</a>' : '') + '</td><td>' + (o.cost_inr ? o.cost_inr.toFixed(2) : '') + '</td></tr>').join('');
    } else if (!running) { tb.innerHTML = '<tr><td colspan="5" class="hint">' + (st.state==='idle' ? 'nothing yet' : 'no rows selected') + '</td></tr>'; }
    if (running && !polling) polling = setInterval(() => poll(false), 1500);
    if (!running && polling) { clearInterval(polling); polling = null; }
  }
  async function poll(force){
    try { const r = await fetch(API + '/status', {cache:'no-store'}); render(await r.json()); } catch (e) { if (force) console.error(e); }
  }
  fetch(API + '/config').then(r => { if (!r.ok) throw new Error('config'); return r.json(); }).then(c => {
    const models = c.models || [];
    const msel = $('model');
    for (const m of models) {
      const o = document.createElement('option');
      o.value = m.id; o.textContent = m.label || m.id; o.selected = !!m.current;
      msel.append(o);
    }
    if (!models.length) $('model').closest('label').hidden = true;
    const list = (c.profiles || []).filter(p => p.enabled !== false);
    profileNames = list.map(p => p.name);
    if (list.length > 1) {
      const sel = $('profile');
      for (const p of list) { const o = document.createElement('option'); o.value = p.name; o.textContent = p.name; sel.append(o); }
      const all = document.createElement('option'); all.value = '__all__'; all.textContent = 'All sheets'; sel.append(all);
      const wanted = new URLSearchParams(window.location.search).getAll('profile');
      if (wanted.length > 1 && list.every(p => wanted.includes(p.name))) sel.value = '__all__';
      else if (wanted.length === 1 && list.some(p => p.name === wanted[0])) sel.value = wanted[0];
      $('profileBox').hidden = false;
      $('target').textContent = list.length + ' sheets configured - pick one above';
      return;
    }
    const only = list[0];
    $('target').textContent = 'Sheet ' + (only ? only.spreadsheetId : c.spreadsheetId) + ' - tab gid ' + (only ? only.gid : c.gid) + (c.signature ? ' - signature "' + c.signature + '"' : ' - signature from the faculty column (the slides\\' teacher if different)');
  }).then(() => { configReady = true; poll(true); }).catch(() => { $('target').textContent = 'Could not load automation settings. Reload this tab to try again.'; });
  poll(true);
})();
</script></body></html>
"""


def render_page(api_base: str = "/api") -> bytes:
    return _PAGE.replace("__API_BASE__", api_base).encode("utf-8")


def _batch_payload(result) -> dict:
    """Batch outcome in the shape the page already understands.

    A single profile keeps exactly the old summary keys, so nothing downstream
    has to special-case the common case; the per-profile breakdown is added
    alongside for multi-sheet runs.
    """
    summaries = [r for r in result.results if r.summary is not None]
    counts: dict = {}
    skipped: dict = {}
    outcomes: list = []
    for entry in summaries:
        for name, value in entry.summary.counts().items():
            counts[name] = counts.get(name, 0) + value
        for name, value in entry.summary.skipped_not_eligible.items():
            skipped[name] = skipped.get(name, 0) + value
        outcomes.extend(asdict(o) for o in entry.summary.outcomes)
    return {
        "runId": summaries[0].summary.run_id if len(summaries) == 1 else result.batch_id,
        "counts": counts,
        "skipped": skipped,
        "totalCostInr": round(result.total_cost_inr, 2),
        "interrupted": result.interrupted,
        "outcomes": outcomes,
        "profiles": [
            {
                "name": r.name,
                "done": r.done,
                "failed": r.failed,
                "costInr": round(r.cost_inr, 2),
                "error": r.error,
            }
            for r in result.results
        ],
    }


def safe_api_response(controller: RunController, method: str, route: str, body: dict | None, owner: str = "") -> tuple[int, dict]:
    """``api_response`` for the HTTP handlers: a bug inside a route becomes a
    500 the page can show, never a dropped connection.

    An uncaught exception in a request handler closes the socket without an
    answer, and the browser can only say "Failed to fetch" - which is how a
    one-word NameError in the add-sheet route stayed invisible. The detail is
    the exception's class and location only, never its message.
    """
    try:
        return api_response(controller, method, route, body, owner)
    except Exception as exc:  # noqa: BLE001
        return 500, {"ok": False, "error": "internal_error", "detail": describe_exception(exc)}


def api_response(controller: RunController, method: str, route: str, body: dict | None, owner: str = "") -> tuple[int, dict]:
    """Route = 'status' | 'config' | 'run' | 'stop' (already stripped of its
    prefix). Returns (http status, json payload). No I/O besides the controller."""
    if method == "GET" and route == "status":
        return 200, controller.status()
    if method == "GET" and route == "config":
        from . import profiles as _profiles
        from . import pw_transport as _pw_transport

        s = controller.settings
        payload = {
            "spreadsheetId": s.spreadsheet_id,
            "gid": s.gid,
            "signature": s.signature,
            "maxCostInr": s.max_cost_inr,
            # No gate in normal use: the server runs as its cached account and
            # the Whitelisted sheet decides who may use the tool. The button
            # appears only on a machine that has no usable token yet, so a
            # fresh install can still sign in from the page.
            "signInRequired": bool(controller.auth is not None and controller.auth.account() is None),
            "models": _pw_transport.selectable_models(),
            "categories": _category_choices(),
        }
        try:
            payload["profiles"] = [t.public() for t in _profiles.resolve_targets(s)]
        except BridgeError as exc:
            payload["profiles"] = []
            payload["profilesError"] = str(exc)
        return 200, payload

    # -- Google sign-in gate (local Studio only) ------------------------------
    if controller.auth is not None:
        if method == "GET" and route == "session":
            return 200, controller.auth.status(owner)
        if method == "POST" and route == "signin":
            try:
                controller.auth.start_sign_in(owner)
            except BridgeError as exc:
                return 409, {"ok": False, "error": exc.code}
            return 202, {"ok": True}
        if method == "POST" and route == "signout":
            controller.auth.sign_out(owner)
            return 200, {"ok": True}
        # Everything below spends money or reads someone's Drive.
        if controller.auth.account(owner) is None:
            return 403, {"ok": False, "error": "sign_in_required"}

    if method == "POST" and route == "tabs":
        return _tabs_list(controller, body or {}, owner)
    if method == "POST" and route == "preview":
        return _preview(controller, body or {}, owner)
    if method == "GET" and route == "profiles":
        return _profiles_list(controller)
    if method == "POST" and route == "profiles":
        return _profiles_add(controller, body or {}, owner)
    if method == "POST" and route == "profiles/delete":
        return _profiles_remove(controller, body or {})

    if method == "POST" and route == "run":
        try:
            controller.start(_clean_options(body or {}, controller.settings), controller.auth.require(owner) if controller.auth else None)
        except BridgeError as exc:
            return 409, {"ok": False, "error": exc.code}
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}
        return 202, {"ok": True}
    if method == "POST" and route == "stop":
        controller.stop()
        return 200, {"ok": True}
    return 404, {"ok": False, "error": "not found"}


def _category_choices() -> list[dict]:
    from notesbot import categories

    return categories.studio_choices()


def _tabs_list(controller: RunController, body: dict, owner: str = "") -> tuple[int, dict]:
    """Every tab of the pasted workbook, classified, so the operator picks the
    right one instead of guessing a gid."""
    from . import profiles as _profiles
    from . import tabs as _tabs

    if controller.auth is None:
        return 400, {"ok": False, "error": "sign_in_required"}
    try:
        account = controller.auth.require(owner)
        spreadsheet_id, gid = _profiles.parse_sheet_link(body.get("link"))
        sheets_service, _drive = account.fresh_services()
        workbook, summaries = _tabs.list_tabs(sheets_service, spreadsheet_id)
    except BridgeError as exc:
        return 400, {"ok": False, "error": exc.code, "detail": exc.detail}
    except Exception as exc:  # noqa: BLE001 - class + location only, never content
        return 502, {"ok": False, "error": "sheets_unavailable", "detail": describe_exception(exc)}
    return 200, {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "title": workbook,
        "selectedGid": gid,
        "tabs": [summary.public() for summary in summaries],
    }


def _preview(controller: RunController, body: dict, owner: str = "") -> tuple[int, dict]:
    """What a run would touch - which tabs, how many lectures - so the
    operator confirms against facts, not a guess. Read-only."""
    from . import profiles as _profiles
    from . import tabs as _tabs

    if controller.auth is None:
        return 400, {"ok": False, "error": "sign_in_required"}
    try:
        account = controller.auth.require(owner)
        targets = _profiles.resolve_targets(controller.settings)
        chosen = _profiles.select(targets, _clean_profiles(body.get("profiles")) or None)
        sheets_service, _drive = account.fresh_services()
        cache: dict = {}
        items: list[dict] = []
        for target in chosen:
            if target.spreadsheet_id not in cache:
                cache[target.spreadsheet_id] = _tabs.list_tabs(sheets_service, target.spreadsheet_id)
            _title, summaries = cache[target.spreadsheet_id]
            if target.all_tabs:
                for summary in summaries:
                    if summary.complete and summary.eligible:
                        items.append({"profile": target.name, "tab": summary.title, "gid": summary.gid, "ready": summary.eligible, "problem": ""})
                continue
            summary = next((s for s in summaries if s.gid == target.gid), None)
            if summary is None:
                items.append({"profile": target.name, "tab": "?", "gid": target.gid, "ready": 0, "problem": "tab not found"})
            elif not summary.supported:
                items.append({"profile": target.name, "tab": summary.title, "gid": target.gid, "ready": 0, "problem": summary.note or summary.kind})
            else:
                items.append({"profile": target.name, "tab": summary.title, "gid": target.gid, "ready": summary.eligible or 0, "problem": ""})
    except BridgeError as exc:
        return 400, {"ok": False, "error": exc.code, "detail": exc.detail}
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - class + location only, never content
        return 502, {"ok": False, "error": "sheets_unavailable", "detail": describe_exception(exc)}
    return 200, {"ok": True, "items": items, "totalReady": sum(int(i["ready"]) for i in items)}


def _profiles_list(controller: RunController) -> tuple[int, dict]:
    from . import profiles as _profiles

    try:
        targets = _profiles.resolve_targets(controller.settings)
    except BridgeError as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "profiles": [t.public() for t in targets]}


def chosen_gid(body_gid: object, link_gid: "int | None") -> "int | None":
    """The tab to add: an explicit choice (the tab picker) beats the gid that
    happens to be in the pasted link, which is merely whichever tab was open."""
    if isinstance(body_gid, int) and not isinstance(body_gid, bool) and body_gid >= 0:
        return body_gid
    if isinstance(body_gid, str) and body_gid.strip().isdigit():
        return int(body_gid.strip())
    return link_gid


def _profiles_add(controller: RunController, body: dict, owner: str = "") -> tuple[int, dict]:
    """Add a sheet from a pasted link, after checking it actually works.

    The check runs with the signed-in operator's own credentials, so "you can
    see this sheet" is answered for the person who will run it. ``owner`` is
    that operator; the router must pass it, like it does for tabs and preview.
    """
    from . import profiles as _profiles

    try:
        name = _profiles.clean_name(body.get("name"))
        spreadsheet_id, link_gid = _profiles.parse_sheet_link(body.get("link"))
        all_tabs = body.get("allTabs") is True
        # Catalog tabs (lecture PDF links) file their notes in this Drive folder.
        output_folder = ""
        if body.get("outputFolder") not in (None, ""):
            output_folder = _profiles.parse_folder_link(body.get("outputFolder"))
        if all_tabs:
            target = _profiles.SheetTarget(name=name, spreadsheet_id=spreadsheet_id, gid=0, all_tabs=True,
                                           output_folder=output_folder)
        else:
            gid = chosen_gid(body.get("gid"), link_gid)
            if gid is None:
                # Never assume 0: a link copied from any tab but the first
                # carries its gid, so a missing one means the wrong link.
                return 400, {
                    "ok": False,
                    "error": "no_gid",
                    "detail": "that link has no #gid= - pick a tab, or open the tab you want and copy the URL again",
                }
            target = _profiles.SheetTarget(name=name, spreadsheet_id=spreadsheet_id, gid=gid,
                                           output_folder=output_folder)

        checked = {}
        if controller.auth is not None:
            account = controller.auth.require(owner)
            try:
                sheets_service, drive_service = account.fresh_services()
                folder_name = _profiles.verify_output_folder(drive_service, output_folder) if output_folder else ""
                checked = (
                    _profiles.verify_workbook(sheets_service, target)
                    if all_tabs
                    else _profiles.verify_target(sheets_service, target)
                )
                if folder_name:
                    checked["outputFolderName"] = folder_name
                    if not checked.get("catalog", all_tabs):
                        # A batch sheet keeps each lecture's notes beside its source PDF.
                        checked.setdefault("warnings", []).append(
                            "this sheet keeps each lecture's notes beside its source PDF; "
                            "the notes folder is used only for sheets of lecture PDF links")
            except BridgeError:
                raise
            except Exception as exc:  # noqa: BLE001 - class + location only, never content
                # Google refused or the network dropped while checking the
                # sheet: say so, and do not save a sheet that was never checked.
                return 502, {"ok": False, "error": "sheets_unavailable", "detail": describe_exception(exc)}

        _profiles.add_profile(target)
    except BridgeError as exc:
        return 400, {"ok": False, "error": exc.code, "detail": exc.detail}
    return 201, {"ok": True, "profile": target.public(), "checked": checked}


def _profiles_remove(controller: RunController, body: dict) -> tuple[int, dict]:
    from . import profiles as _profiles

    try:
        _profiles.remove_profile(str(body.get("name") or ""))
    except BridgeError as exc:
        return 400, {"ok": False, "error": exc.code, "detail": exc.detail}
    return 200, {"ok": True}


def read_json_body(handler: BaseHTTPRequestHandler) -> dict | None:
    """Bounded JSON object body, or None when invalid."""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return None
    if length < 0 or length > 16 * 1024:
        return None
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _same_origin(handler: BaseHTTPRequestHandler) -> bool:
    origin = handler.headers.get("Origin", "")
    host = handler.headers.get("Host", "")
    if handler.headers.get("X-Requested-With", "") != "sheets-bridge":
        return False
    if not origin:
        return True  # non-browser client on loopback
    parts = urlsplit(origin)
    return parts.scheme == "http" and parts.netloc == host


def make_handler(controller: RunController):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sheets-bridge-ui/0.1"

        def log_message(self, fmt, *args):  # quiet access log
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, render_page("/api"), "text/html; charset=utf-8")
            elif path.startswith("/api/"):
                status, payload = safe_api_response(controller, "GET", path[len("/api/"):], None)
                self._json(status, payload)
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if not _same_origin(self):
                self._json(403, {"ok": False, "error": "request rejected"})
                return
            body = read_json_body(self)
            if body is None:
                self._json(400, {"ok": False, "error": "invalid json"})
                return
            if not path.startswith("/api/"):
                self._json(404, {"ok": False, "error": "not found"})
                return
            status, payload = safe_api_response(controller, "POST", path[len("/api/"):], body)
            self._json(status, payload)

    return Handler


def _clean_options(body: dict, settings) -> dict:
    show_page_numbers = body.get("showPageNumbers", True)
    if type(show_page_numbers) is not bool:
        raise ValueError("showPageNumbers must be a boolean")
    limit = body.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
    row = body.get("row")
    if row is not None:
        if isinstance(row, bool) or not isinstance(row, int) or row < 2:
            raise ValueError("row must be >= 2")
    max_cost = body.get("maxCost")
    if max_cost is not None:
        if isinstance(max_cost, bool) or not isinstance(max_cost, (int, float)) or max_cost <= 0:
            max_cost = None
    if max_cost is None:
        max_cost = settings.max_cost_inr
    subject = str(body.get("subject") or "sheet").strip().lower()
    if subject != "sheet" and subject not in SUBJECTS:
        raise ValueError("bad subject")
    from notesbot import categories

    try:
        category = categories.normalize(body.get("category") or categories.DEFAULT)
    except ValueError:
        raise ValueError("bad category") from None
    sanskrit_style = str(body.get("sanskritStyle") or "hindi").strip().lower()
    if sanskrit_style not in ("hindi", "strict"):
        raise ValueError("bad sanskritStyle")
    model = str(body.get("model") or "").strip()
    if model:
        from .pw_transport import is_selectable

        # Fail closed: only ids the dropdown offers may reach the proxy.
        if not is_selectable(model):
            raise ValueError("unknown model")
    return {
        "dryRun": bool(body.get("dryRun")),
        "limit": limit,
        "row": row,
        "redo": bool(body.get("redo")),
        "showPageNumbers": show_page_numbers,
        "maxCost": float(max_cost) if max_cost else None,
        "subject": subject,
        "category": category,
        "sanskritStyle": sanskrit_style,
        "model": model,
        "profiles": _clean_profiles(body.get("profiles")),
    }


def _clean_profiles(raw: object) -> list:
    """Selected profile names. Empty means "decide from what is configured"."""
    if raw is None or raw == "":
        return []
    names = [raw] if isinstance(raw, str) else raw
    if not isinstance(names, list) or len(names) > 32:
        raise ValueError("profiles must be a list of names")
    cleaned = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("profile names must be non-empty strings")
        cleaned.append(name.strip())
    return cleaned


def serve(settings, *, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> int:
    controller = RunController(settings)
    class SingleInstanceServer(ThreadingHTTPServer):
        allow_reuse_address = False

    try:
        server = SingleInstanceServer((host, port), make_handler(controller))
    except OSError as exc:
        print(f"cannot start on port {port}: {exc.strerror or exc}.", flush=True)
        print("Another copy is already running - close it (or use --port N).", flush=True)
        return 2
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"Sheet automation UI: {url}  (Ctrl+C to quit)", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping ...", flush=True)
        controller.stop()
    finally:
        server.server_close()
    return 0
