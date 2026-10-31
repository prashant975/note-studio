"""EXCERPT (read-only reference) from QBG Studio backend/studio/qbg_studio/db.py.
Only these top-level pieces are copied: DB_PATH, EXCLUSIVE_KINDS, connect, init, get_setting, set_setting, claim_job, claim_global_job, reap_orphan_jobs.
They show the pattern; they will not run on their own (imports and helpers are left out).
"""

# --- db.py:34 ---
DB_PATH = (Path(os.environ["QBG_STUDIO_DB_PATH"]).resolve()
           if os.environ.get("QBG_STUDIO_DB_PATH", "").strip()
           else (Path(os.environ["QBG_WORKSPACE"]) / "qbg_studio.db"
                 if os.environ.get("QBG_HOSTED_WORKER") == "1"
                 else Path(__file__).parent / "data" / "qbg_studio.db"))


# --- db.py:45 ---
EXCLUSIVE_KINDS = (
    'autopilot', 'create', 'bot-translation', 'bot-solution', 'translation-run',
    'tagging', 'tagging-write', 'tagging-sync',
    'solution-run', 'solution-write', 'solution-check',
    'bulk-translation', 'bulk-translation-write', 'bulk-translation-check',
    'video-tagging', 'sheet-tagging', 'verification', 'hindi-existing')


# --- db.py:201 ---
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


# --- db.py:209 ---
def init():
    con = connect()
    try:
        con.executescript(SCHEMA)
        # Existing Studio databases predate these columns.  SQLite does not
        # support ADD COLUMN IF NOT EXISTS, so keep the migration deliberately
        # small and idempotent.
        columns = {r["name"] for r in con.execute("PRAGMA table_info(batches)")}
        for name, spec in (
            ("exam_category", "TEXT NOT NULL DEFAULT 'NEET-JEE'"),
            ("upload_mode", "TEXT NOT NULL DEFAULT 'english'"),
            ("review_ready", "INTEGER NOT NULL DEFAULT 0"),
            ("preview_signature", "TEXT NOT NULL DEFAULT ''"),
            ("pilot_signature", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                con.execute("ALTER TABLE batches ADD COLUMN %s %s" % (name, spec))
        run_columns = {r["name"] for r in
                       con.execute("PRAGMA table_info(translation_runs)")}
        if "retry_pass" not in run_columns:
            con.execute("ALTER TABLE translation_runs "
                        "ADD COLUMN retry_pass INTEGER NOT NULL DEFAULT 0")
        job_columns = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
        if "pid" not in job_columns:
            con.execute("ALTER TABLE jobs ADD COLUMN pid INTEGER")
        # Real counters, so a screen can say "row 12 of 48" and work out a
        # time left. pct alone cannot: for a bot it is a bar that moves, not
        # a fraction of the work.
        for name, spec in (
            ("done_n", "INTEGER NOT NULL DEFAULT 0"),
            ("total_n", "INTEGER NOT NULL DEFAULT 0"),
            ("errors_n", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in job_columns:
                con.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (name, spec))
        # The Autopilot job a stage job belongs to (see EXCLUSIVE_KINDS).
        if "parent_job_id" not in job_columns:
            con.execute("ALTER TABLE jobs ADD COLUMN parent_job_id INTEGER")
        if os.environ.get("QBG_HOSTED_WORKER") != "1":
            _relocate_personal_package(con)
    finally:
        con.close()


# --- db.py:406 ---
def get_setting(key, default=""):
    """A small key/value store for choices made on the Settings screens.

    Deliberately separate from the pipeline's config.py: that file is the
    pipeline's own, and the sheet a bot points at is not part of it.
    """
    con = connect()
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?",
                          (key,)).fetchone()
        return row["value"] if row and row["value"] else default
    except sqlite3.OperationalError:
        # Table not created yet - a setting nobody has saved is the default.
        return default
    finally:
        con.close()


# --- db.py:424 ---
def set_setting(key, value):
    con = connect()
    try:
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value or "")))
    except sqlite3.OperationalError:
        init()
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value or "")))
    finally:
        con.close()


# --- db.py:478 ---
def claim_job(kind, batch_id=None, message="", global_create=False):
    """Atomically reserve a background job.

    A check followed by INSERT is unsafe: two tabs can both observe no active
    job and start two creators over the same workbook.  The immediate SQLite
    transaction makes the decision serial, and the partial unique index above
    is a second, durable backstop for live creation across every batch.

    Returns ``(job_id, None)`` on success, or ``(None, reason)`` when a job
    must not be started.
    """
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        if batch_id is not None:
            active = con.execute(
                "SELECT id, kind FROM jobs WHERE batch_id=? AND state IN "
                "('queued','running','orphaned') ORDER BY id DESC LIMIT 1",
                (batch_id,)).fetchone()
            if active:
                con.execute("ROLLBACK")
                return None, "This batch already has a job running."
        if global_create:
            active = _exclusive_active(con, _parent_job())
            if active:
                con.execute("ROLLBACK")
                return None, ("Another QBG portal job is already running. "
                              "Wait for it to finish or be checked before "
                              "starting this one.")
        job_id = _insert_job(con, kind, batch_id, message)
        con.execute("COMMIT")
        return job_id, None
    except sqlite3.IntegrityError:
        # The unique live-create index is intentionally allowed to win any
        # last-moment race that escaped the read above.
        try:
            con.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        return None, ("Another batch is already creating questions on the "
                      "portal. Wait for it to finish or be checked first.")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        con.close()


# --- db.py:549 ---
def claim_global_job(kind, message=""):
    """Reserve a job that belongs to no batch - a bot run.

    Only one portal-writing job at a time.  Translation fast mode has two
    child tabs inside its one controller, but a Solution Bot or a creator
    running beside it can still seize the same logged-in QBG session.
    """
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        active = _exclusive_active(con, _parent_job())
        if active:
            con.execute("ROLLBACK")
            if active["kind"] == kind:
                return None, "That bot is already running."
            return None, ("Another QBG portal job is already running. Wait for "
                          "it to finish before starting this bot.")
        job_id = _insert_job(con, kind, None, message)
        con.execute("COMMIT")
        return job_id, None
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        con.close()


# --- db.py:823 ---
def reap_orphan_jobs():
    """Recover jobs after a Studio restart without releasing a live child.

    A server can die while its ``create_questions.py`` child keeps running.
    Such a child must retain the global live-create lease.  Jobs with a known,
    dead child are returned so the caller can quarantine any workbook rows
    left in ``Creating``.  Old create jobs without a recorded PID are
    deliberately kept as ``orphaned`` until a person confirms recovery.

    The bots outlive the server the same way (each runs in its own process
    group), and a bot still driving the operator's Chrome must keep the
    portal lease too, or a second bot could be started into the same
    session.  A bot job stays ``orphaned`` only while its recorded pid is
    alive - there is no recovery form for bots, so a pid-less or dead one is
    simply failed - and a bot already ``orphaned`` by an earlier restart is
    re-checked here, which is how it is released once its child has ended.
    """
    con = connect()
    stopped_creates, retained_creates = [], []
    bot_kinds = ("bot-translation", "bot-solution", "translation-run")
    try:
        rows = con.execute(
            "SELECT * FROM jobs WHERE state IN ('queued','running')"
            " OR (state='orphaned' AND kind IN (?,?,?))", bot_kinds).fetchall()
        now = time.time()
        for row in rows:
            data = dict(row)
            if row["kind"] in bot_kinds:
                if row["pid"] and _pid_alive(row["pid"]):
                    con.execute(
                        "UPDATE jobs SET state='orphaned',"
                        " error='Studio restarted while this bot was still running. It keeps the portal until it stops; press Stop to end it now.'"
                        " WHERE id=?", (row["id"],))
                else:
                    con.execute(
                        "UPDATE jobs SET state='failed', finished_at=?,"
                        " error='The server stopped while this was running. It was not retried.'"
                        " WHERE id=?", (now, row["id"]))
                continue
            if row["kind"] != "create":
                con.execute(
                    "UPDATE jobs SET state='failed', finished_at=?,"
                    " error='The server stopped while this was running. It was not retried.'"
                    " WHERE id=?", (now, row["id"]))
                continue
            if row["pid"] and not _pid_alive(row["pid"]):
                con.execute(
                    "UPDATE jobs SET state='failed', finished_at=?,"
                    " error='The earlier creator has stopped. Any question left mid-send must be checked on the portal.'"
                    " WHERE id=?", (now, row["id"]))
                stopped_creates.append(data)
            else:
                con.execute(
                    "UPDATE jobs SET state='orphaned',"
                    " error='Studio restarted while a creator may still be running. No new questions can be sent until this is checked.'"
                    " WHERE id=?", (row["id"],))
                retained_creates.append(data)
    finally:
        con.close()
    return {"stopped_creates": stopped_creates,
            "retained_creates": retained_creates}
