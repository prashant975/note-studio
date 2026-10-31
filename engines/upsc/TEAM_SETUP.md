# UPSC_Notes — Team Setup (Windows)

Turn a class recording (YouTube link) + the lecture slides into detailed UPSC
notes (Word + PDF).

> ⚠️ **This folder contains the shared Google sign-in secret. Keep it inside the
> team only — do not upload it to any public place (public GitHub, Drive link,
> WhatsApp group outside the team, etc.).**

---

## Before you start (one-time)

1. **Windows 10 or 11.**
2. **Python 3.10 or newer** — install from https://www.python.org/downloads/
   and, on the first install screen, **tick "Add Python to PATH"**.
   (Only needed once per computer. Skip if you already have it.)
3. **Your `@pw.live` email must be approved** for this app. If sign-in says you
   are "not authorized", ask the admin to add your email to the
   **"Handwritten Notes Automation"** column of the shared Whitelisted sheet.

## Run it

1. **Unzip** this folder anywhere (e.g. Desktop). Keep all files together.
2. **Double-click `START_WINDOWS.bat`.**
   - The first run sets things up and can take a few minutes. Later runs are fast.
   - A black window opens and stays open — that is normal. **Keep it open** while
     you use the app; close it to stop.
3. Your browser opens at **http://localhost:8501**. Sign in with your `@pw.live`
   Google account.

## Make notes

1. **Class video link** — paste the YouTube link of the class (this is the main
   source for the notes).
2. **Lecture slides** — upload the class PDF / PPTX / PPT.
3. **Notes language** — Auto (matches the class), or force English / Hindi.
4. Click **Generate UPSC notes**, then download the Word/PDF.

---

## Required: ffmpeg

The app builds the transcript from the class video link you paste, so it needs
**ffmpeg** (and `ffprobe`, which comes with it) on PATH:

1. Download the **"essentials"** build from https://www.gyan.dev/ffmpeg/builds/
2. Unzip it somewhere permanent, e.g. `C:\ffmpeg`
3. Add its `bin` folder (e.g. `C:\ffmpeg\bin`) to your PATH
4. Open a **new** terminal and check: `ffmpeg -version`

Without it the app will tell you it is missing and stop — it cannot read the
lecture audio. Nothing is downloaded: ffmpeg reads the audio straight off the
video link, so even a 2-hour, 750 MB recording costs no disk space.

## Optional (better output)

- **Handwritten style:** install the free **Kalam** font once —
  https://fonts.google.com/specimen/Kalam → *Get font* → *Download all* → unzip →
  select the `Kalam-*.ttf` files → right-click → **Install for all users**.
  Without it the notes still generate, in a normal font.
- **PDF export** uses Microsoft Word if it is installed. If you only get a Word
  file, open it and *Save As → PDF*.
- **Offline transcription:** `pip install faster-whisper` and set
  `TRANSCRIPTION_ENGINE=faster_whisper` in `.env` to transcribe on your own
  machine instead of through the proxy. Slower, and less accurate on Hinglish
  unless you use a big model (`WHISPER_MODEL=medium` or `large-v3`), which is
  slower again. Most people should leave this alone.

## If something goes wrong

- **Sign-in shows an error / 400** — make sure the app is at
  `http://localhost:8501` (not another port), and that no other copy is already
  running. Close the black window and start again.
- **"Not authorized"** — your `@pw.live` email isn't on the approved list yet
  (see step 3 above).
- **"Couldn't verify access"** — check your internet connection and try again.
- **Nothing happens on double-click** — Python isn't installed or wasn't added to
  PATH; reinstall Python with "Add Python to PATH" ticked.

See `README.md` for the full details of how the app works.
