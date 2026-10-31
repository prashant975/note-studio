"""Prepare a per-user team installation, then start its local web app."""
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parent
STUDIO = ROOT / 'qbg_studio'
sys.path.insert(0, str(STUDIO))


def prepare():
    import db
    import pw_auth
    db.init()
    if not db.get_setting('_team_install_initialized', ''):
        identity = uuid.uuid4().hex[:12]
        con = db.connect()
        try:
            con.execute('BEGIN IMMEDIATE')
            if not con.execute("SELECT 1 FROM settings WHERE key='_team_install_initialized'").fetchone():
                for key, value in (
                    ('install_id', identity), ('studio_queue_tab', 'QBG Queue ' + identity),
                    ('_team_install_initialized', '1'),
                ):
                    con.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', (key, value))
            con.execute('COMMIT')
        except Exception:
            con.execute('ROLLBACK')
            raise
        finally:
            con.close()
    # Share the app's OAuth registration, never the original operator's
    # refresh token. Each teammate signs in as themselves.
    try:
        pw_auth._client_credentials()
    except pw_auth.AuthError:
        deployment = ROOT / 'deployment.json'
        if deployment.exists():
            config = json.loads(deployment.read_text(encoding='utf-8'))
            client = config['oauth_client']
            pw_auth.save_client(client['client_id'], client['client_secret'], client['redirect_uri'])
        # Clean team packages start at Settings with no registration or login.
        # Missing credentials must not prevent opening the setup screen.
    return db


if __name__ == '__main__':
    os.chdir(STUDIO)
    db = prepare()
    if '--check' in sys.argv:
        import app
        import pipeline_bridge as bridge
        assert db.connect().execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert bridge.PIPE_DIR.exists() and bridge.BOTS_DIR.exists()
        print('QBG_CHECK_OK', flush=True)
    else:
        import launch
        sys.exit(launch.main())
