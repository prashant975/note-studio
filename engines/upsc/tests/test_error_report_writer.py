"""Tests for the Error Report XLSX (src/error_report_writer.py).

The sheet is the deliverable, so most of these assert on the saved workbook
rather than on return values: the eight columns, the header style, the severity
colour coding, the banding, and the two things that quietly ruin a report — a
correction that starts with ``=`` being stored as a formula, and a row being
filtered away because the model misspelled its category.

The reference workbook is used where it is present and skipped rather than
failed where it is not, so the suite still passes on a machine without it.
"""
from pathlib import Path
import sys

from openpyxl import Workbook, load_workbook
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.error_report_writer import (
    BAND_FILL,
    BORDER_COLOR,
    COLUMNS,
    HEADER_FILL,
    LEGACY_COLUMNS,
    NO_SOURCE_TEXT,
    SEVERITY_FILLS,
    build_report_rows,
    extract_error_report_block,
    format_source_urls,
    no_issue_rows,
    normalise_error_type,
    normalise_rows,
    normalise_severity,
    parse_error_report_json,
    summarise,
    write_error_report_xlsx,
)

REFERENCE = Path(r"C:/Users/Laptop/Downloads/Error Report Format (1).xlsx")

ROWS = [
    {
        "original_statement": "Real annual GDP growth for FY 2025-26 is estimated at 7.6%.",
        "error_type": "Numerical/outdated",
        "timestamp": "00:23:10 - 00:24:00",
        "severity": "High",
        "error_concern": "The figure does not match the latest MoSPI releases.",
        "correct_information_correction": "Use the latest official figure available at finalisation.",
        "source_urls": ["https://mospi.gov.in/a.pdf", "https://mospi.gov.in/b.pdf"],
    },
    {
        "original_statement": "GDP is produced by normal residents.",
        "error_type": "Conceptual",
        "severity": "High",
        "error_concern": "GDP is based on domestic territory, not residence.",
        "correct_information_correction": "GDP covers the domestic territory irrespective of residence.",
        "source_urls": "https://mospi.gov.in/c.pdf",
    },
    {
        "original_statement": "GDP = GVA + net taxes - net subsidies.",
        "error_type": "Conceptual/formula precision",
        "severity": "Medium",
        "error_concern": "The formula uses loose terms.",
        "correct_information_correction": "= GVA at basic prices + product taxes - product subsidies",
        "source_urls": [],
    },
    {
        "original_statement": "India's HDI (2025): 0.685 (Rank 130/193).",
        "error_type": "Source-year precision",
        "severity": "Low",
        "error_concern": "The value should be tied to the HDR release year.",
        "correct_information_correction": "Per the 2025 HDR, India ranked 130 of 193 with HDI 0.685.",
        "source_urls": ["https://undp.org/india"],
    },
]


def _sheet(path: Path):
    wb = load_workbook(path)
    return wb[wb.sheetnames[0]]


@pytest.fixture
def written(tmp_path):
    rows = normalise_rows(ROWS, strictness="strict")
    return _sheet(write_error_report_xlsx(rows, tmp_path / "report.xlsx"))


# --------------------------------------------------------------- the columns

def test_columns_include_timestamp_after_error_type(written):
    assert [written.cell(1, c).value for c in range(1, 9)] == list(COLUMNS)
    assert written.max_column == 8


def test_rows_are_numbered_from_one_and_are_real_numbers(written):
    numbers = [written.cell(r, 1).value for r in range(2, 6)]
    assert numbers == [1, 2, 3, 4]
    assert all(isinstance(n, int) for n in numbers)


def test_fields_land_in_their_own_columns(written):
    assert written.cell(2, 2).value.startswith("Real annual GDP growth")
    assert written.cell(2, 3).value == "Numerical/outdated"
    assert written.cell(2, 4).value == "00:23:10 - 00:24:00"
    assert written.cell(2, 5).value == "High"
    assert written.cell(2, 6).value.startswith("The figure does not match")
    assert written.cell(2, 7).value.startswith("Use the latest official figure")
    assert written.cell(2, 8).value.startswith("https://mospi.gov.in/a.pdf")


# ---------------------------------------------------------------- formatting

def test_header_matches_the_reference_style(written):
    cell = written.cell(1, 1)
    assert cell.fill.fgColor.rgb.endswith(HEADER_FILL)
    assert cell.font.b is True
    assert cell.font.color.rgb.endswith("FFFFFF")
    assert cell.alignment.horizontal == "center"
    assert cell.alignment.wrapText is True
    assert written.row_dimensions[1].height and written.row_dimensions[1].height >= 25


def test_every_cell_wraps_and_sits_at_the_top(written):
    for row in range(1, 6):
        for col in range(1, 9):
            align = written.cell(row, col).alignment
            assert align.wrapText is True, f"{row},{col} does not wrap"
            assert align.vertical == "top"


def test_body_row_heights_are_left_to_excel(written):
    """Excel auto-fits a wrapped row only while no explicit height is stored.
    Pinning one would clip the long correction and URL cells."""
    for row in range(2, 6):
        assert written.row_dimensions[row].height is None


def test_autofit_is_not_suppressed_at_the_sheet_level(written):
    """sheetFormatPr customHeight="1" disables auto-fit for the whole sheet.
    With it set, every row renders as a single clipped line — most of the
    correction and the whole of the second URL simply do not appear."""
    assert written.sheet_format.customHeight in (None, False)


def test_borders_are_thin_and_the_reference_blue(written):
    for row in (1, 2, 5):
        border = written.cell(row, 1).border
        assert border.top.style == "thin"
        assert border.top.color.rgb.endswith(BORDER_COLOR)


def test_severity_cells_are_colour_coded_and_bold(written):
    expected = ["High", "High", "Medium", "Low"]
    for offset, severity in enumerate(expected):
        cell = written.cell(2 + offset, 5)
        assert cell.value == severity
        assert cell.fill.fgColor.rgb.endswith(SEVERITY_FILLS[severity])
        assert cell.font.b is True


def test_rows_are_banded_like_the_reference(written):
    assert written.cell(2, 1).fill.fgColor.rgb.endswith(BAND_FILL)
    assert not written.cell(3, 1).fill.fgColor.rgb.endswith(BAND_FILL)
    assert written.cell(4, 1).fill.fgColor.rgb.endswith(BAND_FILL)


def test_column_widths_are_in_the_requested_ranges(written):
    widths = {k: v.width for k, v in written.column_dimensions.items()}
    for letter, low, high in (
        ("A", 10, 12), ("B", 35, 45), ("C", 22, 28), ("D", 20, 24),
        ("E", 14, 16), ("F", 40, 50), ("G", 45, 60), ("H", 50, 70),
    ):
        assert low <= widths[letter] <= high, f"{letter} is {widths[letter]}"


def test_header_is_frozen_and_filterable(written):
    assert written.freeze_panes == "A2"
    assert written.auto_filter.ref == "A1:H1"


# ------------------------------------------------------------ cell integrity

def test_a_correction_starting_with_equals_stays_text(written):
    """openpyxl types a leading ``=`` as a formula, which Excel then renders as
    an error instead of the sentence the reviewer needs to read."""
    cell = written.cell(4, 7)
    assert cell.data_type == "s"
    assert cell.value.startswith("= GVA at basic prices")


@pytest.mark.parametrize("codepoint", [0x00, 0x07, 0x0b, 0x1b, 0x7f])
def test_control_characters_are_stripped_not_fatal(tmp_path, codepoint):
    rows = normalise_rows([{
        "original_statement": f"bad{chr(codepoint)}text",
        "error_type": "Factual",
        "severity": "High",
        "error_concern": "concern",
        "correct_information_correction": "fix",
        "source_urls": [],
    }])
    ws = _sheet(write_error_report_xlsx(rows, tmp_path / "ctrl.xlsx"))
    assert ws.cell(2, 2).value == "badtext"


def test_a_cell_longer_than_excel_allows_is_truncated(tmp_path):
    rows = normalise_rows([{
        "original_statement": "x" * 40_000,
        "error_type": "Factual",
        "severity": "High",
        "error_concern": "concern",
        "correct_information_correction": "fix",
        "source_urls": [],
    }])
    ws = _sheet(write_error_report_xlsx(rows, tmp_path / "long.xlsx"))
    assert len(ws.cell(2, 2).value) <= 32_767


def test_urls_are_one_per_line_and_deduplicated():
    joined = format_source_urls(["https://a.gov.in", "https://b.gov.in", "https://A.GOV.IN"])
    assert joined == "https://a.gov.in\nhttps://b.gov.in"
    assert format_source_urls("https://a.gov.in, https://b.gov.in") == (
        "https://a.gov.in\nhttps://b.gov.in")
    assert format_source_urls(None) == ""


def test_a_row_with_no_source_says_so_rather_than_looking_sourced(written):
    assert written.cell(4, 8).value == NO_SOURCE_TEXT


# --------------------------------------------------------------- the content

@pytest.mark.parametrize("given,expected", [
    ("High", "High"), ("high", "High"), ("HIGH", "High"), ("Critical", "High"),
    ("Medium", "Medium"), ("moderate", "Medium"),
    ("Low", "Low"), ("minor", "Low"),
    ("", "Medium"), (None, "Medium"), ("banana", "Medium"), (7, "Medium"),
])
def test_severity_is_always_one_of_the_three(given, expected):
    assert normalise_severity(given) == expected


@pytest.mark.parametrize("given,expected", [
    ("conceptual", "Conceptual"),
    ("Numerical / outdated", "Numerical/outdated"),
    ("SOURCE-YEAR PRECISION", "Source-year precision"),
    ("", "Factual"),
])
def test_error_types_snap_onto_the_controlled_vocabulary(given, expected):
    assert normalise_error_type(given) == expected


def test_an_unknown_error_type_is_kept_rather_than_discarded():
    assert normalise_error_type("Map projection mismatch") == "Map projection mismatch"


def test_rows_with_nothing_to_say_are_dropped():
    rows = normalise_rows([
        {"original_statement": "", "error_concern": "", "severity": "High"},
        {"severity": "High"},
        "not a dict",
        {"original_statement": "real", "error_concern": "why", "severity": "High"},
    ])
    assert len(rows) == 1


def test_a_row_missing_its_correction_says_it_needs_verification():
    rows = normalise_rows([{
        "original_statement": "something",
        "error_type": "Factual",
        "severity": "High",
        "error_concern": "why",
    }])
    assert rows[0]["correct_information_correction"].startswith("Needs verification")


# --------------------------------------------------------------- strictness

def _typed(error_type: str, severity: str = "Low") -> dict:
    return {
        "original_statement": "statement",
        "error_type": error_type,
        "severity": severity,
        "error_concern": "concern",
        "correct_information_correction": "fix",
        "source_urls": [],
    }


@pytest.mark.parametrize("level,error_type,kept", [
    ("basic", "Conceptual", True),
    ("basic", "Numerical/outdated", False),
    ("basic", "Terminology issue", False),
    ("standard", "Numerical/outdated", True),
    ("standard", "Missing qualifier", True),
    ("standard", "Terminology issue", False),
    ("strict", "Terminology issue", True),
    ("strict", "Source-year precision", True),
])
def test_strictness_controls_which_categories_survive(level, error_type, kept):
    rows = normalise_rows([_typed(error_type)], strictness=level)
    assert bool(rows) is kept


def test_a_high_severity_row_is_never_filtered_out_by_strictness():
    """A wrong fact is worth reporting whatever category the model filed it
    under — losing one because of its label is the worse failure."""
    rows = normalise_rows([_typed("Terminology issue", severity="High")], strictness="basic")
    assert len(rows) == 1


def test_an_unknown_strictness_falls_back_to_standard():
    assert normalise_rows([_typed("Numerical/outdated")], strictness="nonsense")


# ------------------------------------------------------------------ counting

def test_summary_counts_by_severity():
    assert summarise(normalise_rows(ROWS, strictness="strict")) == {
        "total": 4, "high": 2, "medium": 1, "low": 1}


def test_the_no_issue_row_counts_as_zero_issues():
    """It is a real row in the sheet but not an issue — reporting "1 issue" for
    a clean lecture is a lie a reviewer would act on."""
    rows = no_issue_rows()
    assert len(rows) == 1
    assert summarise(rows) == {"total": 0, "high": 0, "medium": 0, "low": 0}


# --------------------------------------------------------- splitting the AI

def test_the_error_block_is_peeled_off_and_the_notes_survive():
    text = "# NOTES_START\nreal notes\n# NOTES_END\n\n# ERROR_REPORT_START\n[]\n# ERROR_REPORT_END\n"
    remaining, block, found = extract_error_report_block(text)
    assert found is True
    assert block == "[]"
    assert "real notes" in remaining
    assert "ERROR_REPORT" not in remaining


def test_a_response_without_the_markers_is_left_completely_alone():
    text = "just some notes\nwith lines"
    assert extract_error_report_block(text) == (text, "", False)


def test_an_unterminated_block_still_yields_the_notes():
    """A truncated response must not put a half-written JSON array in the
    student's document."""
    remaining, block, found = extract_error_report_block(
        "notes here\n# ERROR_REPORT_START\n[{\"original_statement\": \"trunc")
    assert found is True
    assert remaining == "notes here"
    assert "original_statement" in block


def test_a_stray_end_marker_is_removed_from_the_notes():
    remaining, block, found = extract_error_report_block("notes\n# ERROR_REPORT_END\ntail")
    assert found is False
    assert "ERROR_REPORT" not in remaining
    assert "notes" in remaining and "tail" in remaining


@pytest.mark.parametrize("block,count", [
    ('[{"original_statement": "a", "error_concern": "b"}]', 1),
    ('```json\n[{"original_statement": "a", "error_concern": "b"}]\n```', 1),
    ('[{"original_statement": "a", "error_concern": "b"},]', 1),      # trailing comma
    ('Here you go:\n[{"original_statement": "a", "error_concern": "b"}]\nDone.', 1),
    ('{"rows": [{"original_statement": "a", "error_concern": "b"}]}', 1),
    ("[]", 0),
])
def test_json_is_recovered_from_the_shapes_models_actually_emit(block, count):
    rows, ok = parse_error_report_json(block)
    assert ok is True
    assert len(rows) == count


def test_unrecoverable_json_is_reported_as_a_failure_not_as_zero_errors():
    rows, ok = parse_error_report_json("the model wrote prose instead")
    assert ok is False
    assert rows == []


def test_an_empty_block_means_no_errors_not_a_parse_failure():
    """The prompt asks for [] when there is nothing to report, and a model that
    emits the markers with nothing between them meant the same thing."""
    assert parse_error_report_json("") == ([], True)
    assert parse_error_report_json("   \n  ") == ([], True)


# --------------------------------------------------- status the sheet records

def test_status_distinguishes_the_four_ways_a_report_can_be_empty():
    rows, status = build_report_rows("[]", marker_found=True)
    assert status == "no_issues" and rows[0]["error_type"] == "No issue"

    rows, status = build_report_rows("", marker_found=False)
    assert status == "missing" and rows[0]["error_type"] == "Report parsing"

    rows, status = build_report_rows("not json at all", marker_found=True)
    assert status == "parse_failed" and rows[0]["original_statement"] == (
        "Error report could not be parsed.")

    rows, status = build_report_rows(
        '[{"original_statement": "a", "error_concern": "b", "severity": "High",'
        ' "error_type": "Conceptual"}]', marker_found=True)
    assert status == "ok" and len(rows) == 1


def test_the_no_issue_sheet_is_still_a_valid_report(tmp_path):
    ws = _sheet(write_error_report_xlsx([], tmp_path / "empty.xlsx"))
    assert [ws.cell(1, c).value for c in range(1, 9)] == list(COLUMNS)
    assert ws.cell(2, 1).value == 1
    assert ws.cell(2, 3).value == "No issue"
    assert ws.cell(2, 4).value is None, "the no-issue row leaves timestamp blank"
    assert ws.cell(2, 5).value == "Low"
    assert ws.cell(2, 8).value is None, "the no-issue row leaves the source blank"
    assert ws.cell(2, 5).fill.fgColor.rgb.endswith(SEVERITY_FILLS["Low"])


# ------------------------------------------------------------- template mode

@pytest.mark.skipif(not REFERENCE.exists(), reason="reference workbook not on this machine")
def test_template_mode_keeps_the_reference_style_and_drops_its_example_rows(tmp_path):
    rows = normalise_rows(ROWS, strictness="strict")
    ws = _sheet(write_error_report_xlsx(rows, tmp_path / "t.xlsx", template_path=REFERENCE))
    original = _sheet(REFERENCE)

    assert [ws.cell(1, c).value for c in range(1, 9)] == list(COLUMNS)
    assert ws.cell(1, 1).fill.fgColor.rgb == original.cell(1, 1).fill.fgColor.rgb
    assert ws.cell(1, 1).font.name == original.cell(1, 1).font.name
    # The template's own widths are preserved, not overwritten with ours.
    assert ws.column_dimensions["H"].width == original.column_dimensions["G"].width
    assert ws.column_dimensions["B"].width == original.column_dimensions["B"].width
    # ...but a column it never set is not left at Google's 12.63 default, which
    # breaks "Numerical/outdated" mid-word.
    assert "C" not in original.column_dimensions
    assert ws.column_dimensions["C"].width >= 22


@pytest.mark.skipif(not REFERENCE.exists(), reason="reference workbook not on this machine")
def test_the_templates_autofit_suppression_is_not_inherited(tmp_path):
    """The reference is a Google Sheets export carrying customHeight="1". Copy
    it through and Excel renders every row as one clipped line — which is
    exactly how the reference itself renders."""
    assert _sheet(REFERENCE).sheet_format.customHeight is True, "reference changed"
    ws = _sheet(write_error_report_xlsx(
        normalise_rows(ROWS, strictness="strict"),
        tmp_path / "t.xlsx", template_path=REFERENCE))
    assert ws.sheet_format.customHeight in (None, False)
    assert all(ws.row_dimensions[r].height is None for r in range(2, 6))
    # Its example content is gone and ours is in its place.
    assert ws.max_row == 5
    assert "HDI" in ws.cell(5, 2).value
    # The reference declares A1:Z1000; a plain round-trip would carry 19 empty
    # columns and a stray used-range into every report.
    assert ws.max_column == 8


@pytest.mark.skipif(not REFERENCE.exists(), reason="reference workbook not on this machine")
def test_template_mode_still_recolours_severity(tmp_path):
    rows = normalise_rows(ROWS, strictness="strict")
    ws = _sheet(write_error_report_xlsx(rows, tmp_path / "t.xlsx", template_path=REFERENCE))
    for offset, severity in enumerate(["High", "High", "Medium", "Low"]):
        cell = ws.cell(2 + offset, 5)
        assert cell.fill.fgColor.rgb.endswith(SEVERITY_FILLS[severity])
        assert cell.font.b is True


def test_a_missing_template_falls_back_instead_of_losing_the_report(tmp_path):
    ws = _sheet(write_error_report_xlsx(
        normalise_rows(ROWS), tmp_path / "n.xlsx", template_path=tmp_path / "nope.xlsx"))
    assert [ws.cell(1, c).value for c in range(1, 9)] == list(COLUMNS)
    assert ws.cell(2, 1).value == 1


def test_a_template_with_the_wrong_columns_is_refused(tmp_path):
    """Writing our rows into someone else's column order produces a
    plausible-looking sheet with the data in the wrong places."""
    wrong = Workbook()
    for col, heading in enumerate(["Date", "Who", "What", "Notes"], start=1):
        wrong.active.cell(row=1, column=col).value = heading
    wrong.save(tmp_path / "wrong.xlsx")

    ws = _sheet(write_error_report_xlsx(
        normalise_rows(ROWS), tmp_path / "out.xlsx", template_path=tmp_path / "wrong.xlsx"))
    assert [ws.cell(1, c).value for c in range(1, 9)] == list(COLUMNS)


def test_a_legacy_template_gains_a_timestamp_column_without_losing_its_widths(tmp_path):
    legacy = Workbook()
    sheet = legacy.active
    for col, heading in enumerate(LEGACY_COLUMNS, start=1):
        sheet.cell(row=1, column=col).value = heading
    sheet.column_dimensions["D"].width = 17
    legacy_path = tmp_path / "legacy.xlsx"
    legacy.save(legacy_path)

    ws = _sheet(write_error_report_xlsx(
        normalise_rows(ROWS, strictness="strict"), tmp_path / "out.xlsx",
        template_path=legacy_path))

    assert [ws.cell(1, c).value for c in range(1, 9)] == list(COLUMNS)
    assert ws.cell(2, 4).value == "00:23:10 - 00:24:00"
    assert ws.cell(2, 5).value == "High"
    assert ws.column_dimensions["E"].width == 17


def test_a_corrupt_template_falls_back_instead_of_raising(tmp_path):
    (tmp_path / "broken.xlsx").write_bytes(b"this is not a workbook")
    ws = _sheet(write_error_report_xlsx(
        normalise_rows(ROWS), tmp_path / "out.xlsx", template_path=tmp_path / "broken.xlsx"))
    assert ws.cell(1, 1).value == COLUMNS[0]


def test_the_output_directory_is_created(tmp_path):
    target = tmp_path / "deep" / "deeper" / "report.xlsx"
    assert write_error_report_xlsx(no_issue_rows(), target).exists()
