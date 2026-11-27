"""Import this computer's app registration; never imports a user's saved login."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"engines/shortnotes"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registration", type=Path)
    args = parser.parse_args()
    info = json.loads(args.registration.expanduser().read_text(encoding="utf-8-sig"))
    client = info.get("installed") or info.get("web") or {}
    if not client.get("client_id", "").endswith(".apps.googleusercontent.com") or not client.get("client_secret"):
        parser.error("Choose a Google app registration, not a saved login.")
    if (client.get("auth_uri") not in ("https://accounts.google.com/o/oauth2/auth", "https://accounts.google.com/o/oauth2/v2/auth")
            or client.get("token_uri") != "https://oauth2.googleapis.com/token"):
        parser.error("Use a Google OAuth registration with the official Google endpoints.")
    value = json.dumps(info)
    if sys.platform == "darwin":
        from sheets_bridge.macos_credentials import protect
        value = json.dumps(protect(value))
    elif os.name == "nt":
        from sheets_bridge.windows_credentials import transform
        value = json.dumps({"enc":"dpapi", "value":transform(value)})
    else:
        parser.error("Native secure registration import requires macOS or Windows.")
    from note_studio import paths
    target = paths.home()/"google-client.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(value,encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)
    print("Google registration saved with native credential protection. Restart the app.")


if __name__ == "__main__":
    main()
