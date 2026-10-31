"""TinyMCE editor discovery and writing, copied from the Solution Addition Bot.

The QBG edit form is a wall of unlabeled TinyMCE iframes. The JavaScript here
reads the DOM geometry around each iframe — headings above it, an A/B/C/D
marker beside it — and classifies every editor as question, option, correct
answer or solution. The classification functions are pure Python over those
readings, so they are unit-tested with synthetic fields.

Writing goes through real keyboard input, not editor.setContent(): on some QBG
pages the Angular form model keeps the previous value unless genuine input
events arrive.
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import QbgBridgeError
from .settings import LONG_TIMEOUT, NORMAL_TIMEOUT, SHORT_TIMEOUT, debug_path
from .textops import clean, html_to_ai_text, solution_compare_key, text_to_plain


@dataclass
class EditorField:
    index: int
    label: str
    html: str
    text: str
    iframe_id: str = ""
    option_marker: str = ""
    near_label: str = ""
    section_label: str = ""
    role_hint: str = ""
    top: float = 0.0
    left: float = 0.0


def wait_for_editors(page):
    page.wait_for_function(
        "() => window.tinymce && window.tinymce.editors && window.tinymce.editors.length > 0",
        timeout=LONG_TIMEOUT,
    )


def get_visible_editors(page):
    wait_for_editors(page)
    data = page.evaluate(
        r"""
        () => {
            const norm = (value) => (value || "").replace(/\s+/g, " ").trim();
            const lower = (value) => norm(value).toLowerCase();
            const editors = Array.from(window.tinymce.editors || []);
            const visible = (element) => {
                if (!element || typeof element.getBoundingClientRect !== "function") return false;
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0 &&
                    rect.height > 0 &&
                    style.visibility !== "hidden" &&
                    style.display !== "none";
            };

            const textItems = Array.from(document.querySelectorAll("body *"))
                .filter((element) => visible(element))
                .map((element) => {
                    const rect = element.getBoundingClientRect();
                    const text = norm(element.innerText || element.textContent || "");
                    return {
                        element,
                        text,
                        textLower: lower(text),
                        top: rect.top,
                        bottom: rect.bottom,
                        left: rect.left,
                        right: rect.right,
                        width: rect.width,
                        height: rect.height,
                        centerY: rect.top + rect.height / 2,
                        area: rect.width * rect.height,
                    };
                })
                .filter((item) => item.text && item.text.length <= 160 && item.area <= 45000);

            const optionLabels = textItems
                .filter((item) => /^[A-D]$/i.test(item.text))
                .sort((a, b) => a.area - b.area);

            const optionMarkerFor = (rect) => {
                const centerY = rect.top + rect.height / 2;
                const candidates = optionLabels
                    .filter((item) =>
                        item.right <= rect.left + 35 &&
                        Math.abs(item.centerY - centerY) <= Math.max(90, rect.height / 2)
                    )
                    .sort((a, b) => {
                        const dy = Math.abs(a.centerY - centerY) - Math.abs(b.centerY - centerY);
                        if (Math.abs(dy) > 1) return dy;
                        return Math.abs(a.right - rect.left) - Math.abs(b.right - rect.left);
                    });
                return candidates.length ? candidates[0].text.toUpperCase() : "";
            };

            const isSolutionLabel = (text) => {
                const t = lower(text).replace(/[:*]+$/g, "").trim();
                return [
                    "solution",
                    "solutions",
                    "text solution",
                    "text solutions",
                    "हल",
                    "समाधान",
                ].includes(t);
            };

            const isQuestionLabel = (text) => {
                const t = lower(text).replace(/[:*]+$/g, "").trim();
                return t === "question" || t === "प्रश्न" || t === "question text" || t === "question statement";
            };

            const isCorrectAnswerLabel = (text) => {
                const t = lower(text).replace(/[:*]+$/g, "").trim();
                return t === "correct answer" ||
                    t === "correct answers" ||
                    t === "correct ans" ||
                    t.includes("correct answer") ||
                    t.includes("सही उत्तर") ||
                    t.includes("correct numerical") ||
                    t.includes("numerical answer");
            };

            const isAnswerOptionHeading = (text) => {
                const t = lower(text).replace(/[:*]+$/g, "").trim();
                return t === "answers options" ||
                    t === "answer options" ||
                    t === "options" ||
                    t === "विकल्प" ||
                    t.includes("answer option") ||
                    t.includes("answers option");
            };

            const nearbyTextForRect = (rect) => {
                const centerY = rect.top + rect.height / 2;
                const candidates = textItems
                    .filter((item) => {
                        if (!item.text || item.text.length > 120) return false;
                        const above = item.bottom <= rect.top + 12 && rect.top - item.bottom <= 110;
                        const leftSide = item.right <= rect.left + 35 && Math.abs(item.centerY - centerY) <= 85;
                        const sameRow = Math.abs(item.centerY - centerY) <= 45 && item.left <= rect.left + 30;
                        return above || leftSide || sameRow;
                    })
                    .sort((a, b) => {
                        const aAbove = a.bottom <= rect.top + 12 ? Math.abs(rect.top - a.bottom) : 9999;
                        const bAbove = b.bottom <= rect.top + 12 ? Math.abs(rect.top - b.bottom) : 9999;
                        if (Math.abs(aAbove - bAbove) > 2) return aAbove - bAbove;
                        return Math.abs(a.centerY - centerY) - Math.abs(b.centerY - centerY);
                    });
                return candidates.length ? candidates[0].text : "";
            };

            const semanticLabelsAbove = (rect) => {
                const predicates = [
                    {role: "solution", fn: isSolutionLabel, priority: 100},
                    {role: "correct_answer", fn: isCorrectAnswerLabel, priority: 90},
                    {role: "question", fn: isQuestionLabel, priority: 80},
                    {role: "options", fn: isAnswerOptionHeading, priority: 70},
                ];
                const all = [];
                for (const item of textItems) {
                    if (item.bottom > rect.top + 14) continue;
                    const verticalDistance = rect.top - item.bottom;
                    if (verticalDistance > 420) continue;
                    const horizontalOk = item.left <= rect.right + 80 && item.right >= rect.left - 260;
                    if (!horizontalOk) continue;
                    for (const predicate of predicates) {
                        if (predicate.fn(item.text)) {
                            all.push({
                                role: predicate.role,
                                text: item.text,
                                distance: verticalDistance,
                                priority: predicate.priority,
                                top: item.top,
                                left: item.left,
                            });
                        }
                    }
                }
                return all.sort((a, b) => {
                    if (Math.abs(a.distance - b.distance) > 8) return a.distance - b.distance;
                    return b.priority - a.priority;
                });
            };

            const roleForRect = (rect, optionMarker) => {
                if (optionMarker) return "option";
                const labels = semanticLabelsAbove(rect);
                if (!labels.length) return "";
                const closeSolution = labels.find((item) => item.role === "solution" && item.distance <= 260);
                if (closeSolution) return "solution";
                const closeCorrect = labels.find((item) => item.role === "correct_answer" && item.distance <= 220);
                if (closeCorrect) return "correct_answer";
                const closeQuestion = labels.find((item) => item.role === "question" && item.distance <= 220);
                if (closeQuestion) return "question";
                return labels[0].role || "";
            };

            const sectionLabelFor = (rect) => {
                const labels = semanticLabelsAbove(rect);
                return labels.length ? labels[0].text : "";
            };

            return editors
                .map((editor, index) => {
                    const iframe = editor.iframeElement || document.getElementById(editor.id + "_ifr");
                    if (!iframe) return null;
                    const rect = iframe.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) return null;

                    const optionMarker = optionMarkerFor(rect);
                    const sectionLabel = sectionLabelFor(rect);
                    const roleHint = roleForRect(rect, optionMarker);

                    const chunks = [];
                    let node = iframe;
                    for (let depth = 0; node && depth < 8; depth += 1) {
                        chunks.push(node.innerText || node.textContent || "");
                        node = node.parentElement;
                    }
                    const bodyHtml = (() => {
                        try {
                            return iframe.contentDocument && iframe.contentDocument.body
                                ? iframe.contentDocument.body.innerHTML || ""
                                : "";
                        } catch (error) {
                            return "";
                        }
                    })();
                    const bodyText = (() => {
                        try {
                            return iframe.contentDocument && iframe.contentDocument.body
                                ? iframe.contentDocument.body.innerText || iframe.contentDocument.body.textContent || ""
                                : "";
                        } catch (error) {
                            return "";
                        }
                    })();
                    const bodyMathText = (() => {
                        try {
                            const body = iframe.contentDocument && iframe.contentDocument.body;
                            if (!body) return "";
                            const values = [];
                            const add = (value) => {
                                const cleaned = norm(value || "");
                                if (cleaned && !values.includes(cleaned)) values.push(cleaned);
                            };
                            Array.from(body.querySelectorAll("img, math, [role='math']")).forEach((element) => {
                                add(element.getAttribute("alt"));
                                add(element.getAttribute("title"));
                                add(element.getAttribute("aria-label"));
                                add(element.getAttribute("data-mathml"));
                                add(element.getAttribute("data-latex"));
                                add(element.getAttribute("data-tex"));
                                add(element.innerText || element.textContent || "");
                            });
                            return values.join(" ");
                        } catch (error) {
                            return "";
                        }
                    })();

                    return {
                        index,
                        iframeId: iframe.id || "",
                        top: rect.top,
                        left: rect.left,
                        optionMarker,
                        nearLabel: nearbyTextForRect(rect),
                        sectionLabel,
                        roleHint,
                        label: norm(chunks.join(" ")).slice(0, 800),
                        html: bodyHtml || editor.getContent() || "",
                        text: editor.getContent({format: "text"}) || bodyText || bodyMathText || "",
                    };
                })
                .filter(Boolean)
                .sort((a, b) => Math.abs(a.top - b.top) > 8 ? a.top - b.top : a.left - b.left);
        }
        """
    )
    return [
        EditorField(
            index=item["index"],
            label=clean(item.get("label")),
            html=item.get("html") or "",
            text=clean(item.get("text") or html_to_ai_text(item.get("html") or "") or text_to_plain(item.get("html") or "")),
            iframe_id=item.get("iframeId") or "",
            option_marker=clean(item.get("optionMarker") or ""),
            near_label=clean(item.get("nearLabel") or ""),
            section_label=clean(item.get("sectionLabel") or ""),
            role_hint=clean(item.get("roleHint") or ""),
            top=float(item.get("top") or 0),
            left=float(item.get("left") or 0),
        )
        for item in data
    ]


# ------------------------------------------------------- classification ----
def editor_label_lower(field):
    return clean(" ".join([
        getattr(field, "near_label", ""),
        getattr(field, "section_label", ""),
        getattr(field, "role_hint", ""),
        field.label,
        field.option_marker,
    ])).lower()


def editor_near_label_lower(field):
    return clean(field.near_label).lower()


def editor_section_label_lower(field):
    return clean(getattr(field, "section_label", "")).lower()


def editor_role_hint(field):
    return clean(getattr(field, "role_hint", "")).lower()


def is_not_available_text(value):
    text = clean(value).lower()
    return text in {
        "--not available--",
        "not available",
        "-not available-",
        "na",
        "n/a",
        "none",
    }


def field_plain_text(field):
    return clean(field.text or text_to_plain(field.html))


def has_solution_hint(field):
    if editor_role_hint(field) == "solution":
        return True
    near_label = editor_near_label_lower(field)
    section_label = editor_section_label_lower(field)
    full_label = editor_label_lower(field)
    positive = [
        "text solution",
        "text solutions",
        "solutions",
        "solution",
        "explanation",
        "समाधान",
        "हल",
    ]
    negative = [
        "video solution",
        "question",
        "option",
        "correct answer",
        "correct ans",
        "correct answers",
        "numerical type",
        "hint",
        "topic",
        "tag",
        "passage",
    ]
    if editor_role_hint(field) in {"question", "option", "correct_answer"}:
        return False
    if any(word in near_label for word in negative):
        return False
    if section_label in {"solution", "solutions", "text solution", "text solutions", "हल", "समाधान"}:
        return True
    return any(word in near_label for word in positive) or any(word in full_label for word in positive)


def solution_field_score(field):
    near_label = editor_near_label_lower(field)
    section_label = editor_section_label_lower(field)
    label = editor_label_lower(field)
    score = 0
    if editor_role_hint(field) == "solution":
        score += 200
    if section_label in {"solution", "solutions", "text solution", "text solutions", "हल", "समाधान"}:
        score += 160
    for token, points in [
        ("text solution", 100),
        ("solutions", 70),
        ("solution", 65),
        ("explanation", 35),
        ("समाधान", 70),
        ("हल", 45),
    ]:
        if token in near_label:
            score += points
        elif token in section_label:
            score += points
        elif token in label:
            score += points // 2
    for token, points in [
        ("video solution", -140),
        ("question", -120),
        ("option", -110),
        ("correct answer", -150),
        ("correct answers", -150),
        ("correct ans", -150),
        ("numerical type", -100),
        ("topic", -60),
        ("tag", -60),
        ("hint", -40),
        ("passage", -80),
    ]:
        if token in near_label:
            score += points
    return score


def is_correct_answer_editor_loose(field):
    if editor_role_hint(field) == "correct_answer":
        return True
    label = editor_label_lower(field)
    near_label = editor_near_label_lower(field)
    section_label = editor_section_label_lower(field)
    combined = f"{near_label} {section_label} {label}"
    return any(
        token in combined
        for token in [
            "correct answers",
            "correct answer",
            "correct ans",
            "numerical type",
            "answer type",
            "numerical answer",
            "correct numerical",
            "सही उत्तर",
        ]
    )


def is_safe_solution_position_candidate(field):
    if field.option_marker or editor_role_hint(field) in {"option", "question", "correct_answer"}:
        return False
    if is_correct_answer_editor_loose(field):
        return False
    label = editor_label_lower(field)
    near_label = editor_near_label_lower(field)
    section_label = editor_section_label_lower(field)
    combined = f"{near_label} {section_label} {label}"
    text = field_plain_text(field)
    if is_not_available_text(text):
        return False
    if any(token in combined for token in ["video solution", "topic", "tag", "hint", "question id", "question *", "प्रश्न"]):
        return False
    # The QBG text-solution editor may be blank, or may hold the old solution.
    # It must never be an option/question/correct-answer editor.
    return True


def find_text_solution_editor(fields):
    visual_fields = sorted(fields, key=lambda field: (field.top, field.left))

    def ranked(candidates):
        def rank(field):
            current_text = field_plain_text(field)
            existing_content_bonus = 0
            if current_text and not is_not_available_text(current_text):
                # When overwriting, the genuine solution editor often already
                # holds the old solution. Prefer it over nearby empty editors.
                existing_content_bonus = min(35, 8 + len(current_text) / 80)
            return (
                solution_field_score(field) + existing_content_bonus,
                -field.top,
                -field.left,
            )
        return sorted(candidates, key=rank, reverse=True)

    # Strongest signal: the editor sits under the QBG heading "Solutions"/"हल".
    role_solution = [
        field for field in visual_fields
        if editor_role_hint(field) == "solution" and is_safe_solution_position_candidate(field)
    ]
    if role_solution:
        return ranked(role_solution)[0]

    # Second signal: surrounding DOM text clearly says Solution/Text Solution.
    explicit_candidates = [field for field in visual_fields if has_solution_hint(field) and is_safe_solution_position_candidate(field)]
    explicit_candidates = ranked(explicit_candidates)
    if explicit_candidates and solution_field_score(explicit_candidates[0]) > 0:
        return explicit_candidates[0]

    # Confirmed SCQ/MCQ QBG layout:
    # question editor -> option A/B/C/D editors -> editor under "Solutions".
    option_fields = [field for field in visual_fields if field.option_marker or editor_role_hint(field) == "option"]
    if option_fields:
        last_option_top = max(field.top for field in option_fields)
        after_options = [
            field for field in visual_fields
            if field.top > last_option_top + 20 and is_safe_solution_position_candidate(field)
        ]
        if after_options:
            return after_options[0]

    # Numerical/subjective layout:
    # question editor -> correct-answer editor if present -> solution editor.
    hard_context = [
        field for field in visual_fields
        if field.option_marker
        or editor_role_hint(field) in {"question", "option", "correct_answer"}
        or is_correct_answer_editor_loose(field)
    ]

    for field in visual_fields:
        text = field_plain_text(field)
        if field in hard_context:
            continue
        if text and not is_not_available_text(text) and not has_solution_hint(field):
            label = editor_label_lower(field)
            if not any(token in label for token in ["solution", "video", "topic", "tag", "hint"]):
                hard_context.append(field)

    start_top = max((field.top for field in hard_context), default=-10**9)
    after_context = [
        field for field in visual_fields
        if field.top > start_top + 20 and is_safe_solution_position_candidate(field)
    ]
    if after_context:
        return after_context[0]

    return None


def find_question_editor(fields):
    """The question-statement editor: labeled when possible, positional otherwise."""
    visual_fields = sorted(fields, key=lambda field: (field.top, field.left))

    labeled = [
        field for field in visual_fields
        if editor_role_hint(field) == "question" and not field.option_marker
    ]
    if labeled:
        return labeled[0]

    # Confirmed SCQ layout: the question editor is the one directly above the
    # option block. Never accept a correct-answer or solution editor there.
    option_fields = [field for field in visual_fields if field.option_marker or editor_role_hint(field) == "option"]
    if option_fields:
        first_option_top = min(field.top for field in option_fields)
        before = [
            field for field in visual_fields
            if field.top < first_option_top - 20
            and not field.option_marker
            and not is_correct_answer_editor_loose(field)
            and not has_solution_hint(field)
            and editor_role_hint(field) not in {"solution", "correct_answer"}
        ]
        if before:
            return before[-1]
    return None


def find_option_editors(fields):
    """The A-D option editors, keyed by their letter, first sighting wins."""
    editors = {}
    for field in sorted(fields, key=lambda field: (field.top, field.left)):
        marker = clean(field.option_marker).upper()
        if marker in {"A", "B", "C", "D"} and marker not in editors:
            editors[marker] = field
    return editors


# --------------------------------------------------------------- writing ----
def _get_tinymce_frame(page, editor_index):
    """The real TinyMCE iframe and body, for trusted keyboard input."""
    info = page.evaluate(
        r"""
        (editorIndex) => {
            if (!window.tinymce || !window.tinymce.editors[editorIndex]) {
                return {ok: false, reason: "TinyMCE editor not found"};
            }
            const editor = window.tinymce.editors[editorIndex];
            const iframe = editor.iframeElement || document.getElementById(editor.id + "_ifr");
            if (!iframe) return {ok: false, reason: "TinyMCE iframe not found"};
            iframe.scrollIntoView({block: "center", inline: "nearest"});
            return {
                ok: true,
                editorId: editor.id || "",
                iframeId: iframe.id || "",
            };
        }
        """,
        editor_index,
    )
    if not info or not info.get("ok") or not info.get("iframeId"):
        raise QbgBridgeError(f"Could not locate TinyMCE iframe for editor {editor_index}: {info}")

    iframe_id = info["iframeId"]
    frame = page.frame(name=iframe_id)
    if frame is None:
        for candidate in page.frames:
            try:
                element = candidate.frame_element()
                if element.get_attribute("id") == iframe_id:
                    frame = candidate
                    break
            except Exception:
                pass
    if frame is None:
        raise QbgBridgeError(f"Could not attach to TinyMCE iframe {iframe_id!r}")
    return info, frame, frame.locator("body")


def _commit_tinymce_value(page, editor_index):
    """Synchronise TinyMCE with its source textarea and Angular form control."""
    return page.evaluate(
        r"""
        (editorIndex) => {
            if (!window.tinymce || !window.tinymce.editors[editorIndex]) {
                return {ok: false, reason: "TinyMCE editor not found"};
            }
            const editor = window.tinymce.editors[editorIndex];
            const source = document.getElementById(editor.id);
            const body = editor.getBody();
            try {
                editor.save();
                editor.setDirty(true);
                editor.nodeChanged();

                // These events supplement the real keyboard events sent by Playwright.
                for (const eventName of ["input", "keyup", "change", "blur"]) {
                    try { editor.fire(eventName); } catch (error) {}
                }

                if (body) {
                    body.dispatchEvent(new Event("change", {bubbles: true}));
                    body.dispatchEvent(new Event("blur", {bubbles: true}));
                }
                if (source) {
                    source.value = editor.getContent({format: "raw"}) || "";
                    source.dispatchEvent(new Event("input", {bubbles: true}));
                    source.dispatchEvent(new Event("change", {bubbles: true}));
                    source.dispatchEvent(new Event("blur", {bubbles: true}));
                }
                if (window.tinymce && typeof window.tinymce.triggerSave === "function") {
                    window.tinymce.triggerSave();
                }
                return {
                    ok: true,
                    html: editor.getContent({format: "raw"}) || "",
                    text: editor.getContent({format: "text"}) || "",
                    sourceValue: source ? (source.value || "") : "",
                };
            } catch (error) {
                return {ok: false, reason: String(error && error.message || error)};
            }
        }
        """,
        editor_index,
    )


def set_editor_html(page, editor_index, html_value, log=print):
    """Replace TinyMCE through real keyboard input so Angular receives trusted events.

    Direct editor.setContent() changes TinyMCE visually, but on some QBG pages
    the Angular form model keeps the previous value. Real keys update both.
    """
    plain_text = text_to_plain(html_value or "")
    _, frame, body = _get_tinymce_frame(page, editor_index)

    body.wait_for(state="visible", timeout=LONG_TIMEOUT)
    body.click(timeout=NORMAL_TIMEOUT)

    platform = page.evaluate("() => navigator.platform || navigator.userAgent || ''")
    select_all = "Meta+A" if "mac" in str(platform).lower() else "Control+A"
    page.keyboard.press(select_all)
    page.keyboard.press("Backspace")
    page.wait_for_timeout(60)

    # Slower than setContent(), but it reliably updates QBG's Angular form value.
    lines = plain_text.splitlines()
    if not lines:
        lines = [""]
    for line_index, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if line_index < len(lines) - 1:
            page.keyboard.press("Enter")

    # A real blur is important: QBG often commits the TinyMCE value on blur.
    page.keyboard.press("Tab")
    page.wait_for_timeout(180)

    result = _commit_tinymce_value(page, editor_index)
    if not result or not result.get("ok"):
        raise QbgBridgeError(f"Could not commit editor index {editor_index}: {result}")

    expected_key = solution_compare_key(plain_text)
    actual_key = solution_compare_key(result.get("text") or result.get("html") or "")
    source_key = solution_compare_key(result.get("sourceValue") or "")
    if expected_key != actual_key:
        raise QbgBridgeError(
            f"Editor index {editor_index} did not accept the real-input replacement. "
            f"Expected start={expected_key[:120]!r}; actual start={actual_key[:120]!r}"
        )
    if source_key and expected_key != source_key:
        # TinyMCE may serialize equivalent HTML differently (entities,
        # paragraph wrappers, harmless spacing). The saved value is still
        # verified after reopening QBG, so do not fail here.
        log(
            f"TinyMCE source serialization differs for editor {editor_index}; "
            "continuing because the editor text matches."
        )
    log(f"Real keyboard overwrite committed in TinyMCE editor {editor_index}.")
    return result


# -------------------------------------------------------- correct options ----
_OPTION_CARDS_JS = """
        ({selectedRecords, optionGroupIndex, applyClicks}) => {
            const letters = ["A", "B", "C", "D"];
            const desired = new Set(
                (selectedRecords || [])
                    .map((item) => Number(item.optionIndex ?? letters.indexOf(item.letter)))
                    .filter((index) => index >= 0 && index < 4)
            );

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

            function hasCheckedState(element) {
                if (!element || typeof element.querySelector !== "function") return false;
                const checkedNode = element.querySelector(
                    "input:checked, [aria-checked='true'], .mat-checkbox-checked, .mat-radio-checked, .mat-mdc-checkbox-checked, .mat-mdc-radio-checked, .mdc-checkbox--selected, .mdc-radio--selected"
                );
                if (checkedNode) return true;
                return String(element.getAttribute("aria-checked") || "").toLowerCase() === "true" ||
                    String(element.className || "").toLowerCase().includes("checked") ||
                    String(element.className || "").toLowerCase().includes("selected");
            }

            function clickControl(card) {
                const clickable =
                    card.control.querySelector(".mat-checkbox-inner-container") ||
                    card.control.querySelector(".mat-radio-container") ||
                    card.control.querySelector(".mdc-checkbox") ||
                    card.control.querySelector(".mdc-radio") ||
                    card.control.querySelector("input[type='checkbox'], input[type='radio']") ||
                    card.control;
                clickable.scrollIntoView({block: "center", inline: "center"});
                clickable.click();
            }

            function optionText(element) {
                return norm(element.innerText || element.textContent || "");
            }

            function climbToOptionCard(control) {
                let best = control;
                let node = control;
                for (let depth = 0; node && depth < 8; depth += 1) {
                    const text = optionText(node);
                    const rect = node.getBoundingClientRect();
                    const looksLikeOption =
                        text.includes("option") ||
                        text.includes("विकल्प") ||
                        letters.some((letter) => text.includes(` ${letter.toLowerCase()} `));
                    const forbidden =
                        text.includes("language") ||
                        text.includes("input language") ||
                        text.includes("exam category") ||
                        text.includes("question type");
                    const tooLarge = rect.height > 260 || rect.width > Math.max(window.innerWidth * 0.86, 940);
                    if (visible(node) && looksLikeOption && !forbidden && !tooLarge) {
                        best = node;
                    }
                    node = node.parentElement;
                }
                return best;
            }

            const controls = Array.from(document.querySelectorAll(
                "mat-checkbox, mat-radio-button, [role='checkbox'], [role='radio'], input[type='checkbox'], input[type='radio']"
            ))
                .filter((control) => visible(control))
                .map((control) => {
                    const root = climbToOptionCard(control);
                    const rect = root.getBoundingClientRect();
                    return {
                        root,
                        control,
                        top: rect.top,
                        left: rect.left,
                        width: rect.width,
                        height: rect.height,
                        text: optionText(root),
                    };
                })
                .filter((item) => item.text.includes("option") || item.text.includes("विकल्प"))
                .sort((a, b) => Math.abs(a.top - b.top) > 8 ? a.top - b.top : a.left - b.left);

            const cards = [];
            for (const item of controls) {
                const duplicate = cards.some((card) => {
                    return card.root === item.root ||
                        (Math.abs(card.top - item.top) < 12 && Math.abs(card.left - item.left) < 12);
                });
                if (!duplicate) cards.push(item);
            }

            if (cards.length < 4) {
                return {
                    ok: false,
                    reason: "Could not identify four visible option cards",
                    cardCount: cards.length,
                    cardTexts: cards.map((card) => card.text).slice(0, 8),
                };
            }

            const start = Number(optionGroupIndex || 0) * 4;
            const activeCards = cards.slice(start, start + 4);
            if (activeCards.length < 4) {
                return {
                    ok: false,
                    reason: "Could not identify four option cards for requested group",
                    requestedGroup: Number(optionGroupIndex || 0),
                    cardCount: cards.length,
                    cardTexts: cards.map((card) => card.text).slice(0, 12),
                };
            }
            const details = [];
            let clicked = 0;
            const checkedLetters = [];

            for (let index = 0; index < activeCards.length; index += 1) {
                const shouldBeChecked = desired.has(index);
                const checkedBefore = hasCheckedState(activeCards[index].control) || hasCheckedState(activeCards[index].root);
                if (checkedBefore) checkedLetters.push(letters[index]);
                if (applyClicks && checkedBefore !== shouldBeChecked) {
                    clickControl(activeCards[index]);
                    clicked += 1;
                }
                details.push({
                    letter: letters[index],
                    desired: shouldBeChecked,
                    checkedBefore,
                    text: activeCards[index].text.slice(0, 180),
                });
            }

            return {
                ok: true,
                clicked,
                details,
                checkedLetters,
                selectedLetters: Array.from(desired).sort().map((index) => letters[index]),
            };
        }
"""


def answer_records_from_letters(letters):
    from .textops import normalize_option_letters

    records = []
    for letter in normalize_option_letters(letters):
        option_index = ord(letter) - ord("A")
        records.append(
            {
                "letter": letter,
                "optionIndex": option_index,
                "groupIndex": 0,
                "indexInAllOptions": option_index,
            }
        )
    return records


def set_correct_options_by_letters(page, letters, language_name, option_group_index=0, log=print):
    selected_records = answer_records_from_letters(letters)
    if not selected_records:
        return 0

    result = page.evaluate(
        _OPTION_CARDS_JS,
        {"selectedRecords": selected_records,
         "optionGroupIndex": int(option_group_index),
         "applyClicks": True},
    )

    if not result or not result.get("ok"):
        shot = debug_path(f"{language_name.lower()}_correct_option_error.png")
        page.screenshot(path=shot, full_page=True)
        raise QbgBridgeError(
            f"Could not update {language_name} correct option(s). "
            f"Screenshot saved: {shot}. Details: {result}"
        )

    option_letters = ", ".join(result.get("selectedLetters", []))
    log(f"Synced {language_name} correct option(s) group {int(option_group_index) + 1}: {option_letters}")
    page.wait_for_timeout(700)
    return int(result.get("clicked", 0))


def read_checked_option_letters(page, option_group_index=0):
    """The currently ticked option letters, or None when the cards can't be read."""
    result = page.evaluate(
        _OPTION_CARDS_JS,
        {"selectedRecords": [],
         "optionGroupIndex": int(option_group_index),
         "applyClicks": False},
    )
    if not result or not result.get("ok"):
        return None
    return list(result.get("checkedLetters") or [])
