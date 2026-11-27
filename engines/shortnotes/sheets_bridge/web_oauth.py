"""Existing Web OAuth registrations with an exact, state-checked loopback callback."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
import time
from urllib.parse import parse_qs, urlparse
import webbrowser

from .errors import BridgeError


def authorize(config, scopes, *, on_url=None, open_browser=True, timeout=300, domain='pw.live'):
    from google_auth_oauthlib.flow import Flow
    client = config['web']
    if (client.get('token_uri') != 'https://oauth2.googleapis.com/token' or
            client.get('auth_uri') not in ('https://accounts.google.com/o/oauth2/auth',
                                           'https://accounts.google.com/o/oauth2/v2/auth')):
        raise BridgeError('invalid_registration', 'Use a Google OAuth app registration.')
    targets = []
    for uri in client.get('redirect_uris', []):
        parsed = urlparse(uri)
        if (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port
                and parsed.path == '/auth/google/callback' and not parsed.query and not parsed.fragment):
            targets.append(uri)
    if not targets:
        raise BridgeError('invalid_registration', 'The Web registration needs the app loopback callback.')
    target = targets[0]
    parsed = urlparse(target)
    flow = Flow.from_client_config(config, scopes=list(scopes), redirect_uri=target,
                                   autogenerate_code_verifier=True)
    result = {}
    state = ''

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            incoming = urlparse(self.path)
            fields = parse_qs(incoming.query)
            if incoming.path != parsed.path or not secrets.compare_digest(fields.get('state', [''])[0], state):
                self.send_error(400, 'Invalid sign-in response')
                return
            result['code'] = fields.get('code', [''])[0]
            result['error'] = fields.get('error', [''])[0]
            body = b'<h2>Sign-in received</h2><p>Your sign-in is being checked. You can close this window.</p>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # Never log authorization codes.

    try:
        server = HTTPServer((parsed.hostname, parsed.port), Callback)
    except OSError as exc:
        raise BridgeError('signin_port_busy', 'Finish or close the other app’s Google sign-in, then retry.') from exc
    server.timeout = .25
    try:
        url, state = flow.authorization_url(access_type='offline', prompt='consent', hd=domain)
        if on_url is not None:
            on_url(url)
        if open_browser:
            webbrowser.open(url)
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        raise BridgeError('sign_in_timed_out', 'Google sign-in timed out. Press Sign in again.')
    if result.get('error') or not result.get('code'):
        raise BridgeError('sign_in_cancelled', 'Google sign-in was cancelled or denied.')
    flow.fetch_token(code=result['code'])
    return flow.credentials
