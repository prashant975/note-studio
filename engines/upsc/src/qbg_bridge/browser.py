"""Reaching a logged-in QBG edit form, copied from the Solution Addition Bot.

Two ways in, in order of preference:

1. An already-running debug Chrome (`--remote-debugging-port=9222`) that the
   operator has logged in to QBG with. The bot attaches over CDP and inherits
   the session — no credential ever passes through this code.
2. `QBG_HEADLESS=1`: a persistent headless profile at ``QBG_CHROME_PROFILE``
   that was logged in once by running headed.

Everything else here is defensive navigation: QBG is a live Angular SPA, so
readiness is judged by real controls (TinyMCE editors, the Update button), not
by browser load events.
"""
from __future__ import annotations

import re
import sys
import time

from .errors import QbgBridgeError
from .settings import (
    CHROME_CDP_URL,
    LONG_TIMEOUT,
    NORMAL_TIMEOUT,
    QBG_CHROME_PROFILE,
    QBG_FORM_SETTLE_MS,
    QBG_HEADLESS,
    QBG_STABLE_CHECKS,
    QBG_STABLE_POLL_MS,
    QBG_TAB_SETTLE_MS,
    SHORT_TIMEOUT,
    debug_path,
)
from .textops import clean, normalize_language
from .editors import wait_for_editors


def debug_chrome_command() -> str:
    """The debug-Chrome launch command for this operating system."""
    profile = QBG_CHROME_PROFILE
    if sys.platform.startswith("win"):
        return (
            'Run this in PowerShell (keep the window open):\n\n'
            '  & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            '--remote-debugging-port=9222 '
            f'--user-data-dir="{profile}"\n\n'
            "  If Chrome is not at that path, try:\n"
            '  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"'
        )
    if sys.platform == "darwin":
        return (
            "Run this in Terminal (keep the window open):\n\n"
            "  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome "
            "--remote-debugging-port=9222 "
            '--user-data-dir="$HOME/qbg_chrome_debug_profile"'
        )
    return (
        "Run this in your terminal (keep the window open):\n\n"
        "  google-chrome --remote-debugging-port=9222 "
        '--user-data-dir="$HOME/qbg_chrome_debug_profile"'
    )


def connect_browser(playwright):
    """(browser, context, page, owns_context) for a QBG session."""
    if QBG_HEADLESS:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=QBG_CHROME_PROFILE,
            channel="chrome",
            headless=True,
            viewport={"width": 1440, "height": 1100},
            args=["--disable-gpu", "--disable-dev-shm-usage", "--window-size=1440,1100"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        return None, context, page, True

    try:
        browser = playwright.chromium.connect_over_cdp(CHROME_CDP_URL)
    except Exception as exc:
        raise QbgBridgeError(
            "Could not connect to Chrome.\n\n"
            "Open debug Chrome once, log in to QBG, keep it open, then retry.\n\n"
            f"{debug_chrome_command()}\n\n"
            f"Original error: {exc}"
        ) from exc

    if not browser.contexts:
        raise QbgBridgeError("Connected to Chrome, but no browser context was found.")

    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()
    return browser, context, page, False


def click_first(page, locators, timeout=SHORT_TIMEOUT):
    for locator in locators:
        try:
            locator.first.click(timeout=timeout)
            return True
        except Exception:
            pass
    return False


def get_page_state(page):
    try:
        return page.evaluate(
            """
            () => {
                const text = (document.body && (document.body.innerText || document.body.textContent) || "")
                    .replace(/\\s+/g, " ")
                    .trim()
                    .slice(0, 900);
                return {url: window.location.href, title: document.title || "", text};
            }
            """
        )
    except Exception as exc:
        return {"url": "", "title": "", "text": f"Could not read page state: {exc}"}


def raise_if_login_page(page):
    state = get_page_state(page)
    combined = clean(" ".join([state.get("url", ""), state.get("title", ""), state.get("text", "")])).lower()
    if (
        "/auth" in combined
        or "sign in with google" in combined
        or "login" in combined
        or "email or phone" in combined
        or "accounts.google.com" in combined
    ):
        shot = debug_path("qbg_login_required.png")
        page.screenshot(path=shot, full_page=True)
        raise QbgBridgeError(
            "QBG login required in the Chrome profile used by the bot. "
            "Log in inside the debug Chrome window and keep it open. "
            f"Current URL: {state.get('url')}. Screenshot saved: {shot}"
        )


def is_edit_form_visible(page):
    try:
        return bool(
            page.evaluate(
                """
                () => {
                    if (window.tinymce && window.tinymce.editors && window.tinymce.editors.length > 0) {
                        return true;
                    }
                    const text = (document.body && (document.body.innerText || document.body.textContent) || "")
                        .replace(/\\s+/g, " ")
                        .trim()
                        .toLowerCase();
                    return (
                        text.includes("input language") ||
                        text.includes("exam category") ||
                        text.includes("question type") ||
                        text.includes("correct answer") ||
                        text.includes("correct answers")
                    );
                }
                """
            )
        )
    except Exception:
        return False


def click_edit_button(page):
    page.wait_for_timeout(700)
    if click_first(
        page,
        [
            page.get_by_role("button", name=re.compile(r"^edit$", re.I)),
            page.get_by_role("link", name=re.compile(r"^edit$", re.I)),
            page.locator("button").filter(has_text=re.compile(r"Edit", re.I)),
            page.locator("a").filter(has_text=re.compile(r"Edit", re.I)),
            page.locator("[role='button']").filter(has_text=re.compile(r"Edit", re.I)),
            page.locator("mat-icon").filter(has_text=re.compile(r"edit", re.I)),
        ],
        timeout=NORMAL_TIMEOUT,
    ):
        page.wait_for_timeout(QBG_TAB_SETTLE_MS)
        return True

    clicked = page.evaluate(
        """
        () => {
            const visible = (element) => {
                if (!element || typeof element.getBoundingClientRect !== "function") return false;
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0 &&
                    rect.height > 0 &&
                    style.visibility !== "hidden" &&
                    style.display !== "none";
            };
            const norm = (value) => (value || "").replace(/\\s+/g, " ").trim().toLowerCase();
            const candidates = Array.from(document.querySelectorAll("button, a, [role='button'], mat-icon, span, div"))
                .filter((element) => visible(element))
                .filter((element) => {
                    const text = norm(element.innerText || element.textContent || element.getAttribute("aria-label") || element.getAttribute("title") || "");
                    return text === "edit" || text.includes(" edit") || text.includes("edit ");
                })
                .sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    return (ar.width * ar.height) - (br.width * br.height);
                });
            const target = candidates[0];
            if (!target) return false;
            let node = target;
            for (let depth = 0; node && depth < 5; depth += 1) {
                const tag = (node.tagName || "").toLowerCase();
                const role = (node.getAttribute && node.getAttribute("role") || "").toLowerCase();
                if (tag === "button" || tag === "a" || role === "button") {
                    node.scrollIntoView({block: "center", inline: "center"});
                    node.click();
                    return true;
                }
                node = node.parentElement;
            }
            target.scrollIntoView({block: "center", inline: "center"});
            target.click();
            return true;
        }
        """
    )
    if clicked:
        page.wait_for_timeout(QBG_TAB_SETTLE_MS)
        return True
    return False


def ensure_edit_mode(page):
    raise_if_login_page(page)
    try:
        wait_for_editors(page)
        return
    except Exception:
        pass

    if is_edit_form_visible(page):
        try:
            wait_for_editors(page)
            return
        except Exception:
            pass

    for _ in range(5):
        if click_edit_button(page):
            try:
                wait_for_editors(page)
                return
            except Exception:
                pass
        page.wait_for_timeout(400)
        raise_if_login_page(page)
        if is_edit_form_visible(page):
            try:
                wait_for_editors(page)
                return
            except Exception:
                pass

    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(700)
        if click_edit_button(page):
            wait_for_editors(page)
            return
    except Exception:
        pass

    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(700)
        if click_edit_button(page):
            wait_for_editors(page)
            return
    except Exception:
        pass

    try:
        wait_for_editors(page)
        return
    except Exception:
        pass

    state = get_page_state(page)
    shot = debug_path("edit_mode_error.png")
    page.screenshot(path=shot, full_page=True)
    raise QbgBridgeError(
        "Could not enter QBG edit mode or find TinyMCE editors. "
        "Check that this account has Edit access and the question is editable. "
        f"Current URL: {state.get('url')}. Title: {state.get('title')}. "
        f"Visible text: {state.get('text')}. Screenshot saved: {shot}"
    )


def expand_solution_sections(page, log=print):
    """Expand the solutions/details section once, only when a real control exists."""
    try:
        already_open = page.evaluate(
            r"""
            () => {
                const visible = (element) => {
                    if (!element || typeof element.getBoundingClientRect !== "function") return false;
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== "hidden" && style.display !== "none";
                };
                if (window.tinymce && Array.from(window.tinymce.editors || []).some((editor) => {
                    const iframe = editor.iframeElement || document.getElementById(editor.id + "_ifr");
                    return iframe && visible(iframe);
                })) return true;
                const bodyText = (document.body && (document.body.innerText || document.body.textContent) || "")
                    .replace(/\s+/g, " ").toLowerCase();
                return bodyText.includes("video solution (url)") || bodyText.includes("other solutions");
            }
            """
        )
        if already_open:
            return 0
    except Exception:
        pass

    candidates = [
        page.get_by_role("button", name=re.compile(r"^See Solutions\s*&\s*Topics details$", re.I)),
        page.get_by_role("button", name=re.compile(r"^See Solutions", re.I)),
        page.get_by_role("link", name=re.compile(r"^See Solutions", re.I)),
        page.locator("mat-expansion-panel-header").filter(has_text=re.compile(r"Solutions?\s*&\s*Topics", re.I)),
        page.locator("[role='button']").filter(has_text=re.compile(r"^\s*See Solutions", re.I)),
    ]
    for locator in candidates:
        try:
            target = locator.first
            if target.is_visible(timeout=300):
                target.click(timeout=NORMAL_TIMEOUT)
                page.wait_for_timeout(QBG_TAB_SETTLE_MS)
                log("Expanded solution/details section once.")
                return 1
        except Exception:
            continue
    return 0


# ------------------------------------------------------------- languages ----
def click_language_tab(page, names):
    locators = []
    for name in names:
        pattern = re.compile(name, re.I)
        locators.extend(
            [
                page.get_by_role("tab", name=pattern),
                page.locator("mat-tab-label, .mat-tab-label, [role='tab']").filter(has_text=pattern),
                page.locator(".mat-tab-label-content").filter(has_text=pattern),
            ]
        )
    return click_first(page, locators, timeout=NORMAL_TIMEOUT)


def ensure_language_checkbox_checked(page, names, log=print):
    result = page.evaluate(
        """
        ({names}) => {
            window.scrollTo(0, 0);
            const wanted = names.map((name) => (name || "").replace(/\\s+/g, " ").trim().toLowerCase());
            const visible = (element) => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== "hidden" && style.display !== "none";
            };
            const norm = (value) => (value || "").replace(/\\s+/g, " ").trim().toLowerCase();
            const candidates = Array.from(document.querySelectorAll("mat-checkbox"))
                .map((element, domIndex) => {
                    const rect = element.getBoundingClientRect();
                    const input = element.querySelector("input[type='checkbox']");
                    return {
                        element,
                        input,
                        domIndex,
                        text: norm(element.innerText || element.textContent || ""),
                        top: rect.top,
                        left: rect.left,
                        checked: Boolean(input && input.checked) ||
                            String(element.className || "").toLowerCase().includes("checked") ||
                            String(element.getAttribute("aria-checked") || "").toLowerCase() === "true"
                    };
                })
                .filter((item) => visible(item.element) && wanted.includes(item.text))
                .sort((a, b) => Math.abs(a.top - b.top) > 8 ? a.top - b.top : a.left - b.left);

            if (!candidates.length) {
                return {ok: false, reason: "No visible language checkbox found", names};
            }

            const item = candidates[0];
            if (!item.checked) {
                const clickable =
                    item.element.querySelector(".mat-checkbox-inner-container") ||
                    item.element.querySelector(".mdc-checkbox") ||
                    item.input ||
                    item.element;
                clickable.click();
            }
            return {ok: true, clicked: !item.checked, text: item.text, top: item.top, left: item.left};
        }
        """,
        {"names": names},
    )
    if result and result.get("ok"):
        page.wait_for_timeout(1000 if result.get("clicked") else 300)
        return True
    log(f"Language checkbox safe selection failed: {result}")
    return False


def open_english_tab(page):
    click_language_tab(page, ["English", "EN"])
    page.wait_for_timeout(QBG_TAB_SETTLE_MS)


def open_or_add_hindi_tab(page):
    hindi_names = ["Hindi", "हिन्दी", "हिंदी"]
    if click_language_tab(page, hindi_names):
        page.wait_for_timeout(QBG_TAB_SETTLE_MS)
        return

    if not ensure_language_checkbox_checked(page, hindi_names):
        shot = debug_path("hindi_checkbox_error.png")
        page.screenshot(path=shot, full_page=True)
        raise QbgBridgeError(f"Could not tick Hindi checkbox. Screenshot saved: {shot}")

    if click_language_tab(page, hindi_names):
        page.wait_for_timeout(QBG_TAB_SETTLE_MS)
        return

    page.wait_for_timeout(350)
    if click_language_tab(page, hindi_names):
        page.wait_for_timeout(QBG_TAB_SETTLE_MS)
        return

    shot = debug_path("hindi_tab_error.png")
    page.screenshot(path=shot, full_page=True)
    raise QbgBridgeError(f"Could not find Hindi tab after selecting Hindi. Screenshot saved: {shot}")


def open_hindi_tab_if_available(page, log=print):
    hindi_names = ["Hindi", "हिन्दी", "हिंदी"]
    if click_language_tab(page, hindi_names):
        page.wait_for_timeout(QBG_TAB_SETTLE_MS)
        expand_solution_sections(page, log=log)
        return True
    log("Hindi tab is not enabled on QBG; skipped Hindi correct-answer sync.")
    return False


def open_language_tab(page, language):
    if normalize_language(language) == "Hindi":
        open_or_add_hindi_tab(page)
    else:
        open_english_tab(page)
    expand_solution_sections(page)


# ---------------------------------------------------------- form settling ----
def wait_for_qbg_form_to_settle(page, reason="QBG form", log=print):
    """Wait adaptively for a usable, stable QBG edit form.

    Readiness is judged by the real TinyMCE/select/Update controls rather than
    fixed sleeps. Two consecutive stable samples are normally enough.
    """
    wait_for_editors(page)
    if QBG_FORM_SETTLE_MS > 0:
        page.wait_for_timeout(QBG_FORM_SETTLE_MS)

    last_signature = None
    stable_checks = 0
    deadline = time.time() + min(max(5.0, LONG_TIMEOUT / 1000), 12.0)
    while time.time() < deadline:
        try:
            signature = page.evaluate(
                r"""
                () => {
                    const visible = (element) => {
                        if (!element || typeof element.getBoundingClientRect !== "function") return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 &&
                            style.visibility !== "hidden" && style.display !== "none";
                    };
                    const editors = window.tinymce && window.tinymce.editors
                        ? Array.from(window.tinymce.editors).filter((editor) => {
                            const iframe = editor.iframeElement || document.getElementById(editor.id + "_ifr");
                            return iframe && visible(iframe);
                        }).length
                        : 0;
                    const selects = Array.from(document.querySelectorAll("mat-select, select"))
                        .filter(visible).length;
                    const updateButtons = Array.from(document.querySelectorAll("button"))
                        .filter(visible)
                        .filter((button) => {
                            const text = (button.innerText || button.textContent || "")
                                .replace(/\s+/g, " ").trim().toLowerCase();
                            return text === "update" || text === "save";
                        }).length;
                    return `${editors}|${selects}|${updateButtons}`;
                }
                """
            )
        except Exception:
            signature = None

        usable = False
        if signature:
            try:
                editor_count, _, update_count = [int(value) for value in signature.split("|")]
                usable = editor_count > 0 and update_count > 0
            except (ValueError, AttributeError):
                usable = False

        if usable and signature == last_signature:
            stable_checks += 1
        else:
            last_signature = signature
            stable_checks = 0

        if usable and stable_checks >= QBG_STABLE_CHECKS:
            log(f"{reason} loaded and stable: {signature}")
            return signature
        page.wait_for_timeout(QBG_STABLE_POLL_MS)

    log(f"{reason} stability wait ended with signature: {last_signature}")
    return last_signature


# -------------------------------------------------------- type detection ----
def get_visible_select_values(page, log=print):
    try:
        result = page.evaluate(
            """
            () => {
                const visible = (element) => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none";
                };
                const norm = (value) => (value || "").replace(/\\s+/g, " ").trim();
                const matValues = Array.from(document.querySelectorAll("mat-select"))
                    .filter((element) => visible(element))
                    .map((element) => {
                        const rect = element.getBoundingClientRect();
                        const valueNode = element.querySelector(".mat-select-value-text, .mat-mdc-select-value-text, .mat-select-value, .mat-mdc-select-value");
                        const value = norm(valueNode ? valueNode.innerText || valueNode.textContent : element.innerText || element.textContent);
                        return {value, top: rect.top, left: rect.left};
                    })
                    .filter((item) => item.value && !item.value.includes("-Select-") && item.value.toLowerCase() !== "select");

                const nativeValues = Array.from(document.querySelectorAll("select"))
                    .filter((element) => visible(element))
                    .map((element) => {
                        const rect = element.getBoundingClientRect();
                        const option = element.options[element.selectedIndex];
                        const value = norm(option && (option.text || option.value) || element.value || "");
                        return {value, top: rect.top, left: rect.left};
                    })
                    .filter((item) => item.value && !item.value.includes("-Select-") && item.value.toLowerCase() !== "select");

                return matValues.concat(nativeValues)
                    .sort((a, b) => Math.abs(a.top - b.top) > 8 ? a.top - b.top : a.left - b.left)
                    .map((item) => item.value);
            }
            """
        )
        if result:
            log(f"Visible select values: {result}")
        return result
    except Exception:
        return []


def get_current_question_type_from_radio(page, log=print):
    """Read only the checked radio inside/nearest the QBG Question Type section."""
    from .textops import normalize_question_type_value

    try:
        result = page.evaluate(
            r"""
            () => {
                const norm = (value) => (value || "").replace(/\s+/g, " ").trim();
                const upper = (value) => norm(value).toUpperCase();
                const visible = (element) => {
                    if (!element || typeof element.getBoundingClientRect !== "function") return false;
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== "hidden" && style.display !== "none";
                };
                const isChecked = (element) => {
                    const input = element.matches("input[type='radio']")
                        ? element
                        : element.querySelector("input[type='radio']");
                    if (input && input.checked) return true;
                    if (String(element.getAttribute("aria-checked") || "").toLowerCase() === "true") return true;
                    const cls = String(element.className || "").toLowerCase();
                    return cls.includes("checked") || cls.includes("selected");
                };
                const directLabel = (element) => {
                    const candidates = [
                        element.getAttribute && element.getAttribute("aria-label"),
                        element.getAttribute && element.getAttribute("value"),
                        element.querySelector && element.querySelector(".mat-radio-label-content, .mdc-label, label"),
                    ];
                    for (const candidate of candidates) {
                        const value = typeof candidate === "string"
                            ? norm(candidate)
                            : norm(candidate && (candidate.innerText || candidate.textContent));
                        if (value) return value;
                    }
                    return norm(element.innerText || element.textContent || "");
                };
                const questionTypeDistance = (element) => {
                    const rect = element.getBoundingClientRect();
                    const labels = Array.from(document.querySelectorAll("body *"))
                        .filter((node) => visible(node))
                        .map((node) => {
                            const text = upper(node.innerText || node.textContent || "");
                            const r = node.getBoundingClientRect();
                            return {node, text, rect: r};
                        })
                        .filter((item) => item.text === "QUESTION TYPE" || item.text === "QUESTION TYPE *");
                    let best = 1e9;
                    for (const item of labels) {
                        const vertical = Math.abs(rect.top - item.rect.bottom);
                        const horizontal = Math.max(0, item.rect.left - rect.right, rect.left - item.rect.right);
                        const distance = vertical + horizontal;
                        if (distance < best) best = distance;
                    }
                    return best;
                };
                const contextHasQuestionType = (element) => {
                    let node = element;
                    for (let depth = 0; node && depth < 8; depth += 1) {
                        const text = upper(node.innerText || node.textContent || "");
                        if (text.includes("QUESTION TYPE")) return true;
                        node = node.parentElement;
                    }
                    return false;
                };

                const seen = new Set();
                const controls = [];
                for (const element of Array.from(document.querySelectorAll("mat-radio-button, [role='radio'], input[type='radio']"))) {
                    if (!visible(element) || !isChecked(element)) continue;
                    const root = element.matches("input[type='radio']")
                        ? (element.closest("mat-radio-button, [role='radio']") || element)
                        : element;
                    if (seen.has(root)) continue;
                    seen.add(root);
                    const rect = root.getBoundingClientRect();
                    const label = directLabel(root);
                    controls.push({
                        label,
                        inQuestionTypeSection: contextHasQuestionType(root),
                        distance: questionTypeDistance(root),
                        top: rect.top,
                        left: rect.left,
                    });
                }

                controls.sort((a, b) => {
                    if (a.inQuestionTypeSection !== b.inQuestionTypeSection) {
                        return a.inQuestionTypeSection ? -1 : 1;
                    }
                    if (Math.abs(a.distance - b.distance) > 5) return a.distance - b.distance;
                    if (Math.abs(a.top - b.top) > 8) return a.top - b.top;
                    return a.left - b.left;
                });
                return controls;
            }
            """
        )
    except Exception as exc:
        log(f"Could not read question type radio safely: {exc}")
        return ""

    # Accept only a checked radio that is actually in, or very close to, the
    # Question Type section. Never use an unrelated checked radio as fallback.
    for item in result or []:
        if not item.get("inQuestionTypeSection") and float(item.get("distance") or 1e9) > 260:
            continue
        question_type = normalize_question_type_value(item.get("label") or "")
        if question_type:
            log(f"Current question type from radio: {question_type}")
            return question_type

    log("Question type could not be safely identified from the radio controls.")
    return ""


def get_main_question_type_from_select_values(values, log=print):
    from .textops import normalize_question_type_value

    for value in values:
        question_type = normalize_question_type_value(value)
        if question_type:
            log(f"Main question type: {question_type}")
            return question_type
    return ""
