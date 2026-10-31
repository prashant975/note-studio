# Updating Studio from inside Studio

The version panel at the bottom of the sidebar shows **Update to …** when a
newer release is in the team's release folder, and Settings → **Updates**
shows the same with details and a **Check for updates** button. Pressing it
downloads the installer, checks it against the SHA-256 Google Drive reports
for the file, closes Studio, installs silently and starts Studio again. About
a minute, mostly the download. Saved work, uploads, the sheet key and the
sign-in stay, exactly as with a manual install.

## Where releases live

One Google Drive folder, **QBG Studio releases**, shared with the team's
service account. Each release is a file named `QBG_Studio_Setup_<version>.exe`;
its description in Drive is shown as the release note. Every Studio reads the
folder and downloads through the Drive API with the service-account key it
already has for the team sheet, so nothing new is stored on any computer and
no teammate has to change anything. The folder id is built into each release
(`release.json`, `releases`); Settings → Updates can point one computer at
another folder, for a test.

## Publishing a release

    python build_team_installer.py --publish <Drive folder link or id> --notes "what changed"

The build bakes the folder into the release and writes a versioned copy,
`dist/team/<version>/QBG_Studio_Setup_<version>.exe`. It then tries to upload
that file into the folder. Google gives a service account no storage of its
own, so into a folder in My Drive the upload is refused; the build then names
the file to drop into the folder by hand, which takes a minute in the browser.
When the folder lives in a Shared drive with the service account as a member,
the upload goes through by itself.

Google computes the SHA-256 of an uploaded file within moments; a file whose
checksum is not there yet is not offered.

## What a build checks before it can be published

The bundled Python runtime is a copy of the one installed on the build
machine, so a module the developer's Python has and the runtime lacks reaches
every teammate as "No module named ..." (lxml, 25 September 2026: every
installed CLAT export failed). The build now finds every third-party import in
the code and refuses to package when the bundled runtime cannot import one.
To add a dependency, install it into `%LOCALAPPDATA%\Programs\QBGStudio\_internal\python`
with its own `python.exe -m pip install <name>` before building; imports
guarded by try/except are treated as optional.

## How a Studio finds out

The version panel asks the server every five minutes; the server re-reads the
folder in the background when its copy is older than 30 minutes, so a teammate
sees the button within about half an hour of a release without pressing
anything. **Check for updates** reads it now.

## What the update does, in order

1. Refuses unless a strictly newer version is listed and this Studio was
   installed (a Studio run from source cannot install over itself).
2. Downloads the installer through the Drive API to
   `%TEMP%\qbg_studio_update` and refuses a file whose SHA-256 or size is
   not what Drive reported.
3. Writes a small PowerShell helper and starts it detached, then exits with
   code 0, which makes the desktop launcher quit quietly.
4. The helper waits until no Studio process is left, runs the installer with
   `/VERYSILENT /NORESTART /SUPPRESSMSGBOXES`, records the installer's exit
   code beside its log, and starts `QBG_Studio.exe`.
5. The open browser tab keeps polling; when the server answers with a
   different version, the page reloads itself.

If the installer ends with an error, the Updates card says so with the log
path, and the previous version keeps running.
