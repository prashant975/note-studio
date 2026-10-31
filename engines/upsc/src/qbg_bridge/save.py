"""Clicking Update and proving the write left the building.

Copied from the Solution Addition Bot. The page's own XHR/fetch is wrapped so
the outgoing payload can be inspected (and, on a retry only, its
``xCategoryTags`` objects converted to plain string IDs — some QBG records
otherwise fail backend validation). Nothing else in the payload is ever
touched, and Other Solutions is never written.

Success is decided in layers: the best evidence is a captured full-question
request whose payload visibly contains the text that was typed; failing that,
the latest full-question response after activity settles; the reopen
verification in client.py remains the final word either way.
"""
from __future__ import annotations

import json
import re
import time

from .errors import QbgBridgeError
from .settings import (
    NORMAL_TIMEOUT,
    QBG_PATCH_OBSERVED_GRACE_MS,
    QBG_SAVE_GRACE_MS,
    QBG_SAVE_RESPONSE_WAIT_SECONDS,
    debug_path,
)
from .textops import clean, normalize_language, solution_compare_key


def install_qbg_payload_patch(page, target_language, patch_xcategory=False, log=print):
    """Install a narrowly-scoped QBG request patch.

    On the normal first attempt no request field is changed. On a retry only,
    ``xCategoryTags`` objects are converted to their string IDs. Every other
    payload field, including Other Solutions, is preserved exactly as QBG
    produced it.
    """
    language_key = "hindi" if normalize_language(target_language) == "Hindi" else "english"

    result = page.evaluate(
        r"""
        ({languageKey, patchXCategory}) => {
            const state = window.__QBG_SOLUTION_PAYLOAD_PATCH__ || {};
            state.languageKey = languageKey || "english";
            state.patchXCategory = Boolean(patchXCategory);
            state.lastPatch = null;
            state.lastError = "";

            const idKeys = [
                "_id", "id", "uuid", "unique_id", "uniqueId",
                "xCategoryTagId", "x_category_tag_id",
                "categoryTagId", "category_tag_id", "tagId", "tag_id",
                "categoryId", "category_id", "value", "key", "code", "slug"
            ];

            const cleanString = (value) => {
                if (typeof value !== "string") return "";
                return value.trim();
            };

            const stringIdFromObject = (item, seen = new Set(), depth = 0) => {
                if (typeof item === "string") return cleanString(item);
                if (!item || typeof item !== "object" || Array.isArray(item)) return "";
                if (depth > 12 || seen.has(item)) return "";
                seen.add(item);

                // QBG sometimes nests the same identifier repeatedly, e.g.
                // {unique_id: {unique_id: {unique_id: "actual-tag-id"}}}.
                for (const key of idKeys) {
                    const rawValue = item[key];
                    const directValue = cleanString(rawValue);
                    if (directValue) return directValue;
                    if (rawValue && typeof rawValue === "object" && !Array.isArray(rawValue)) {
                        const nestedValue = stringIdFromObject(rawValue, seen, depth + 1);
                        if (nestedValue) return nestedValue;
                    }
                }

                for (const [key, rawValue] of Object.entries(item)) {
                    if (!/(^|_)(id|uuid)$|Id$|ID$/.test(key)) continue;
                    const directValue = cleanString(rawValue);
                    if (directValue) return directValue;
                    if (rawValue && typeof rawValue === "object" && !Array.isArray(rawValue)) {
                        const nestedValue = stringIdFromObject(rawValue, seen, depth + 1);
                        if (nestedValue) return nestedValue;
                    }
                }

                for (const containerKey of ["tag", "category", "xCategoryTag", "data", "meta"]) {
                    const nested = item[containerKey];
                    if (!nested || typeof nested !== "object" || Array.isArray(nested)) continue;
                    const nestedValue = stringIdFromObject(nested, seen, depth + 1);
                    if (nestedValue) return nestedValue;
                }
                return "";
            };

            const patchPayloadObject = (payload) => {
                if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
                    return {payload, patched: false, reason: "Payload is not a JSON object"};
                }

                let tagsPatched = false;
                let oldTagTypes = [];
                let newTags = [];

                if (state.patchXCategory && Array.isArray(payload.xCategoryTags)) {
                    oldTagTypes = payload.xCategoryTags.map((item) =>
                        Array.isArray(item) ? "array" : typeof item
                    );
                    newTags = payload.xCategoryTags.map(stringIdFromObject);
                    const missingIndexes = [];
                    newTags.forEach((value, index) => {
                        if (!value) missingIndexes.push(index);
                    });
                    if (missingIndexes.length) {
                        const keys = missingIndexes.map((index) => {
                            const item = payload.xCategoryTags[index];
                            return item && typeof item === "object" && !Array.isArray(item)
                                ? Object.keys(item)
                                : [];
                        });
                        throw new Error(
                            `Could not extract string ID from xCategoryTags index(es) ${missingIndexes.join(", ")}. ` +
                            `Object keys: ${JSON.stringify(keys)}`
                        );
                    }
                    newTags = newTags.filter((value, index, values) => values.indexOf(value) === index);
                    payload.xCategoryTags = newTags;
                    tagsPatched = true;
                }

                state.lastPatch = {
                    tagsPatched,
                    hadSolutionsArray: Array.isArray(payload.solutions),
                    oldTagTypes,
                    newTags,
                    languageKey: state.languageKey,
                    patchXCategory: Boolean(state.patchXCategory),
                    otherSolutionTouched: false,
                };
                state.patchHistory = Array.isArray(state.patchHistory) ? state.patchHistory : [];
                state.patchHistory.push(state.lastPatch);
                if (state.patchHistory.length > 12) state.patchHistory = state.patchHistory.slice(-12);
                state.lastError = "";
                return {payload, patched: tagsPatched, details: state.lastPatch};
            };

            const shouldPatch = (method, url) => {
                const m = String(method || "").toUpperCase();
                const u = String(url || "");
                return ["POST", "PUT", "PATCH"].includes(m) &&
                    /api\.penpencil\.co\/qbg\/questions\//i.test(u);
            };

            const patchBody = (body) => {
                if (typeof body !== "string" || !body.trim().startsWith("{")) return body;
                const parsed = JSON.parse(body);
                const patched = patchPayloadObject(parsed);
                return JSON.stringify(patched.payload);
            };

            if (!state.installed) {
                state.installed = true;

                const originalOpen = XMLHttpRequest.prototype.open;
                const originalSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this.__qbgPatchMethod = method;
                    this.__qbgPatchUrl = url;
                    return originalOpen.call(this, method, url, ...rest);
                };
                XMLHttpRequest.prototype.send = function(body) {
                    try {
                        if (shouldPatch(this.__qbgPatchMethod, this.__qbgPatchUrl)) {
                            body = patchBody(body);
                        }
                    } catch (error) {
                        state.lastError = String(error && error.message || error);
                        console.error("QBG payload patch failed:", error);
                        throw error;
                    }
                    return originalSend.call(this, body);
                };

                const originalFetch = window.fetch.bind(window);
                window.fetch = async function(input, init = {}) {
                    try {
                        const url = typeof input === "string" ? input : (input && input.url || "");
                        const method = init.method || (input && input.method) || "GET";
                        if (shouldPatch(method, url) && typeof init.body === "string") {
                            init = {...init, body: patchBody(init.body)};
                        }
                    } catch (error) {
                        state.lastError = String(error && error.message || error);
                        console.error("QBG fetch payload patch failed:", error);
                        throw error;
                    }
                    return originalFetch(input, init);
                };
            }

            window.__QBG_SOLUTION_PAYLOAD_PATCH__ = state;
            return {
                ok: true,
                installed: state.installed,
                languageKey: state.languageKey,
                patchXCategory: state.patchXCategory,
            };
        }
        """,
        {"languageKey": language_key, "patchXCategory": bool(patch_xcategory)},
    )
    if not result or not result.get("ok"):
        raise QbgBridgeError(f"Could not install QBG request patch: {result}")

    if patch_xcategory:
        log(
            f"Installed retry-only X Category payload fix for {target_language}. "
            "Only visible editors are written; Other Solutions is untouched."
        )
    else:
        log(
            f"Using normal QBG save for {target_language}. Only visible editors "
            "are written; Other Solutions is untouched."
        )


def get_qbg_payload_patch_state(page):
    try:
        return page.evaluate(
            """
            () => {
                const state = window.__QBG_SOLUTION_PAYLOAD_PATCH__ || {};
                return {
                    lastPatch: state.lastPatch || null,
                    patchHistory: state.patchHistory || [],
                    lastError: state.lastError || "",
                };
            }
            """
        )
    except Exception as exc:
        return {"lastPatch": None, "patchHistory": [], "lastError": f"Could not read patch state: {exc}"}


def save_qbg(page, expected_text="", target_language="English",
             use_xcategory_fallback=False, log=print):
    """Click Update and wait for QBG's full-question write.

    ``expected_text`` is any text just typed into an editor (the solution when
    one was written, otherwise the question or an option); seeing it inside an
    outgoing payload is the strongest available signal that the right request
    left. The reopen verification in client.py remains the final word.
    """
    if use_xcategory_fallback:
        log("Waiting for delayed X Category background activity to finish before retry Update...")
        page.wait_for_timeout(1400)
    else:
        log("Trying normal QBG Update without touching X Category Tags or Other Solutions...")
        page.wait_for_timeout(120)

    install_qbg_payload_patch(
        page,
        target_language,
        patch_xcategory=use_xcategory_fallback,
        log=log,
    )

    expected_fragment = solution_compare_key(expected_text)[:50]
    write_responses = []

    def is_qbg_question_write(response):
        try:
            method = (response.request.method or "").upper()
            return (
                method in {"POST", "PUT", "PATCH"}
                and re.search(r"api\.penpencil\.co/qbg/questions/", response.url, flags=re.I)
            )
        except Exception:
            return False

    def request_json_for(response):
        try:
            value = response.request.post_data_json
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def response_contains_expected(response):
        try:
            payload = response.request.post_data or ""
        except Exception:
            payload = ""
        return bool(expected_fragment and expected_fragment in solution_compare_key(payload))

    def response_has_full_question_payload(response):
        payload_json = request_json_for(response)
        return isinstance(payload_json, dict) and isinstance(payload_json.get("solutions"), list)

    def record_response(response):
        if is_qbg_question_write(response):
            write_responses.append(response)

    page.on("response", record_response)
    try:
        exact_buttons = page.get_by_role("button", name=re.compile(r"^(UPDATE|SAVE)$", re.I))
        visible_buttons = []
        for index in range(exact_buttons.count()):
            candidate = exact_buttons.nth(index)
            try:
                if candidate.is_visible() and candidate.is_enabled():
                    visible_buttons.append(candidate)
            except Exception:
                pass

        log("Clicking QBG Update and waiting for the full question payload...")
        if visible_buttons:
            button = visible_buttons[-1]
            button.scroll_into_view_if_needed(timeout=NORMAL_TIMEOUT)
            button.click(timeout=NORMAL_TIMEOUT)
        else:
            clicked = page.evaluate(
                r"""
                () => {
                    const visible = (element) => {
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 &&
                            style.visibility !== "hidden" && style.display !== "none" &&
                            !element.disabled;
                    };
                    const buttons = Array.from(document.querySelectorAll("button"))
                        .filter(visible)
                        .filter((element) => {
                            const text = (element.innerText || element.textContent || "")
                                .replace(/\s+/g, " ").trim().toLowerCase();
                            return text === "update" || text === "save";
                        });
                    const target = buttons[buttons.length - 1];
                    if (!target) return false;
                    target.scrollIntoView({block: "center", inline: "center"});
                    target.click();
                    return true;
                }
                """
            )
            if not clicked:
                shot = debug_path("save_button_error.png")
                page.screenshot(path=shot, full_page=True)
                raise QbgBridgeError(f"Could not click the QBG Update button. Screenshot saved: {shot}")

        click_started_at = time.time()
        deadline = click_started_at + QBG_SAVE_RESPONSE_WAIT_SECONDS
        matched_response = None
        last_count = -1
        last_activity = time.time()

        while time.time() < deadline:
            page.wait_for_timeout(180)

            if len(write_responses) != last_count:
                last_count = len(write_responses)
                last_activity = time.time()

            # Best match: the full outgoing payload visibly contains the text
            # that was typed into the editor.
            for candidate in reversed(write_responses):
                if response_contains_expected(candidate):
                    matched_response = candidate
                    break
            if matched_response is not None:
                page.wait_for_timeout(250)
                break

            # Otherwise accept the latest full-question response after network
            # activity settles. Post-save reopen verification remains final.
            full_candidates = [
                candidate for candidate in write_responses
                if response_has_full_question_payload(candidate)
            ]
            if full_candidates:
                latest_full = full_candidates[-1]
                if latest_full.status >= 400 or time.time() - last_activity >= 0.8:
                    matched_response = latest_full
                    break

            # Request interception can finish before Playwright exposes the
            # matching response event. Once the patch confirms a full solutions
            # payload was sent and no patch error occurred, stop waiting after
            # a short grace and rely on the mandatory reopen verification.
            live_patch_state = get_qbg_payload_patch_state(page)
            live_history = live_patch_state.get("patchHistory") or []
            saw_full_payload_live = any(
                isinstance(item, dict) and item.get("hadSolutionsArray")
                for item in live_history
            )
            if live_patch_state.get("lastError") or (
                saw_full_payload_live
                and (time.time() - click_started_at) * 1000 >= QBG_PATCH_OBSERVED_GRACE_MS
            ):
                break

        patch_state = get_qbg_payload_patch_state(page)
        patch_error = clean(patch_state.get("lastError") or "")
        patch_history = patch_state.get("patchHistory") or []
        patch_details = patch_state.get("lastPatch")

        if patch_error:
            shot = debug_path("qbg_payload_patch_error.png")
            page.screenshot(path=shot, full_page=True)
            raise QbgBridgeError(
                f"QBG X Category payload fix failed before the API request: {patch_error}. "
                f"Screenshot saved: {shot}"
            )

        if patch_history:
            log("QBG request history (Other Solutions untouched):")
            for index, item in enumerate(patch_history[-8:], start=max(1, len(patch_history) - 7)):
                log(f"  request {index}: {item}")
        elif patch_details:
            log(f"QBG request patch details: {patch_details}")

        if matched_response is None:
            full_candidates = [
                candidate for candidate in write_responses
                if response_has_full_question_payload(candidate)
            ]
            if full_candidates:
                matched_response = full_candidates[-1]

        if matched_response is None:
            error_candidates = [candidate for candidate in write_responses if candidate.status >= 400]
            if error_candidates:
                matched_response = error_candidates[-1]

        if matched_response is None:
            saw_full_payload = any(
                bool(item and item.get("hadSolutionsArray"))
                for item in patch_history
                if isinstance(item, dict)
            )
            if not saw_full_payload:
                summaries = []
                for response in write_responses[-10:]:
                    try:
                        payload = response.request.post_data or ""
                        payload_json = request_json_for(response)
                        summaries.append(
                            {
                                "status": response.status,
                                "method": response.request.method,
                                "url": response.url,
                                "payload_keys": sorted(payload_json.keys()) if isinstance(payload_json, dict) else [],
                                "payload_length": len(payload),
                                "contains_expected_text": response_contains_expected(response),
                                "has_solutions_array": response_has_full_question_payload(response),
                            }
                        )
                    except Exception as exc:
                        summaries.append({"error": str(exc)})

                log(f"Captured QBG write responses while waiting: {summaries or '<none>'}")
                shot = debug_path("qbg_full_update_request_missing.png")
                page.screenshot(path=shot, full_page=True)
                raise QbgBridgeError(
                    f"QBG did not send a full Update request within {QBG_SAVE_RESPONSE_WAIT_SECONDS:g} seconds. "
                    f"Screenshot saved: {shot}"
                )

        if matched_response is not None:
            response = matched_response
            status = response.status
            request = response.request
            payload = request.post_data or ""
            contains_new_value = response_contains_expected(response)
            log(
                f"QBG full save response: {status} {request.method} {response.url} | "
                f"payload contains the typed text: {contains_new_value} | Other Solutions touched: False"
            )

            if status >= 400:
                try:
                    response_body = response.text() or ""
                except Exception as exc:
                    response_body = f"<could not read response body: {exc}>"

                request_json = request_json_for(response)
                diagnostic = {
                    "status": status,
                    "method": request.method,
                    "url": response.url,
                    "response_body": response_body,
                    "payload_contains_expected_text": contains_new_value,
                    "other_solution_touched_by_bot": False,
                    "payload_patch_state": patch_state,
                    "xCategoryTags": (
                        request_json.get("xCategoryTags")
                        if isinstance(request_json, dict) else None
                    ),
                    "solutions": (
                        request_json.get("solutions")
                        if isinstance(request_json, dict) else None
                    ),
                    "raw_post_data_start": payload[:4000],
                }
                diagnostic_path = debug_path("qbg_http_error_diagnostic.json")
                try:
                    with open(diagnostic_path, "w", encoding="utf-8") as handle:
                        json.dump(diagnostic, handle, ensure_ascii=False, indent=2, default=str)
                except OSError as exc:
                    log(f"Could not write {diagnostic_path}: {exc}")

                compact_body = clean(response_body)[:1800]
                log(f"QBG HTTP {status} response body: {compact_body or '<empty response body>'}")
                shot = debug_path("qbg_http_save_error.png")
                page.screenshot(path=shot, full_page=True)
                raise QbgBridgeError(
                    f"QBG save request failed with HTTP {status}: {response.url}. "
                    f"Backend message: {compact_body or 'empty response body'}. "
                    f"Diagnostic saved: {diagnostic_path}"
                )
        else:
            log(
                "A full QBG payload was observed but its response was not captured; "
                "continuing to reopen and verify the edited fields."
            )

        page.wait_for_timeout(QBG_SAVE_GRACE_MS)
    finally:
        try:
            page.remove_listener("response", record_response)
        except Exception:
            pass
