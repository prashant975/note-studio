"""`python -m sheets_bridge --test-model`: one tiny live call through the PW proxy.

Signs in with Google, checks the allowlist, then asks the model for one word.
That call is a real (cheap) provider call, so it must appear as a new row in
the `Usage Cost 2.0` tab (rolled up per task) - which makes this the onboarding's live test.

Never prints the token or any document content.
"""
from __future__ import annotations

import re
import time

import pw_access

from .errors import BridgeError, describe_exception
from notesbot.errors import ModelBudgetError

# A reasoning model spends part of its output budget thinking before it writes.
# At 16 tokens a GPT-5.6 model can use all of them up and answer nothing, which
# would read as a broken route. A few hundred still costs a fraction of a rupee.
PROBE_MAX_TOKENS = 512
PROBE_PROMPT = "Reply with the single word: OK"
# One more try for a gateway blip; a real outage fails both.
PROBE_RETRY_DELAY_SECONDS = 5.0


def _probe(model: str):
    from notesbot import gemini_client

    return gemini_client._lite_client().chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [{"type": "text", "text": PROBE_PROMPT}]}],
        max_tokens=PROBE_MAX_TOKENS,
    )


def response_shape(value, path: str = "", out: list | None = None, depth: int = 0) -> list:
    """`choices[0].message.content: str 'OK'` lines for a proxy reply.

    Used only for the fixed probe prompt, so short string values are safe to
    show; they are cut at 60 characters regardless.
    """
    out = [] if out is None else out
    if depth > 6 or len(out) > 60:
        return out
    if isinstance(value, dict):
        if not value:
            out.append(f"{path or '.'}: {{}}")
        for key, item in value.items():
            response_shape(item, f"{path}.{key}" if path else str(key), out, depth + 1)
    elif isinstance(value, list):
        if not value:
            out.append(f"{path}: []")
        for index, item in enumerate(value[:3]):
            response_shape(item, f"{path}[{index}]", out, depth + 1)
    elif isinstance(value, str):
        out.append(f"{path}: str {value[:60]!r}")
    else:
        out.append(f"{path}: {type(value).__name__} {value!r}"[:120])
    return out


PDF_PROBE_FACT = ("Newton's first law: a body stays at rest or in uniform motion "
                  "unless a net external force acts on it.")


def _probe_pdf(model: str):
    """A one-page PDF plus the app's real notes schema: the shape of every
    generation call. A text probe cannot show that the route accepts a PDF
    attachment and strict JSON - and a batch fails on every row if it does not.
    """
    import base64

    from fpdf import FPDF
    from notesbot import config as notes_config
    from notesbot import gemini_client
    from notesbot.gemini_client import _generation_schema_json
    from notesbot.prompts import ShortNotes

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=14)
    pdf.multi_cell(0, 10, "Lecture 1 - Laws of motion\n\n" + PDF_PROBE_FACT)
    data = base64.b64encode(bytes(pdf.output())).decode("ascii")
    return gemini_client._lite_client().chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": (
                "Make concise study notes from the attached one-page PDF, using only what "
                "the page teaches. Put that teaching in one section with source_local_page 1 "
                "and source_order 1.")},
            {"type": "file", "file": {"filename": "probe.pdf",
                                      "file_data": f"data:application/pdf;base64,{data}"}},
        ]}],
        max_tokens=8192,
        # The same sampling and thinking settings the generator sends, so a
        # parameter the model refuses shows up here rather than on every row.
        temperature=0.2,
        extra_body={"reasoning_effort": notes_config.LITELLM_REASONING_EFFORT},
        response_format={"type": "json_schema", "json_schema": {
            "name": "ShortNotes", "schema": _generation_schema_json(ShortNotes), "strict": True}},
    )


def check_pdf_probe(response) -> str:
    """'' when the reply is valid notes that read the PDF, else the reason."""
    from notesbot.gemini_client import _strip_json_fences
    from notesbot.prompts import ShortNotes

    text = (response.choices[0].message.content or "").strip()
    if not text:
        return "the model returned no notes JSON"
    try:
        notes = ShortNotes.model_validate_json(_strip_json_fences(text))
    except Exception as exc:  # noqa: BLE001 - the reply is the fixed probe's
        return f"the notes JSON did not match the app's schema ({type(exc).__name__})"
    from .coverage import _notes_text

    # Every visible field, not just headings and bullets: the model is free to
    # put the page's teaching in key terms, notes or a table, and did so on a
    # live run - a headings-and-points check called that "unread".
    body = _notes_text(notes).lower()
    if not notes.sections:
        return "the notes JSON had no sections"
    if not any(word in body for word in ("force", "rest", "motion", "newton", "inertia")):
        # The probe page is fixed, so its notes are safe to quote.
        return ("the notes do not mention what the PDF says - the attachment may not have "
                f"been read (notes text: {' '.join(body.split())[:120]!r})")
    return ""


def _transient(exc: BaseException) -> bool:
    from openai import APIConnectionError, APITimeoutError

    return isinstance(exc, (APIConnectionError, APITimeoutError))


def describe_probe_failure(model: str, exc: BaseException, elapsed: float, attempts: int) -> str:
    """One actionable line: status, what it means, and the proxy's own words.

    The probe prompt is fixed, so the proxy's text cannot contain lecture
    material - which is what makes it safe to show here and nowhere else.
    """
    from .pw_openai import STATUS_MEANING

    status = getattr(exc, "proxy_status", 0)
    if status:
        what = f"the proxy answered HTTP {status}"
        meaning = STATUS_MEANING.get(status)
        if meaning:
            what += f" ({meaning})"
    else:
        what = f"no answer from the proxy ({describe_exception(exc)})"
    tries = "" if attempts == 1 else f", {attempts} attempts"
    line = f"{model} could not be reached: {what} after {elapsed:.0f}s{tries}"
    detail = getattr(exc, "proxy_detail", "")
    if detail:
        line += f". Proxy said: {detail}"
    return line + ". Pick a Gemini model in the dropdown to run now."


def preflight(model: str, *, sleep=time.sleep) -> str:
    """Prove a newly selected model's route answers. '' means good to go.

    Only `gpt-` models are checked: the Gemini route is what every production
    run has used. A wrong route or a missing proxy permission would otherwise
    be discovered one failed row at a time, after the batch had already
    claimed rows and written failure codes into the sheet.
    """
    from .pw_openai import is_gpt_model
    from .pw_openai import route as gpt_route
    from .pw_transport import ProxyRequestError, set_task

    if not is_gpt_model(model):
        return ""
    set_task(task_id=pw_access.new_task_id("preflight"), filename="preflight",
             input_unit="No. of pages", count=1)
    started = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        try:
            response = _probe(model)
            break
        except (ProxyRequestError, ModelBudgetError) as exc:  # app-written text, safe to show
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            if attempts == 1 and _transient(exc):
                sleep(PROBE_RETRY_DELAY_SECONDS)
                continue
            return describe_probe_failure(model, exc, time.monotonic() - started, attempts)
    choice = response.choices[0]
    if not (choice.message.content or "").strip():
        why = " (it spent its whole output budget)" if choice.finish_reason == "length" else ""
        return f"{model} returned no text through {gpt_route()}{why}"

    # A plain-text answer does not prove a notes call works: the first live GPT
    # run passed the text probe, while every real call - PDF, strict schema,
    # the generator's sampling settings - came back HTTP 400. One small
    # notes-shaped call catches that before any row is claimed.
    started = time.monotonic()
    try:
        problem = check_pdf_probe(_probe_pdf(model))
    except (ProxyRequestError, ModelBudgetError) as exc:
        said = getattr(exc, "proxy_detail", "")
        return f"{model} refused a notes-shaped call: {exc}" + (f". Proxy said: {said}" if said else "")
    except Exception as exc:  # noqa: BLE001
        return describe_probe_failure(model, exc, time.monotonic() - started, 1)
    if problem:
        return f"{model} answered a notes-shaped call, but {problem}"
    return ""


def _sign_in(settings, *, interactive: bool):
    """(access, model, drive_service) after sign-in, allowlist and transport
    install; None on failure."""
    from .google_auth import build_services, load_credentials, token_provider, user_email
    from .pw_gate import ProxyAccess

    try:
        creds = load_credentials(settings.client_secret_path, settings.token_path, interactive=interactive)
    except BridgeError as exc:
        print(f"[!!]  {exc}")
        return None
    _sheets, drive_service = build_services(creds)
    access = ProxyAccess(token_provider(creds), email=user_email(creds))
    print(f"account    : {access.email or '(email not exposed by the token)'}")
    if access.email and not access.domain_ok():
        print(f"[!!]  {access.email} is not an @pw.live account - the proxy will refuse it")
        return None
    status = access.allow_status()
    print(f"allowlist  : {status}")
    if status != "allowed":
        return None
    model = access.install_transport()
    print(f"model      : {model}")
    return access, model, drive_service


_DRIVE_ID = re.compile(r"(?:/d/|[?&]id=)([A-Za-z0-9_-]{20,})")


def _lecture_bytes(source: str, drive_service, max_bytes: int) -> tuple[str, bytes]:
    """(name, pdf bytes) for a local path, a Drive file id, or a Drive link.

    Fetching by id uses the same DriveClient and token as a batch run, so the
    probe sees exactly the file the failed row saw.
    """
    from pathlib import Path

    from .drive import DriveClient

    local = Path(source)
    if local.is_file():
        return local.name, local.read_bytes()
    match = _DRIVE_ID.search(source)
    file_id = match.group(1) if match else (source.strip() if re.fullmatch(r"[A-Za-z0-9_-]{20,}", source.strip()) else "")
    if not file_id:
        raise BridgeError("not_found", f"{source} is neither a local file nor a Drive id/link")
    client = DriveClient(drive_service)
    file = client.get_file(file_id)
    data = client.download_pdf(file, max_bytes)
    # Keep a copy beside the app's other state, so the pages can be looked at
    # after the probe (a batch never stores the source it fetched).
    keep = Path.home() / ".shortnotes_bridge" / "probe" / f"{file_id}.pdf"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_bytes(data)
    print(f"saved copy  : {keep}")
    return file.name, data


def _shape_of_notes(text: str) -> list[str]:
    """Counts and field names of a notes reply - never its prose.

    The reply is generated from the operator's own local PDF, but it still
    only prints structure so the output can be pasted into a ticket.
    """
    import json

    from notesbot.gemini_client import _strip_json_fences

    try:
        data = json.loads(_strip_json_fences(text))
    except (TypeError, ValueError):
        return [f"not JSON ({len(text)} chars)"]
    if not isinstance(data, dict):
        return [f"JSON {type(data).__name__}, not an object"]
    lines = [f"top-level keys: {sorted(data)}"]
    sections = data.get("sections")
    if not isinstance(sections, list):
        return lines + [f"sections: {type(sections).__name__} {sections!r}"[:80]]
    lines.append(f"sections: {len(sections)}")
    for index, section in enumerate(sections[:12]):
        if not isinstance(section, dict):
            lines.append(f"  [{index}] {type(section).__name__}")
            continue
        counts = {key: len(value) for key, value in section.items()
                  if isinstance(value, list) and key != "visual_ids"}
        lines.append(
            f"  [{index}] heading={len(str(section.get('heading') or ''))} chars,"
            f" page={section.get('source_local_page')!r} order={section.get('source_order')!r},"
            f" lists={counts}")
    return lines


def probe_pdf_file(settings, path: str, *, group_index: int = 1, language: str = "auto",
                   subject: str = "", interactive: bool = True, show_text: bool = False,
                   verify: bool = False) -> int:
    """`--probe-pdf FILE`: the real generation call on one page group of a local
    PDF, through the configured model, reporting what came back and why the
    generator kept or dropped it. Touches no sheet and uploads nothing.

    This exists because a batch only logs the class of a failure, and the
    generator's own diagnostics go to the console of whatever process ran it.
    """
    from pathlib import Path

    from notesbot import gemini_client

    from . import coverage
    from .errors import describe_exception

    signed = _sign_in(settings, interactive=interactive)
    if signed is None:
        return 1
    _access, model, drive_service = signed

    try:
        name, pdf_bytes = _lecture_bytes(path, drive_service, settings.max_input_bytes)
    except BridgeError as exc:
        print(f"[!!]  could not read the lecture: {exc}")
        return 1
    groups = coverage.split_pages(pdf_bytes, coverage.pages_per_call())
    if not 1 <= group_index <= len(groups):
        print(f"[!!]  group {group_index} does not exist: the file splits into {len(groups)} group(s)")
        return 1
    group = groups[group_index - 1]
    language = (language or "auto").strip().lower()
    if language == "auto":
        language = coverage.detect_source_language(pdf_bytes) or "auto"
    print(f"file       : {name} - {len(groups)} group(s) of up to {coverage.pages_per_call()} page(s)")
    print(f"probing    : group {group_index} = pages {group.first_page}-{group.last_page}"
          f" ({len(group.pdf_bytes):,} bytes), language={language}")

    # Record every reply the generator receives, through the client it uses.
    recorded: list = []
    real_build = gemini_client._build_visual_lite_client

    class Recording:
        def __init__(self, inner):
            self._inner = inner
            self.chat = type("chat", (), {})()
            self.chat.completions = type("completions", (), {})()
            self.chat.completions.create = self._create

        def _create(self, **kwargs):
            reply = self._inner.chat.completions.create(**kwargs)
            recorded.append(reply)
            return reply

        def with_options(self, **kwargs):
            return Recording(self._inner.with_options(**kwargs))

        def close(self):
            return self._inner.close()

    gemini_client._build_visual_lite_client = lambda: Recording(real_build())
    from .pw_transport import set_task
    set_task(task_id=pw_access.new_task_id("probe-pdf"), filename=name,
             input_unit="No. of pages", count=group.page_count)
    started = time.monotonic()
    outcome = ""
    try:
        if verify:
            # The batch's own path for one group: generate, retry policy,
            # then the PDF-backed source check - so a coverage failure can be
            # reproduced on one group instead of a whole paid row.
            notes, error = coverage._generate_group(
                group, instruction=coverage.language_instruction(language), language=language,
                subject_hint=subject, emit=lambda line: print(f"      {line}"),
                allow_split=0, verify_source=True)
            if notes is None:
                raise error or RuntimeError("no notes")
        else:
            notes = gemini_client.generate_short_notes(
                group.pdf_bytes, coverage.language_instruction(language), language, subject)
        outcome = (f"[OK]  generator returned {len(notes.sections)} section(s) with "
                   f"{sum(len(s.points) for s in notes.sections)} point(s) in {time.monotonic() - started:.1f}s")
        if show_text:
            # Opt-in, for synthetic probe pages whose content is the operator's
            # own test text: the only way to see whether the page was read.
            for section in notes.sections:
                print(f"      # {section.heading}")
                for point in section.points:
                    print(f"        - {point}")
    except Exception as exc:  # noqa: BLE001
        detail = str(exc) if isinstance(exc, ModelBudgetError) else describe_exception(exc)
        outcome = f"[!!]  generator failed after {time.monotonic() - started:.1f}s: {detail}"
    finally:
        gemini_client._build_visual_lite_client = real_build

    print()
    print(f"model replies recorded: {len(recorded)}")
    for number, reply in enumerate(recorded, start=1):
        choice = reply.choices[0] if reply.choices else None
        text = (choice.message.content or "") if choice else ""
        usage = reply.usage
        raw = getattr(reply, "_raw", None) or {}
        inner = raw.get("result") if isinstance(raw, dict) else None
        details = ((inner or {}).get("usage") or {}).get("completion_tokens_details") or {}
        print(f"  reply {number}: finish={getattr(choice, 'finish_reason', None)}"
              f" tokens in/out={usage.prompt_tokens}/{usage.completion_tokens}"
              f" reasoning={details.get('reasoning_tokens')} content_chars={len(text)}")
        for line in _shape_of_notes(text):
            print(f"    {line}")
    print()
    print(outcome)
    return 0 if outcome.startswith("[OK]") else 1


def test_model(settings, *, interactive: bool = True) -> int:
    from notesbot import gemini_client

    from .google_auth import build_services, load_credentials, token_provider, user_email
    from .pw_gate import ProxyAccess
    from .pw_openai import is_gpt_model
    from .pw_openai import route as gpt_route
    from .pw_transport import ProxyRequestError, set_task

    try:
        creds = load_credentials(settings.client_secret_path, settings.token_path, interactive=interactive)
    except BridgeError as exc:
        print(f"[!!]  {exc}")
        return 1
    build_services(creds)  # proves the Drive/Sheets scopes are usable too

    access = ProxyAccess(token_provider(creds), email=user_email(creds))
    print(f"account    : {access.email or '(email not exposed by the token)'}")
    print(f"app        : {pw_access.APP_NAME}")
    print(f"proxy      : {pw_access.PROXY_BASE_URL}")
    if access.email and not access.domain_ok():
        print(f"[!!]  {access.email} is not an @pw.live account - the proxy will refuse it")
        return 1

    status = access.allow_status()
    print(f"allowlist  : {status}")
    if status != "allowed":
        print("      add this email under the app's column in the Whitelisted sheet" if status == "denied" else "      the proxy could not decide (network/token/server)")
        return 1

    model = access.install_transport()
    print(f"model      : {model}")
    print(f"route      : {gpt_route() if is_gpt_model(model) else '/api/gemini/generate'}")

    task_id = pw_access.new_task_id("onboarding-test")
    set_task(task_id=task_id, filename="onboarding-test", input_unit="No. of pages", count=1)
    print(f"task_id    : {task_id}")

    started = time.monotonic()
    try:
        # The client the generator itself will use, so this proves the real path.
        response = _probe(model)
        text = (response.choices[0].message.content or "").strip()
        finish = response.choices[0].finish_reason
        usage = response.usage
    except (ProxyRequestError, ModelBudgetError) as exc:
        # Written by this app, never echoed proxy text: says which side to fix.
        print(f"[!!]  proxy call failed after {time.monotonic() - started:.1f}s: {describe_exception(exc)}\n      {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[!!]  {describe_probe_failure(model, exc, time.monotonic() - started, 1)}")
        return 1

    elapsed = time.monotonic() - started
    if not text:
        why = " - it spent its whole output budget" if finish == "length" else ""
        print(f"[!!]  empty answer in {elapsed:.1f}s - the proxy replied but the model returned no text{why}")
        raw = getattr(response, "_raw", None)
        if raw is not None:
            print("      reply shape (fixed probe prompt, safe to share):")
            for line in response_shape(raw):
                print(f"        {line}")
        return 1
    print(f"[OK]  model answered in {elapsed:.1f}s: {text[:40]!r}")
    print(f"[OK]  tokens in/out: {usage.prompt_tokens}/{usage.completion_tokens}")

    started = time.monotonic()
    try:
        response = _probe_pdf(model)
    except (ProxyRequestError, ModelBudgetError) as exc:
        said = getattr(exc, "proxy_detail", "")
        print(f"[!!]  PDF + notes-schema call failed after {time.monotonic() - started:.1f}s: {exc}")
        if said:
            print(f"      Proxy said: {said}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[!!]  PDF + notes-schema call: {describe_probe_failure(model, exc, time.monotonic() - started, 1)}")
        return 1
    problem = check_pdf_probe(response)
    if problem:
        print(f"[!!]  PDF + notes-schema call answered in {time.monotonic() - started:.1f}s, but {problem}")
        return 1
    usage = response.usage
    print(f"[OK]  read a PDF and returned valid notes JSON in {time.monotonic() - started:.1f}s"
          f" (tokens in/out {usage.prompt_tokens}/{usage.completion_tokens})")
    print()
    print("This was a real provider call: within ~15 minutes the 'Usage Cost 2.0'")
    print("tab of the control sheet should show 'Handwritten Notes Automation' with")
    print(f"Input Unit 'No. of pages' and a Volume, for Task ID {task_id}")
    return 0
