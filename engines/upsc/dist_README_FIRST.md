# UPSC_Notes — Team Guide (Windows, no install)

**Version 2.13.3 · 12 October 2026**

This release fixes the Google Drive transcript request's authentication and adds
**Use existing Drive transcript (optional)** below a Drive recording link. If
the transcript is visible in Drive but automatic retrieval is blocked, paste
the full text there to skip audio extraction and speech recognition. The app
still reads existing captions automatically when Google's service allows it.
A PDF pasted into the video-link field now gets a clear input message. Diagram
cleanup runs during notes generation: backgrounds become transparent before
the Word and PDF files are built. You can turn cleanup off for a faster run.
The Windows package also skips source-file watching to speed up startup.

This update fixes Drive transcript cache permissions, malformed Drive token
responses and stored account data, fraction grouping in notes, and numeric/source
preservation during Hindi and Marathi repairs. Failed Drive refreshes are reported
per lecture, and unusable cached transcripts are retrieved again automatically.
Notes, DMP and DPP PDFs now use the bundled PDF renderer. No Microsoft Word
or LibreOffice installation is required. The existing Word builders are retained.
Hindi/Marathi text remains searchable and copyable. DPP replacement now explains
when a topic has no unused questions at the selected difficulty.

What changed in this version (the issues the team reported):

- **Drive recordings shared only inside PW now work.** Sign out and sign in
  again once, and tick the Google Drive box when Google asks — see
  "Class recordings shared only inside PW" below.
- **Cleaner notes.** Headings no longer print as `## H2:`, formulas no longer
  print as `$...$` (you get →, H₂O, CO₂), tables print as tables instead of
  `| --- |` lines, flowcharts print as steps joined by ↓, and a flowchart no
  longer repeats the bullets next to it. The page number in the footer no
  longer runs into "Daily Class Notes".
- **Hindi and Marathi notes**: lines that still had English phrases are
  rewritten in Hindi / Marathi before the notes are made. Marathi notes no
  longer slip into Hindi.
- **Mains practice (DMP) sheets** follow the house format: "<Subject> DMP <No>"
  at the top, the topic box under it, marks and words beside the question, and
  a "How to Approach the Question?" box. Set **Subject**, **Topic** and
  **DMP No.** in the boxes above the download buttons.
- **Model answers fit the word limit** (150 words for 10 marks, 250 for 15) as
  introduction, body and conclusion — no extra sub-headings, no "Point:". An
  answer that still runs long is flagged beside it.
- **Practice set (DPP) PDFs** follow the house format: the subject as the
  title with "DPP: 1" beside it, two columns, options side by side when short,
  and "Statement 1 is correct:" in bold in the solutions.

The AI is Gemini (`gemini-3.5-flash`). If it is unavailable, the app falls
back to `gemini-3.6-flash` automatically and the run's warnings say so.
Diagram redraws and class-recording transcription also run on Gemini. Close
an older app before starting this version.

Turn a class recording (Google Drive, YouTube, or a direct video link) + the
lecture slides into detailed UPSC notes (Word + PDF). **No Python, no ffmpeg,
no setup — everything is included. Just run the app.**

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
   Google account. When Google asks, tick **"See and download all your Google
   Drive files"** and click **Continue** — see below.

## Before it will let you in (one-time)

Your `@pw.live` email must be **approved** for this app. If sign-in says
"not authorized", ask the admin to add your email to the
**"Handwritten Notes Automation"** column of the shared Whitelisted sheet.

## Class recordings shared only inside PW

The app uses the signed-in person's Google Drive access to open recordings
shared only inside PW or with named people. That account must have access to
the file, the owner must permit downloading, and PW's Google Workspace settings
must permit the app's Drive access. The sign-in asks Google to let the app **see
your Google Drive files** (read-only: it never changes or deletes anything).

- **Signed in before this version?** Click **Sign out** in the left panel and
  sign in again once, and tick the Drive box when Google asks.
- The left panel says **Google Drive: connected** when it worked.
- Signing out removes the app's Drive access from this computer.

## Make notes

1. **Lecture video link** — paste the class recording link. **Required.**
   Google Drive, YouTube, and direct video links all work. YouTube and Drive
   links are recognised by name; anything else is *tried* as a direct video
   link, and whether it really is one is only known once the download starts.
   The transcript is read from the video automatically when available. If the
   Drive transcript is visible in your browser but cannot be read by the app,
   open **Use existing Drive transcript (optional)** and paste its full text.
   Pasted text is used directly, without audio extraction or speech recognition.
2. **Lecture slides** — upload the class PDF / PPTX / PPT. **Optional.** Many
   classes have no deck; leave it empty and the notes are written from the
   recording alone, without the slide diagrams.
3. **Notes language** — **you have to choose one.** There is no default,
   deliberately: the language is invisible until the notes are already written,
   and getting it wrong wastes the whole run. It is the language the notes are
   *written* in, independent of the language the class was taught in — a Hindi
   class can produce Marathi notes.
4. Click **Generate UPSC notes**, then download the Word/PDF.

The important diagrams, maps and flowcharts from the slides are placed into the
notes automatically, next to the topic they explain. Logos, decorations and
plain-text slides are left out — nothing to configure.

**Make diagram backgrounds transparent** is on by default. The app cleans the
placed diagrams before exporting Word and PDF, retaining the original images.
This can add generation time. Turn the option off to keep the original diagrams;
if editing fails, the original is used and a warning is shown.

Your finished files are saved next to the app in an **`outputs`** folder.

## What you get (changed)

Every run now gives you **three files and no others**:

| File | What it is |
| --- | --- |
| `*_upsc_notes.docx` | The notes, student-ready |
| `*_upsc_notes.pdf` | The same notes as PDF |
| `*_error_report.xlsx` | **One sheet listing what the lecture got wrong** |

The old quality / error / error-fixed / raw-notes reports are gone from the
download area. They are still written into the run folder if a developer needs
them — you just are not asked to read them any more.

## The Error Report

One row per real problem, in the agreed seven-column format: **Error. No. |
Original statement | Error type | Severity | Error / concern | Correct
information / correction | Source URL(s)**. Severity is colour-coded — red for
High, yellow for Medium, green for Low.

The corrected information is **already in the notes**. The sheet is the record
of what changed, for the reviewer — students never see it.

Three things worth knowing, because they are deliberate:

- **"No issues found" is written down, not left blank.** If the checks ran and
  found nothing, the sheet still downloads with one row saying so — so you can
  tell it apart from a check that never happened.
- **If the AI's report could not be read, the sheet says that too**, and the app
  shows a warning. It is never presented as "0 issues", because that is the one
  message that would stop you reviewing by hand.
- **The app never silently deletes what the faculty taught.** A point it cannot
  confirm is reported as needing verification and left in the notes — "no source
  found" is never treated as "wrong".

Under **UPSC format & layout → Error report** you can switch the report off,
choose how strict it is (Basic / Standard / Strict), and upload
`Error Report Format (1).xlsx` as a template to match its styling exactly.

Under **Advanced Quality Settings** you can turn off source verification, the
latest-data check, the visual PDF check, or automatic corrections. Off/Basic/
Strict controls how deep the check goes; Strict is the default and is the only
level that looks anything up on the web.

> Verifying facts against **standard books** only works once the reference
> library is loaded — until then book-based checks honestly report "source not
> found" instead of guessing.

## About this document

When a run finishes, the card may show a short **"About this document"** note.
It appears only when the finished document differs from what the page promised —
and it stays silent otherwise, which is what makes it worth reading:

- **the page limit could not be checked** — page count is measured from a
  rendered PDF with the bundled renderer. If PDF generation fails, the limit is not
  enforced and the notes ship at full length;
- **some slide diagrams are not in the notes** — a diagram is left out rather
  than dropped into a section it does not belong to. The slides still have it.

## Practice sets (DPP) — works out of the box

The **Daily Practice Problems** panel builds a question set from the QBG bank
for a lecture you have already generated notes for. Every question is an
existing bank question tagged to an existing taxonomy row — nothing is invented.

The two master workbooks now **ship inside this download**, in the `data`
folder next to `UpscNotesApp.exe`:

```
UpscNotesApp/
  UpscNotesApp.exe
  data/
    UPSC QBG Tags.xlsx
    DPP and Infinite Practice Questions.xlsx
```

Keep that folder where it is — if the panel ever says the workbooks are
missing, the `data` folder was moved or the files were deleted. The first use
in a session takes about **40 seconds** to read the bank — it says so before
you commit to the wait, and every set after the first is immediate.

Choose the topics by Subject and Chapter. Then either set how many Easy,
Medium and Hard questions you want, or pick the questions yourself. The
subject printed on the PDFs is filled in from the lecture. You can change it.

A built set downloads in four forms: the question paper alone, the answers and
solutions alone, one PDF with each question followed by its answer and
solution, and a ZIP holding every file.

These workbooks are PW's own question content — treat this ZIP with the same
care as the embedded sign-in secret and keep it inside the team.

(An admin can instead point `QBG_TAGS_PATH` and `QUESTION_BANK_PATH` at a shared
copy; those always win over the `data` folder.)

---

## Optional (better output)

- **Handwritten style:** install the free **Kalam** font once —
  https://fonts.google.com/specimen/Kalam → *Get font* → *Download all* → unzip →
  select the `Kalam-*.ttf` files → right-click → **Install for all users**.
  Without it the notes still generate, in a normal font.
- **PDF export** works with the bundled renderer. If an export fails, the app
  shows the error and keeps the editable Word document available.

## ffmpeg — included, nothing to install

The app reads the lecture audio with **ffmpeg**, which is **bundled inside this
folder** — you do not need to install anything. If your machine already has its
own ffmpeg on PATH, that one is used instead.

The video itself is never downloaded, so a 2-hour recording uses no disk space.
(When Google Drive or YouTube already have a transcript for the video, even the
audio step is skipped.)

## If something goes wrong

- **"Windows protected your PC"** — click *More info → Run anyway* (unsigned app).
- **Sign-in error / 400** — close the black window and any extra copies, then
  start again. The app must be at `http://localhost:8501`.
- **"Not authorized"** — your `@pw.live` email isn't approved yet (see above).
- **"Couldn't verify access"** — check your internet and try again.
- **A Drive video "is not shared with your PW account"** — you cannot open it
  either; ask whoever shared it to give your account access (Viewer).
- **A Drive video says to "sign out and sign in again"** — do that, and tick the
  Drive box when Google asks.
- **A Drive video mentions the "Google Drive API"** — the admin still has to
  switch it on for the app (one-time); until then use an "Anyone with the
  link" link.
- **"Port 8501 already in use"** — an old copy is still running; close its black
  window (or restart the PC) and try again.
- **A tab shows a red error box** (for example `No module named ...`) — the
  build itself is missing something. Open a Command Prompt in the
  `UpscNotesApp` folder and run `UpscNotesApp.exe --selftest`: it checks that
  every part of the app can load and builds a sample practice set, without
  starting the server, and ends with **SELF-TEST PASSED** or a list of exactly
  what is broken. Send that output to the admin with your report.
