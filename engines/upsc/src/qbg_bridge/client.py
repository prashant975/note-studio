"""Open a QBG question by its ID and overwrite parts of it, verified.

This is the Solution Addition Bot's per-row flow, generalised: instead of
"write the sheet's solution and answer", the caller says which of the four
things to overwrite — question statement, option texts, correct option
letters, solution — and the same machinery carries it: enter edit mode, type
through real keyboard events, click Update while watching the network, then
reopen the question and prove the portal kept every field.

Navigation accepts either a full qbg-admin link or a bare question unique_id.
An id goes through ``QBG_QUESTION_LINK_TEMPLATE`` when that has been confirmed,
and otherwise through the portal's question-search page.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from .browser import (
    connect_browser,
    ensure_edit_mode,
    expand_solution_sections,
    get_current_question_type_from_radio,
    get_main_question_type_from_select_values,
    get_visible_select_values,
    is_edit_form_visible,
    open_english_tab,
    open_hindi_tab_if_available,
    open_language_tab,
    open_or_add_hindi_tab,
    raise_if_login_page,
    wait_for_qbg_form_to_settle,
)
from .editors import (
    find_option_editors,
    find_question_editor,
    find_text_solution_editor,
    get_visible_editors,
    read_checked_option_letters,
    set_correct_options_by_letters,
    set_editor_html,
)
from .errors import QbgBridgeError
from .save import save_qbg
from .settings import (
    LONG_TIMEOUT,
    NORMAL_TIMEOUT,
    OVERWRITE_RETRIES,
    QBG_INITIAL_SETTLE_MS,
    QBG_SEARCH_URL,
    QBG_TAB_SETTLE_MS,
    SHORT_TIMEOUT,
    VERIFY_OVERWRITE,
    debug_path,
)
from .textops import (
    clean,
    html_for_editor,
    html_to_ai_text,
    is_qbg_link,
    looks_like_qbg_id,
    normalize_language,
    normalize_option_letters,
    question_link,
    solution_compare_key,
    strip_for_qbg_solution,
    text_to_plain,
)
from .xcategory import (
    ensure_ai_guru_personalization_selected,
    refresh_x_category_tags_by_toggle,
)


@dataclass(frozen=True)
class QbgEdit:
    """What to overwrite on the question. None / empty means leave alone."""

    language: str = "English"
    question: str | None = None
    options: dict[str, str] = field(default_factory=dict)  # letter -> new text
    correct_letters: tuple[str, ...] = ()
    solution: str | None = None

    def normalized(self) -> "QbgEdit":
        language = normalize_language(self.language)
        if not language:
            raise QbgBridgeError(
                f"Invalid language {self.language!r}. Use English or Hindi only.")

        options = {}
        for letter, value in (self.options or {}).items():
            letter = clean(letter).upper()
            if letter not in {"A", "B", "C", "D"}:
                raise QbgBridgeError(f"Option letter must be A-D, not {letter!r}.")
            if value is None:
                continue
            value = str(value).strip()
            if not value:
                raise QbgBridgeError(f"Option {letter} cannot be replaced with empty text.")
            options[letter] = value

        letters = tuple(normalize_option_letters(list(self.correct_letters)))
        question = self.question.strip() if isinstance(self.question, str) else None
        if question == "":
            raise QbgBridgeError("The question statement cannot be replaced with empty text.")
        solution = (strip_for_qbg_solution(self.solution)
                    if isinstance(self.solution, str) else None)
        if isinstance(self.solution, str) and not solution:
            raise QbgBridgeError("The solution cannot be replaced with empty text.")

        if question is None and not options and not letters and solution is None:
            raise QbgBridgeError("Nothing to push: the edit changes no field.")
        return replace(self, language=language, question=question,
                       options=options, correct_letters=letters, solution=solution)

    def summary(self) -> str:
        parts = []
        if self.question is not None:
            parts.append("question")
        parts.extend(f"option {letter}" for letter in sorted(self.options or {}))
        if self.correct_letters:
            parts.append("correct answer " + ", ".join(self.correct_letters))
        if self.solution is not None:
            parts.append("solution")
        return ", ".join(parts) or "nothing"


@dataclass
class QbgPushResult:
    qbg_ref: str
    url: str
    fields: str
    attempts: int
    verified: bool
    message: str
    seconds: float


# ------------------------------------------------------------ navigation ----
def _open_via_question_search(page, qbg_id, log=print):
    """Reach a question through the portal's search page, by its unique_id.

    This is the fallback while the direct per-question link format is
    unconfirmed. It is written defensively — several candidate controls, a
    screenshot on failure — because the search page's exact markup has not
    been captured the way the edit form's has.
    """
    log(f"No confirmed direct-link template; searching the portal for {qbg_id}...")
    page.goto(QBG_SEARCH_URL, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    page.wait_for_timeout(max(QBG_INITIAL_SETTLE_MS, 800))
    raise_if_login_page(page)

    filled = False
    for locator in [
        page.get_by_role("searchbox"),
        page.locator("input[type='search']"),
        page.locator("input[placeholder*='search' i]"),
        page.locator("input[placeholder*='id' i]"),
        page.locator("input[formcontrolname*='search' i]"),
        page.locator("input[formcontrolname*='id' i]"),
        page.locator("textarea[placeholder*='search' i]"),
    ]:
        try:
            box = locator.first
            box.wait_for(state="visible", timeout=SHORT_TIMEOUT)
            box.click(timeout=SHORT_TIMEOUT)
            box.fill(qbg_id, timeout=SHORT_TIMEOUT)
            page.keyboard.press("Enter")
            filled = True
            break
        except Exception:
            continue
    if not filled:
        shot = debug_path("qbg_search_box_missing.png")
        page.screenshot(path=shot, full_page=True)
        raise QbgBridgeError(
            "Could not find a search box on the QBG question-search page. "
            f"Screenshot saved: {shot}. The reliable fix is to confirm the "
            "direct link format once: open any question in the portal, copy "
            "its URL, and set QBG_QUESTION_LINK_TEMPLATE with {qbg_id} in "
            "place of the question id."
        )

    page.wait_for_timeout(1500)
    raise_if_login_page(page)

    opened = False
    for locator in [
        page.locator(f"a[href*='{qbg_id}']"),
        page.get_by_text(qbg_id, exact=True),
        page.get_by_text(qbg_id),
        page.locator("tr, mat-row, mat-card, .mat-card, .mat-mdc-card").filter(has_text=qbg_id),
    ]:
        try:
            target = locator.first
            target.wait_for(state="visible", timeout=NORMAL_TIMEOUT)
            target.click(timeout=NORMAL_TIMEOUT)
            opened = True
            break
        except Exception:
            continue

    if not opened:
        # A single-result search can land straight on the question.
        if is_edit_form_visible(page):
            return
        shot = debug_path("qbg_search_result_missing.png")
        page.screenshot(path=shot, full_page=True)
        raise QbgBridgeError(
            f"Searched for {qbg_id} but no result could be opened. "
            f"Screenshot saved: {shot}. Check the id exists in the portal, or "
            "confirm the direct link format once and set "
            "QBG_QUESTION_LINK_TEMPLATE."
        )
    page.wait_for_timeout(max(QBG_INITIAL_SETTLE_MS, 500))


def open_question(page, qbg_ref, log=print) -> str:
    """Navigate to the question and enter edit mode. Returns the page URL."""
    ref = clean(qbg_ref)
    if is_qbg_link(ref):
        link = ref
    elif looks_like_qbg_id(ref):
        link = question_link(ref)
    else:
        raise QbgBridgeError(
            f"{qbg_ref!r} is neither a qbg-admin link nor a QBG question id.")

    if link:
        page.goto(link, wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
        # QBG is a live SPA; the form-readiness checks below are more reliable
        # than the browser load event.
        page.wait_for_timeout(QBG_INITIAL_SETTLE_MS)
        raise_if_login_page(page)
    else:
        _open_via_question_search(page, ref, log=log)

    expand_solution_sections(page, log=log)
    ensure_edit_mode(page)
    expand_solution_sections(page, log=log)
    wait_for_qbg_form_to_settle(page, "QBG edit form", log=log)
    return page.url


# --------------------------------------------------------------- reading ----
def read_question_fields(page, language="English", log=print) -> dict:
    """What the edit form currently shows for one language."""
    open_language_tab(page, language)
    page.wait_for_timeout(QBG_TAB_SETTLE_MS)
    wait_for_qbg_form_to_settle(page, "QBG read", log=log)
    fields = get_visible_editors(page)

    def text_of(field_obj):
        if field_obj is None:
            return ""
        return clean(field_obj.text
                     or html_to_ai_text(field_obj.html)
                     or text_to_plain(field_obj.html))

    question_type = get_current_question_type_from_radio(page, log=log)
    if not question_type:
        question_type = get_main_question_type_from_select_values(
            get_visible_select_values(page, log=log), log=log)

    return {
        "url": page.url,
        "language": normalize_language(language) or "English",
        "question_type": question_type,
        "question": text_of(find_question_editor(fields)),
        "options": {letter: text_of(field_obj)
                    for letter, field_obj in find_option_editors(fields).items()},
        "correct_letters": read_checked_option_letters(page),
        "solution": text_of(find_text_solution_editor(fields)),
    }


def _require_valid_ref(qbg_ref) -> str:
    """Reject a bad reference before any browser is started."""
    ref = clean(qbg_ref)
    if not (is_qbg_link(ref) or looks_like_qbg_id(ref)):
        raise QbgBridgeError(
            f"{qbg_ref!r} is neither a qbg-admin link nor a QBG question id.")
    return ref


def fetch_question(qbg_ref, language="English", log=print) -> dict:
    """Open the question by id or link and return its current fields."""
    from playwright.sync_api import sync_playwright

    qbg_ref = _require_valid_ref(qbg_ref)
    with sync_playwright() as playwright:
        browser, context, page, owns_context = connect_browser(playwright)
        try:
            open_question(page, qbg_ref, log=log)
            return read_question_fields(page, language, log=log)
        finally:
            if owns_context:
                context.close()
            else:
                _ = browser


# --------------------------------------------------------------- writing ----
def _sync_correct_letters(page, edit: QbgEdit, log=print):
    """Tick the correct options, keeping every enabled language tab aligned.

    Options are language-independent, and QBG can reject an update when the
    enabled tabs' answers differ — so both tabs are synced, as the bot does.
    """
    open_english_tab(page)
    expand_solution_sections(page, log=log)
    set_correct_options_by_letters(page, list(edit.correct_letters), "English", log=log)

    if edit.language == "Hindi":
        open_or_add_hindi_tab(page)
        expand_solution_sections(page, log=log)
        set_correct_options_by_letters(page, list(edit.correct_letters), "Hindi", log=log)
    elif open_hindi_tab_if_available(page, log=log):
        set_correct_options_by_letters(page, list(edit.correct_letters), "Hindi", log=log)


def _apply_edit(page, edit: QbgEdit, log=print) -> str:
    """Write every requested field. Returns text usable as the save witness."""
    if edit.correct_letters:
        _sync_correct_letters(page, edit, log=log)

    open_language_tab(page, edit.language)
    page.wait_for_timeout(QBG_TAB_SETTLE_MS)
    fields = get_visible_editors(page)

    if edit.question is not None:
        question_editor = find_question_editor(fields)
        if question_editor is None:
            shot = debug_path("question_editor_error.png")
            page.screenshot(path=shot, full_page=True)
            raise QbgBridgeError(
                f"Could not identify the {edit.language} question editor. "
                f"Screenshot saved: {shot}")
        set_editor_html(page, question_editor.index, html_for_editor(edit.question), log=log)
        log(f"Filled {edit.language} question statement in editor index {question_editor.index}.")

    if edit.options:
        option_editors = find_option_editors(fields)
        missing = [letter for letter in edit.options if letter not in option_editors]
        if missing:
            shot = debug_path("option_editor_error.png")
            page.screenshot(path=shot, full_page=True)
            raise QbgBridgeError(
                f"Could not identify option editor(s) {', '.join(missing)} for "
                f"{edit.language}. Found: {', '.join(sorted(option_editors)) or 'none'}. "
                f"Screenshot saved: {shot}")
        for letter in sorted(edit.options):
            set_editor_html(page, option_editors[letter].index,
                            html_for_editor(edit.options[letter]), log=log)
            log(f"Filled {edit.language} option {letter} in editor index "
                f"{option_editors[letter].index}.")

    if edit.solution is not None:
        solution_editor = find_text_solution_editor(fields)
        if solution_editor is None:
            shot = debug_path(f"{edit.language.lower()}_solution_editor_error.png")
            page.screenshot(path=shot, full_page=True)
            labels = [
                f"idx={f.index}, role={f.role_hint!r}, section={f.section_label!r}, "
                f"near={f.near_label!r}, option={f.option_marker!r}, "
                f"text={clean(f.text)[:40]!r}"
                for f in fields
            ]
            raise QbgBridgeError(
                f"Could not identify the {edit.language} text solution editor. "
                f"Screenshot saved: {shot}. "
                f"Visible editor labels: {' | '.join(labels[:12])}")
        set_editor_html(page, solution_editor.index,
                        html_for_editor(edit.solution), log=log)
        log(f"Filled {edit.language} text solution in editor index "
            f"{solution_editor.index} (role: {solution_editor.role_hint!r}, "
            f"section: {solution_editor.section_label!r}).")

    for witness in (edit.solution, edit.question,
                    *(edit.options[letter] for letter in sorted(edit.options))):
        if witness:
            return witness
    return ""


def _verify_edit(page, reopen_ref, edit: QbgEdit, log=print) -> tuple[bool, str]:
    """Reopen the question and confirm every pushed field was kept."""
    open_question(page, reopen_ref, log=log)
    open_language_tab(page, edit.language)
    page.wait_for_timeout(QBG_TAB_SETTLE_MS)
    wait_for_qbg_form_to_settle(page, "QBG verification form", log=log)
    fields = get_visible_editors(page)

    problems = []

    def check(name, expected, field_obj):
        if field_obj is None:
            problems.append(f"{name} editor could not be identified during verification")
            return
        actual = (field_obj.text or html_to_ai_text(field_obj.html)
                  or text_to_plain(field_obj.html))
        expected_key = solution_compare_key(expected)
        actual_key = solution_compare_key(actual)
        if expected_key != actual_key:
            problems.append(
                f"{name}: expected start={expected_key[:140]!r}; "
                f"saved start={actual_key[:140]!r}")

    if edit.question is not None:
        check("question", edit.question, find_question_editor(fields))
    if edit.options:
        option_editors = find_option_editors(fields)
        for letter in sorted(edit.options):
            check(f"option {letter}", edit.options[letter], option_editors.get(letter))
    if edit.solution is not None:
        check("solution", edit.solution, find_text_solution_editor(fields))
    if edit.correct_letters:
        checked = read_checked_option_letters(page)
        if checked is None:
            log("Ticked options could not be read back; the correct answer is "
                "saved but stays unverified this run.")
        elif sorted(checked) != sorted(edit.correct_letters):
            problems.append(
                f"correct answer: expected {', '.join(sorted(edit.correct_letters))}; "
                f"saved {', '.join(sorted(checked)) or 'none'}")

    return (not problems), "; ".join(problems)


def edit_question(qbg_ref, edit: QbgEdit, log=print) -> QbgPushResult:
    """The whole job: open, overwrite, save with the retry ladder, verify.

    This overwrites live portal content — call it only on an explicit,
    per-question human instruction (in the app, the Push button is that
    instruction).
    """
    from playwright.sync_api import sync_playwright

    edit = edit.normalized()
    qbg_ref = _require_valid_ref(qbg_ref)
    started = time.monotonic()

    with sync_playwright() as playwright:
        browser, context, page, owns_context = connect_browser(playwright)
        try:
            resolved_url = open_question(page, qbg_ref, log=log)
            # Reopen by URL only when the URL provably names this question;
            # a SPA can sit on a generic route, and reopening that would
            # verify whatever the search page happens to show.
            reopen_ref = resolved_url
            if looks_like_qbg_id(clean(qbg_ref)) and clean(qbg_ref) not in (resolved_url or ""):
                reopen_ref = clean(qbg_ref)

            # The base question type is reliably stored on the English tab.
            open_english_tab(page)
            expand_solution_sections(page, log=log)
            page.wait_for_timeout(QBG_TAB_SETTLE_MS)
            question_type = get_current_question_type_from_radio(page, log=log)
            if not question_type:
                question_type = get_main_question_type_from_select_values(
                    get_visible_select_values(page, log=log), log=log)
            if question_type == "COMP":
                raise QbgBridgeError(
                    "This is a Comprehension question. Its child questions "
                    "cannot be mapped safely, so it must be edited by hand.")
            if (edit.correct_letters and question_type in {"SCQ", "TF", "AR"}
                    and len(edit.correct_letters) != 1):
                raise QbgBridgeError(
                    f"Question type is {question_type}, so exactly one correct "
                    f"option letter is required; received "
                    f"{', '.join(edit.correct_letters)}.")

            last_error = ""
            for attempt in range(1, OVERWRITE_RETRIES + 1):
                if attempt == 2:
                    log(f"Retrying forced overwrite with X Category tick/untick "
                        f"(attempt {attempt}/{OVERWRITE_RETRIES})...")
                elif attempt >= 3:
                    log(f"Retrying forced overwrite with AI Guru Personalization "
                        f"fallback (attempt {attempt}/{OVERWRITE_RETRIES})...")

                if attempt > 1:
                    wait_for_qbg_form_to_settle(
                        page, f"QBG form before overwrite attempt {attempt}", log=log)
                expected_text = _apply_edit(page, edit, log=log)

                if attempt == 2:
                    refresh_x_category_tags_by_toggle(page, log=log)
                    wait_for_qbg_form_to_settle(
                        page, "QBG form after X Category tick/untick refresh", log=log)
                elif attempt >= 3:
                    ensure_ai_guru_personalization_selected(page, log=log)
                    wait_for_qbg_form_to_settle(
                        page, "QBG form after AI Guru X Category selection", log=log)

                try:
                    save_qbg(
                        page,
                        expected_text=expected_text,
                        target_language=edit.language,
                        use_xcategory_fallback=attempt > 1,
                        log=log,
                    )
                except QbgBridgeError as save_exc:
                    last_error = str(save_exc)
                    if attempt < OVERWRITE_RETRIES:
                        next_action = (
                            "tick and untick one X Category tag"
                            if attempt == 1
                            else "select AI Guru Personalization in X Category Tags")
                        log(f"QBG save failed; the next attempt will {next_action}. "
                            f"Reason: {last_error}")
                        continue
                    raise

                if not VERIFY_OVERWRITE:
                    return QbgPushResult(
                        qbg_ref=clean(qbg_ref), url=resolved_url,
                        fields=edit.summary(), attempts=attempt, verified=False,
                        message="Saved; reopen verification is disabled "
                                "(QBG_VERIFY_OVERWRITE=0).",
                        seconds=time.monotonic() - started)

                matched, problems = _verify_edit(page, reopen_ref, edit, log=log)
                if matched:
                    log(f"Verified overwrite after save: {edit.summary()} "
                        f"exactly match the pushed values.")
                    return QbgPushResult(
                        qbg_ref=clean(qbg_ref), url=page.url or resolved_url,
                        fields=edit.summary(), attempts=attempt, verified=True,
                        message="Overwrite verified by reopening the question.",
                        seconds=time.monotonic() - started)

                last_error = problems
                log(f"Overwrite verification failed: {problems}")

            shot = debug_path(f"{edit.language.lower()}_overwrite_verification_error.png")
            page.screenshot(path=shot, full_page=True)
            raise QbgBridgeError(
                f"QBG did not retain the new {edit.language} values after "
                f"{OVERWRITE_RETRIES} attempt(s). {last_error}. "
                f"Screenshot saved: {shot}")
        finally:
            if owns_context:
                context.close()
            else:
                _ = browser
