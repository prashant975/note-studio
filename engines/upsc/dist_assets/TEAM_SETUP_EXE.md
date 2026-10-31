# UPSC_Notes — Team Guide (Windows, no install)

Turn a class recording (YouTube link) + the lecture slides into detailed UPSC
notes (Word + PDF). **No Python or setup needed — just run the app.**

> ⚠️ **This folder contains the shared Google sign-in secret. Keep it inside the
> team only — never upload it to any public place.**

---

## Run it

1. **Unzip this folder** somewhere permanent (e.g. Desktop). Keep everything
   together — do not move the `.exe` out of the folder on its own.
2. Open the folder and **double-click `UpscNotesApp.exe`**.
   - Windows SmartScreen may warn ("Windows protected your PC") because the app
     is not code-signed. Click **More info → Run anyway**. This is expected.
   - A black window opens and stays open — that is normal. **Keep it open** while
     you use the app; close it to stop.
3. Your browser opens at **http://localhost:8501**. Sign in with your `@pw.live`
   Google account.

## Before it will let you in (one-time)

Your `@pw.live` email must be **approved** for this app. If sign-in says
"not authorized", ask the admin to add your email to the
**"Handwritten Notes Automation"** column of the shared Whitelisted sheet.

## Make notes

1. **Class video link** — paste the YouTube link of the class (main source).
2. **Lecture slides** — upload the class PDF / PPTX / PPT.
3. **Notes language** — Auto (matches the class), or force English / Hindi.
4. Click **Generate UPSC notes**, then download the Word/PDF.

Your finished files are saved next to the app in an **`outputs`** folder.

---

## Optional (better output)

- **Handwritten style:** install the free **Kalam** font once —
  https://fonts.google.com/specimen/Kalam → *Get font* → *Download all* → unzip →
  select the `Kalam-*.ttf` files → right-click → **Install for all users**.
  Without it the notes still generate, in a normal font.
- **PDF export** uses Microsoft Word if it is installed. If you only get a Word
  file, open it and *Save As → PDF*.
- **Videos with no captions** are transcribed from audio, which needs **ffmpeg**
  (https://www.gyan.dev/ffmpeg/builds/ — "essentials" build, add its `bin` to
  PATH). Not needed when the video has captions (most do).

## If something goes wrong

- **"Windows protected your PC"** — click *More info → Run anyway* (unsigned app).
- **Sign-in error / 400** — close the black window and any extra copies, then
  start again. The app must be at `http://localhost:8501`.
- **"Not authorized"** — your `@pw.live` email isn't approved yet (see above).
- **"Couldn't verify access"** — check your internet and try again.
- **"Port 8501 already in use"** — an old copy is still running; close its black
  window (or restart the PC) and try again.
