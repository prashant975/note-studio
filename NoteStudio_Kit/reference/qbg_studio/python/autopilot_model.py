"""Ordered model chains for Autopilot, so a failed stage retries with another.

One place decides which model each stage tries first and what it falls back to.
A provider is only ever changed BETWEEN attempts, never inside one request, and
every attempt records the model it used. Since 25 September 2026 the best
Gemini the PW proxy serves leads every stage (the operator's choice), the
proxy's other Gemini follows, and the GPT models are the fallbacks.
"""
import json

import db
import paper_ai
import proxy_gpt
import solution_service
import tagging_model
import bulk_translation_model

SETTING = 'autopilot_model_chains'

# role -> (ordered default chain, the models that role is allowed to use)
# A chain saved before 20 September 2026 that still names a retired Gemini
# model simply loses it (_clean keeps allowed models only).
import proxy_gemini
SOL, TERRA, LUNA = 'gpt-5.6-sol-ard-content', 'gpt-5.6-terra-ard-content', 'gpt-5.6-luna-ard-content'
GEMINI = proxy_gemini.BEST
GEMINI_2 = proxy_gemini.SECOND
GPT = (SOL, TERRA, LUNA)
# The best Gemini first for EVERY stage (the operator's choice, 25 September
# 2026), the check included. Gemini 2.5 Pro comes LAST, after the GPT models
# (the operator's choice, 30 September 2026): in the usage log of 24-29
# September it cost Rs 7.15 a call and took 63 s, against Rs 2.12 and 24 s for
# 3.6 Flash and Rs 1.55-1.93 for GPT Sol and Terra - Rs 450 on 63 calls, most
# of it Autopilot checking readings with it.
ROLES = {
    'read': ((GEMINI, SOL, TERRA, LUNA, GEMINI_2), tuple(paper_ai.MODELS)),
    'check': ((GEMINI, TERRA, SOL, LUNA, GEMINI_2), tuple(paper_ai.MODELS)),
    'solutions': ((GEMINI, SOL, TERRA, LUNA, GEMINI_2), tuple(solution_service.MODELS)),
    'translation': ((GEMINI, LUNA, TERRA, SOL, GEMINI_2), tuple(bulk_translation_model.MODELS)),
    'tagging': ((GEMINI, SOL, TERRA, LUNA, GEMINI_2), tuple(tagging_model.MODELS)),
}
LABELS = dict(proxy_gpt.MODELS)
LABELS.update(proxy_gemini.MODELS)
# Chains saved before 25 September 2026 still put GPT first for the check and
# the answers; they are set aside once, so every stage starts from Gemini.
# v2: the same again after run 293, so a saved "2.5 Pro checks" order goes too.
# v3: once more on 30 September 2026, for "2.5 Pro last" (see ROLES).
RESET_MARKER = '_chains_gemini_everywhere_v3'


def label(model):
    return LABELS.get(model, model) + ' · ' + model if model in LABELS else model


def _clean(role, values):
    """Keep supported models, in order, without repeats; never return empty."""
    allowed = ROLES[role][1]
    chain = [m for m in dict.fromkeys(values) if m in allowed]
    # Gemini 2.5 Pro is always the last resort, in saved runs' orders too
    # (run 302's order still had it second, for its repairs and retries).
    chain = [m for m in chain if m != GEMINI_2] + [m for m in chain if m == GEMINI_2]
    return tuple(chain) or tuple(m for m in ROLES[role][0] if m in allowed) or (allowed[0],)


def usable(role, values):
    """The models a run may actually use now.

    A run started before a model was withdrawn keeps that name in its saved
    configuration; handing it to a stage fails the whole range with "Choose one
    of the listed models" (run 184, Biology Q91-180, after Gemini was removed
    on 20 September 2026). The saved order is kept, unknown names are dropped,
    and the current default chain stands in if nothing is left.
    """
    return _clean(role, values or ())


def chains():
    """The saved chains, filled in from the defaults for anything missing."""
    try:
        saved = json.loads(db.get_setting(SETTING, '{}'))
        if not isinstance(saved, dict):
            saved = {}
    except ValueError:
        saved = {}
    if not db.get_setting(RESET_MARKER, ''):
        # Once: chains saved under the old policy (GPT checks and answers)
        # give way to the Gemini-first defaults; later choices are kept.
        if saved:
            db.set_setting(SETTING, '{}')
        db.set_setting(RESET_MARKER, '1')
        saved = {}
    result = {}
    for role, (default, _allowed) in ROLES.items():
        values = saved.get(role)
        result[role] = _clean(role, values if isinstance(values, list) else default)
    return result


def chain(role, first=''):
    """The chain for one role, optionally with an explicit first choice."""
    if role not in ROLES:
        raise ValueError('Unknown Autopilot model role.')
    values = list(chains()[role])
    if first:
        if first not in ROLES[role][1]:
            raise ValueError('Choose a supported model for ' + role + '.')
        values = [first] + [m for m in values if m != first]
    return tuple(values)


def save(values):
    """values: {role: [model, ...]}. Unknown roles and models are dropped."""
    if not isinstance(values, dict):
        raise ValueError('Choose a model for each stage.')
    saved = {role: list(_clean(role, values.get(role) or ROLES[role][0])) for role in ROLES}
    db.set_setting(SETTING, json.dumps(saved))
    return saved
