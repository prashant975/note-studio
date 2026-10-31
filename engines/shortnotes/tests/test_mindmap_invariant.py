"""Regression coverage for the final, renderable mind-map invariant."""
from __future__ import annotations

from notesbot.gemini_client import (
    _ensure_mindmap,
    _merge,
    _mindmap_hook,
    _preserve_generated_fields,
)
from notesbot.notes_pdf import NotesPDF
from notesbot.prompts import (
    Branch,
    Compare,
    MindMap,
    Section,
    ShortNotes,
    Visual,
    WhereItem,
)


def _notes(*, mindmap=None, audience="NEET", quote="Revise the why.") -> ShortNotes:
    return ShortNotes(
        lecture_no="L-7", subject="Chemistry", title="Chemical Bonding",
        audience=audience, quote=quote,
        sections=[
            Section(heading="Ionic Bond", points=["**Electron transfer** forms ions."]),
            Section(heading="Covalent Bond", points=["**Electron sharing** forms molecules."]),
        ],
        mindmap=mindmap,
    )


def test_merge_preserves_metadata_and_unions_duplicate_mindmap_children():
    first = _notes(mindmap=MindMap(center="Bonding", branches=[
        Branch(label="Ionic", children=[
            "transfer", "ions", "lattice", "electrostatic attraction",
        ]),
    ]))
    second = _notes(mindmap=MindMap(center="", branches=[
        Branch(label=" ionic ", children=[
            "LATTICE", "high melting point", "conducts when molten",
        ]),
        Branch(label="Covalent", children=["sharing"]),
    ]), audience="", quote="")
    first.faculty = "Venky Anna"      # printed on the cover, which only the first part sees

    merged = _merge([first, second])

    assert merged.faculty == "Venky Anna"
    assert merged.audience == "NEET"
    assert merged.quote == "Revise the why."
    assert merged.mindmap.center == "Bonding"
    assert [branch.label for branch in merged.mindmap.branches] == ["Ionic", "Covalent"]
    assert merged.mindmap.branches[0].children == [
        "transfer",
        "ions",
        "lattice",
        "electrostatic attraction",
        "high melting point",
        "conducts when molten",
    ]


def test_merge_retains_nine_unique_source_branches_for_paginated_rendering():
    source = _notes(mindmap=MindMap(
        center="Bonding",
        branches=[
            Branch(label=f"Topic {index}", children=[f"Source hook {index}"])
            for index in range(1, 10)
        ],
    ))

    merged = _merge([source])

    assert [branch.label for branch in merged.mindmap.branches] == [
        f"Topic {index}" for index in range(1, 10)
    ]


def test_ensure_mindmap_retains_nine_synthesised_source_branches():
    ninth_heading = "Long Source Topic With More Than Five Words"
    notes = ShortNotes(
        lecture_no="L-11",
        subject="Chemistry",
        title="Nine Topics",
        sections=[
            Section(
                heading=(f"Topic {index}" if index < 9 else ninth_heading),
                points=[f"Source fact {index} remains complete."],
            )
            for index in range(1, 10)
        ],
        mindmap=MindMap(
            center="Nine Topics",
            branches=[
                Branch(label=f"Topic {index}", children=[f"Source hook {index}"])
                for index in range(1, 9)
            ],
        ),
    )

    _ensure_mindmap(notes)

    assert [branch.label for branch in notes.mindmap.branches] == [
        *[f"Topic {index}" for index in range(1, 9)],
        ninth_heading,
    ]


def test_replacement_pass_cannot_drop_valid_source_mindmap_or_metadata():
    source = _notes(mindmap=MindMap(center="Bonding", branches=[
        Branch(label="Ionic", children=["transfer"]),
    ]))
    candidate = _notes(mindmap=None, audience="", quote="")

    kept = _preserve_generated_fields(source, candidate)

    assert kept.audience == source.audience
    assert kept.quote == source.quote
    assert kept.mindmap.model_dump() == source.mindmap.model_dump()


def test_missing_mindmap_is_synthesised_from_headings_and_content_hooks():
    notes = _notes(mindmap=None)

    _ensure_mindmap(notes)

    assert notes.mindmap.center == "Chemical Bonding"
    assert notes.mindmap.branches
    assert all(branch.label and branch.children for branch in notes.mindmap.branches)


def test_synthesised_hooks_keep_complete_source_sentences_without_mutating_sections():
    source_hooks = [
        "Electrolysis drives non-spontaneous chemical reactions via electricity.",
        "Converts chemical energy from redox reactions into electrical energy.",
    ]
    notes = ShortNotes(
        lecture_no="L-10", subject="Chemistry", title="Electrochemistry",
        sections=[
            Section(heading="Electrolysis", points=[source_hooks[0]]),
            Section(heading="Daniell Cell", points=[source_hooks[1]]),
        ],
    )
    original_sections = [section.model_dump() for section in notes.sections]

    _ensure_mindmap(notes)

    assert [branch.children[0] for branch in notes.mindmap.branches] == source_hooks
    assert [section.model_dump() for section in notes.sections] == original_sections


def test_synthesised_hook_accepts_the_exact_render_safe_boundary():
    boundary = (
        "aaaaaaa bbbbbbb ccccccc ddddddd eeeeeee fffffff ggggggg hhhhhhh "
        "iiiiiii jjjjjjj kkkkkkk lllllll."
    )
    assert len(boundary) == 96
    assert len(boundary.split()) == 12

    hook = _mindmap_hook(Section(heading="Boundary", points=[boundary]))

    assert hook == boundary


def test_synthesised_hook_preserves_all_source_asterisks_from_a_trick():
    oversized = (
        "This deliberately oversized source sentence has far more than twelve "
        "words and must never be sliced into a dangling fragment."
    )
    source_trick = "Rate = k * **concentration**."

    hook = _mindmap_hook(Section(
        heading="Rate Law", points=[oversized], tricks=[source_trick],
    ))

    assert hook == source_trick


def test_long_source_heading_is_preserved_whole_as_a_fallback_hook():
    oversized = (
        "This deliberately oversized source sentence has far more than twelve "
        "words and must never be sliced into a dangling fragment."
    )
    oversized_heading = (
        "electrochemical electrochemical electrochemical electrochemical "
        "electrochemical"
    )

    hook = _mindmap_hook(Section(
        heading=oversized_heading, points=[oversized],
    ))

    assert hook == oversized_heading


def test_long_source_heading_is_synthesised_without_truncation():
    notes = ShortNotes(
        lecture_no="L-10", subject="Chemistry", title="Electrochemistry",
        sections=[
            Section(
                heading=(
                    "electrochemical electrochemical electrochemical "
                    "electrochemical electrochemical"
                ),
                points=["This source fact is concise and complete."],
            ),
            Section(
                heading="Valid Topic",
                points=["This valid source fact is concise and complete."],
            ),
        ],
    )

    _ensure_mindmap(notes)

    assert [branch.label for branch in notes.mindmap.branches] == [
        (
            "electrochemical electrochemical electrochemical "
            "electrochemical electrochemical"
        ),
        "Valid Topic",
    ]


def test_mindmap_center_uses_the_first_bounded_metadata_candidate():
    oversized = (
        "electrochemical electrochemical electrochemical electrochemical "
        "electrochemical"
    )
    notes = ShortNotes(
        lecture_no="L-10", subject="Chemistry", title="Electrochemistry",
        sections=[], mindmap=MindMap(center=oversized, branches=[]),
    )

    _ensure_mindmap(notes)

    assert notes.mindmap.center == "Electrochemistry"
    assert notes.mindmap.branches[0].model_dump() == {
        "label": "Electrochemistry", "children": ["Electrochemistry"],
    }


def test_empty_source_does_not_invent_an_emergency_mindmap():
    oversized = (
        "electrochemical electrochemical electrochemical electrochemical "
        "electrochemical"
    )
    notes = ShortNotes(
        lecture_no="L-10", subject=oversized, title=oversized,
        sections=[], mindmap=MindMap(center=oversized, branches=[]),
    )

    _ensure_mindmap(notes)

    assert notes.mindmap is None


def test_mindmap_center_falls_back_to_a_bounded_source_section_heading():
    notes = ShortNotes(
        lecture_no="",
        subject="",
        title="",
        sections=[
            Section(
                heading="Ionic Bond",
                points=["Electron transfer forms oppositely charged ions."],
            ),
        ],
        mindmap=None,
    )

    _ensure_mindmap(notes)

    assert notes.mindmap.center == "Ionic Bond"
    assert notes.mindmap.branches[0].model_dump() == {
        "label": "Ionic Bond",
        "children": ["Electron transfer forms oppositely charged ions."],
    }


def test_eight_synthesised_branches_choose_bounded_whole_hooks_and_fit_page():
    oversized = (
        "This deliberately oversized source sentence has far more than twelve "
        "words and must never be sliced into a dangling fragment."
    )
    boundary = (
        "aaaaaaa bbbbbbb ccccccc ddddddd eeeeeee fffffff ggggggg hhhhhhh "
        "iiiiiii jjjjjjj kkkkkkk lllllll."
    )
    expected_hooks = [boundary] * 8
    notes = ShortNotes(
        lecture_no="L-10", subject="Chemistry", title="Electrochemistry",
        sections=[
            Section(
                heading=f"Topic {index}",
                points=[oversized, expected_hooks[index - 1]],
            )
            for index in range(1, 9)
        ],
    )

    _ensure_mindmap(notes)

    hooks = [branch.children[0] for branch in notes.mindmap.branches]
    assert hooks == expected_hooks
    assert all(len(hook.split()) <= 12 and len(hook) <= 96 for hook in hooks)

    pdf = NotesPDF(notes, "by Test", watermark=False)
    pdf.add_page()
    pdf._render_mindmap(notes.mindmap)

    # One page: under the header when it fits there, else a page of its own.
    assert pdf.page_no() <= 2
    assert pdf.get_y() <= pdf.page_break_trigger


def test_merge_keeps_valid_visual_compare_and_note_only_sections():
    """Multi-chunk merging must not erase renderable non-bullet sections."""
    source = ShortNotes(
        lecture_no="L-8", subject="Chemistry", title="Cells",
        sections=[
            Section(heading="Draw the cell", diagram="Label anode and cathode."),
            Section(
                heading="Compare cells",
                compare=Compare(
                    left_title="Galvanic", left_points=["Spontaneous"],
                    right_title="Electrolytic", right_points=["Driven"],
                ),
            ),
            Section(heading="Remember", notes=["Salt bridge completes the circuit."]),
            Section(
                heading="Source Figure",
                visual=Visual(
                    kind="core_diagram",
                    prompt="Faithfully redraw the PDF figure labelled R.",
                ),
            ),
            Section(heading="Source Glossary", key_terms=["R : resistance"]),
            Section(
                heading="Source Symbol",
                where=[WhereItem(symbol="$R$", meaning="Resistance")],
            ),
            Section(heading="Teacher Heading"),
        ],
    )

    merged = _merge([source])

    assert [section.heading for section in merged.sections] == [
        "Draw the cell", "Compare cells", "Remember", "Source Figure",
        "Source Glossary", "Source Symbol",
        "Teacher Heading",
    ]
