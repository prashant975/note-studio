import os
from pathlib import Path
import pytest
from note_studio import engine_host, paths


def test_local_engine_environment_has_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ENGINES_DIR", tmp_path)
    suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    python = tmp_path / "upsc/.venv" / suffix
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.delenv("NOTE_STUDIO_UPSC_PYTHON", raising=False)
    assert engine_host.runtime("upsc")[0] == str(python)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows DPAPI")
def test_new_windows_credentials_use_dpapi_sidecars(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"engines/shortnotes"))
    from sheets_bridge import google_auth
    from sheets_bridge.windows_credentials import transform
    import json
    class Credentials:
        def to_json(self):
            return '{"refresh_token":"synthetic-sidecar-secret"}'
    legacy = tmp_path/"token.json"
    legacy.write_text('{"legacy":"preserve"}')
    google_auth._save(Credentials(), legacy)
    assert legacy.read_text() == '{"legacy":"preserve"}'
    record = json.loads((tmp_path/"token.protected.json").read_text())
    assert record["enc"] == "dpapi"
    assert "synthetic-sidecar-secret" not in str(record)
    assert json.loads(transform(record["value"], decrypt=True))["refresh_token"] == "synthetic-sidecar-secret"
