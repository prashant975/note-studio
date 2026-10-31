"""The reviewer-facing Error Report: one block per error, each with its source.

The XLSX next to this is an eight-column sheet in a client-specified layout, good
for sorting and filtering. It is not good for the job this file does, which is
letting a person check a claim: that needs the generated sentence, the source
sentence, and the place in the lecture the source sentence came from, laid out
together and readable top to bottom.

Every location here comes from src/error_locator, which finds it by searching the
real transcript and the real slides. When it cannot, this report says so in
words. It never prints a plausible-looking timestamp — a reviewer who jumps to
00:42:18 and hears something unrelated stops trusting every other row, so
"Timestamp unavailable" is the more useful output.
"""
from __future__ import annotations

from .error_locator import LOCATION_UNAVAILABLE, SourceLocation

#: Verification wording. Nothing in this pipeline can mark an error verified —
#: a human does that — so the report says what is actually true of every row.
PENDING_VERIFICATION = "Pending manual verification"

_SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}


def _fmt_confidence(row: dict, location: SourceLocation) -> str:
    """The model's confidence in the finding, or the match's if that is all there is.

    Reported separately from the location's confidence rather than blended:
    they answer different questions ("is this really an error?" versus "is this
    really where it came from?") and averaging them would hide a firm error with
    a weak location, which is exactly the row a reviewer most needs to see.
    """
    raw = row.get("confidence")
    try:
        if raw is not None and str(raw).strip() != "":
            return f"{float(raw):.2f}"
    except (TypeError, ValueError):
        pass
    return "not reported by the checker"


def render_error_block(index: int, row: dict, location: SourceLocation) -> str:
    """One ERROR #NN block."""
    rule = "-" * 66
    out = [rule, f"ERROR #{index:02d}", rule, ""]

    out += [f"Type:\n{row.get('error_type') or 'Unclassified'}", ""]
    out += [f"Severity:\n{(row.get('severity') or 'Medium').upper()}", ""]

    out += [f"Location:\n{location.describe()}", ""]

    if location.kind == "video":
        line = f"Timestamp:\n{location.timestamp}"
        if location.url:
            # A real seek link where the host supports one. Drive and direct
            # files get no link rather than a broken one.
            line += f"\nOpen at this point: {location.url}"
        out += [line, ""]
    elif location.located:
        out += ["Timestamp:\nNot applicable for this source type", ""]
    else:
        out += ["Timestamp:\nTimestamp unavailable", ""]

    statement = (row.get("original_statement") or "").strip()
    out += [f"AI-generated note:\n\"{statement}\"" if statement
            else "AI-generated note:\nNot recorded", ""]

    if location.source_text:
        out += [f"Source text ({location.source_label}):\n\"{location.source_text}\"", ""]
    else:
        out += [f"Source text:\n{LOCATION_UNAVAILABLE}", ""]

    out += [f"Issue:\n{row.get('error_concern') or 'Not described'}", ""]
    out += [f"Correct version:\n{row.get('correct_information_correction') or 'Not provided'}", ""]

    urls = (row.get("source_urls") or "").strip()
    if urls:
        out += [f"Reference source(s):\n{urls}", ""]

    out += [f"Confidence:\n{_fmt_confidence(row, location)}", ""]
    if location.located:
        out += [f"Location match confidence:\n{location.confidence:.2f}", ""]
    out += [f"Verification:\n{PENDING_VERIFICATION}", ""]
    return "\n".join(out)


def render_detailed_error_report(
    rows: list[dict],
    locations: list[SourceLocation],
    *,
    lecture_title: str = "",
    video_url: str = "",
    input_name: str = "",
    language: str = "",
    transcript_has_timestamps: bool = True,
) -> str:
    """The whole report: a header, a summary, then one block per error."""
    rows = rows or []
    locations = locations or [SourceLocation() for _ in rows]

    out = ["# Error Report — with source locations", ""]
    if lecture_title:
        out.append(f"**Lecture:** {lecture_title}")
    if input_name:
        out.append(f"**Source file:** {input_name}")
    if video_url:
        out.append(f"**Recording:** {video_url}")
    if language:
        out.append(f"**Notes language:** {language}")
    out.append("")

    located = [loc for loc in locations if loc.located]
    out += [
        "## Summary", "",
        f"- Errors reported: **{len(rows)}**",
        f"- With a located source: **{len(located)}** of {len(rows)}",
    ]
    by_severity: dict[str, int] = {}
    for row in rows:
        key = (row.get("severity") or "Medium").title()
        by_severity[key] = by_severity.get(key, 0) + 1
    for severity in sorted(by_severity, key=lambda s: _SEVERITY_ORDER.get(s, 9)):
        out.append(f"- {severity}: {by_severity[severity]}")
    out.append("")

    if not transcript_has_timestamps:
        out += [
            "> The transcript for this class carries no timings, so errors traced to "
            "the recording are reported by segment number instead of by clock time. "
            "No timestamp has been estimated.", "",
        ]
    if len(located) < len(rows):
        out += [
            "> Some errors could not be traced to a specific point in the source. Those "
            "are marked \"" + LOCATION_UNAVAILABLE + "\" rather than being given a "
            "guessed location.", "",
        ]

    out += ["## Errors", ""]
    if not rows:
        out += ["No errors were reported for this lecture.", ""]
        return "\n".join(out)

    order = sorted(
        range(len(rows)),
        key=lambda i: _SEVERITY_ORDER.get((rows[i].get("severity") or "Medium").title(), 9))
    for position, index in enumerate(order, start=1):
        location = locations[index] if index < len(locations) else SourceLocation()
        out.append("```")
        out.append(render_error_block(position, rows[index], location))
        out.append("```")
        out.append("")
    return "\n".join(out)
