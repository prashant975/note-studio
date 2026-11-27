"""First launch explains setup without hiding the route to an independent sign-in."""

def test_first_launch_shows_registration_and_separate_signin_action(client):
    response = client.get('/settings')
    assert response.status_code == 200
    assert 'Set up Google sign-in on this computer' in response.text
    assert '~/.note_studio/google-client.json' in response.text
    assert 'Set up my separate sign-in' in response.text
    assert 'Windows token file' in response.text


def test_existing_registration_path_keeps_signin_available(tmp_path, monkeypatch):
    from note_studio import signin
    registration = tmp_path/'existing registration.json'
    registration.write_text('{}')
    monkeypatch.setenv('SHEETS_BRIDGE_CLIENT_SECRET',str(registration))
    monkeypatch.setitem(signin._state,'auth',None)
    assert signin.registration_ready()


def test_popup_start_uses_browser_owned_window(monkeypatch):
    from note_studio import signin
    class Auth:
        def start_sign_in(self, *, open_browser, on_url):
            assert open_browser is False
            on_url('https://accounts.google.com/o/oauth2/v2/auth?state=synthetic')
    monkeypatch.setattr(signin, 'auth', lambda: Auth())
    assert signin.start_popup().startswith('https://accounts.google.com/')


def test_web_registration_callback_rejects_wrong_state_and_keeps_pkce(tmp_path, monkeypatch):
    import socket
    from threading import Event, Thread
    import requests
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'engines/shortnotes'))
    from sheets_bridge import web_oauth
    import google_auth_oauthlib.flow
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    target = f'http://127.0.0.1:{port}/auth/google/callback'
    observed = {}
    creds = object()
    class Flow:
        credentials = creds
        def authorization_url(self, **kwargs):
            return 'https://accounts.google.com/o/oauth2/auth?state=expected', 'expected'
        def fetch_token(self, **kwargs):
            observed['exchange'] = kwargs
    def create(config, **kwargs):
        assert kwargs['autogenerate_code_verifier'] is True
        assert kwargs['redirect_uri'] == target
        return Flow()
    monkeypatch.setattr(google_auth_oauthlib.flow.Flow, 'from_client_config', create)
    ready = Event()
    def run():
        observed['result'] = web_oauth.authorize({'web': {
            'auth_uri':'https://accounts.google.com/o/oauth2/auth',
            'token_uri':'https://oauth2.googleapis.com/token', 'redirect_uris':[target]}},
            ['openid'], open_browser=False, on_url=lambda url: ready.set(), timeout=5)
    worker = Thread(target=run)
    worker.start()
    assert ready.wait(3)
    session = requests.Session(); session.trust_env = False
    bad = session.get(target,params={'state':'wrong','code':'synthetic'},timeout=2)
    assert bad.status_code == 400 and 'exchange' not in observed
    good = session.get(target,params={'state':'expected','code':'synthetic'},timeout=2)
    assert good.status_code == 200 and 'synthetic' not in good.text
    worker.join(3)
    assert not worker.is_alive()
    assert observed['result'] is creds and observed['exchange'] == {'code':'synthetic'}


def test_startup_signin_page_is_available(client):
    response=client.get('/signin')
    assert response.status_code==200
    assert 'data-auth-start' in response.text and 'Sign in to' in response.text
    assert 'Set up my separate sign-in' in response.text


def test_launcher_opens_signin_entry_point(monkeypatch):
    from note_studio import launch
    opened=[]
    monkeypatch.setattr(launch,'answering_instance',lambda port:launch.paths.instance_id())
    monkeypatch.setattr(launch.webbrowser,'open',opened.append)
    monkeypatch.delenv('NOTE_STUDIO_NO_BROWSER',raising=False)
    launch._open_when_ready('http://127.0.0.1:3100',3100)
    assert opened==['http://127.0.0.1:3100/signin']


def test_imported_registration_wins_over_legacy_but_preserves_fallback(tmp_path, monkeypatch):
    from note_studio import old_app, paths
    home=tmp_path/'new home';home.mkdir()
    original=tmp_path/'original';original.mkdir()
    legacy=original/'client_secret.json';legacy.write_text('{"installed":{}}')
    configured=home/'google-client.json';configured.write_text('{"enc":"synthetic"}')
    monkeypatch.setattr(paths,'home',lambda:home)
    monkeypatch.setattr(paths,'shortnotes_original',lambda:original)
    monkeypatch.delenv('NOTE_STUDIO_GOOGLE_CLIENT',raising=False)
    assert old_app.environment('no')['SHEETS_BRIDGE_CLIENT_SECRET']==str(configured)
    configured.unlink()
    assert old_app.environment('no')['SHEETS_BRIDGE_CLIENT_SECRET']==str(legacy)
    assert legacy.read_text()=='{"installed":{}}'
