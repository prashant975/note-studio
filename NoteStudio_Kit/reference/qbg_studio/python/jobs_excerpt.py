"""EXCERPT (read-only reference) from QBG Studio backend/studio/qbg_studio/jobs.py.
Only these top-level pieces are copied: request_stop, stop_requested, check_gate, _finish, _run_guarded.
They show the pattern; they will not run on their own (imports and helpers are left out).
"""

# --- jobs.py:48 ---
def request_stop(job_id):
    """Record a Stop for a job whose child may already be gone."""
    _STOP_REQUESTED.add(int(job_id))
    try:
        # A Stop on the task the queue is running holds the queue (job_queue).
        import job_queue
        job_queue.note_stop(job_id)
    except Exception:
        pass


# --- jobs.py:59 ---
def stop_requested(job_id):
    return int(job_id) in _STOP_REQUESTED


# --- jobs.py:389 ---
def check_gate(batch, action):
    """Raise GateError unless this creation action is allowed right now."""
    try:
        pb.EC.category_id(batch['exam_category'])
    except ValueError as exc:
        raise GateError(str(exc)) from exc
    gate = batch["gate"]
    out_dir = batch["out_dir"]
    if dict(batch).get('upload_mode') in pb.ML.MODES:
        rows = pb.read_rows(out_dir)
        if not rows or any(not r.get('upload_languages') for r in rows):
            raise GateError('This batch is missing its language workbook. Re-extract the supplied papers.')
    if action == "preview":
        return
    # The Hindi reading of a two-language paper never creates questions of its
    # own: they go up from the English batch, both languages under one QBG id.
    merged = db.get_settings(batch['id']).get('merged_into')
    if merged:
        raise GateError('This batch is the Hindi side of batch %s. Its questions are uploaded from that batch, '
                        'as one question in both languages.' % merged)
    # Any-format input uses Preview -> Edit -> Upload, with automatic validation.
    con = db.connect()
    try:
        has_ai = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_imports'").fetchone()
        source = con.execute('SELECT * FROM paper_imports WHERE batch_id=?',(batch['id'],)).fetchone() if has_ai else None
    finally:
        con.close()
    if source:
        # Content in another language than the batch's (Hindi text, English
        # chosen) goes up only on the operator's yes (paper_ai.language_question).
        import paper_ai
        question = paper_ai.language_question(batch['id'])
        if question and question['mismatch'] and not question['confirmed']:
            raise GateError(paper_ai.language_message(question))
    if not batch["review_ready"]:
        raise GateError("This paper has not been prepared for human review yet. "
                        "Open Review, then approve the questions you have checked.")
    global_problems = global_report_problems(out_dir)
    if global_problems:
        raise GateError("The question paper and answer key still disagree. "
                        "Resolve the reported document problem and read the "
                        "paper again before creating anything.")
    c = pb.counts(out_dir)
    for row in pb.read_rows(out_dir):
        if source and row.get('paper_language','english') != dict(source).get('language','english'):
            raise GateError('The workbook language differs from the uploaded paper. Read the saved source again before uploading.')
        if row.get('upload_languages') and row['status'] == pb.STATUS['pick']:
            try:
                hi = pb.ML.hindi_for(row)
                errors = pb.ML.content_errors(row, hi, row['upload_languages'])
                valid = row.get('language_approval') == pb.ML.signature(row, hi, row['upload_languages'])
            except ValueError as exc:
                errors, valid = [str(exc)], False
            if errors or not valid:
                raise GateError('Review and approve every language again for '+row['row_ref']+'.')
    if c["creating"]:
        raise GateError(
            "%d question(s) are still marked as being created from an earlier "
            "run. Those may already exist on the portal. Open the batch, check "
            "them on the portal, and sort them out before creating anything "
            "else." % c["creating"])
    if c["needs_check"]:
        raise GateError(
            "%d question(s) need checking on the portal before anything else "
            "is created. They may already exist there. Nothing will be sent "
                "until that is zero." % c["needs_check"])

    # Duplicates used to stop the whole run here. They are now refused
    # one row at a time by create_questions.py, in the moment before it
    # sends - which catches strictly more of them (identical question
    # text as well as identical refs, and every row rather than the
    # pilot's first few) and still sends nothing. Blocking the batch as
    # well would only mean the operator never gets the report saying
    # which ones were duplicates.
    # Another paper's unsettled row is NOT this paper's problem. It used to
    # stop creation everywhere, so one interrupted question froze every other
    # paper in the app until someone went and settled it - which is a very
    # long way from the duplicate it was guarding against. This paper's own
    # unsettled rows are still refused, just above, and create_questions
    # refuses a duplicate row by row, by text, across every paper. That is
    # where the protection belongs. Elsewhere it is said, not enforced:
    # other_paper_risk() puts it on the screen.
    if source:
        if action!='full':raise GateError('Use Upload to QBG on the paper preview.')
        import paper_ai
        selected=[r for r in pb.read_rows(out_dir) if r['status']==pb.STATUS['pick']]
        if not selected:raise GateError('No questions are ready to upload from this preview.')
        for row in selected:
            # The preview's own rule (paper_ai.ready_to_send): a note about the
            # picture travels with the question; batch 65 was refused for them.
            if row.get('qbg_id') or not paper_ai.ready_to_send(row):
                raise GateError('Correct the marked content in '+row['row_ref']+' before uploading.')
        if not batch['preview_signature'] or batch['preview_signature']!=pending_signature(out_dir):
            raise GateError('The paper changed. Open its preview before uploading.')
        return
    size = pilot_size()

    if action == "pilot":
        if not size:
            raise GateError("The pilot step is switched off, so there is "
                            "nothing for it to do. Preview, then create.")
        if gate not in ("previewed", "piloted"):
            raise GateError("Look at the preview first, so you can see "
                            "exactly what would be sent.")
        if (not batch["preview_signature"] or
                batch["preview_signature"] != pending_signature(out_dir, size)):
            raise GateError("The approved questions changed after the preview. "
                            "Preview this exact group again before running the pilot.")
        return

    if action == "full":
        if not size:
            # No pilot: the preview is the only thing standing in front of a
            # live send, so it must cover exactly what is about to go.
            if gate not in ("previewed", "piloted"):
                raise GateError("Look at the preview first, so you can see "
                                "exactly what would be sent.")
            if (not batch["preview_signature"] or
                    batch["preview_signature"] != pending_signature(out_dir)):
                raise GateError("The approved questions changed after the "
                                "preview. Preview this exact group again "
                                "before creating.")
            return
        if gate != "piloted":
            raise GateError("Run the %d-question pilot first and check those "
                            "on the portal, before creating the rest." % size)
        if (not batch["pilot_signature"] or
                batch["pilot_signature"] != pending_signature(out_dir)):
            raise GateError("The approved questions changed after the pilot. "
                            "Preview and run a fresh pilot before creating the rest.")
        return
    raise GateError("Unknown action.")


# --- jobs.py:524 ---
def _finish(job_id, ok, message, error=""):
    # Redact here, not at each call site. Every _friendly_*_error() builds its
    # text from the last few lines of raw subprocess output, and a proxy or a
    # library can echo a request header into that. redact_secrets() says it
    # runs "before anything reaches SQLite" - this is the one place that is
    # true for a job's error, and the Create screen shows that error verbatim
    # inside a technical-detail disclosure.
    db.update_job(job_id, state="done" if ok else "failed", pct=100,
                  message=pb.redact_secrets(message),
                  error=pb.redact_secrets(error), finished_at=time.time())
    _STOP_REQUESTED.discard(int(job_id))


# --- jobs.py:537 ---
def _run_guarded(step, job_id, body, *args):
    """Run a job body so that the job ALWAYS reaches _finish.

    A BackgroundTask that raises simply disappears: the job row stays
    'running' with no finished_at, running_job()/claim_job() treat the batch
    as busy for ever, and nothing in the UI can clear it until a restart.
    Seen for real when review.xlsx was open in Excel during a convert.
    Whatever the body raises after the claim, the job is failed with the
    reason instead.
    """
    try:
        body(job_id, *args)
    except Exception as exc:
        try:
            job = db.get_job(job_id)
            if job and job["state"] in ("done", "failed"):
                return          # the body finished it before the error
        except Exception:
            pass
        _finish(job_id, False, "%s stopped unexpectedly" % step,
                pb.redact_secrets(str(exc) or exc.__class__.__name__))
