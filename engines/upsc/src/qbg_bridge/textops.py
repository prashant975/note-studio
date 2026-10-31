"""Pure text and HTML helpers, copied from the QBG Solution Addition Bot.

Nothing in this module touches a browser, so everything here is unit-tested
directly. The parsing quirks are load-bearing: the comparison keys decide
whether an overwrite is declared verified, and the HTML-to-text rules decide
what maths inside MathType images survives as readable text.
"""
from __future__ import annotations

import html
import re
from urllib.parse import unquote, urlparse

from .settings import QBG_HOST, QBG_QUESTION_LINK_TEMPLATE


def clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def header_key(value) -> str:
    return clean(value).lower()


# ------------------------------------------------------- id and link ----
# QBG unique_ids as seen in the export workbook and the creation API:
# 25 lowercase alphanumerics, e.g. a8hri1z75sqihd0v9vphoisa5.
_QBG_ID = re.compile(r"^[a-z0-9]{20,32}$")


def looks_like_qbg_id(value) -> bool:
    return bool(_QBG_ID.fullmatch(clean(value)))


def is_qbg_link(value) -> bool:
    try:
        parsed = urlparse(str(value or ""))
        return parsed.scheme in {"http", "https"} and QBG_HOST in parsed.netloc
    except (ValueError, AttributeError):
        return False


def question_link(qbg_id: str) -> str:
    """The direct edit link for a question, or "" while the format is unconfirmed."""
    template = QBG_QUESTION_LINK_TEMPLATE
    if not template or template.upper().startswith("UNCONFIRMED"):
        return ""
    return template.replace("{qbg_id}", clean(qbg_id))


# ------------------------------------------------------ language names ----
def normalize_language(value) -> str:
    text = header_key(value)
    if text in {"english", "eng", "en"}:
        return "English"
    if text in {"hindi", "hin", "hi"}:
        return "Hindi"
    return ""


# ------------------------------------------------------- HTML to text ----
def text_to_plain(html_value) -> str:
    value = re.sub(r"<br\s*/?>", "\n", html_value or "", flags=re.I)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()


def _extract_attr_value(tag, attr_name):
    match = re.search(
        rf"""\b{re.escape(attr_name)}\s*=\s*(?:(['\"])(.*?)\1|([^\s>]+))""",
        tag or "",
        flags=re.I | re.S,
    )
    if not match:
        return ""
    return html.unescape(match.group(2) or match.group(3) or "").strip()


def _strip_tags_to_text(value):
    value = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.I)
    value = re.sub(r"</(?:p|div|li|tr|td|th)>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _latex_wrap_if_math_like(value):
    text = clean(value)
    if not text:
        return ""
    if text.startswith("\\(") and text.endswith("\\)"):
        return text
    math_like = bool(
        re.search(r"\\(?:frac|sqrt|sin|cos|tan|log|ln|theta|alpha|beta|gamma|pi|vec|overrightarrow|cdot|times|leq|geq|neq)", text)
        or re.search(r"[=+\-*/^_{}]|\d|[xyzabcprstuv]\b", text, flags=re.I)
    )
    if math_like and len(text) <= 220:
        return f"\\({text}\\)"
    return text


def math_text_from_img_tag(img_tag):
    """The best readable text hiding inside a MathType/Wiris <img> tag."""
    candidates = []
    for attr in [
        "alt", "title", "aria-label", "data-mathml", "data-latex", "data-tex",
        "data-math", "data-original-title", "wrs:alt",
    ]:
        value = _extract_attr_value(img_tag, attr)
        if value:
            candidates.append(value)

    src = _extract_attr_value(img_tag, "src")
    if src.startswith("data:image/svg"):
        try:
            svg = unquote(src.split(",", 1)[1]) if "," in src else unquote(src)
            for pattern in [
                r"<title[^>]*>([\s\S]*?)</title>",
                r"<desc[^>]*>([\s\S]*?)</desc>",
                r"aria-label=['\"]([^'\"]+)['\"]",
                r"alt=['\"]([^'\"]+)['\"]",
                r"data-mathml=['\"]([^'\"]+)['\"]",
                r"<text[^>]*>([\s\S]*?)</text>",
            ]:
                for match in re.finditer(pattern, svg, flags=re.I):
                    value = _strip_tags_to_text(match.group(1))
                    if value:
                        candidates.append(value)
        except (ValueError, IndexError):
            pass

    cleaned = []
    for value in candidates:
        value = _strip_tags_to_text(value)
        value = re.sub(r"^(formula|math|equation)\s*:?\s*", "", value, flags=re.I).strip()
        if value and value not in cleaned:
            cleaned.append(value)
    if cleaned:
        return _latex_wrap_if_math_like(cleaned[0])

    if re.search(r"wiris|mathtype|formula|math", img_tag or "", flags=re.I):
        return "[unreadable MathType formula image]"
    return "[image]"


def html_to_ai_text(html_value):
    """TinyMCE HTML as readable text, including MathType image labels."""
    value = html_value or ""
    if not value:
        return ""

    value = re.sub(r"<img\b[^>]*>", lambda m: " " + math_text_from_img_tag(m.group(0)) + " ", value, flags=re.I | re.S)
    value = re.sub(r"<math[\s\S]*?</math>", lambda m: " " + _latex_wrap_if_math_like(_strip_tags_to_text(m.group(0))) + " ", value, flags=re.I)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.I)
    value = re.sub(r"</(?:div|li|tr|td|th)>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\n\s*\n\s*\n+", "\n\n", value)
    value = "\n".join(clean(line) for line in value.splitlines() if clean(line))
    return value.strip()


def html_for_editor(text):
    """Plain text as the paragraph HTML TinyMCE expects."""
    lines = [line.rstrip() for line in (text or "").strip().splitlines()]
    lines = [line for line in lines if line.strip()]
    if not lines:
        return ""
    return "".join(f"<p>{html.escape(line)}</p>" for line in lines)


def _contains_real_html_markup(value):
    """True only for real HTML tags, not mathematical inequalities.

    A check of ``<[^>]+>`` would treat text such as ``0<u<4`` as HTML whenever
    a later ``>`` occurred, which silently removed part of the mathematics.
    """
    raw = str(value or "")
    return bool(
        re.search(
            r"</?(?:p|div|br|span|img|strong|em|b|i|u|sub|sup|ul|ol|li|"
            r"table|tbody|thead|tfoot|tr|td|th|blockquote|pre|code|math|"
            r"mrow|annotation)(?:\s+[^<>]*?)?\s*/?>",
            raw,
            flags=re.I,
        )
    )


def solution_compare_key(value):
    """Normalize app/editor text for reliable post-save comparison."""
    raw = str(value or "")
    if _contains_real_html_markup(raw):
        raw = html_to_ai_text(raw) or text_to_plain(raw)
    raw = html.unescape(raw).replace("\u00a0", " ")
    raw = raw.replace(r"\[", r"\(").replace(r"\]", r"\)")
    raw = re.sub(r"\$\$([\s\S]*?)\$\$", r"\(\1\)", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def strip_for_qbg_solution(text):
    value = (text or "").strip()
    value = re.sub(r"^\s*```(?:text|html|markdown)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```\s*$", "", value)
    value = re.sub(r"^\s*(Solution|Answer)\s*:?\s*", "", value, flags=re.I)
    value = value.replace("\\[", "\\(").replace("\\]", "\\)")
    value = re.sub(r"\$\$([\s\S]*?)\$\$", r"\\(\1\\)", value)
    value = re.sub(r"(?<!\\)\$(?!\$)([^\n$]+?)(?<!\\)\$", r"\\(\1\\)", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


# --------------------------------------------------------- answer letters ----
def normalize_option_letters(values):
    letters = []
    if isinstance(values, str):
        raw_values = re.findall(r"\b[A-D]\b", values.upper())
    elif isinstance(values, (list, tuple)):
        raw_values = []
        for item in values:
            raw_values.extend(re.findall(r"\b[A-D]\b", str(item).upper()))
    else:
        raw_values = []

    for letter in raw_values:
        if letter not in letters:
            letters.append(letter)
    return letters


def compact_option_letters(answer_text):
    value = clean(answer_text).upper()
    if not value:
        return ""
    value = re.sub(r"\b(CORRECT|ANSWER|ANSWERS|OPTION|OPTIONS|IS|ARE)\b", " ", value)
    value = value.replace("AND", ",")
    return re.sub(r"[\[\](){}:;=\-_/&,+\s]", "", value)


def parse_option_letters_from_answer(answer_text):
    compact = compact_option_letters(answer_text)
    if not compact or not re.fullmatch(r"[A-D]{1,4}", compact):
        return []
    letters = []
    for letter in compact:
        if letter not in letters:
            letters.append(letter)
    return letters


def indexes_to_letters(indexes) -> tuple[str, ...]:
    """0-based option indexes, as the bank stores them, to portal letters."""
    letters = []
    for index in indexes or ():
        try:
            index = int(index)
        except (TypeError, ValueError):
            continue
        if 0 <= index <= 3:
            letter = chr(ord("A") + index)
            if letter not in letters:
                letters.append(letter)
    return tuple(letters)


# ---------------------------------------------------------- question type ----
def normalize_question_type_value(value):
    text = clean(value).upper()
    if not text or text in {"-SELECT-", "SELECT"}:
        return ""

    exact_aliases = {
        "NUMERICAL": "NUMERICAL",
        "SCQ": "SCQ",
        "SINGLE CHOICE": "SCQ",
        "SINGLE CHOICE (SCQ)": "SCQ",
        "MCQ": "MCQ",
        "MULTI CHOICE": "MCQ",
        "MULTI CHOICE (MCQ)": "MCQ",
        "MULTIPLE CHOICE": "MCQ",
        "TF": "TF",
        "TRUE OR FALSE": "TF",
        "TRUE OR FALSE (TF)": "TF",
        "SUB": "SUB",
        "SUBJECTIVE": "SUB",
        "SUBJECTIVE (SUB)": "SUB",
        "FIB": "FIB",
        "FILL IN BLANKS": "FIB",
        "FILL IN BLANKS (FIB)": "FIB",
        "AR": "AR",
        "ASSERTION REASON": "AR",
        "ASSERTION REASON (AR)": "AR",
        "COMP": "COMP",
        "COMPREHENSION": "COMP",
        "COMPREHENSION (COMP)": "COMP",
        "ML": "ML",
        "MATCHING LIST": "ML",
        "MATCHING LIST (ML)": "ML",
    }
    if text in exact_aliases:
        return exact_aliases[text]

    bracket_match = re.fullmatch(r"(.+)\s+\(([A-Z]+)\)", text)
    if bracket_match and bracket_match.group(2) in exact_aliases:
        return exact_aliases[bracket_match.group(2)]
    return ""
