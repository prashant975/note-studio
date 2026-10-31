"""Count the AI requests an Autopilot run makes, per stage - for its scoreboard.

The operator wants to compare runs by time, cost and quality. Cost here is
counted where every AI request passes (proxy_gpt.request_json): each request,
whether it failed, how long it took, and its tokens when the PW proxy reports
them.

A stage sets measuring(run_id, stage) around its work. The context follows the
work into every worker thread, because the app's own ThreadPoolExecutor
(qbg_concurrency) copies it. Outside a measured stage nothing is recorded, and
a failure to record never affects the request itself.
"""
import contextvars
import time
from contextlib import contextmanager

import db

CURRENT = contextvars.ContextVar('qbg_ai_meter', default=None)   # (run_id, stage)
SCHEMA = '''CREATE TABLE IF NOT EXISTS ai_usage (
    run_id INTEGER NOT NULL, stage TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0, PRIMARY KEY (run_id, stage))'''


def initialize():
    con = db.connect()
    try:
        con.execute(SCHEMA)
    finally:
        con.close()


@contextmanager
def measuring(run_id, stage):
    token = CURRENT.set((int(run_id), str(stage)))
    try:
        yield
    finally:
        CURRENT.reset(token)


def record(requests=1, failed=0, prompt_tokens=0, output_tokens=0, seconds=0.0):
    """Add to the measured stage's counts. Never raises."""
    where = CURRENT.get()
    if not where:
        return
    try:
        con = db.connect()
        try:
            con.execute(SCHEMA)
            con.execute('INSERT INTO ai_usage(run_id,stage,requests,failed,prompt_tokens,output_tokens,seconds) '
                        'VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id,stage) DO UPDATE SET '
                        'requests=requests+excluded.requests, failed=failed+excluded.failed, '
                        'prompt_tokens=prompt_tokens+excluded.prompt_tokens, '
                        'output_tokens=output_tokens+excluded.output_tokens, seconds=seconds+excluded.seconds',
                        (*where, int(requests), int(failed), int(prompt_tokens or 0), int(output_tokens or 0),
                         float(seconds or 0)))
        finally:
            con.close()
    except Exception:
        pass


@contextmanager
def request():
    """Wrap one AI request: counts it, its time, and whether it failed.

    Usage: with ai_meter.request() as tokens: ...; tokens.update(prompt=.., output=..)
    """
    started = time.monotonic()
    tokens = {}
    try:
        yield tokens
    except BaseException:
        record(failed=1, seconds=time.monotonic() - started)
        raise
    record(prompt_tokens=tokens.get('prompt', 0), output_tokens=tokens.get('output', 0),
           seconds=time.monotonic() - started)


def usage(run_id):
    """{stage: {requests, failed, prompt_tokens, output_tokens, seconds}} plus 'total'."""
    con = db.connect()
    try:
        con.execute(SCHEMA)
        rows = con.execute('SELECT * FROM ai_usage WHERE run_id=?', (int(run_id),)).fetchall()
    finally:
        con.close()
    keys = ('requests', 'failed', 'prompt_tokens', 'output_tokens', 'seconds')
    result = {r['stage']: {k: r[k] for k in keys} for r in rows}
    result['total'] = {k: sum(v[k] for v in result.values()) for k in keys}
    return result
