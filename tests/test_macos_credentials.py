"""Credential compatibility tests use synthetic keys and never access a user's Keychain."""
import importlib
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines/shortnotes"))


@pytest.fixture
def vault(monkeypatch):
    module = importlib.import_module("sheets_bridge.macos_credentials")
    monkeypatch.setattr(module, "_key", lambda: b"k" * 32)
    return module


def test_ciphertext_is_authenticated_and_contains_no_secret(vault):
    from cryptography.exceptions import InvalidTag
    secret = "synthetic-refresh-token"
    protected = vault.protect(secret)
    assert secret not in str(protected)
    assert vault.unprotect(protected) == secret
    import base64
    raw = bytearray(base64.b64decode(protected["value"]))
    raw[-1] ^= 1
    protected["value"] = base64.b64encode(raw).decode()
    with pytest.raises(InvalidTag):
        vault.unprotect(protected)


def test_another_computer_cannot_read_credentials(vault, monkeypatch):
    from cryptography.exceptions import InvalidTag
    record = vault.protect("synthetic")
    monkeypatch.setattr(vault, "_key", lambda: b"x" * 32)
    with pytest.raises(InvalidTag):
        vault.unprotect(record)


@pytest.mark.parametrize("enc", ["dpapi", "plain", "unknown"])
def test_foreign_or_plain_credentials_are_not_accepted(vault, enc):
    with pytest.raises(ValueError):
        vault.unprotect({"enc": enc, "value": "synthetic"})


def test_keychain_failure_never_returns_plaintext(vault, monkeypatch):
    def unavailable():
        raise RuntimeError("Keychain unavailable")
    monkeypatch.setattr(vault, "_key", unavailable)
    with pytest.raises(RuntimeError):
        vault.protect("synthetic")


def test_google_registration_is_read_without_plaintext_temporary_file(vault, tmp_path):
    import json
    from sheets_bridge import google_auth
    registration={'web':{'client_id':'synthetic.apps.googleusercontent.com','client_secret':'synthetic-private-registration'}}
    target=tmp_path/'google-client.json'
    target.write_text(json.dumps(vault.protect(json.dumps(registration))))
    assert google_auth.client_config(target)==registration
    assert google_auth.client_id_of(target)=='synthetic.apps.googleusercontent.com'
    assert 'synthetic-private-registration' not in target.read_text()
    assert list(tmp_path.iterdir())==[target]
