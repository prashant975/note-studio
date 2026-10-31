# sheets_bridge — one-button Sheet → Drive → notes → Drive → Sheet automation

For every sheet row whose **"Class Notes Status (By BM)" = Done**, it opens the
row's Drive link, finds the class-notes PDF for that row's **Chapter + Lecture
Number**, generates the short notes with this repo's generator, and uploads
**`<source> - Concise Notes.pdf`** — the studygram PDF, rendered by the unchanged
`notesbot.notes_pdf` — into the **same Drive folder**, beside its source.

Two control columns are appended:

| Q — Auto Status | R — Review Status |
|---|---|
| empty · `claimed <timestamp>` · **`review`** · `failed: <code>` | a **Review / Done / Regenerate dropdown** |

A finished lecture reads **`review`** in Q — the file exists but nobody has
checked it — and **Review** in the R dropdown, which a person switches to
**Done** after looking at the PDF. **M is marked `Done`** (with the **P**
timestamp) so the content team's own column records that the file was produced.

A row is never paid for twice: M = Done, `Q = review` (or the older `done`), or
any value in R all stop it. `--redo` overrides all three.

**Regenerate.** Setting R to **Regenerate** (both sheet types) asks the next run
for fresh notes: the stored copy of the old notes is discarded, no other row's
notes are copied, and the new PDF is written over the old one in its folder -
a new version of the same file, so its link (and the catalog's Concise Notes
Link) stays the same; Drive keeps the old version in the file's history. R then
goes back to **Review**. While the attempt runs, or if it fails, R keeps saying
Regenerate, so the next run tries again.

The PDF keeps its original hand-sketched format exactly
(`build_notes_pdf(notes, signature)`, untouched) with the **PW logo stamped in
the corner and as the watermark**.

### Equations

Formulas become **native Word equations** via
[`equation_kit`](../equation_kit/README.md) (OMML), which Drive keeps when it
converts the file into a Google Doc: real fractions, vector arrows and true
sub/superscripts, still editable by the reviewer. `$...$` spans are rewritten
into the kit's protected tags, and its repair pass rescues formulas the model
wrote as prose (`H2O` → H₂O, `m/s2` → m s⁻², `v0` → v₀). See
[`inline_text.py`](inline_text.py).

### Completeness

Before finalizing, the automation now renders a draft PDF and reviews every
output page against the notes JSON and source PDF. The final check covers
language, grammar, readable formulas/matrices, complete rendered content and
layout defects. Local checks also reject invalid PDFs and formulas that cannot
render. One minimal text-correction pass is allowed; corrected notes are checked
against the source, rendered again, and reviewed again. Unresolved findings
prevent the upload and the Done status.

An approval receipt binds the checked PDF and notes JSON by SHA-256 hashes.
Retries reuse those exact approved PDF bytes without another AI charge. A
missing or mismatched receipt triggers fresh generation instead of uploading
an unreviewed re-render. The old cache revision is no longer used. These checks
add model usage, and model-based visual/language review can still miss defects.

Generation now includes a PDF-backed coverage and repetition review for each
page group. After merging, a separate focused review checks repetition across
the whole lecture, followed by another source coverage check.
Repeated information on different slides keeps the most complete relevant
version, including every distinct condition and explanation. The review also
checks bullets, key-term boxes, callouts, tables, flows and symbol legends.
Concise notes exclude all questions, MCQs, options, answers, worked examples
and solution/proof/derivation steps. Unique general facts taught inside a
question are retained once as declarative theory, with their source conditions;
question-specific calculations are omitted. Biological/chemical process steps
remain eligible theory. Lecturer biographies and administrative material are
also excluded. These omissions are intentional and do not trigger coverage
repairs. The final check blocks explicit Q&A even if the model overlooks it.
The saved suffix is ` - Concise Notes.pdf`; previous output suffixes are replaced
instead of accumulating. Older approvals and cached Q&A notes are invalidated.
Revision maps are omitted so the same theory is not printed a second time.
Legacy map-only facts are recovered into the main theory before map removal.

Each review may request one coverage repair and up to two duplicate cleanups;
every change is checked against the source again. An unresolved **coverage**
finding stops the row for retry. Unresolved **duplicates** do not: the same
review that reports them found no coverage gap, so the notes are complete, and
a reviewer that keeps naming fresh duplicates would otherwise discard a
verified lecture. What is left is logged and recorded in the quality receipt
(`advisory_issues`). Exact copies are also removed locally when rendering.
This adds model calls and cost; a model review can still miss semantic errors.
The cache revision prevents reuse of notes generated before these checks.

The generator sizes its own chunks by bytes and cannot go below 1 MB, which
sent a 21-page lecture as four six-page calls — and one of those came back with
a fraction of its slides, so a whole topic (the autoimmune disorders) never
reached the notes. [`coverage.py`](coverage.py) drives generation a **page
group at a time** instead:

* `SHEETS_BRIDGE_PAGES_PER_CALL` pages per model call (default 4),
* source-verified runs use actual theory coverage instead of a page-to-bullet
  quota, so repeated questions do not force longer notes,
* a group the model returns only empty shells for (`complete theory notes
  nahi mile`) is halved, and a failed half halved again, down to single pages -
  dense maths slides do this to a 4-page group far more than to a 1-page one.
  Every part must answer; a partial recovery is refused,
* a group with no study content at all (title, "Thank you" slides) is skipped
  instead of failing the lecture,
* the parts are merged with the generator's own `_merge`, preserving lecture
  order and pooling the mind-map branches.

Groups are generated **`SHEETS_BRIDGE_GROUP_WORKERS` at a time** (3 by default) with their *starts*
still paced 3 s apart, so the gateway sees a trickle rather than a burst - a 44-page lecture was
twelve calls end to end, minutes of wall clock for one row. Each group keeps its own error, so an
edge group is judged by its own verdict and never by a neighbour's failure. Set the count to 1 to
restore the strictly sequential behaviour.

Calls are paced 3 s apart with up to 3 attempts each: fired back-to-back the
gateway dropped whole groups, while the same pages succeeded on their own.
A group that still fails is skipped **only at the edges** of the lecture (title
/ "Thank you" slides); a failing middle group fails the row, because notes that
silently drop a topic look complete. Overlapping groups repeat a topic, so
sections whose headings match (English gloss included — bilingual headings
often match only there) are folded together, keeping every point. A
presentation-only revision instruction keeps a Hindi lecture's headings in
Hindi.

Cost scales with the number of calls: ~4 pages per call is roughly 6 calls
(₹30–35) for a 21-page lecture, against one call (₹7) that carried 3 of its 20
topics. The generated notes are cached, so re-rendering or re-uploading later
costs nothing.

### PW branding on the PDF

The renderer tiles the **teacher's name** across every page and ships a PW logo
watermark that is switched off — and drawn at 5.5% opacity when switched on,
which is invisible. [`pdf_branding.py`](pdf_branding.py) replaces that at
runtime: the **PW logo watermark** sits behind the content at a visible
opacity, and a small **PW logo** is stamped in the top-right corner of every
page. The teacher's signature still appears in the header and footer.

| Variable | Default | Meaning |
|---|---|---|
| `SHEETS_BRIDGE_WATERMARK_OPACITY` | `0.10` | how strong the PW watermark is (capped at 0.5) |
| `SHEETS_BRIDGE_NAME_WATERMARK` | *(off)* | `1` brings back the tiled teacher name as well |

### Hindi

`notesbot.notes_pdf` is Latin-only: its sanitiser drops every character at or
above U+0250, so Hindi lectures rendered with empty bullets. The repo already
ships the Kalam Devanagari handwriting face in `fonts/`, unused.
[`pdf_fonts.py`](pdf_fonts.py) wires it in **at runtime** — Kalam registered as
the fallback face, the sanitiser widened to keep Indic text, and Devanagari
words drawn through a shaping-capable call (`FPDF.text()` applies neither
fallback nor shaping, which is why matras landed in the wrong place). Hindi
notes now stay in Hindi, in the same hand-drawn style; Latin words keep the
original path, so nothing else about the PDF changes.

Shaping has to be switched on for **both** painters. The body flows through
`_draw_word`, but the title chip, the running header on every page and the
mind-map nodes are drawn with `cell()`/`multi_cell()`, which apply the fallback
face — so the glyphs appear — yet shape only when text shaping is enabled, and
it is off by default. Unshaped Devanagari keeps codepoint order, so conjuncts
never form and every virama prints as a naked halant: the title read
`स् वा स् थ् य` while the body drew `स्वास्थ्य` correctly — the same word in two
formats on one page. `pdf_fonts.py` now enables shaping per call and only for
strings containing Indic text, so Latin keeps its exact old metrics. Measuring
calls (`get_string_width`, dry-run `multi_cell`) are wrapped too: unshaped
fpdf2 sums per-character advances and over-measures a conjunct cluster, and
measuring differently from drawing is what makes text drift or wrap early.

It only **imports** from `notesbot` (the generator package is not modified) and
calls the library directly — no HTTP app, sessions, CSRF or Blob storage:

```python
with cost_meter.metered_job() as meter:
    notes = generate_short_notes(pdf_bytes, "", language, subject_hint)
    out   = build_notes_pdf(notes, signature)
cost = meter.snapshot()["inr"]
```

## Catalog sheets: lecture PDF links filed in a Drive tree

A second sheet shape is supported: the content team's catalog, one row per
lecture with a direct PDF link and no status columns.

| exam | batch | subject | chapter | Lecture_Date | lecture_topic | downloadable_link |
|---|---|---|---|---|---|---|

It is recognised from its header (no `Class Notes Status` / `Final File
Status` columns, but a link, a lecture topic, batch, subject and chapter).
Every row with a link is ready; there is no trigger column.

**Where the notes go.** A catalog profile names an **output Drive folder**
(`outputFolder` in `sheets.json`, the *Notes Drive folder* field of the
Studio's **+ Add sheet**, or `--output-folder LINK` on the command line). A
catalog tab without one is refused before anything is written
(`output_folder_missing`). Each lecture is filed as

```
<exam category>/<batch>/<subject>/<chapter>/<lecture title> Concise Notes.pdf
```

* **Exam** - NEET, JEE (IIT-JEE and JEE are one exam), Foundation, each its
  own folder. An existing folder spelled `IIT-JEE` is used as the JEE folder.
  An exam that is not known gets a folder with the exam's own name. The exam
  also picks the format rules (`notesbot/format_rules`) per row: NEET and JEE
  rows both follow NEET-JEE; a blank exam uses the run's category.
* **Batch, chapter** - folders named exactly as the cells (whitespace
  tidied). **Subject** - the subject's standard name (`mapping.subject_name`):
  no teacher, no AIR tag, one spelling ("Maths", "Physics", "Organic
  Chemistry", "Zoology"), so one subject is one folder. Existing folders are
  matched case-insensitively and reused; missing ones are created only after
  the notes are ready, so a failed row leaves no empty folders.
* **File name** - the topic before its first `||`, without `NO DPP`,
  `Recorded`, `Rescheduled at ...` noise, plus ` Concise Notes.pdf`. A
  different lecture with the same name in the same folder gets ` (2)`.

**Written back to the sheet** (four columns after the catalog's own):
`Auto Status` (`claimed ...`, `review`, `failed: <code>`, `skipped:
not_a_lecture`), `Review Status` (the Review/Done/Regenerate dropdown), **`Concise
Notes Link`** - the Drive link of the filed PDF - and **`Lecture Folder
Link`** - the Drive link of the chapter folder it was filed in. A tab whose
column after `Concise Notes Link` already holds a team's own column (e.g.
`Uploading status (by BM Team)`) keeps it; the folder link then goes to the
first free column after the headers. A header of ours that a team renamed
(`Review Status (by ARD Team)`) is still read and written as ours and never
renamed back; only blank header cells are ever written. Upload-tracking columns
("Uploading status", "Upload status") are never read as the batch sheets' BM
trigger.

**Page 1 and language.** Lecture number = the number right before the colon
in the topic (`Capacitance 3: ...` -> Lecture - 03). Subject by its standard
name (no teacher, no AIR tag); chapter without `Ch-09 :`. The signature is the
profile signature when one is set (used as is), else the `faculty_name` column
(`faculty_id` and `faculty_email` are never read as a name), else the teacher
named in the subject cell (`Physics By Saleem Ahmad Sir`). The teacher printed
on the slides (`By - Venky Anna`, an intro slide, a footer or photo caption,
read by the notes model) then wins when it is a different person - the slides
show who took the class; the same teacher spelt differently (`ROHIT AGRAWAL`,
`रोहित अग्रवाल सर`) keeps the sheet's spelling. No name anywhere: `PW
Faculty`. The row log says which was used (`signature: ... - why`). Columns
are found by header, so the faculty columns inserted after `subject` (2026-10-01)
shift nothing that matters. Hindi batches get Hindi notes. Batches
taught in a script the PDF font cannot draw (Gujarati, Telugu, Tinglish,
Tamil, Kannada, Bangla ...) get English notes, and page 1 uses the English
chapter name from the cell's brackets.

**Paying once.** Filed PDFs carry Drive `properties`: `shortnotesSource` (a
digest of the PDF link) and `shortnotesRender` (a digest of everything that
shapes the rendered PDF). So:

* a row whose notes are already in its chapter folder is marked for review at
  no cost, even if a reviewer renamed the file; a same-named file without the
  stamp (made by hand) is adopted too;
* a PDF listed again under another batch is **copied** from the first copy -
  in the same run, or found by `shortnotesRender` from an earlier run;
* a PDF that failed earlier in the run is not paid for again on its repeats.

A PDF listed under two different teachers or two categories is generated for
each, because the signature or format rules differ.

**Not lectures.** Rows whose batch contains `Pi Lens`, `UXCC`, `VP LIVE` or
`Smart DPP` are marked `skipped: not_a_lecture` and never generated.
`skipBatches` in the profile replaces that list (`[]` skips nothing).

**Links.** `https://` PDF links (static.pw.live) are downloaded directly: 100
MB cap, PDF signature check, four attempts on a dropped connection or 5xx.
Drive file and folder links in a catalog also work (a folder is resolved with
the topic's lecture number).

```powershell
python -m sheets_bridge --profile "Class Notes Catalog (Concise Notes)" --dry-run   # the planned path of every row
python -m sheets_bridge --spreadsheet-id LINK --gid 0 --output-folder FOLDER_LINK --limit 3
```

## Model access: the PW shared proxy (pw-app-kit v2)

This app holds **no provider API keys**. Every model call leaves through
`https://pw-apps-proxy.vercel.app` with the signed-in `@pw.live` user's Google
token:

| Piece | Where |
|---|---|
| Kit client, `APP_NAME = "Handwritten Notes Automation"` | [`pw_access.py`](../pw_access.py) (repo root) |
| Allowlist gate (fails closed) + identity | [`pw_gate.py`](pw_gate.py) |
| OpenAI-shape → Gemini `generateContent` adapter | [`pw_transport.py`](pw_transport.py) |
| GPT adapter (`/api/gpt/generate`, strict JSON schema) | [`pw_openai.py`](pw_openai.py) |
| One `task_id` per lecture row | [`generator.py`](generator.py) + [`runner.py`](runner.py) |

The generator speaks to its gateway with an OpenAI-compatible client;
`pw_transport.install()` swaps that client for one that calls
`pw_access.gemini_generate`, so `notesbot` needs no edits. Requests carry
`filename`, `input_unit="No. of pages"`, `count=<pages>` and the row's
`task_id`; the proxy rolls every call of a task up into one row per
(task, app, user, file, model) on the `Usage Cost 2.0` tab. `count` is the
lecture's page count on every call of that lecture, `input_unit` is always
`No. of pages`.

**The allowlist is checked before every paid row** and fails closed — a denial
*or* an undecidable answer (proxy unreachable, bad token) stops that row with
`not_authorized` / `allowlist_undecided`, before any claim, download or
generation.

### Switching to GPT

`PW_PROXY_MODEL` decides the provider. A `gemini-` id is translated into a
`generateContent` body; a `gpt-` id is sent to `/api/gpt/generate` almost
unchanged, because the generator already speaks OpenAI chat-completions.
[`pw_openai.py`](pw_openai.py) only has to rewrite the JSON schema for OpenAI
strict mode (every property required, no extras, `maxItems: 0` fields dropped),
rename `max_tokens` to `max_completion_tokens`, and carry the same ledger
metadata. No OpenAI key is involved: the signed-in Google token authorises it,
exactly as for Gemini.

```powershell
$env:PW_PROXY_MODEL="gpt-5.6-terra-ard-content"; python -m sheets_bridge --test-model
```

**Kit support.** The 2026-09-16 pw-app-kit update ships `gpt_generate` (plus
`gpt_tts`/`gpt_image`) and documents the live ids; [`pw_openai.py`](pw_openai.py)
calls that helper and keeps a direct post only for an older kit copy. The root
`pw_access.py` is that kit client with `APP_NAME` set and one local fix kept:
one proxy session pass per signed-in Google identity, with `reset_session()`
on sign-out, so a second account never inherits the first one's allowlist
verdict. Re-apply that block if the kit is ever copied over again.

If the proxy has not enabled GPT for this app, that call fails with a message
saying so (HTTP 401/403), and `/api/openai/generate` is a 404 — the live route
is `/api/gpt/generate`. Nothing about the Gemini path changes meanwhile, so
switching back is one line of `.env`. Prompts, coverage thresholds and the
Hindi language gate were all tuned on Gemini, so treat the first GPT run as a
measurement, not a migration.

Client-side combined logging is not used: no `log:false`, no
`UsageSession.flush()`, no `/api/usage-log`.

```powershell
python verify_onboarding.py .env          # kit self-check
python -m sheets_bridge --test-model      # one live proxy call (creates a ledger row)
```

**Diagram images are off under the proxy.** The kit's `gemini_image` helper
takes a text prompt only, and this app's diagrams are redrawn *from the source
page crop* (image input), which that endpoint cannot express. Notes are
generated text-only until the proxy exposes an image-input route.

### Hindi lectures come out in Hindi - measured, not just requested

Two things decide the language of a lecture's notes:

1. **The sheet's Batch column** (`… Hindi …` → hindi). When it is blank or
   unrecognised the row used to run as `auto`, and `auto` drifted into English
   on a Hindi lecture - a matrices lecture came back with 12 of its 16 headings
   in English. Now, for an `auto` row, **the source PDF itself is read**: if
   its wording is substantially Devanagari the lecture is generated as Hindi.
   The sheet is a hint; the lecture is the authority.
2. **The generated answer is measured.** `coverage.hindi_share()` counts Hindi
   words against English words (Latin runs of three or more letters, so `A`,
   `kA`, `i`, `j` and formulas are symbols, not English) across every field -
   headings, points, definitions, notes, tricks, tables and the mind map. A
   group under `MIN_HINDI_SHARE` (**90%**) is asked again - up to two more
   times - with a blunt Hindi-only instruction, and the most-Hindi answer is
   kept. The gate only reacts when the answer actually contains English words
   (four or more letters), so `A`, `kA`, `sin`, `aij` and formulas never
   trigger it. The prompt itself demands at least 90% Devanagari in every
   field, definitions and mind map included.

   The re-asks do not always reach 90%, and generation keeps its best answer
   when they do not (79% and 81% are normal for biology and chemistry, whose
   Hindi notes carry English exam terms). The finished notes are therefore
   judged against a separate floor, `MIN_HINDI_SHARE_FINAL` (**70%**), in
   [`final_quality.local_findings`](../notesbot/final_quality.py). Setting that
   gate to the 90% target instead rejected finished lectures that generation
   had already accepted — after every call had been paid for. Below the floor
   the notes really are half English, and the row still fails.
3. **The page furniture follows.** "KEY TERMS", "Where,", "Page N", the
   mind-map title and the diagram placeholder are the renderer's own fixed
   labels; in a Hindi document they are drawn as मुख्य शब्द, जहाँ,, पृष्ठ N,
   माइंड मैप — त्वरित पुनरावृत्ति and चित्र — यहाँ बनाएँ. Widths are measured
   through the same swap, so nothing shifts.

**Reuse of cached notes is off** (`SHEETS_BRIDGE_CACHE=off` in `.env`): notes
generated under an older prompt must never be re-uploaded as if new.

The log shows it working: `pages 9-12: only 12% Hindi - asking again in
Hindi` then `pages 9-12: now 96% Hindi`.

### Live progress

A lecture takes minutes, so the run reports as it goes rather than only at the
end. Every row line carries its place in the batch and each finished row adds a
running tally:

```
[2/5 row 3] generating (2,116,373 bytes source) ...
[2/5 row 3] done: https://drive.google.com/file/d/…/view  cost=INR 14.34
progress: 2 done, 0 failed, 3 left (INR 23.16 so far)
```

The Studio shows the same thing next to the button
(`Lecture 2/5 · 2 done · 0 failed · ₹23.16`), updated every two seconds.

### Generation payload per page group

`VISUAL_SOURCE_CHUNK_MAX_BYTES` is a **safety ceiling on one call's payload**,
not the completeness knob — `coverage.py` is what decides how much of a lecture
each call sees (`SHEETS_BRIDGE_PAGES_PER_CALL`). Set too low it does real
damage: at 1 MB a 4-page group was silently re-split into several *paid* calls,
and any lecture holding a single page bigger than 1 MB failed outright with
`generate:source_chunk_too_large` — a scanned slide is routinely 2–3 MB. It is
now 9 MB, the code's own maximum.

## The button

```powershell
python -m sheets_bridge --serve          # or double-click "Start Sheet Automation.bat"
```

opens the **Short Notes Studio app itself at http://localhost:3000** (unchanged
UI, same password login) with one extra button under the header:
**Open automation**. The button opens the Automation tab first; choose the sheet,
model and run options there, then click **Run automation now**. **Include page
numbers** is checked by default; uncheck it to omit page numbers from every PDF
in that run. Merely opening the tab never starts a batch. The whole batch runs on this machine in
the background; the line next to the button shows progress
(`3 done · 1 failed`) and, when finished, the totals and cost. Clicking that
status line opens **/automation**, an optional details page with the live log,
per-row outcomes and *Preview only (dry run)* / *Stop* controls.

The button appears only when the Studio is served by this local command — the
deployed Vercel site never shows it (its `/api/automation` endpoint does not
exist there). Reason: the job needs *your* Google login token, the generator
credentials from `.env`, and can run for hours — none of which a serverless
function can hold.

`--serve-page` serves just the standalone automation page (no Studio login);
`--serve` requires the Studio password (`APP_PASSWORD` from `.env`, or
`localdev` when unset) before the button/API can be used.

### Which Google account it runs as

The automation spends money using **one person's** Google access — their token
is what opens the lecture PDFs and uploads the notes back. That account is the
one whose token is cached on this machine: restored silently at start-up, the
same way the CLI has always worked. The browser opens **once**, on a machine
with no usable token, and never again.

There is deliberately **no per-person sign-in gate in the page**. What decides
who may use the tool is the allowlist PW already maintains: the account must be
`@pw.live` **and** listed under this app's column in the `Whitelisted` sheet
([`pw_gate.py`](pw_gate.py)), checked before the account is adopted, again
before every paid row, and **before the token is ever written to disk** — so an
account that is not allowed leaves nothing behind. A denial reads
`not_authorized`; a personal account `not_pw_account`.

Two consequences worth stating plainly:

* anyone who can reach this loopback-only server and knows the Studio password
  acts as that cached account. That is the accepted trade-off for a
  single-machine local tool; the allowlist, not the browser session, is the
  access control;
* Google's own consent screen is **not** the gate. Setting the OAuth consent
  screen to *Internal* restricts by Google Cloud **organisation membership**,
  which has nothing to do with the `Whitelisted` sheet — an Internal app in the
  wrong organisation blocks everyone with `Error 403: org_internal`. Use
  *External* unless the project genuinely lives in the pw.live organisation.

Tokens are filed per account under `~/.shortnotes_bridge/tokens/`, and a token
issued by a **different OAuth client** is detected and ignored, so swapping
clients asks for one fresh sign-in instead of failing with Google's opaque
`invalid_client`. `--check-setup` reports how many cached tokens belong to the
client currently configured.

### Costing: one row per run

Nothing about money is added to the *lecture* sheets. Each finished run appends
exactly **one** row to a `Handwritten Notes Runs` tab in the PW platform
workbook (*App Allowed Emails*), created on first use:

```
Run ID | Finished At | Operator | Sheets | Lectures Done | Failed | Total Cost (INR) | Status
```

One press of the button = one row, however many lectures or sheets it covered:
the combined cost and the number of lectures for that run. `Status` is
`completed`, `stopped` (you pressed Stop) or `stopped: cost cap` — a run cut
short still records what it already paid for, and cannot be misread as a full
one. Nothing is written for a dry run, or for a run that found everything
already generated. A failure to write the row is logged and never fails the
run, which by then has already generated and uploaded the notes.

The figures are the gateway's own: `generator._cost()` prefers the
proxy-reported cost and only falls back to a local token estimate. Money spent
before a lecture failed is carried on the exception as `partial_cost_inr` and
counted, so a failed row never hides spend.

`SHEETS_BRIDGE_LOG_SHEET` picks a different workbook (or `off` to disable),
`SHEETS_BRIDGE_LOG_TAB` a different tab. `Usage Cost 2.0` in that workbook is
machine-synced by another system and is **never** written to.

### What the shared ledger records

Those raw rows carry this app's own metadata, and the unit is the **sheet row**,
not the page:

| Input Unit | Count |
|---|---|
| `No. of rows` | how many lecture rows that run processed |

The proxy stamps it on **every** line of the run, so it must be read **once per
Task ID** and never summed - one lecture is 5-8 model calls, and each repeats
the same count. Since every call of a run shares one `task_id`, one run = one
Task ID = one row count. The manual upload flow has no sheet rows, so it still
reports `No. of pages`.

**The proxy's own per-call rows cannot be suppressed from here.** In kit v2 the
proxy writes one raw usage row per provider call, server-side —
`UsageSession.flush()` is a documented no-op. What this app controls is the
`task_id`: every call of one run shares **one** id, so those raw rows group
into a single task per run, which is how the kit says to combine them.

### Use a pw.live project, not a personal one

The consent screen is the app's own OAuth client. If that client lives in a
**personal** Google Cloud project, Google shows *"This app hasn't been verified"*
(the app asks for `auth/drive`, a **restricted** scope, and `auth/spreadsheets`,
a **sensitive** one), and — while the consent screen is in *Testing* — refresh
tokens die after **7 days**, so everyone is silently signed out every week.

Creating the client inside a Google Cloud project owned by the **pw.live**
organisation and setting the OAuth consent screen to **Internal** removes both:
Internal apps skip verification entirely, tokens stop expiring on a 7-day clock,
and only `@pw.live` accounts can consent — Google itself enforcing the rule the
allowlist gate already applies.

Nothing in the code changes: point `SHEETS_BRIDGE_CLIENT_SECRET` at the new JSON
(or replace `client_secret.json`). Tokens issued by the *old* client are
detected and ignored, so each operator simply signs in once more instead of
hitting Google's opaque `invalid_client`; `--check-setup` reports how many
cached tokens belong to the client currently configured.

1. Google Cloud Console → **OAuth client ID → Desktop app**; enable the
   **Google Drive API** and **Google Sheets API**; download the client JSON as
   `client_secret.json` in the repo root (or set `SHEETS_BRIDGE_CLIENT_SECRET`).
   Installed-app flow, *not* a service account — the folders are shared with
   the human account that will click the button.
2. No provider keys: model access comes from the PW proxy. Optionally set
   `PW_PROXY_MODEL` in `.env` to pick the proxy-side Gemini model.
3. First click opens a Google sign-in tab once; the token is cached at
   `~/.shortnotes_bridge/token.json` and refreshed silently afterwards. The
   account must be `@pw.live` and listed under the app's column in the
   `Whitelisted` sheet.

Scopes requested are `.../auth/drive`, `.../auth/spreadsheets`, plus `openid`
and `.../auth/userinfo.email` so the proxy can verify the PW user
(`drive.file` would 404 on every existing lecture PDF).

## Environment defaults

| Variable | Default | Meaning |
|---|---|---|
| `SHEETS_BRIDGE_SPREADSHEET_ID` | *(none)* | target spreadsheet when no `sheets.json` exists; there is deliberately no built-in default |
| `SHEETS_BRIDGE_GID` | `0` | tab **gid** (never resolved by name) |
| `SHEETS_BRIDGE_SIGNATURE` | *(empty → column E)* | overrides Lecture Faculty for every row |
| `SHEETS_BRIDGE_MAX_COST_INR` | *(none)* | stop once cumulative spend passes this |
| `PW_PROXY_MODEL` | `gemini-3.7-flash` | selected after Hindi/English source-coverage comparison; plain id, no `vertex_ai/`. A `gpt-` id (e.g. `gpt-5.6-terra-ard-content`) switches the whole run to the proxy's GPT route |
| `PW_PROXY_GPT_PATH` | `/api/gpt/generate` | proxy route used for `gpt-` models (`/api/openai/generate` does not exist) |
| `PW_PROXY_REASONING_EFFORT` | *(model default)* | `none`/`low`/`medium`/`high` for `gpt-` models; each lecture makes dozens of calls, so `high` is expensive |
| `SHEETS_BRIDGE_CACHE` | `~/.shortnotes_bridge/pending` | where generated notes are kept for a free retry; `off` disables reuse so every lecture is generated again |
| `SHEETS_BRIDGE_GROUP_WORKERS` | `3` | page groups generated at once (1 = the old strictly sequential behaviour, max 6) |
| `SHEETS_BRIDGE_PAGES_PER_CALL` | `4` | source pages per model call — lower = more complete, more cost |
| `SHEETS_BRIDGE_PROFILES` | `sheets.json` | named sheets; overrides the two variables above |
| `SHEETS_BRIDGE_CLIENT_SECRET` | `client_secret.json` | OAuth desktop client JSON |
| `SHEETS_BRIDGE_TOKEN` | `~/.shortnotes_bridge/token.json` | cached OAuth token |

## Command line (same engine, no page)

```powershell
python -m sheets_bridge --list-tabs LINK     # classify every tab of a workbook: which can run, rows ready
python -m sheets_bridge --list-profiles      # print the configured sheets and exit
python -m sheets_bridge --profile new-batch  # run one named sheet (repeatable)
python -m sheets_bridge --all-profiles       # run every enabled sheet, one after another
python -m sheets_bridge --dry-run            # resolve + print; no generation, upload or writes
python -m sheets_bridge --limit 1            # first eligible row, live
python -m sheets_bridge --row 7              # only Sheets-UI row 7
python -m sheets_bridge --redo --row 7       # even if M is already Done
python -m sheets_bridge --subject auto       # let the model decide (default: map column D)
python -m sheets_bridge --max-cost 200       # stop after cumulative spend passes ₹200
python -m sheets_bridge --output-folder LINK # catalog sheets: the Drive folder notes are filed in
```

## What happens per row

1. **Trigger** — J = Done and M ≠ Done. Rows failed last time are retried;
   a row `claimed` by another run less than 3 h ago is skipped - the stamp
   after the word is what decides (a stale claim from a crashed run is picked
   up again). Before every write the row's Drive Link / Lecture Number /
   Chapter are re-read: if someone inserted, deleted or sorted rows meanwhile
   the write is refused (`row moved`) instead of landing on another lecture.
2. **Locate the PDF** — the Drive Link may be a **folder** link or a **file**
   link (plain URL, rich link or Drive smart chip). For a folder the children
   are listed and the source PDF is chosen from two signals:
   - **Chapter (column G)** — production files are named
     `<Chapter> <NN>  Class Notes.pdf`, and one folder holds several chapters,
     so three files can carry `06`. Column G (usually `English
हिन्दी`) is split
     into variants and matched against the file name.
   - **Lecture number (column H)** — `05`, `L05 …`, `Lecture-5 …`, `व्याख्यान ०५ …`.

   PPTs, Google-native files and our own `- Short Notes.pdf` and `- Concise Notes.pdf` outputs are ignored;
   between `… Class Notes.pdf` and `… DPP.pdf`, "notes" wins. **Anything
   unresolved fails the row** — `lecture_ambiguous` (several files still match),
   `chapter_not_found` (that lecture number exists, but only under another
   chapter), `lecture_not_found`, `lecture_number_missing`, `no_pdf_in_folder`.
   It never guesses.
3. **Duplicate check** — if `<source name> - Concise Notes.pdf` already exists in
   the folder, the row is marked `done` / **Review** and nothing is generated
   (also under *redo*; rename the old PDF in Drive to force a regeneration).
4. **Claim** — Q = `claimed` is written **before** any expensive work.
5. **Generate** — subject from D (`"chem"` before `"physic"`, so *Physical
   Chemistry* → chemistry; blank → auto), language from C (`… Hindi …` → hindi),
   signature from E (`by …`; a blank E1 header right after Subject is still
   read as the faculty column). The teacher printed on the slides replaces it
   when the slides name a different person; a row with no faculty is signed
   with the slides' teacher, else `PW Faculty`. `SHEETS_BRIDGE_SIGNATURE` /
   the Studio's signature is used as is.
6. **Upload** — the studygram PDF is uploaded into the same folder,
   `supportsAllDrives` on every call.
   A transfer killed by a dropped connection (Windows `ConnectionAbortedError`
   / WinError 10053, 429s, 5xx) is repeated up to 4 times; before each repeat
   the folder is re-checked, so a partially-succeeded upload is adopted instead
   of creating a duplicate. Files under 5 MB go in one request rather than a
   resumable session.

   The generated **notes** are cached under `~/.shortnotes_bridge/pending`
   until the upload is confirmed, so a retry after an upload failure re-renders
   the Doc from them instead of paying to generate them again
   (`SHEETS_BRIDGE_CACHE` overrides the location).
7. **Write back** — one batched update: `Q` = `done`, `R` = the Doc link,
   **plus M = Done and P = `YYYY-MM-DD HH:MM:SS`**. On any failure `Q` becomes
   `failed: <code>` and **M/P are never touched**. The proxy-reported cost is
   still tracked in the run summary, the log and the usage ledger — it is no
   longer written to the sheet.

Rows run **strictly sequentially**; one bad row never stops the batch; the run
ends with a summary (counts per outcome, failure codes, total ₹).

## Error codes (column T)

`blank_link`, `invalid_link`, `not_a_folder`, `no_pdf_in_folder`,
`lecture_number_missing`, `lecture_not_found`, `lecture_ambiguous`,
`not_authorized`, `allowlist_undecided`, `column_layout_mismatch`, `computed_tab`,
`computed_status_column`, `control_column_conflict`, `no_sheet`, `no_runnable_tabs`,
`chapter_not_found`, `drive_not_found`,
`drive_forbidden`, `no_parent_folder`, `not_pdf`,
`too_large`, `download:*`, `generate:<ExceptionClass|VisualFidelityError code>`,
`render:*`, `upload:*`, `sheet_write_failed[:claim|:done]`, `interrupted`.
Catalog tabs add `output_folder_missing`, `output_folder_unavailable`,
`output_folder_not_a_folder`, `download:http_<status>`, `download:unreachable`,
`copy:*`, `create_folder:*`, `name_collision` and the `skipped: not_a_lecture` label.
Console/UI logs carry only the exception class and `file:line` — never message
text or document content (these are real student materials).

## Tests

```powershell
python -m pytest tests/test_sheets_bridge.py -q     # 130 tests, no network needed
```
