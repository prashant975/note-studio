"""EXCERPT (read-only reference) from QBG Studio backend/studio/qbg_studio/app.py.
Only these top-level pieces are copied: _prepare, _startup, _nav_context, render, _go, _pw_guard, _same_site_only.
They show the pattern; they will not run on their own (imports and helpers are left out).
"""

# --- app.py:138 ---
def _prepare():
    """Idempotent. Called at import AND at startup so the app is usable no
    matter how it was launched (uvicorn, a test client, or an import)."""
    db.init()
    tagging.initialize()
    solutions.initialize()
    bulk_translations.initialize()
    report_sheets.initialize()
    paper_ai.initialize()
    autopilot.initialize()
    import video_tagging
    video_tagging.initialize()
    import sheet_tagging
    sheet_tagging.initialize()
    import verification
    verification.initialize()
    import usage_sheet
    usage_sheet.initialize()
    import team_data
    team_data.initialize()
    import celebrations
    celebrations.initialize()
    import job_queue
    job_queue.initialize()
    pb.apply_identity_setting()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    BATCHES.mkdir(parents=True, exist_ok=True)


# --- app.py:170 ---
@app.on_event("startup")
def _startup():
    _prepare()
    # Ask the status probes once, off the request path. Cold they cost about
    # four seconds of network round trips, and the connection strip is on
    # four screens - paying for that inside the first page render is what
    # made the app look like it had a broken backend.
    try:
        if not HOSTED:
            pb.warm_caches()
    except Exception:
        pass
    # Settled AI usage goes to the operator's own sheet while Studio runs. A
    # desktop app is closed often, so "Send now" on Settings covers the rest.
    try:
        if not HOSTED:
            import usage_sheet
            usage_sheet.start_writer()
    except Exception:
        pass
    # Each finished task's summary goes to the team's collection sheet, from
    # the desktop app and from every website workspace alike.
    try:
        import team_data
        team_data.start_writer()
    except Exception:
        pass
    # Team support: pull the lead's safe, signed fixes, apply them (and tell the
    # teammate) or hold them for approval, and push outcomes back. A teammate's
    # Studio only - the owner's laptop and an unconfigured build do nothing.
    try:
        import team_support
        team_support.initialize()
        if not HOSTED:
            team_support.start_poller()
    except Exception:
        pass
    # A creator can outlive the server on Windows. Keep its durable lease if
    # it is still alive; if it has definitely stopped, quarantine its claimed
    # rows before allowing any later creation run.
    recovery = db.reap_orphan_jobs()
    tagging.recover()
    solutions.recover()
    bulk_translations.recover()
    import video_tagging
    video_tagging.recover()
    import sheet_tagging
    sheet_tagging.recover()
    import verification
    verification.recover()
    # The queue (job_queue): what waited through a restart waits for the
    # operator's "Continue the queue"; then one entry at a time, by itself.
    import job_queue
    job_queue.recover()
    job_queue.start_pump()
    for job in recovery["stopped_creates"]:
        batch = db.get_batch(job["batch_id"])
        if not batch or not pb.xlsx_path(batch["out_dir"]).exists():
            continue
        try:
            jobs.quarantine_creating(
                batch["id"], batch["out_dir"],
                "The earlier creation process stopped. This question may "
                "already be on the portal; check it before doing anything else.")
        except Exception:
            # The orphan job remains marked failed and any live-create lease
            # is gone only after the child is proven dead. A locked workbook
            # will still be caught by the normal create gate.
            pass


# --- app.py:273 ---
def _nav_context():
    """What the shell shows, using only work that is already done.

    Every page renders the shell, so nothing in here may make a network
    call. SQLite is fast; the health badge comes from the cache that the
    Overview, the batch list and the bot tabs each warm when they render.
    """
    nav = {"running": "", "health_bad": 0,
           "translation_tag": "", "translation_cls": "",
           "solution_tag": "", "solution_cls": "",
           "settings_tag": "", "settings_cls": "",
           "autopilot_running": 0, "queue_waiting": 0, "queue_running": 0}
    try:
        # The Smart Queue's badge: how many wait, and whether it is running one now.
        import job_queue
        line = job_queue.query("SELECT state FROM job_queue WHERE state IN ('waiting','running')")
        nav["queue_waiting"] = sum(item["state"] == "waiting" for item in line)
        nav["queue_running"] = sum(item["state"] == "running" for item in line)
    except Exception:
        pass
    try:
        for job in db.active_jobs():
            # An Autopilot run owns the portal for its whole duration, so its
            # own stage jobs must not each claim the one chip in the top bar.
            if job["kind"] == "autopilot" or job["parent_job_id"]:
                nav["autopilot_running"] = job["parent_job_id"] or job["id"]
            if nav["running"]:
                continue
            if job["state"] == "orphaned":
                nav["running"] = _ORPHANED_LABELS.get(job["kind"],
                                                      "Bot needs checking")
            else:
                nav["running"] = _RUNNING_LABELS.get(job["kind"], "Running")
    except Exception:
        pass
    if nav["autopilot_running"]:
        nav["running"] = "Autopilot running"

    health = pb.health_cached()
    if health:
        nav["health_bad"] = health["bad"]
        if health["bad"]:
            nav["settings_tag"] = str(health["bad"])
            nav["settings_cls"] = "danger"

    # Rows waiting for each bot, only if the sheet was already read for
    # some other reason on this request cycle.
    queue = pb.studio_queue_status_cached()
    if queue and queue.get("ok"):
        for key, prefix in (("translation", "translation"), ("solution", "solution")):
            waiting = (queue.get("waiting") or {}).get(key)
            if waiting:
                nav[prefix + "_tag"] = str(waiting)
                nav[prefix + "_cls"] = ""
    return nav


# --- app.py:330 ---
def render(request, name, **ctx):
    ctx.setdefault("request", request)
    ctx.setdefault("preflight", pb.preflight())
    ctx.setdefault("nav", _nav_context())
    if name == "settings.html":
        import paper_ai
        ctx.setdefault("word_reader", paper_ai.word_reader())
    response = templates.TemplateResponse(name, ctx)

    # Never let a browser cache a screen in this app. Every page states the
    # CURRENT state of a batch - which buttons are live, what the gate allows,
    # how a run is going - and a cached copy shows the state from before the
    # last action. That is what made the three steps look stuck: preview or
    # pilot would succeed server-side, but going back to the create screen
    # re-showed the stale, greyed-out version of it.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# --- app.py:471 ---
def _go(path, toast="", kind="info"):
    """Redirect, optionally carrying a one-line message for a toast.

    app.js shows it and strips it from the address bar, so a refresh does not
    replay a message about something that already happened. Only for things
    that happen OFF screen - never to announce state the page already shows.
    """
    if toast:
        sep = "&" if "?" in path else "?"
        path = "%s%stoast=%s&kind=%s" % (
            path, sep, urllib.parse.quote(toast), urllib.parse.quote(kind))
    return RedirectResponse(path, 303)


# --- app.py:1383 ---
def _pw_guard(request, action, b=None):
    """Return a blocked response, or None when the action may proceed."""
    try:
        pb.require_pw(action)
        return None
    except pb.pw_auth.ProxyAuthError as exc:
        return render(request, "pw_blocked.html", b=b, action=action,
                      signin=_SIGNIN,
                      why=str(exc), pw=pb.pw_state(force=True))


# --- app.py:2961 ---
@app.middleware("http")
async def _same_site_only(request: Request, call_next):
    """Refuse a state-changing request that a page on another site sent.

    Studio has no login of its own, so it would otherwise act on a form that
    some other open tab auto-submitted to this port - and the operator's PW
    sign-in would let that start a bot or drop the sheet. Browsers label
    where a request came from (Sec-Fetch-Site, Origin): this app's own pages
    send same-origin / this host, so only those pass. A request with neither
    header is not from a browser (curl, a test client) and is left alone.
    """
    if request.method in ("POST", "PUT", "DELETE"):
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        crossed = site is not None and site not in ("same-origin", "none")
        if origin is not None and not _local_host(origin):
            crossed = True
        if crossed:
            return PlainTextResponse(
                "Refused: this request came from a page outside QBG One.",
                status_code=403)
    return await call_next(request)
