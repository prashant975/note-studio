"""The X Category Tags recovery ladder, copied from the Solution Addition Bot.

Some QBG records refuse to persist an otherwise-valid Update. The bot's
production fix, reached only after a normal save has failed, is to nudge the
X Category Tags control so Angular re-emits the whole form value:

1. tick one tag and immediately untick it (final selection unchanged), then
2. as a last resort, leave "AI Guru Personalization" selected.

Both operations verify the control returned to the intended state before the
Update is pressed again.
"""
from __future__ import annotations

from .browser import wait_for_qbg_form_to_settle
from .errors import QbgBridgeError
from .settings import NORMAL_TIMEOUT, SHORT_TIMEOUT, X_CATEGORY_SETTLE_MS, debug_path
from .textops import clean, header_key


def _locate_x_category_tags_select(page):
    """The actual enabled X Category Tags mat-select, never a false page-level match."""
    result = page.evaluate(
        r"""
        () => {
            const visible = (element) => {
                if (!element || typeof element.getBoundingClientRect !== "function") return false;
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== "hidden" && style.display !== "none";
            };
            const norm = (value) => (value || "").replace(/\s+/g, " ").trim().toLowerCase();
            const all = Array.from(document.querySelectorAll("mat-select"));
            const candidates = all.map((element, index) => {
                if (!visible(element)) return null;
                const disabled =
                    String(element.getAttribute("aria-disabled") || "").toLowerCase() === "true" ||
                    element.hasAttribute("disabled") ||
                    norm(element.className || "").includes("disabled");
                const field = element.closest("mat-form-field") || element.parentElement;
                const fieldText = norm(field && (field.innerText || field.textContent) || "");
                const ownText = norm([
                    element.innerText,
                    element.textContent,
                    element.getAttribute("aria-label"),
                    element.getAttribute("placeholder"),
                    element.getAttribute("title"),
                ].filter(Boolean).join(" "));
                const controlName = norm(element.getAttribute("formcontrolname") || "");
                const id = norm(element.id || "");
                const strong =
                    controlName.includes("xcategory") ||
                    controlName.includes("x_category") ||
                    fieldText.includes("x category tags") ||
                    fieldText.includes("x-category tags") ||
                    ownText.includes("x category tags") ||
                    id.includes("xcategory");
                const isMultiple =
                    element.hasAttribute("multiple") ||
                    String(element.getAttribute("aria-multiselectable") || "").toLowerCase() === "true" ||
                    Boolean(element.querySelector("mat-select-trigger"));
                return {index, disabled, strong, isMultiple, ownText, fieldText, controlName};
            }).filter(Boolean);

            const strongEnabled = candidates.filter((item) => item.strong && !item.disabled);
            if (strongEnabled.length) {
                const item = strongEnabled[strongEnabled.length - 1];
                return {...item, mode: "exact-local-field"};
            }

            const multipleEnabled = candidates.filter((item) => item.isMultiple && !item.disabled);
            if (multipleEnabled.length) {
                const item = multipleEnabled[multipleEnabled.length - 1];
                return {...item, mode: "multiple-fallback"};
            }

            // In the hydrated QBG form, X Category is the final enabled tagging select.
            const enabled = candidates.filter((item) => !item.disabled);
            if (enabled.length >= 8) {
                const item = enabled[enabled.length - 1];
                return {...item, mode: "last-enabled-select-fallback"};
            }
            return {index: -1, mode: "missing"};
        }
        """
    )
    if not result or int(result.get("index", -1)) < 0:
        return None, result or {"index": -1, "mode": "missing"}
    return page.locator("mat-select").nth(int(result["index"])), result


def _open_x_category_tags_dropdown(page, attempts=3, log=print):
    last_details = None
    for attempt in range(1, attempts + 1):
        select_locator, details = _locate_x_category_tags_select(page)
        last_details = details
        if select_locator is not None:
            try:
                select_locator.scroll_into_view_if_needed(timeout=NORMAL_TIMEOUT)
                select_locator.click(timeout=NORMAL_TIMEOUT)
                options = page.locator("mat-option:visible, [role='option']:visible")
                options.first.wait_for(state="visible", timeout=NORMAL_TIMEOUT)
                if details.get("mode") != "exact-local-field":
                    log(
                        f"Using X Category Tags {details.get('mode')} at mat-select index "
                        f"{details.get('index')} (displayed value: {clean(details.get('ownText') or '')!r})."
                    )
                return select_locator, options
            except Exception as exc:
                last_details = {**(details or {}), "error": str(exc)}
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        if attempt < attempts:
            page.wait_for_timeout(900)
            wait_for_qbg_form_to_settle(page, f"QBG X Category Tags wait {attempt}", log=log)

    raise QbgBridgeError(
        "Could not safely open the enabled X Category Tags dropdown. "
        f"Details: {last_details}"
    )


def _x_category_option_is_selected(option):
    try:
        aria_selected = (option.get_attribute("aria-selected") or "").lower() == "true"
        class_name = (option.get_attribute("class") or "").lower()
        checkbox_checked = option.locator(
            "input[type='checkbox']:checked, [aria-checked='true'], "
            ".mat-checkbox-checked, .mat-mdc-checkbox-checked, .mdc-checkbox--selected"
        ).count() > 0
        return aria_selected or "mat-selected" in class_name or "mdc-list-item--selected" in class_name or checkbox_checked
    except Exception:
        return False


def _find_x_category_option(options, wanted_text):
    wanted = header_key(wanted_text)
    for index in range(options.count()):
        option = options.nth(index)
        try:
            label = clean(option.inner_text(timeout=SHORT_TIMEOUT))
        except Exception:
            continue
        if header_key(label) == wanted:
            return option, label
    return None, ""


def _reopen_and_find_x_category_option(page, wanted_text, log=print):
    _, options = _open_x_category_tags_dropdown(page, attempts=2, log=log)
    option, label = _find_x_category_option(options, wanted_text)
    return option, label


def refresh_x_category_tags_by_toggle(page, log=print):
    """Tick and immediately untick one tag, leaving the final tag set unchanged."""
    _, options = _open_x_category_tags_dropdown(page, log=log)

    candidate = None
    candidate_text = ""
    original_selected = False

    # Prefer an unselected non-AI-Guru tag so the exact operation is tick -> untick.
    for index in range(options.count()):
        option = options.nth(index)
        try:
            label = clean(option.inner_text(timeout=SHORT_TIMEOUT))
        except Exception:
            continue
        if not label or header_key(label) in {"select", "x category tags", "ai guru personalization"}:
            continue
        disabled = (
            (option.get_attribute("aria-disabled") or "").lower() == "true"
            or "disabled" in (option.get_attribute("class") or "").lower()
        )
        if disabled:
            continue
        if not _x_category_option_is_selected(option):
            candidate, candidate_text, original_selected = option, label, False
            break

    # If every non-AI tag is already selected, temporarily untick/re-tick one.
    if candidate is None:
        for index in range(options.count()):
            option = options.nth(index)
            try:
                label = clean(option.inner_text(timeout=SHORT_TIMEOUT))
            except Exception:
                continue
            if not label or header_key(label) in {"select", "x category tags", "ai guru personalization"}:
                continue
            if _x_category_option_is_selected(option):
                candidate, candidate_text, original_selected = option, label, True
                break

    if candidate is None:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        raise QbgBridgeError("No usable X Category Tag was available for the tick/untick refresh.")

    candidate.click(timeout=NORMAL_TIMEOUT)
    page.wait_for_timeout(350)

    # Multi-select panels usually remain open; reopen safely if QBG closed it.
    visible_options = page.locator("mat-option:visible, [role='option']:visible")
    if visible_options.count() == 0:
        candidate, _ = _reopen_and_find_x_category_option(page, candidate_text, log=log)
    else:
        candidate, _ = _find_x_category_option(visible_options, candidate_text)
    if candidate is None:
        shot = debug_path("x_category_restore_error.png")
        page.screenshot(path=shot, full_page=True)
        raise QbgBridgeError(
            f"Could not restore temporary X Category Tag {candidate_text!r}. "
            f"Screenshot saved: {shot}"
        )

    candidate.click(timeout=NORMAL_TIMEOUT)
    page.wait_for_timeout(350)

    # Confirm the candidate returned to its original state.
    visible_options = page.locator("mat-option:visible, [role='option']:visible")
    if visible_options.count() == 0:
        check_option, _ = _reopen_and_find_x_category_option(page, candidate_text, log=log)
    else:
        check_option, _ = _find_x_category_option(visible_options, candidate_text)
    if check_option is None or _x_category_option_is_selected(check_option) != original_selected:
        shot = debug_path("x_category_restore_error.png")
        page.screenshot(path=shot, full_page=True)
        raise QbgBridgeError(
            f"X Category Tag {candidate_text!r} did not return to its original state. "
            f"The bot stopped before Update. Screenshot saved: {shot}"
        )

    page.keyboard.press("Escape")
    page.wait_for_timeout(X_CATEGORY_SETTLE_MS)
    action = "unticked and re-ticked" if original_selected else "ticked and unticked"
    log(
        f"Refreshed X Category Tags by {action} {candidate_text!r}; "
        "the final tag selection is unchanged."
    )
    return candidate_text


def ensure_ai_guru_personalization_selected(page, log=print):
    """Final fallback: leave AI Guru Personalization selected and update QBG."""
    _, options = _open_x_category_tags_dropdown(page, log=log)
    option, label = _find_x_category_option(options, "AI Guru Personalization")
    if option is None:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        shot = debug_path("ai_guru_x_category_missing.png")
        page.screenshot(path=shot, full_page=True)
        raise QbgBridgeError(
            "AI Guru Personalization was not available in X Category Tags. "
            f"Screenshot saved: {shot}"
        )

    if _x_category_option_is_selected(option):
        # Force the Angular form value to refresh while keeping AI Guru selected.
        option.click(timeout=NORMAL_TIMEOUT)
        page.wait_for_timeout(350)
        visible_options = page.locator("mat-option:visible, [role='option']:visible")
        if visible_options.count() == 0:
            option, _ = _reopen_and_find_x_category_option(page, label, log=log)
        else:
            option, _ = _find_x_category_option(visible_options, label)
        if option is None:
            raise QbgBridgeError("Could not reselect AI Guru Personalization after temporarily unticking it.")
        option.click(timeout=NORMAL_TIMEOUT)
        action = "temporarily unticked and re-ticked"
    else:
        option.click(timeout=NORMAL_TIMEOUT)
        action = "selected"

    page.wait_for_timeout(400)
    visible_options = page.locator("mat-option:visible, [role='option']:visible")
    if visible_options.count() == 0:
        check_option, _ = _reopen_and_find_x_category_option(page, label, log=log)
    else:
        check_option, _ = _find_x_category_option(visible_options, label)
    if check_option is None or not _x_category_option_is_selected(check_option):
        shot = debug_path("ai_guru_x_category_select_error.png")
        page.screenshot(path=shot, full_page=True)
        raise QbgBridgeError(
            "AI Guru Personalization could not be left selected in X Category Tags. "
            f"Screenshot saved: {shot}"
        )

    page.keyboard.press("Escape")
    page.wait_for_timeout(X_CATEGORY_SETTLE_MS)
    log(
        f"Final X Category fallback: {action} 'AI Guru Personalization'; "
        "it will remain selected for this question before Update."
    )
    return label
