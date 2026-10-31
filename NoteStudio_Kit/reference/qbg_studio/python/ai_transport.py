"""One door for every AI request: the model name chooses the transport.

Studio's AI stages used to call proxy_gpt directly. Gemini 3.6 Flash was
confirmed through the PW proxy on 24 September 2026 (JSON mode, a correct
Hindi translation, usage and cost in the envelope). On 25 September 2026 the
operator chose the best available Gemini for EVERY stage - reading, the
independent check, answers and solutions, translation, tagging, Word
equation pictures - with the proxy's other Gemini (2.5 Pro) next and GPT
only as the fallback on a rate limit. A reading is still checked by a
different model: the other Gemini. autopilot_model.ROLES holds the chain
per stage.

Gemini 3.6 Flash thinks before it answers and its thinking is billed as
output (257 thought tokens on a one-line task), so a short maxOutputTokens
truncates it; every caller here already allows thousands.

When Gemini's budget is spent (the proxy's "Budget has been exceeded"), the
same request goes to the first available GPT model instead, and Gemini is
left alone for BUDGET_PAUSE_SECONDS - the operator's rule of 28 September
2026: GPT stands in ONLY for a spent Gemini budget. A rate limit, a bad reply
or a sign-in problem never switches to GPT here; the callers' own retries and
the second Gemini deal with those as before.
"""
import time

import ai_request
import db
import proxy_gemini
import proxy_gpt

GPT_CHECKER = 'gpt-5.6-sol-ard-content'     # kept for saved batches that named it
BEST = proxy_gemini.BEST
SECOND = proxy_gemini.SECOND
# The GPT models that stand in for a spent Gemini budget, first available first.
GPT_FALLBACK = ('gpt-5.6-sol-ard-content', 'gpt-5.6-terra-ard-content', 'gpt-5.6-luna-ard-content')
BUDGET_PAUSE_SECONDS = 30 * 60
BUDGET_SETTING = 'gemini_budget_spent_until'


def is_gemini(model):
    return str(model or '').startswith('gemini')


def transport_for(model):
    if model in proxy_gemini.MODELS:
        return proxy_gemini
    if model in proxy_gpt.MODELS:
        return proxy_gpt
    raise ValueError('Choose an available AI model.')


def budget_spent(exc):
    """True for the proxy's "Budget has been exceeded" - a quota no wait clears."""
    return isinstance(exc, ai_request.ProxyQuotaError) and 'budget' in str(exc).lower()


def gemini_paused_until():
    """Epoch until which Gemini requests go straight to GPT (0 when they do not).
    Kept in the database, so a restart does not ask a spent budget again."""
    try:
        return float(db.get_setting(BUDGET_SETTING, '0') or 0)
    except (TypeError, ValueError):
        return 0.0


def budget_status():
    """What the AI models card says about a spent Gemini budget."""
    until = gemini_paused_until()
    return {'using_gpt': until > time.time(), 'until': until}


def _stand_in(call, cause):
    """The request on the first GPT model that answers; the last refusal otherwise."""
    last = cause
    for model in GPT_FALLBACK:
        if model not in proxy_gpt.MODELS:
            continue
        try:
            return call(model)
        except ai_request.ProxyQuotaError as exc:
            last = exc
    raise last or ai_request.ProxyQuotaError('The Gemini budget is spent and no GPT model is available.')


def _send(model, call):
    """call(model), with GPT standing in when - and only when - Gemini's budget is spent."""
    if not is_gemini(model):
        return call(model)
    if gemini_paused_until() > time.time():
        return _stand_in(call, None)
    try:
        return call(model)
    except ai_request.ProxyQuotaError as exc:
        if not budget_spent(exc):
            raise
        try:
            db.set_setting(BUDGET_SETTING, '%d' % (time.time() + BUDGET_PAUSE_SECONDS))
        except Exception:
            pass
        return _stand_in(call, exc)


def request_json(model, content, schema, *, task_id, count, filename, max_tokens=16384):
    return _send(model, lambda chosen: transport_for(chosen).request_json(
        chosen, content, schema, task_id=task_id, count=count, filename=filename, max_tokens=max_tokens))


def generate_content(*, model, contents, config, task_id, count, filename, timeout=300):
    return _send(model, lambda chosen: transport_for(chosen).generate_content(
        model=chosen, contents=contents, config=config, task_id=task_id, count=count, filename=filename,
        timeout=timeout))


def checker_for(read_model, chosen=''):
    """The model that independently checks a reading.

    An operator's explicit choice stands. Otherwise the best Gemini checks,
    whatever read: the operator's rule of 25 September 2026 is the best
    Gemini everywhere, and the first run that checked with the proxy's other
    Gemini (2.5 Pro, run 293) spent 24 minutes and about Rs 300 on 70
    check requests of 20-100 s each, four times this morning's read stage.
    The second opinion is a separate request with the pages in front of it.

    A batch saved with Gemini 2.5 Pro as its checker (every Autopilot batch
    before 30 September 2026 - Autopilot forced a checker other than the
    reader) is checked by the best Gemini now: 2.5 Pro cost Rs 7.15 a call
    against Rs 2.12 and was the largest single cost in the usage log. Any
    other saved choice (an old GPT checker) stands.
    """
    if chosen and chosen != SECOND:
        return chosen
    return BEST
