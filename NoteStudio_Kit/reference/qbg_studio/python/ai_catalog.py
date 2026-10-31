"""The AI models card on Settings.

First choices per stage, what the PW proxy says is live, a one-request test,
and how each model has done on this computer. The proxy's catalogue is asked
only when the operator presses the button: Settings must open without a
network round trip, and the last answer is kept. The comparison reads the
saved readings and the usage ledger, never a workbook, so it is quick.
"""
import json
import time

import autopilot_model
import db

SETTING = 'ai_catalog'
TEST_SETTING = 'ai_model_test'
ERROR_SETTING = 'ai_models_error'
PATHS = ('/api/gpt/models', '/api/gemini/models')
DEFAULT_TEST_MODEL = autopilot_model.GEMINI
ROLE_LABELS = {'read': 'Reading pages', 'check': 'Independent check', 'solutions': 'Answers and solutions',
               'translation': 'Translation', 'tagging': 'Tagging'}


def names(body):
    """Model ids out of the shapes a catalogue endpoint tends to use."""
    if isinstance(body, dict):
        for key in ('models', 'data', 'result', 'items'):
            if key in body:
                return names(body[key])
        return [str(k) for k in body] if body and all(isinstance(v, dict) for v in body.values()) else []
    if isinstance(body, list):
        found = []
        for item in body:
            if isinstance(item, str):
                found.append(item)
            elif isinstance(item, dict):
                value = item.get('id') or item.get('name') or item.get('model')
                if value:
                    found.append(str(value))
        return found
    return []


def check():
    """Ask the proxy for its model lists and keep the answer."""
    import requests
    import pipeline_bridge as pb
    import pw_access
    import pw_auth
    result = dict(checked_at=time.time(), lists={}, errors=[])
    try:
        headers = pw_access._headers(pw_access._auth_token(pw_auth.token_provider()))
    except Exception as exc:
        result['errors'].append('Sign in to PW first: ' + pb.redact_secrets(str(exc))[:200])
        headers = None
    for path in PATHS if headers is not None else ():
        try:
            response = requests.get(pw_access.PROXY_BASE_URL + path, headers=headers, timeout=20, allow_redirects=False)
        except Exception as exc:
            result['errors'].append('%s: %s' % (path, pb.redact_secrets(str(exc))[:200]))
            continue
        if response.status_code != 200:
            result['errors'].append('%s: HTTP %d (the proxy has no such listing)' % (path, response.status_code))
            continue
        try:
            result['lists'][path] = names(response.json())
        except ValueError:
            result['errors'].append('%s: the reply was not JSON' % path)
    db.set_setting(SETTING, json.dumps(result))
    return result


def test_model(model):
    """One small billed request, so the operator can see a model answer."""
    import ai_transport
    import pipeline_bridge as pb
    started = time.monotonic()
    result = dict(model=model, tested_at=time.time(), ok=False, seconds=0.0, error='')
    try:
        text = ai_transport.request_json(model, 'Reply with the JSON object {"ok": true} and nothing else.',
                                         {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']},
                                         task_id='settings-model-test', count=1, filename='Settings model test',
                                         max_tokens=2048)
        result['ok'] = bool(json.loads(text).get('ok'))
    except Exception as exc:
        result['error'] = pb.redact_secrets(str(exc))[:300]
    result['seconds'] = round(time.monotonic() - started, 1)
    db.set_setting(TEST_SETTING, json.dumps(result))
    return result


def _stored(key):
    try:
        value = json.loads(db.get_setting(key, '') or '{}')
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _empty():
    return dict(papers=0, questions=0, flagged=0, calls=0, tokens=0, seconds=0.0, cost=0.0)


def comparison():
    """How each model has done here: readings from paper_imports, calls from
    the usage ledger."""
    stats = {}
    con = db.connect()
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'paper_imports' in have:
            for r in con.execute('SELECT model, records, analysis FROM paper_imports'):
                try:
                    records = json.loads(r['records'] or '[]')
                    analysis = json.loads(r['analysis'] or '{}')
                except ValueError:
                    continue
                if not records:
                    continue
                s = stats.setdefault(r['model'], _empty())
                s['papers'] += 1
                s['questions'] += int(analysis.get('questions') or len(records))
                s['flagged'] += sum(1 for x in records if isinstance(x, dict) and x.get('issues'))
        if 'usage_calls' in have:
            for r in con.execute('SELECT model, count(*) AS n, sum(tokens_in+tokens_out) AS t, sum(seconds) AS s, '
                                 'sum(cost_inr) AS c FROM usage_calls GROUP BY model'):
                s = stats.setdefault(r['model'], _empty())
                s['calls'] = r['n']
                s['tokens'] = r['t'] or 0
                s['seconds'] = r['s'] or 0.0
                s['cost'] = r['c'] or 0.0
    finally:
        con.close()
    rows = []
    for model, s in stats.items():
        rows.append(dict(model=model, label=autopilot_model.LABELS.get(model, model), **s,
                         flagged_pct=round(100 * s['flagged'] / s['questions']) if s['questions'] else None,
                         seconds_per_call=round(s['seconds'] / s['calls'], 1) if s['calls'] else None))
    rows.sort(key=lambda d: (-d['papers'], -d['calls']))
    return rows


def status():
    catalog = _stored(SETTING)
    import ai_transport
    budget = ai_transport.budget_status()
    if budget['using_gpt']:
        from datetime import datetime
        budget['until_text'] = datetime.fromtimestamp(budget['until']).strftime('%H:%M')
    return dict(chains=autopilot_model.chains(), roles=autopilot_model.ROLES, role_labels=ROLE_LABELS,
                labels={m: autopilot_model.label(m) for m in autopilot_model.LABELS},
                catalog=catalog, checked_at=catalog.get('checked_at'),
                test=_stored(TEST_SETTING), comparison=comparison(),
                error=db.get_setting(ERROR_SETTING, ''), gemini_budget=budget)
