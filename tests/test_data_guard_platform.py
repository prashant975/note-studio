"""Git checkout newline conversion must not mask a real protected-prompt edit."""
import importlib.util
from pathlib import Path


def test_git_baseline_accepts_crlf_but_rejects_changed_prompt(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1]/"NoteStudio_Kit/tools/data_guard.py"
    spec = importlib.util.spec_from_file_location("guard_platform_test", source)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    original, copy = tmp_path/"original", tmp_path/"project/engine"
    original.mkdir()
    copy.mkdir(parents=True)
    (original/"prompts.py").write_bytes(b"PROMPT = 'preserve wording'\n")
    (copy/"prompts.py").write_bytes(b"PROMPT = 'preserve wording'\r\n")
    monkeypatch.setattr(guard, "ORIGINALS", {"sample": {"root": original}})
    monkeypatch.setattr(guard, "ENGINE_COPIES", {"sample": {
        "copy": "engine", "allowed": {}, "protected": ["prompts.py"]}})
    assert guard.drift(tmp_path/"project", normalize_line_endings=True) == 0
    (copy/"prompts.py").write_bytes(b"PROMPT = 'changed wording'\r\n")
    assert guard.drift(tmp_path/"project", normalize_line_endings=True) == 1
