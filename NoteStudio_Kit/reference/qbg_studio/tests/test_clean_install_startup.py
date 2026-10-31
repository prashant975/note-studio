"""A clean team install can reach Settings without copied credentials."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'qbg_studio'))
import db
import pw_auth
import start_installed


def isolated_install(tmp_path, monkeypatch):
    monkeypatch.setattr(start_installed, 'ROOT', tmp_path)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'qbg_studio.db')
    def missing():
        raise pw_auth.AuthError('Not configured')
    monkeypatch.setattr(pw_auth, '_client_credentials', missing)


def test_clean_install_without_deployment_opens_setup(tmp_path, monkeypatch):
    isolated_install(tmp_path, monkeypatch)
    monkeypatch.setattr(pw_auth, 'save_client', lambda *a: (_ for _ in ()).throw(AssertionError('No credential should be saved')))
    start_installed.prepare()
    identity = db.get_setting('install_id')
    assert identity
    assert db.get_setting('studio_queue_tab') == 'QBG Queue ' + identity
    start_installed.prepare()
    assert db.get_setting('install_id') == identity


def test_existing_registration_is_not_replaced(tmp_path, monkeypatch):
    isolated_install(tmp_path, monkeypatch)
    monkeypatch.setattr(pw_auth, '_client_credentials', lambda: ('existing-id', 'existing-secret'))
    monkeypatch.setattr(pw_auth, 'save_client', lambda *a: (_ for _ in ()).throw(AssertionError('Existing registration must be retained')))
    (tmp_path/'deployment.json').write_text('not read', encoding='utf-8')
    start_installed.prepare()


def test_private_deployment_configuration_remains_supported(tmp_path, monkeypatch):
    isolated_install(tmp_path, monkeypatch)
    client = dict(client_id='test-id', client_secret='test-value', redirect_uri='http://127.0.0.1/callback')
    (tmp_path/'deployment.json').write_text(json.dumps(dict(oauth_client=client)),encoding='utf-8')
    saved=[]
    monkeypatch.setattr(pw_auth, 'save_client', lambda *a: saved.append(a))
    start_installed.prepare()
    assert saved == [('test-id','test-value','http://127.0.0.1/callback')]
