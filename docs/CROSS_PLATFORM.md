# macOS and Windows development

Use Python **3.13**, Git and a browser. Keep this repository separate from the other projects. Never copy .venv, runtime state, Google tokens or browser profiles between computers. On this Mac the checkout is ~/Projects/note-studio. A managed uv Python 3.13 is installed in ~/.local/bin; new Terminal windows have it on PATH.

## Fresh clone and source launch

macOS (install Python 3.13 first; uv users can run uv python install 3.13):

```bash
mkdir -p ~/Projects
git clone https://github.com/prashant975/note-studio.git ~/Projects/note-studio
cd ~/Projects/note-studio
git switch codex/macos-windows-compatibility
python3.13 scripts/setup.py --dev
bash scripts/start.sh
```

Windows PowerShell (install Python 3.13 with the Python launcher first):

```powershell
New-Item -ItemType Directory -Force "$HOME\Projects"
git clone https://github.com/prashant975/note-studio.git "$HOME\Projects\note-studio"
cd "$HOME\Projects\note-studio"
git switch codex/macos-windows-compatibility
py -3.13 scripts/setup.py --dev
.\.venv\Scripts\python.exe run_studio.py
```

For an existing clone skip git clone and keep all existing local changes. After the compatibility branch is merged, use main instead. Setup installs into independent environments and never overwrites .env or saved data. Windows also has scripts/start.ps1; if your execution policy prevents scripts, use the Python command above.

Startup binds to 127.0.0.1, chooses an available port, and opens your browser when ready. Stop source apps with Ctrl+C in their terminal. Child-engine Stop also ends its process tree on macOS and Windows.

## Authentication

Obtain a Google Desktop OAuth registration from your administrator. Keep it outside Git at ~/.note_studio/google-client.json, or point NOTE_STUDIO_GOOGLE_CLIENT at the external file. This is an application registration, not another person's saved login. In Settings, choose independent sheets/sign-in, then Sign in with Google using an allowed @pw.live account. Google Drive/Sheets permissions and PW allowlisting are checked by the existing flow.

New macOS tokens are encrypted with a per-app key stored in the native login Keychain. New Windows tokens use DPAPI in *.protected.json sidecars. Existing Windows shared files are preserved for the older standalone app; it keeps its independent login. Existing plaintext legacy files are not deleted automatically. Do not copy them to another computer. A Windows-encrypted or legacy plaintext token is never imported as a macOS sign-in.

Google client configuration and PW/network access cannot be supplied through Git. Do not paste credentials into chat, commit them, or ship a person's refresh token in an installer. A Keychain prompt belongs to macOS and must be handled locally.

## Local data

~/.note_studio/ on both platforms (NOTE_STUDIO_HOME overrides). This holds the SQLite database, runs, category packs, settings and engine state. Only an explicit choice in Settings shares ~/.shortnotes_bridge/ with the older app. Engine .venv folders are independent and ignored. DPP workbooks, team update keys and original project data are external inputs; no private workbooks or keys are bundled by this change.

Back up local data with the application stopped. Git moves source only; it does not synchronize workspaces or installed applications. Updating/rebuilding keeps data separate. Do not use git clean -xfd in a working data folder.

All copied child engines get their own environment through setup.py; the original Downloads folders are optional. FFmpeg and FFprobe are external prerequisites for recording/audio workflows (macOS with Homebrew: brew install ffmpeg). Use Settings → System → Check setup. DPP needs the two private master workbooks configured there. Handwriting fonts and Word/LibreOffice may be needed by particular engines. Microsoft Word COM automation is Windows-only; portable PDF renderers remain available.

## Tests

macOS:

```bash
.venv/bin/python -m pytest -q tests
.venv/bin/python -m pip install -r requirements-browser.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python scripts/desktop_smoke.py
```

Use .\.venv\Scripts\python.exe for these commands on Windows. CHROMIUM_PATH may point at an installed Chrome/Edge executable. Browser checks use temporary workspaces and synthetic data; they make no paid model calls or real portal posts.

The engine drift gate for a fresh clone is .venv/bin/python NoteStudio_Kit/tools/data_guard.py drift --project . --baseline-ref origin/main. This compares engines to the trusted Git baseline; it does not claim to compare absent original folders. The existing snapshot/compare gates still watch local data.

See [verified results](COMPATIBILITY_REPORT.md) and the GitHub Actions run for actual platform evidence.

## Desktop packages

Source startup and installed apps are separate. Pulling Git does **not** update an installed executable or .app.

macOS: the builder needs a relocatable **uv CPython 3.13** base, not a Python.framework installation. Create the environments with that interpreter, then:

```bash
.venv/bin/python packaging/build_macos.py
open "dist/NoteCraft.app"
```

The .app contains its own Python, libraries and source, including independent libraries for all three child engines. It needs no repository or external Python to run. It is built for the Mac's CPU architecture. Builds are unsigned local apps: public distribution needs an Apple signing identity and notarization, which this repository does not contain. Move an older generated dist/NoteCraft.app aside before rebuilding. The Mac bundle uses the existing browser UI and has a native Dock menu: Quit stops its server. Logs are in ~/Library/Logs/live.pw.NoteCraft/studio.log. Windows automatic installer updates are not used on macOS. Stop a running bundle before replacing it. For reliable manual start/stop, run "dist/NoteCraft.app/Contents/MacOS/launch" in Terminal and use Ctrl+C.

Install Inno Setup 6, then run .\.venv\Scripts\python.exe packaging/build_installer.py --no-signin --no-team-key. Child libraries come from engines/<engine>/.venv first, with the original Windows environment as a fallback. --no-signin and --no-team-key deliberately create an unconfigured package; configure Google and update access separately. Existing full/smaller-edition flags and installer identity are preserved.

## Moving development between computers

Commit source changes on the current computer, push, then on the other computer:

```powershell
cd "$HOME\Projects\note-studio"
git status
git fetch origin
git switch codex/macos-windows-compatibility
git pull --ff-only
py -3.13 scripts/setup.py --dev
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\python.exe run_studio.py
```

On macOS use cd ~/Projects/note-studio, python3.13 for setup and .venv/bin/python for tests/start. If git status is dirty, commit or stash your work before switching/pulling. Resolve divergence normally; never force-push. Rebuild/install the desktop package separately after pulling when you want the installed application updated.
