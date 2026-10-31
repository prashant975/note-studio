import json

import pytest

from notesbot import categories, generation_profile
from notesbot.prompts import ShortNotes, Section, WorkedExample, build_prompt
from sheets_bridge.output_cache import key


@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    """An empty rules folder; tests add only the files they need."""
    monkeypatch.setattr(categories, "RULES_DIR", tmp_path)
    categories._cache.update(signature=None, rules=None)
    yield tmp_path
    categories._cache.update(signature=None, rules=None)


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_no_rule_files_leave_the_prompt_exactly_as_before(rules_dir):
    before = build_prompt("english", "sig", subject_hint="physics")
    for name in categories.CATEGORIES:
        with generation_profile.exam_category(name):
            assert build_prompt("english", "sig", subject_hint="physics") == before


@pytest.mark.parametrize("lean", [False, True])
def test_category_and_subject_files_layer_in_order(rules_dir, lean):
    write(rules_dir, "foundation/all-subjects.txt", "Use short, simple sentences.")
    write(rules_dir, "foundation/physics.txt", "Box every formula.")
    with generation_profile.lean_requests(lean), generation_profile.exam_category("foundation"):
        prompt = build_prompt("english", "sig", subject_hint="physics")
        other = build_prompt("english", "sig", subject_hint="biology")
    assert "CATEGORY: Foundation notes" in prompt
    assert prompt.index("SUBJECT HINT") < prompt.index("Use short, simple sentences.") < prompt.index("Box every formula.")
    assert "Use short, simple sentences." in other and "Box every formula." not in other
    with generation_profile.lean_requests(lean), generation_profile.exam_category("neet_jee"):
        assert "Use short, simple sentences." not in build_prompt("english", "sig", subject_hint="physics")


def test_run_subject_beats_the_prompt_hint(rules_dir):
    write(rules_dir, "foundation/mathematics.txt", "Maths rules.")
    with generation_profile.exam_category("foundation"), generation_profile.exam_subject("mathematics"):
        assert "Maths rules." in build_prompt("english", "sig")
        assert categories.current_subject() == "mathematics"
    assert categories.current_subject("Maths") == "mathematics"
    assert categories.current_subject("SST") == "social_science"
    assert categories.current_subject("auto") == ""


def test_category_and_subject_are_scoped_to_their_context():
    assert categories.current() == "neet_jee"
    with generation_profile.exam_category("foundation"), generation_profile.exam_subject("physics"):
        assert (categories.current(), categories.current_subject()) == ("foundation", "physics")
    assert (categories.current(), categories.current_subject()) == ("neet_jee", "")
    with generation_profile.exam_category("jee"):   # a pre-merge id
        assert categories.current() == "neet_jee"


def test_neet_and_jee_are_one_neet_jee_category():
    assert categories.CATEGORIES == ("neet_jee", "foundation")
    assert categories.LABELS["neet_jee"] == "NEET-JEE"
    assert [choice["label"] for choice in categories.studio_choices()] == ["NEET-JEE", "Foundation"]


def test_normalize_refuses_unknown_categories():
    for value in (" JEE ", "neet", "NEET-JEE", "neet_jee", "Neet Jee"):
        assert categories.normalize(value) == "neet_jee", value
    assert categories.normalize("Foundation") == "foundation"
    with pytest.raises(ValueError):
        categories.normalize("ias")


def test_edits_apply_without_a_restart(rules_dir):
    write(rules_dir, "neet_jee/all-subjects.txt", "First version.")
    assert categories.commands("neet_jee", "") == "First version."
    write(rules_dir, "neet_jee/all-subjects.txt", "Second version, longer.")
    assert categories.commands("neet_jee", "") == "Second version, longer."
    assert categories.commands("jee", "") == categories.commands("neet", "") == "Second version, longer."


def test_cache_key_keeps_old_neet_keys_and_follows_rule_edits(rules_dir):
    args = dict(file_id="f", out_name="o.pdf", language="english", subject="physics", signature="")
    for old_id in ("neet", "jee"):   # NEET cache entries from before the merge stay valid
        assert key(**args) == key(**args, category=old_id) == key(**args, category="neet_jee")
    assert len({key(**args, category=name) for name in categories.CATEGORIES}) == 2
    before = key(**args, category="neet_jee")
    write(rules_dir, "neet_jee/physics.txt", "Box formulas.")
    after = key(**args, category="neet_jee")
    assert after != before
    assert key(**args, category="foundation") == key(**args, category="foundation")  # untouched category


def test_switches_merge_category_and_subject(rules_dir):
    write(rules_dir, "rules.json", json.dumps({"formats": {
        "foundation/all-subjects": {"mind_map": False},
        "foundation/mathematics": {"solved_examples": True},
    }}))
    assert categories.switches("foundation", "mathematics") == {"mind_map": False, "solved_examples": True}
    assert categories.switches("foundation", "science") == {"mind_map": False}
    assert categories.switches("neet_jee", "mathematics") == {}


# ---- the shipped rules -----------------------------------------------------

def shipped():
    categories._cache.update(signature=None, rules=None)


def test_foundation_maths_keeps_solved_examples_and_nothing_else_does():
    shipped()
    assert categories.solved_examples("foundation", "mathematics")
    for category, subject in [("foundation", "science"), ("foundation", "sanskrit"), ("neet_jee", "mathematics"),
                              ("foundation", "")]:
        assert not categories.solved_examples(category, subject), (category, subject)


@pytest.mark.parametrize("lean", [False, True])
def test_foundation_maths_prompt_asks_for_solved_examples(lean):
    from notesbot import prompts
    shipped()
    with generation_profile.lean_requests(lean), generation_profile.exam_category("foundation"):
        prompt = build_prompt("english", "sig", subject_hint="mathematics")
    assert "CONCISE NOTES — THEORY + SOLVED EXAMPLES" in prompt
    assert "FOUNDATION MATHS NOTES" in prompt
    for theory_only, _with_examples in prompts._SOLVED_EXAMPLE_EDITS:
        assert theory_only not in prompt
    assert "Always leave worked_examples empty" not in prompt
    with generation_profile.lean_requests(lean), generation_profile.exam_category("neet_jee"):
        neet = build_prompt("english", "sig", subject_hint="mathematics")
    assert "SOLVED EXAMPLES" not in neet and "Always leave worked_examples empty" in neet


def test_every_theory_only_line_exists_in_its_template():
    """A template edit must not silently leave a 'no examples' line behind."""
    from notesbot import prompts
    for theory_only, _with_examples in prompts._SOLVED_EXAMPLE_EDITS:
        assert (theory_only in prompts._PROMPT_TEMPLATE) or (theory_only in prompts._LEAN_PROMPT_TEMPLATE)


@pytest.mark.parametrize("lean", [False, True])
def test_schema_allows_solved_examples_only_for_foundation_maths(lean):
    from notesbot.gemini_client import _generation_schema_json
    shipped()

    def example_field(category, subject):
        with generation_profile.lean_requests(lean), generation_profile.exam_category(category), \
                generation_profile.exam_subject(subject):
            schema = _generation_schema_json(ShortNotes)
        return schema["$defs"]["Section"]["properties"].get("worked_examples"), schema["$defs"]

    field, defs = example_field("foundation", "mathematics")
    assert field is not None and "maxItems" not in field and "WorkedExample" in defs
    field, _defs = example_field("neet_jee", "mathematics")
    assert field is None if lean else field["maxItems"] == 0


def test_question_guard_accepts_solved_examples_only_where_kept():
    from notesbot.concise_policy import question_content
    shipped()
    notes = ShortNotes(lecture_no="", subject="Maths", title="Polynomials", sections=[Section(
        heading="Zeroes", points=["A zero of p(x) makes p(x) = 0."],
        worked_examples=[WorkedExample(statement="Find the zero of x - 2.", steps=["x - 2 = 0", "x = 2"], result="2")],
    )])
    with generation_profile.exam_category("foundation"), generation_profile.exam_subject("mathematics"):
        assert question_content(notes) == []
    with generation_profile.exam_category("neet_jee"), generation_profile.exam_subject("mathematics"):
        assert question_content(notes)


def test_rule_subjects_are_accepted_everywhere():
    from notesbot import web_service
    from sheets_bridge import mapping
    shipped()
    for subject in categories.category_subjects("foundation"):
        assert subject in mapping.SUBJECTS and subject in web_service.SUBJECTS, subject


@pytest.mark.parametrize("raw, subject", [
    ("Science", "science"), ("Physical Science", "physics"), ("SST", "social_science"),
    ("Social Science", "social_science"), ("Political Science", "social_science"), ("History", "social_science"),
    ("English", "english"), ("Hindi", "hindi"), ("Maths", "mathematics"), ("Botany", "biology"),
    ("विज्ञान", "science"), ("सामाजिक विज्ञान", "social_science"), ("Mental Ability", "auto"),
])
def test_sheet_subject_column_maps_foundation_subjects(raw, subject):
    from sheets_bridge.mapping import map_subject
    assert map_subject(raw) == subject
