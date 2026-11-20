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


def test_local_qc_engine_checks_a_synthetic_pdf_without_signin(tmp_path):
    from note_studio.engines import notes_qc
    import fitz
    python, _ = engine_host.runtime("notes_qc")
    if not Path(python).is_file() or not (paths.ENGINES_DIR/"notes_qc/.venv").is_dir():
        pytest.skip("Install the independent QC environment with scripts/setup.py")
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Synthetic lecture notes: Integers and prime numbers.\nA prime number has exactly two positive divisors.")
    pdf = tmp_path/"synthetic.pdf"
    document.save(pdf)
    document.close()
    events = []
    result = engine_host.run("notes_qc", 91100, tmp_path/"run",
                             notes_qc.build_request([pdf], full=False), events.append)
    assert result["status"] == "complete", result
    outputs = [event for event in events if event["event"] == "output"]
    assert outputs and all(Path(event["path"]).is_file() for event in outputs)


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
