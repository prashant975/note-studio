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
