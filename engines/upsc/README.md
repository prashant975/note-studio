# UPSC_Notes

Turn UPSC lecture PPTs/PDFs into detailed, exam-ready study notes.

```text
Lecture PPT/PDF
→ slide/page extraction
→ subject auto-detection (Polity, History, Geography, Economy, …)
→ AI notes generation using UPSC subject prompts (Prelims facts + Mains analysis)
→ clean typed DOCX formatting
→ automatic map/diagram insertion
→ PDF export
```

Every file is turned into a structured notes set:

```text
Topics Covered in the Class
<Main Topic Heading>
  • explanatory bullets (terms, dates, Articles, data)
Prelims Pointers   ← crisp, memorisable facts
Mains Angle        ← analytical value-additions for answer writing
```

## 1. Subjects covered

Auto-detected from the file name and content (13 GS subjects):

Polity & Governance · Ancient / Medieval / Modern History · Art & Culture ·
Geography · Economy · Environment & Ecology · Science & Technology ·
International Relations · Society · Internal Security · Ethics (GS4).

Detection lives in [src/subject_detect.py](src/subject_detect.py); per-subject
emphasis (what to stress for Prelims vs Mains) lives in
[src/upsc_subjects.py](src/upsc_subjects.py). Tune either to change note quality.

## 2. Install on Windows

Open PowerShell in this folder:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Or double-click `START_WINDOWS.bat`.

## 3. Configure `.env`

**No AI key is needed.** AI runs through the shared **PW proxy**
([pw_access.py](pw_access.py)); the proxy holds the key. Copy `.env.example` to
`.env` — it selects the model and names the app:

```env
MODEL_NAME=gemini-3.5-flash
FALLBACK_MODEL_NAME=gemini-3.6-flash
IMAGE_MODEL_NAME=gemini-2.5-flash-image
APP_NAME=UPSC_Notes
```

### Access & the app name (important)

There are **two** names, kept deliberately separate:

| Name | Where | What it controls |
| --- | --- | --- |
| **Display name** | `APP_NAME` (`.env` / `src/config.py`) | UI titles, output branding — cosmetic, safe to change. Set to `UPSC_Notes`. |
| **Proxy identity** | `APP_NAME` in [pw_access.py](pw_access.py) — currently `Handwritten Notes Automation` | Authorization + usage logging on the shared PW proxy. |

The proxy authorises a user only if their `@pw.live` email is listed under the
proxy-identity **column** of the shared
[Whitelisted sheet](https://docs.google.com/spreadsheets/d/1aaF3y0VsgyB_YcyfDK33VWcCagzwxBPOwjoe2TvfbHE),
and the identity is registered on the proxy (`/api/apps`).

The proxy identity is **a literal in `pw_access.py`, not an environment
variable** — the pw-app-kit client reads no env override for it, so there is no
`PROXY_APP_NAME` setting (earlier revisions of this README documented one that
never actually worked). To change the identity you must edit that one line.

- It stays **`Handwritten Notes Automation`**, so everyone already whitelisted
  keeps working after the UPSC rebrand.
- To switch it to `UPSC_Notes`, first ask the proxy admin to add a
  **"UPSC_Notes"** column to the Whitelisted sheet and register the app, then
  edit `APP_NAME` in `pw_access.py`. **Do not** switch before that — the app
  fails closed and every user is denied.

Verify the identity is registered:

```powershell
python verify_onboarding.py
```

## 4. Google login

Google login needs real OAuth credentials in `.streamlit/secrets.toml` (create
it from `.streamlit/secrets.toml.example`). Redirect URI in Google Cloud:

```text
http://localhost:8501/oauth2callback
```

The proxy verifies a Google **id_token**, so expose it:

```toml
[auth]
expose_tokens = ["access", "id"]
```

### Google Drive videos shared only inside PW

The sign-in also asks for **read-only Google Drive** access
(`https://www.googleapis.com/auth/drive.readonly`), with offline access, so the
app can open a class recording exactly as the signed-in person could — including
one shared only inside pw.live or with named people (`src/drive_auth.py`). The
template already carries the settings (`client_kwargs` scope + `prompt`, and
`authorize_params = { access_type = "offline" }`).

Once, in the Google Cloud project that owns the OAuth client:

1. **APIs & Services → Library → Google Drive API → Enable.** Without it the
   app tells the user to ask for exactly this.
2. **OAuth consent screen → Data access / Scopes:** add `.../auth/drive.readonly`.
   For an *Internal* (pw.live-only) app no Google verification is needed.

Then each person signs out of the app and signs in again once, and ticks
"See and download all your Google Drive files" when Google asks. The profile
panel shows "Google Drive: connected". The refresh token is kept per person
in `%LOCALAPPDATA%\UPSC_Notes\drive_access.json`, encrypted for that Windows
account (DPAPI), and deleted when they sign out.

Each person must also be approved in the app's PW allowlist. For a lecture
shared with named people, the person signed into the app must be one of those
people. PW's Google Workspace administrator must permit this OAuth app to
access Drive; a `pw.live` address by itself does not grant file access.

## 5. Run

```powershell
python -m streamlit run app.py
```

In the app you can:

- **Upload multiple lecture files at once** — each becomes its own DOCX/PDF.
- **Preview** the generated notes inline before downloading.
- **Inserted images** tab — see the maps/diagrams the pipeline placed in the notes.
- **Edit & rebuild** — tweak the notes text and regenerate the DOCX/PDF without
  calling the AI again.

### Class video: the transcript is the content

Paste the class recording link and choose the notes language. You can also
upload the lecture PPT/PDF. The transcript is read from the recording link
automatically.

| Paste this | Recognised as | How the transcript is obtained |
| --- | --- | --- |
| `https://drive.google.com/file/d/.../view` | Google Drive | **Drive's own transcript first**, when readable through the player's timedtext service; if unavailable or unreadable, FFmpeg streams the audio and STT runs. Existing text pasted into the optional Drive transcript field skips both. |
| `https://youtube.com/watch?v=...`, `https://youtu.be/...` | YouTube | Published captions if there are any, else the audio; STT |
| `https://…cloudfront.net/…/lecture_720p.mp4` and other direct/CDN links | Direct video URL | FFmpeg streams the audio off the URL; STT |

Drive and YouTube follow the same transcript-first rule: text Google already made
is used before any media is touched. Drive uses the current playback endpoint
and timedtext service; the older `get_video_info` endpoint remains a fallback.
Local verification retrieved **32,488 words in 2.53 s** from a three-hour Drive
recording, with no video download or STT. Repeat retrieval took **0.85 s** from
the account-scoped cache after checking current Drive access. Times depend on
the connection and recording. A PDF link in the video field is identified as a
PDF, with guidance to upload it in the lecture file field. The fallback fires automatically when the native
transcript is missing, empty, or unreadable — `run_metadata.json` records which
path served the run (`native_transcript_found`, `stt_used`,
`transcript_retrieval_seconds`).

A transcript visible in Drive's browser viewer is not always readable with the
app's OAuth sign-in. Google's private playback API can refuse the app's Google
project even when the same token can download the recording. OAuth requests
therefore omit Google's viewer API key, which belongs to a different project.
For these recordings, open **Use existing Drive transcript (optional)** and paste
the full text from Drive's Transcript panel. Supplied text takes precedence over
the video link before any lookup or audio extraction; it is recorded as
`transcription_engine=supplied`, with `stt_used=false`.

The kind of link is **detected from its host**, not its file extension — signed
CDN URLs frequently end in a query string rather than `.mp4`. All three converge
on one normalized transcript ([src/models.py](src/models.py) `NormalizedTranscript`),
so everything downstream is unchanged.

**No video is ever downloaded.** FFmpeg opens the remote URL and decodes only the
audio track: a 750 MB, 2-hour lecture yields 10 MB of audio in about 2½ minutes
(measured at ~44× realtime), and the video bytes are never stored.

When a transcript is available the pipeline switches to a **transcript-driven** flow:

- **Transcript = the content.** It is what the teacher actually taught — the
  concepts, reasoning, examples, and exam emphasis ("*this is important for
  Prelims*", "*asked in 2019*"). PW decks are usually sparse (headings +
  keywords); a 2-hour class is ~15,000 words. The substance is in the teaching.
- **Slides = reference.** They supply the topic structure and heading wording,
  the correct spelling of terms/Articles/dates/schemes, and tell the pipeline
  which slide holds a diagram/map worth inserting.

When the two disagree on a fact, term, or spelling, **the slides win** —
auto-generated captions mangle terms, so the slides are the ground truth.

The whole class is written in **one AI call**: a 2-hour transcript plus 40 slides
is only ~0.4 MB / ~23K tokens, far inside both the proxy's 4 MB body limit and
Gemini's context. That beats chunking — the model sees the full arc of the
lecture and nothing has to be stitched back together. Slide *images* are not sent
on this path (images are what force small chunks); diagrams are still inserted
afterwards from the DTP notes' slide numbers.

#### How the transcript is made

```
lecture video link
   -> source router      src/video_sources/   detect host, validate against the service
   -> media              src/media/           ffprobe checks for an audio track, ffmpeg extracts it
   -> STT router         src/transcription/   gemini | faster_whisper
   -> normalized transcript                   full_text + timed segments + gaps
   -> existing cleaner, note engine, QC, repair, DOCX/PDF/XLSX   (unchanged)
```

YouTube captions short-circuit the media path entirely: they are free, arrive in
~2 s, and are *text*, so the proxy's ~4 MB body limit never applies. Manual
captions are preferred over auto-generated ones.

**Transcription engines** — set `TRANSCRIPTION_ENGINE` in `.env`:

| Engine | Speed on a 2-hour class | Notes |
| --- | --- | --- |
| `gemini` *(default)* | minutes | Runs through the PW proxy. Audio is compressed to **Opus 12 kbps mono** and split into ~25-min chunks, because 25 min ≈ 2.4 MB base64 against the proxy's ~4 MB ceiling (16 kbps MP3 would be 4.0 MB and would not fit) |
| `faster_whisper` | see below | Local and offline, uses no proxy quota. Extra install: `pip install faster-whisper`. Downloads its model on first use. Tune with `WHISPER_MODEL` (`tiny`/`small`/`medium`/`large-v3`) |

Measured on the reference lecture (2 h 01 m, CPU, int8, VAD on): `tiny` finished
in **4.5 minutes** (~27× realtime) — but it mangles Hindi terms badly, so it is
only useful for smoke-testing. `small` is the smallest size worth real use and is
several times slower again; `large-v3` is better still on Hinglish and slower
again. On a GPU set `WHISPER_DEVICE=cuda`. This accuracy/time trade is why Gemini
is the shipped default.

If the selected engine cannot run, the other is used instead (disable with
`TRANSCRIPTION_FALLBACK=false`). A failed *chunk* does not lose the lecture: the
gap is recorded as a time range like `50:00-1:15:00`, reported in the error
report, and the rest of the class carries on.

**A transcript is now required.** If none can be produced, the run stops with a
plain error rather than quietly shipping slides-only notes that look complete.
Use `--no-transcript` on the CLI for slides-only debugging.

**The same lecture is never transcribed twice.** A finished transcript is cached
under `runs/_transcript_cache/`, so re-running a class — to change the notes
language, or after a failed render — reuses it. Document repair never re-touches
the video. The transcript is saved to `runs/run_.../transcript.txt`, with timings
in `transcript_segments.json`.

**Requirements:** `ffmpeg` and `ffprobe` on PATH. Public "anyone with the link"
Drive files need no credentials; to reach files shared only inside the
organisation, point `GOOGLE_SERVICE_ACCOUNT_JSON` or
`GOOGLE_APPLICATION_CREDENTIALS` at a service account with read access. Nothing
here bypasses Drive or YouTube permissions — a file the app is not authorised to
read returns a clear error.

```powershell
python run_cli.py --input lecture.pdf --video-url "https://drive.google.com/file/d/.../view"
python run_cli.py --input lecture.pdf --video-url "https://youtu.be/..." --transcription-engine faster_whisper
```

### Language: notes match the class

The notes language is **auto-detected from both the class transcript and the
slides** ([src/language_detect.py](src/language_detect.py)). No setting needed.

**The transcript wins when it is confident**, because PW decks are frequently in
English even when the class is *taught* in Hindi — and the student needs notes in
the language they are actually learning in. The slides are the fallback when
there is no video or no usable transcript signal.

Reading the transcript is not as simple as counting Devanagari, because YouTube's
Hindi ASR writes **English speech in Devanagari** ("गवर्नेंस" = governance), which
makes every such transcript look 100% Hindi. So Devanagari text is scored on
**function words** — genuine Hindi grammar words (है, में, नहीं) versus
transliterated English ones (टू, एंड, ऑफ). Function words are used because they
are frequent and unavoidable in either language, while topic words are shared.

| Class | Slides | Transcript | Notes |
| --- | --- | --- | --- |
| Hindi-medium (PW typical) | English | Hindi speech | **Hindi** |
| English-medium, Hinglish captions | English | English transliterated to Devanagari | **English** |
| Hindi-medium | Hindi | Hindi | **Hindi** |
| No video | English | — | **English** (from slides) |

In Hindi notes, technical terms, proper nouns, and Act/scheme names are kept in
their original form. The chosen language is also **locked in the prompt**, so the
model can't drift back to English mid-way.

**Choosing the language.** The app has a **Notes language** selector in the upload
panel:

| Choice | Behaviour |
| --- | --- |
| **Auto (match the class)** — default | Uses the table above: transcript first, slides as fallback |
| **English** | Always writes English notes, whatever the class language |
| **हिंदी / Hindi** | Always writes Hindi notes |

On the CLI, pass `--language auto|English|Hindi`.

### Detailed vs revision notes

The app defaults to **Detailed** notes (`MODE = "complete"` in
[app.py](app.py)) — full Prelims + Mains coverage. Switch `MODE` to `"summary"`
for concise last-mile **Revision** notes.

### Batch mode from the command line

```powershell
python run_cli.py --input path\to\lecture_folder --subject auto --mode complete
```

`--subject auto` auto-detects; pass an explicit key (e.g. `polity`, `modern`,
`geography`) to force one. A single file works too (`--input lecture.pdf`).

## 6. Output style

Notes retain the existing handwritten style: Coming Soon for English/Latin and
Kalam for Hindi/Marathi. DCN uses the PW OnlyIAS template and reserved notes
column; DMP uses the exam-sheet template.
Maps, timelines, and diagrams referenced in the notes are inserted automatically.

**Make diagram backgrounds transparent** is enabled by default in Notes
generation. After selecting and placing the lecture diagrams, the app edits
them for readability on white paper and removes the background before building
both Word and PDF files. Cleanup runs concurrently for the placed diagrams and
can add generation time. Turn the option off to use the original images.
Originals are retained in the run folder; if cleanup fails, the notes use the
original diagram and show a warning. The saved visual registry also keeps the
edited image paths for later exports and rebuilds.

## 7. PDF export

PDF export uses the bundled ReportLab renderer. No Microsoft Word,
PowerPoint, or LibreOffice installation is required for exporting notes, DMP,
or DPP PDFs. The existing DOCX builders still produce the editable Word files.
Hindi/Marathi fonts, tables, diagrams, notes columns, and page numbering are
rendered directly. Pagination can differ when a DOCX is opened in another editor;
the app's page-limit check measures the PDF it actually exports.

## 8. Output files

Each run creates:

```text
runs/run_.../
  input/
  rendered/
  output/
    *_upsc_notes.docx
    *_upsc_notes.pdf
  notes_raw.txt
  filter_report.json
  run_metadata.json
```
