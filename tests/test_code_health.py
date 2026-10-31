"""Code guards: no undefined names (pyflakes), and the Studio process never holds
a bare `src` or `config` module (those belong to child-process engines)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKED = [*sorted((ROOT / "note_studio").rglob("*.py")), *sorted((ROOT / "tests").rglob("*.py")),
           *sorted((ROOT / "packaging").glob("*.py")),
           ROOT / "run_studio.py", ROOT / "NoteStudio_Kit" / "tools" / "copy_engine.py"]


def test_no_undefined_names_or_unused_imports():
    from pyflakes.api import checkPath
    from pyflakes.reporter import Reporter

    class Collect:
        def __init__(self):
            self.lines = []

        def write(self, text):
            self.lines.append(text)

    out = Collect()
    reporter = Reporter(out, out)
    problems = sum(checkPath(str(path), reporter) for path in CHECKED)
    assert problems == 0, "".join(out.lines)


def test_no_bare_src_or_config_in_the_studio_process():
    import importlib

    from note_studio import app as app_module   # imports every Studio module
    from note_studio import engine_catalog

    assert app_module.app.title
    engine_catalog.put_shortnotes_on_path()
    for name in ("notesbot", "notesbot.categories", "notesbot.config", "sheets_bridge", "sheets_bridge.settings"):
        importlib.import_module(name)

    copy = (ROOT / "engines" / "shortnotes").resolve()
    for name in ("notesbot", "sheets_bridge"):
        assert Path(sys.modules[name].__file__).resolve().is_relative_to(copy), name
    assert "src" not in sys.modules and "config" not in sys.modules
    assert not any(name.startswith(("src.", "config.")) for name in sys.modules)


def test_the_name_is_set_in_one_place():
    """A rename is one edit in brand.py: no template spells the name out."""
    for template in (ROOT / "note_studio" / "templates").glob("*.html"):
        body = re.sub(r"\{#.*?#\}", "", template.read_text(encoding="utf-8"), flags=re.S)
        assert "NoteCraft" not in body, template.name
