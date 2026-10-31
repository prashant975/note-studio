"""The note-type registry agrees with the packs and with the engine's own cache keys."""
from __future__ import annotations

import importlib

from note_studio import categories, engine_catalog, note_types


def test_ids_are_unique_and_the_ready_types_are_the_connected_engines():
    ids = [t["id"] for t in note_types.all_types()]
    assert len(ids) == len(set(ids))
    assert [t["id"] for t in note_types.all_types() if t["status"] == "ready"] == [
        "concise_handdrawn", "upsc_dcn", "upsc_simple", "upsc_mains", "upsc_prelims", "upsc_mains_practice",
        "dpp_from_notes", "pw_books_handwritten", "typeset_notes"]


def test_the_upsc_types_use_the_engines_own_formats_and_defaults():
    """engine_format must be one of the UPSC app's UPSC_OUTPUT_FORMATS, and the
    model defaults its .env and src/config.py defaults (read as text: the
    engine's `src` is never imported into this process)."""
    import re

    config = (engine_catalog.engine_dir("upsc") / "src" / "config.py").read_text(encoding="utf-8")
    formats = re.search(r"UPSC_OUTPUT_FORMATS = \{(.*?)\}", config, re.S).group(1)
    for type_id, fmt in (("upsc_dcn", "dcn"), ("upsc_simple", "simple"), ("upsc_mains", "mains"),
                         ("upsc_prelims", "prelims")):
        entry = note_types.get(type_id)
        assert entry["engine_format"] == fmt and f'"{fmt}"' in formats
        assert entry["models"]["notes"] == ["gemini-3.5-flash", "gemini-3.6-flash"]
        assert entry["models"]["speech"] == ["gemini-3.5-flash", "gemini-3.6-flash"]
        assert entry["models"]["images"] == ["gemini-2.5-flash-image"]
    for default in ('"gemini-3.5-flash"', '"gemini-2.5-flash-image"', '"gemini-3.6-flash"'):
        assert default in config, default


def test_cache_keys_are_the_engines_own():
    """Never change these: finished notes would be paid for again."""
    engine_catalog.put_shortnotes_on_path()
    output_cache = importlib.import_module("sheets_bridge.output_cache")
    final_quality = importlib.import_module("notesbot.final_quality")
    revision = note_types.get("concise_handdrawn")["cache_revision"]
    assert revision == {"notes": output_cache.GENERATION_REVISION, "quality": final_quality.QUALITY_VERSION}


def test_every_pack_offers_known_types_in_a_language_both_allow():
    categories.seed()
    for pack in categories.all_packs():
        offered = categories.offered_types(pack)
        assert [t["id"] for t in offered] == pack["note_types"], pack["id"]
        for entry in offered:
            assert categories.language_choices(pack, entry), (pack["id"], entry["id"])


def test_the_upload_pages_choices_are_offered():
    categories.seed()
    neet = categories.get("neet_jee")
    assert [s["id"] for s in categories.subject_choices(neet)] == [
        "auto", "chemistry", "physics", "biology", "mathematics", "sanskrit", "other"]
    hand = note_types.get("concise_handdrawn")
    assert [l["id"] for l in categories.language_choices(neet, hand)] == [
        "auto", "english", "hindi", "gujarati", "sanskrit_hindi", "sanskrit", "hinglish"]
    defaults = note_types.option_defaults("concise_handdrawn")
    assert defaults["logo_watermark"] is True and defaults["watermark"] is False
