"""EXCERPT (read-only reference) from QBG Studio backend/studio/qbg_studio/pipeline_bridge.py.
Only these top-level pieces are copied: looks_technical, redact_secrets, _child_env, stream_command, stop_process.
They show the pattern; they will not run on their own (imports and helpers are left out).
"""

# --- pipeline_bridge.py:3348 ---
def looks_technical(text, strict=False):
    """True when a string is developer output, not something to show a user.

    This test was written three times as Jinja expressions across
    _events.html and _progress.html, with three different marker lists that
    had already drifted: the job.error path accepted a '{' that the row-detail
    path rejected, so a JSON body reaching job.error would have been printed
    to the operator. It is the filter in front of subprocess output, so it
    lives in one place and it fails closed.

    strict=True is for RAW process stdout - job.detail, and the per-line pass
    over a captured tail. Those paths additionally reject anything merely
    shaped like a log line, because none of it was written for a reader.
    Non-strict is for text jobs.py composed itself, which is allowed to use
    ordinary words like "error".
    """
    value = (text or "").strip()
    if not value:
        return True
    low = value.lower()
    markers = ["[errno", "winerror", "traceback", "exception", ".py",
               "\\", "{", "}", '["', "bearer", "token", "eyj"]
    if strict:
        if len(value) > 150:
            return True
        markers += ["error", "warn", "line ", "http "]
    return any(marker in low for marker in markers)


# --- pipeline_bridge.py:3377 ---
def redact_secrets(text):
    """Safe for job messages and workbook error cells.

    The app never deliberately prints a bearer token, but a proxy or a future
    library error can echo request headers.  Redact before anything reaches
    SQLite, review.xlsx, or a template.
    """
    value = str(text or "")
    value = _BEARER_RE.sub("Bearer [redacted]", value)
    return _JWT_RE.sub("[redacted session]", value)


# --- pipeline_bridge.py:3720 ---
def _child_env():
    env = dict(os.environ)
    # Neutralises the pipeline's U+2192 print inside its timeout handler,
    # which would otherwise abort a whole live batch on a cp1252 console.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    # Put pandoc/LibreOffice on the child's PATH when this process's own PATH
    # predates their installation.
    extra = _tool_paths()
    if extra:
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    # token_status() accepts a common pasted 'Bearer ' prefix or wrapping
    # quotes.  The real creator must receive that exact same cleaned value,
    # otherwise the UI can say the session is valid while the child sends
    # 'Bearer Bearer …' and every request is rejected.
    raw = env.get(C.TOKEN_ENV_VAR, "") or saved_portal_token()
    cleaned = clean_bearer_token(raw)
    if cleaned:
        env[C.TOKEN_ENV_VAR] = cleaned
    return env


# --- pipeline_bridge.py:3743 ---
def stream_command(argv, on_line, cwd=None, on_started=None, extra_env=None,
                   new_process_group=False):
    """Run a pipeline script, streaming stdout lines to on_line.

    Returns (returncode, full_output). Never raises on a non-zero exit.

    Every child gets its own process group on Windows. A Ctrl+C in Studio's
    console is delivered to every process in that console's group, and
    create_questions.py saves review.xlsx with a plain wb.save() after every
    row - an interrupt landing inside one of those saves leaves the sheet
    truncated. Its own group keeps the console's Ctrl+C away from the child.
    For the bots it is also what lets stop_process() deliver a Ctrl+Break to
    that child alone, the same interrupt the .bat launchers rely on.
    `new_process_group` is kept for its callers; it no longer changes
    anything.
    """
    env = _child_env()
    if extra_env:
        env.update(extra_env)
    if os.environ.get('QBG_HOSTED_WORKER') == '1':
        from hosted.task import child_command
        argv, env = child_command(argv, env)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if os.environ.get('QBG_HOSTED_WORKER') == '1':
            flags |= subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        argv, cwd=str(cwd or PIPE_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=flags)
    if on_started:
        try:
            on_started(proc.pid)
        except Exception:
            # The job remains queued rather than running if recording its PID
            # fails; a restart will keep the lease quarantined rather than
            # risk starting another creator.
            pass
    lines = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            try:
                on_line(line)
            except Exception:
                pass
    finally:
        proc.stdout.close()
        proc.wait()
    return proc.returncode, "\n".join(lines)


# --- pipeline_bridge.py:3859 ---
def stop_process(pid):
    """Stop a bot, and report success only once it is actually gone.

    The interrupt (Ctrl+Break, what the .bat windows rely on) goes first,
    because a bot killed outright mid-question can leave that question
    half-edited on the portal. But the bots install no Break handler, and in
    fast mode the controller's workers sit in their own process groups where
    the Break never reaches them - so after a bounded wait the whole process
    tree is killed with taskkill /T, or a worker could go on typing into the
    same Chrome as the next run. Returns False when the pid was already gone
    or could not be stopped, so a caller can tell 'stopping' from 'nothing
    there to stop'.
    """
    if not pid:
        return False
    pid = int(pid)
    if not db.pid_alive(pid):
        return False
    import signal
    try:
        if os.name == "nt":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.kill(pid, signal.SIGINT)
    except Exception:
        pass
    if _wait_gone(pid, 10):
        return True
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:
        return False
    return _wait_gone(pid, 5)
