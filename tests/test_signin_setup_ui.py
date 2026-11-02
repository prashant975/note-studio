"""First launch explains setup without hiding the route to an independent sign-in."""

def test_first_launch_shows_registration_and_separate_signin_action(client):
    response = client.get('/settings')
    assert response.status_code == 200
    assert 'Set up Google sign-in on this computer' in response.text
    assert '~/.note_studio/google-client.json' in response.text
    assert 'Set up my separate sign-in' in response.text
    assert 'Windows token file' in response.text
