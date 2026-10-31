"""requirements.resolve(): layers in order, comments dropped, switches merged,
and a fingerprint that moves with every rule change and only then."""
from __future__ import annotations

from note_studio import categories, requirements


def _rules(cid: str):
    return categories.packs_dir() / cid / "rules"


def test_a_category_without_rules_resolves_to_the_engine_prompt_only():
    categories.seed()
    resolved = requirements.resolve("neet_jee", "concise_handdrawn", "physics", "english")
    assert [l.layer for l in resolved.layers] == ["engine"]
    assert resolved.layers[0].path.endswith("notesbot/prompts.py")
    assert resolved.fingerprint == "" and resolved.switches == {}


def test_foundation_maths_brings_its_rule_file_and_solved_examples_switch():
    categories.seed()
    resolved = requirements.resolve("foundation", "concise_handdrawn", "maths", "english")
    assert resolved.subject == "mathematics"
    assert [l.layer for l in resolved.layers] == ["engine", "subject"]
    assert resolved.switches["solved_examples"] is True
    assert resolved.layers[1].text == (_rules("foundation") / "mathematics.txt").read_text(encoding="utf-8").strip()


def test_layers_come_in_order_and_comments_are_dropped():
    categories.seed()
    rules = _rules("neet_jee")
    (rules / "all-subjects.txt").write_text("# note to self\nKeep units.\n", encoding="utf-8")
    (rules / "physics.txt").write_text("Box every formula.", encoding="utf-8")
    (rules / "types").mkdir()
    (rules / "types" / "concise_handdrawn.txt").write_text("One mind map at the end.", encoding="utf-8")
    resolved = requirements.resolve("neet_jee", "concise_handdrawn", "physics", "english",
                                    profile={"instructions": "# x\nBatch: Arjuna."}, extra="Keep the teacher's examples.")
    assert [l.layer for l in resolved.layers] == ["engine", "category", "subject", "category_type", "profile", "run"]
    assert resolved.layers[1].text == "Keep units."
    # The hand-drawn engine applies category + subject itself; the rest go as its instructions.
    assert resolved.text("type", "category_type", "profile", "run") == (
        "One mind map at the end.\n\nBatch: Arjuna.\n\nKeep the teacher's examples.")


def test_only_this_runs_instructions_are_sent_unchanged():
    categories.seed()
    resolved = requirements.resolve("neet_jee", "concise_handdrawn", "auto", "auto", extra="  Keep every reaction.  ")
    assert resolved.text("type", "category_type", "profile", "run") == "Keep every reaction."


def test_the_fingerprint_moves_with_a_rule_edit_and_only_then():
    categories.seed()
    first = requirements.resolve("foundation", "concise_handdrawn", "sanskrit", "hindi").fingerprint
    assert first == requirements.resolve("foundation", "concise_handdrawn", "sanskrit", "hindi").fingerprint
    (_rules("foundation") / "sanskrit.txt").write_text("Only the lesson's verses.", encoding="utf-8")
    assert requirements.resolve("foundation", "concise_handdrawn", "sanskrit", "hindi").fingerprint != first


def test_an_empty_or_comment_only_file_adds_nothing():
    categories.seed()
    (_rules("neet_jee") / "all-subjects.txt").write_text("# only comments\n\n", encoding="utf-8")
    resolved = requirements.resolve("neet_jee", "concise_handdrawn", "", "")
    assert [l.layer for l in resolved.layers] == ["engine"] and resolved.fingerprint == ""
