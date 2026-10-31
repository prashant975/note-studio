# QBG Studio

Takes a faculty Word paper all the way to live questions on the QBG portal —
without opening a terminal or editing a spreadsheet.

You upload the two Word files, check the questions on screen (especially the
equations), and press a button to create them. QBG Studio does the rest.

---

## Starting it

**Double-click `RUN_QBG_STUDIO.bat`** — or, with the team package,
`START_QBG_STUDIO.bat` at the top of the package. Both run the same
`launch.py`.

A black window opens and your browser goes to QBG Studio once it answers. If
QBG Studio is already running, the launcher opens that one instead of
starting a second copy; if port 8000 is taken by something else it moves to
the next free port and says so. On a source checkout the first start spends
a minute installing the libraries; the team package carries its own Python
with everything in it, so nothing is installed.

**Leave the black window open while you work.** Closing it stops QBG Studio.
Nothing is lost when you do — everything is saved as you go.

If the browser does not open by itself, go to <http://127.0.0.1:8000>.

---

## What you need before the first run

| What | Why | Needed for |
|---|---|---|
| **Python 3.13** | runs the app | everything — not needed with the team package, which carries its own |
| **Pandoc** | reads Word files | reading a *new* Word paper |
| **A PW sign-in** | reads equations and translates, on PW's shared keys; also the gate on every step that writes | reading a paper, converting equations, preview, pilot, create, both bots |
| **A QBG login** | creates the questions | the final step |

QBG Studio checks all of these when it starts and tells you, in plain
language, what is missing and where to get it. Nothing crashes — screens that
need a missing piece simply explain what to install.

Pandoc is only needed to read a **new** Word paper. A paper that was already
prepared on another computer can be reviewed and created here without it.
LibreOffice is no longer needed: equation pictures are drawn by Pillow with
Windows' own renderer.

### Installing Pandoc

Fastest route, in a Command Prompt:

```
winget install --id JohnMacFarlane.Pandoc --silent --accept-package-agreements --accept-source-agreements
```

Otherwise use the installer from pandoc.org/installing.

QBG Studio looks for both on the PATH and, failing that, in the places their
installers use, so it usually notices a fresh install without a restart. If
the New batch screen still says they are missing, restart QBG Studio.

### Signing in to PW (once)

Open **Settings** and press **Sign in with Google**. Choose your `@pw.live`
account in the tab that opens. That is the whole thing.

QBG Studio holds no AI key of its own. Reading equations and translating
questions run on PW's shared keys through the PW proxy, which records the
usage against **QBG Automation** and checks that you are on that app's
allowlist first. If you are not on it yet, the screen says so and names what
to ask for.

The same sign-in is the gate on every step that reads a new paper, converts
equations, previews, creates, or runs a bot: each of those is refused with a
"Stopped before anything happened" page until you are signed in and on the
allowlist. Without a sign-in you can still open, review and export a batch
that was already read, and type formulas in by hand.

**One-time, per computer** (for whoever installs QBG Studio): before anyone
can sign in, the Google sign-in client has to be pasted in. On **Settings**,
under *PW sign-in*, expand **Set it up now**, paste the OAuth client ID and
client secret the admin gives you (a *Desktop app* client; leave the
redirect address empty), press **Save and continue**, then **Sign in with
Google**. It is stored encrypted on this PC and never shown again; **Change
client** on the same card replaces it. Neither value is an AI key: they
identify the app to Google at login and give access to nothing that costs
money.

Setting `PW_GOOGLE_CLIENT_ID` and `PW_GOOGLE_CLIENT_SECRET` as Windows
environment variables is only a fallback for a managed machine. When they are
set they win over the pasted client, and the *Change client* button goes
away.

### Signing in to the portal

Two ways, either is fine:

1. **The portal browser** — the same debug Chrome the Translation and
   Solution bots use. Press *Open the portal browser* on the Batches screen
   (or start `START_DEBUG_CHROME.bat`), log in to QBG in the Chrome window
   that opens, and leave it open. *Check the login* on the same card
   confirms it.
2. **A token** — set `QBG_BEARER_TOKEN` as a Windows environment variable
   (Windows key → `environment variables` → *Environment Variables* → *New*).

QBG Studio never shows, saves, or writes down your session. The most it will
ever tell you is *"valid, expires in 137 hours"*.

---

## The five steps

### 1. Upload

*New batch* → drop the **question paper** in the first box and the **answer
key & solutions** in the second. Getting these the right way round matters.

Press **Read this paper**. It takes about a minute — most of that is drawing
the equations.

### 2. Read the report

You get a summary: how many questions were found, how many answers, how many
equations, and — most importantly — **what is wrong**.

Problems are listed one by one, and each links straight to that question.

> If the report says the two documents disagree — a missing question number, a
> missing answer — that is real. Sort it out before creating anything. QBG
> Studio will not let you skip past it quietly.

### 3. Review — the part that matters

One question per screen.

- **Left:** the question exactly as it will look on the portal.
- **Right:** every equation in that question — the **picture from the original
  Word file** beside the **formula that was read from it**, with a live
  preview underneath.

Your job on the right-hand side is one thing: **does the preview match the
picture?** If not, edit the box; it re-renders as you type. Physics is
unforgiving — a wrong exponent is a wrong question.

| Key | Does |
|---|---|
| <kbd>A</kbd> | Approve — ready to create |
| <kbd>S</kbd> | Skip for now |
| <kbd>F</kbd> | Flag — something is wrong, with a note |
| <kbd>←</kbd> <kbd>→</kbd> | Previous / next |
| <kbd>Ctrl</kbd>+<kbd>Enter</kbd> | Save the formula you are editing |

**Convert equations** reads every equation with AI in one go. Check them
afterwards — that is what this screen is for.

**Approve N text-only** approves the questions that have no equations and no
pictures in one click. It asks first, and names the number.

You can close the browser at any point. Reopening the batch puts you back on
the same question.

Some questions cannot be approved, and the screen says why:

- an equation has not been converted yet
- the question contains a **picture** — those must be built by hand in the
  portal for now
- the answer key does not say which option is correct
- it is not a single-correct question — only those can be created at present

### 4. Create

Three buttons, and they unlock in order:

1. **Preview** — shows exactly what would be sent. Creates nothing, changes
   nothing.
2. **Create N as a test** — the pilot, available once you have looked at the
   preview. N is the **Test run size** in Settings (default 5; 0 switches
   the test run off, and the step is then shown as skipped). *Then go and look
   at those N on the portal.*
3. **Create all** — available once the pilot has finished cleanly, or
   straight after the preview when the test run is off.

Changing the test run size in Settings resets the preview and pilot approval
of every batch, because that approval was given for a different group size.

You will be asked to confirm, and the confirmation names the number.

That order is enforced by the app itself, not just by greyed-out buttons.

> **Why the fuss?** Creating a question cannot be undone, and sending the same
> one twice puts a duplicate in the live student-facing bank. Every safeguard
> here exists for that one reason.

**If you see "needs check"** — a question timed out on the way to the portal.
It may have been created anyway. QBG Studio will never re-send it on its own.
Search for it on the portal, decide, and clear it. That number must be zero
before the next batch.

### 5. Export

The batch screen has:

- **Created IDs (CSV)** — the QBG ids, for your records or for adding rows
  to the bots' sheet by hand (translating no longer needs it — see step 6)
- **The review sheet** — the full spreadsheet

### 6. Translate and add solutions

Both bots are the same two programs the `.bat` files start; QBG Studio
launches them as child processes, points each at a tab of its own in the
control sheet, and shows their progress. You never edit the Google Sheet for
a translation.

**Translate** — open **Translate questions** in the sidebar. Questions
created here that are not yet translated are pre-ticked; or paste QBG ids or
question links into the box. Choose the direction and press **Translate**.
QBG Studio makes a private tab in the control sheet for that run alone
(`QBG Run <install id>-<run number>`), starts the Translation Bot on it with
the number of workers set in Settings, gives every failed question one
automatic second attempt, and sends you to the run's page. Every run stays
under **Past runs** with its own **Excel report**; the *Excel report* button
on the Translate screen covers everything Studio has created and where its
translation stands.

**Solutions** — open **Solutions** in the sidebar. It runs the Solution
Addition Bot on the rows of Studio's own queue tab (`QBG Studio Queue` on
this computer; a fresh install names it `QBG Queue <install id>`) whose
*Sol Addition Status* is `Pick for Addition`. Rows reach that tab from the
**Send to bots** page (`/queue`) or from the sheet card's *Add rows to it*;
the *Correct Option / Answer* and *Text Sol* columns the bot writes from
have to be filled in the sheet by hand — nothing in Studio writes them yet.
Use **Try one row first**, then **Run N questions**; **Keep running** leaves
the bot watching the tab until you press Stop, which is what the old black
window did.

Both need the portal browser open and logged in (the *Portal browser* card
on the Batches screen, and on the Solutions screen). The sheet card on the
Solutions screen is also
where `credentials.json`, the service-account key that opens the sheet, is
added or replaced.

---

## Things that are not finished yet

These are known, and QBG Studio says so on screen rather than failing:

| What | What happens now |
|---|---|
| **Question types** | Only single-correct (SCQ) can be created. Others are refused with a clear message. |
| **Hindi** | Uploads without any setup: the portal's own language registry supplies the language id when the run starts (24 September 2026 - before that, a Hindi paper was refused on every fresh install for want of a hand-copied id). The Settings field is optional. |
| **Pictures** | Cannot be uploaded automatically. Those questions stay flagged for manual work. |
| **Solution source data** | The Solution Addition Bot reads *Correct Option / Answer* and *Text Sol* from the sheet; nothing in QBG Studio writes those columns yet, so they are filled in by hand. |

The portal link format **is** set
(`https://qbg-admin.penpencil.co/question-details?question={qbg_id}`) and
Settings shows *Portal link set*. Leave that box empty unless the portal
changes its address.

---

## When something goes wrong

**"review.xlsx is open in Excel"** — close it. QBG Studio saves to that file
after every question and cannot while Excel is holding it.

**"Your portal session expired"** — log in to QBG again in debug Chrome, then
click Retry on the Create screen.

**Equation conversion stopped early** — usually the PW proxy refusing or
rate-limiting a call. Wait a few minutes and run it again; everything already
converted is kept.

**The black window closed by accident** — nothing is lost. Double-click
`RUN_QBG_STUDIO.bat` (`START_QBG_STUDIO.bat` with the team package) again
and open the batch; you will be back where you
were. If it closed *during* a creation run, check the portal before starting
another one — QBG Studio will tell you which questions to look at.

---

## For whoever maintains this

QBG Studio is a thin local web layer over the existing pipeline in
`2_Question_Creation_Pipeline/`. It **imports and calls** those modules rather
than reimplementing them — in particular `create_questions.build_payload()` is
used verbatim, because its payload shape was verified against a real captured
request.

```
launch.py           what both launchers run: reuse a running Studio, free port, open browser
app.py              FastAPI routes
jobs.py             background jobs + the server-side creation gate
db.py               SQLite: batches, jobs, equation snapshots, events
pipeline_bridge.py  the only module that touches the pipeline or the bots
templates/          Jinja2, one file per screen
static/             CSS, and KaTeX + HTMX vendored locally
```

and, one level up at the toolkit root, shared with the pipeline and the bots:

```
pw_access.py        the PW App Kit v2 client, APP_NAME = "QBG Automation"
pw_auth.py          Google sign-in, token store, and the allowlist gate
pw_login.py         `python pw_login.py` - sign in from a terminal
```

Design decisions worth knowing before changing anything:

- **No provider key is anywhere in this toolkit.** Every AI request goes to
  GPT (Sol, Terra or Luna) through the PW shared proxy: `proxy_gpt.py` is the
  transport and `ai_request.py` holds the request shapes (`Part`, `Config`)
  and the two errors every caller handles. Gemini, and the classic Translation
  Bot built on it, were removed on 20 September 2026; Bulk translation (GPT)
  replaces the bot.
- **The allowlist gate is server-side and fails closed.** `_pw_guard` in
  app.py runs before any job is scheduled, and the child processes gate
  themselves as well, so a hand-crafted POST cannot spend anything. The
  cached `preflight.pw` only decides what the UI *offers*.
- **One `task_id` per user task, and one stable `input_unit`.** The proxy
  groups a job's calls by task_id; without one its volume cannot be totalled.
  This app always sends `input_unit="No. of questions"` and repeats the job's
  question total as `count` on every call in the task - never a per-call 1,
  which would multiply the volume by the number of calls per question. That
  convention is documented in `ai_request.py`; do not mix it with the other.
- **The bots run as child processes, unchanged.** Same reasoning as creation:
  their claim-and-verify behaviour against the sheet is preserved exactly.
  Studio starts them with `RUN_ONCE=1` by default, in a new process group so
  Stop can deliver the Ctrl+Break they already handle rather than killing
  them mid-question.

- **`review.xlsx` stays the source of truth** for question content and status,
  because `create_questions.py` reads and writes it and its crash-safety model
  is built around that file. SQLite holds only what the sheet cannot.
- **Creation runs as a subprocess** of the real `create_questions.py`, so its
  claim-before-send behaviour is preserved exactly rather than reimplemented
  slightly differently. The child gets `PYTHONUTF8=1`, which sidesteps an
  encoding crash in the pipeline's timeout handler that would otherwise abort
  a live batch.
- **Preview runs in-process and read-only.** The pipeline's own dry run writes
  status changes back to the sheet; a preview that says "creates nothing"
  should also change nothing.
- **Equation edits are never destructive.** The pristine cell text, tokens and
  all, is snapshotted before conversion; every rendered cell is recomputed
  from that snapshot plus the current formula map. A blank formula is refused
  rather than substituted, so a failed conversion leaves the token visible and
  the question un-approvable — instead of silently deleting the equation.
- **The gate lives in `jobs.check_gate()`**, not in the templates. A
  hand-crafted POST cannot skip preview → pilot → all.
- **Local only.** No authentication, no multi-tenancy, bound to 127.0.0.1. The
  pipeline drives the operator's own logged-in Chrome and the portal allows
  one session at a time.
