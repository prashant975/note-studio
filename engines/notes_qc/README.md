# UPSC PDF QC

Check already-generated UPSC notes files and get back one Excel workbook saying
what is wrong with them.

Upload one file or twenty — PDF, Word, PowerPoint, Excel, images or plain text —
press **RUN QC**, download the report. No note generation, no transcripts, no
video, no settings.

**It never modifies your PDF.** The file is copied into a working folder, opened
read-only, and the copy is deleted at the end of the run.

---

## What it is

A quality-control tool for finished notes. It is handed one file and nothing
else — no slides, no lecture recording, no source document — so it reads the
notes back out of the page and judges them on their own internal correctness and
consistency.

Ten checks run per file. Seven need no network at all. Three call a language
model, and a fourth calls one only when the PDF's own text layer turns out to be
unreadable.

The output is one XLSX: a summary sheet, the **Error Report**, and a per-file
sheet for each file in the batch. There is no format/visual sheet — this app
reports what is wrong in the NOTES, not how the page was drawn.

---

## Install (Windows / PowerShell)

```powershell
cd C:\Users\Laptop\Downloads\upsc_file_QC

python -m venv .venv
.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell refuses to run the activate script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
```

> **Streamlit must be 1.58 or newer.** Sign-in depends on the `expose_tokens`
> setting, which older versions ignore completely — the app then sends an empty
> token and every sign-in fails the access check. `requirements.txt` pins this;
> just don't install Streamlit yourself at an older version.

---

## Sign-in setup (one time)

The app ships no AI provider key. Paid model calls are made by the PW proxy,
which holds the provider key on its side and authorises the Google account you
signed in with. So the app needs exactly one credential file of its own: the
Google OAuth client that identifies you.

### 1. Create the Google OAuth client

In Google Cloud Console → **APIs & Services** → **Credentials**:

1. **Create Credentials → OAuth client ID**, Application type **Web
   application**.
2. Under **Authorised redirect URIs**, add exactly:
   ```
   http://localhost:8501/oauth2callback
   ```
   The port must match the port you run on. If you run
   `streamlit run app.py --server.port 8502`, register that port instead —
   Google rejects a callback URI it does not have on file.
3. Keep the generated **client ID** and **client secret** for the next step.

On the consent screen you only need `openid`, `email` and `profile`. The app
reads your email address and nothing else, so no Google API needs enabling and
the client needs no verification review.

### 2. Fill in `.streamlit/secrets.toml`

```powershell
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
notepad .streamlit\secrets.toml
```

Set `client_id`, `client_secret`, and a long random `cookie_secret`:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Leave `expose_tokens = ["access", "id"]` exactly as it is. The proxy verifies
the Google **id token**, and without that line the app has nothing to send.

> `secrets.toml` is gitignored and holds credentials — never commit it.
>
> Streamlit also reads a user-level `%USERPROFILE%\.streamlit\secrets.toml` when
> a project has none of its own. If sign-in fails complaining about a provider
> or a missing key you believe you set, check that file is not shadowing this
> project's — the project's own copy takes precedence once it exists.

### 3. Proxy access

The proxy decides who may make paid calls. It fails closed: an account it does
not recognise gets the seven offline checks and "Check not run" everywhere else,
rather than silently running up cost.

This app is onboarded to the **PW shared proxy** using PW App Kit v2:

| Thing | Value |
| --- | --- |
| `APP_NAME` | `upsc_file_QC` (in `pw_access.py`) |
| Proxy base URL | `https://pw-apps-proxy.vercel.app` |
| Allowlist tab | `Whitelisted` — column header must match `APP_NAME` exactly |
| Raw export tab | `Raw Usage Ledger Export` |
| Allowed domain | `pw.live` |

To be let in, your `@pw.live` email must appear under the **`upsc_file_QC`**
column of the `Whitelisted` sheet. Ask an admin to add it if it is not there.

Confirm the app is still registered at any time:

```
https://pw-apps-proxy.vercel.app/api/apps
```

Do not change the proxy URL and do not add a provider key to this app — the
proxy holds the keys and makes the paid calls. To re-check the wiring:

```powershell
python verify_onboarding.py .env
```

Every provider call goes through `pw_access` helpers only, and one `task_id` is
created per batch (`src/pipeline.py`) and passed to every call in it, so the
proxy's raw usage rows for a run can be grouped by
`Task ID + App Name + Email + Model`.

### 4. `.env` (optional)

Every value has a working default, so an empty `.env` is valid.

```powershell
Copy-Item .env.example .env
```

**No API key goes in here** — nothing reads one. See `.env.example`, which
documents every variable inline. The ones worth knowing:

| Variable | Default | What it does |
| --- | --- | --- |
| `MODEL_NAME` | `gemini-3.5-flash` | the model the proxy calls |
| `FALLBACK_MODEL_NAME` | `gemini-3.6-flash` | used once if the first is unavailable; empty disables it |
| `PW_PROXY_BASE_URL` | built-in | the proxy's base URL |
| `RUNS_DIR` / `OUTPUT_DIR` | `runs` / `output` | where working files and reports go |
| `APP_NAME` | `UPSC PDF QC` | name shown in the tab and page header |
| `MAX_PARALLEL_AI_CALLS` | `4` | AI calls in flight across the whole run |
| `MAX_PARALLEL_PDFS` | `3` | PDFs checked at once — **see the caveat below** |
| `MAX_VISUAL_CHECK_PAGES` | `0` | **0 = every page.** Pages sent to the AI visual check. Set a positive number to sample instead; the run then reports how many of how many it saw |
| `VISUAL_BATCH_PAGES` | `6` | pages per visual-check call (full coverage has to travel in batches) |
| `VISUAL_MAX_OUTPUT_TOKENS` | `4000` | output ceiling per visual-check call |
| `LAYOUT_RENDER_PAGES` | `12` | pages the layout check saves as PNGs. The overflow scan itself always covers **every** page |
| `MIN_NOTES_WORDS` | `400` | below this the notes are reported as too short |
| `ERROR_REPORT_MAX_OUTPUT_TOKENS` | `32000` | output ceiling per error-report call. A reply that hits it is detected, salvaged and reported — never silently dropped |
| `VERIFY_FINDINGS` | `1` | run the second-opinion pass that tries to refute each finding. `0` disables it (faster, cheaper, measurably noisier) |
| `VERIFY_BATCH_SIZE` | `8` | claims per verification call |
| `VERIFY_MAX_OUTPUT_TOKENS` | `8000` | output ceiling per verification call |

---

## Run it

```powershell
.venv\Scripts\Activate.ps1
streamlit run app.py
```

Then in the browser:

1. Sign in with your Google account.
2. **Upload Files** — drag in one file, or select many at once (Ctrl+click or
   Ctrl+A in the file picker). There is no per-file step; every file you select
   is queued.
3. **RUN QC** — the page shows `Checked 3 / 12` and the file name as each one
   finishes.
4. **Download Error Report** when it finishes.

Each PDF is an independent job. They are never merged, and a file that fails
never stops the ones behind it — the workbook says which one failed and why,
beside the results for the files that succeeded.

### From the command line

Useful for large batches:

```powershell
python run_cli.py "C:\path\to\notes.pdf" --session
python run_cli.py "C:\path\to\folder" --session    # every PDF in the folder
python run_cli.py notes1.pdf notes2.pdf --no-ai    # free checks only
```

**`--session` is the normal way to sign in here.** The browser app swaps your
Google sign-in for a proxy-issued 7-day pass and stores it under
`%LOCALAPPDATA%\UPSC_Notes`; `--session` borrows that pass, so you sign in once
in the browser and can then run batches from the terminal for a week. Failing
that, pass a Google id token with `--token` or `UPSC_QC_GOOGLE_TOKEN`. With
neither, the AI stages report "unavailable" and the seven offline checks still
run.

---

## Input formats

Anything that is not already a PDF is converted to one before checking, so every
stage below sees the paginated document it was written against.

| Format | How it is read |
| --- | --- |
| `.pdf` | read directly |
| `.docx` `.doc` `.rtf` `.odt` | exported to PDF by the installed **Word** |
| `.pptx` `.ppt` `.odp` | exported to PDF by the installed **PowerPoint** |
| `.xlsx` `.xls` `.ods` | exported to PDF by the installed **Excel** |
| `.png` `.jpg` `.tif` `.bmp` `.gif` | wrapped as a one-page PDF by PyMuPDF |
| `.txt` `.svg` | laid out and wrapped as a PDF by PyMuPDF |

Office conversion drives the Office apps over COM, so **Microsoft Office must be
installed** for those four rows. Nothing extra is installed by this app, and no
Python package was added for it. A file whose format is not in the table is
reported as "not checked" with the reason, and the rest of the batch continues.

**The page-layout check is skipped for anything that did not arrive as a PDF.**
It measures the PW notes template — the 5.5 in content column and the header
logo — and a converted Word file or a photograph has no such geometry, so
running it would report the source format rather than a real defect. Those files
show `Not applicable` for that stage; every other check still runs in full.

The original file is never modified. It is copied in, converted in the copy's
folder, and read from there.

---

## What gets checked

| # | Stage | What it looks for | Where it lives |
| --- | --- | --- | --- |
| 0 | **Text recovery (AI)** | runs only when the text layer is scrambled: reads the pages back as text so the text-reading checks below work normally | `src/pdf/text_recovery.py` |
| 1 | File integrity | zero pages, every page blank, unreadable file | `src/qc/format_check.py` |
| 2 | Page layout | body text crossing the 5.5 in content column into the "Space for Notes" box; missing header logo | `src/pdf_layout_check.py` |
| 3 | Page analysis | text clipped by the page edge, tables past the column or off the page, blank and near-blank pages, missing page numbers, bad page breaks, figures below 96 DPI, mixed page sizes | `src/pdf/analyzer.py` |
| 4 | Deterministic | leaked internal markers, raw HTML/JSON/markdown, malformed tables, duplicate and empty headings, repeated paragraphs, placeholder text, broken bullet nesting, over-long sentences, mixed date formats, inconsistent proper nouns | `src/qc/deterministic_checks.py` |
| 5 | Structure | placeholder or prefix-less lecture title, unbreakable title runs, missing QUICK REVISION / Key Sources, heading ladder out of order, fragmentary bullets, mojibake, lost subscripts (H₂O written as H2O), notes too short, unqualified date-sensitive claims, over-wide table rows | `src/upsc_quality_checker.py` |
| 6 | Content completeness | missing "Topics Covered in the Class", notes too short, DTP notes pointing at slides that do not exist | `src/quality_checker.py` |
| 8 | **Review (AI)** | the full rule set in `config/upsc_qc_rules.yaml` — factual, conceptual, language, compliance and formatting rules | `src/qc/ai_review.py` |
| 9 | **Error Report (AI)** | wrong facts, wrong concepts, wrong laws/articles/schemes/bodies, outdated data, missing qualifiers, overgeneralised and unsupported claims, definition errors, unit/formula/symbol problems, diagram–table mismatches | `src/qc_prompts.py` + `src/error_report_writer.py` |
| 10 | **Verification (AI)** | every claimed error is put back to the model with instructions to *refute* it. Refuted claims are removed; unsettleable ones are kept at Low and labelled | `src/qc/verify.py` |

Severity is **High / Medium / Low**, coloured the same way in the sheet.

### One report, and everything goes in it

The only output is the Error Report. Geometry and rendering — page numbers,
table overflow, clipped text, blank pages, DPI, the AI visual render check —
are not reported at all: they are typesetting matters, and a reviewer hunting a
wrong fact should not have to wade through them.

Everything that IS about the notes lands in that one report, whichever stage
found it:

| Source | What it contributes |
| --- | --- |
| Error Report call (AI) | wrong facts, concepts, laws, figures, definitions |
| **Independent review (AI)** | the full framework rule set — factual, conceptual and language, each row tagged with the rule id that fired |
| Deterministic checks | proper-noun consistency (Factual #6), sentence clarity (English #3) |
| Structural checks | heading ladder out of order, missing QUICK REVISION / Key Sources, placeholder title, notes too short |

**The independent review used to be the biggest source of missed errors.** It
applies the whole framework and its findings went to the format/visual sheet —
so a reviewer reading the Error Report never saw them. They are report rows now,
and they are put through the verification pass like every other AI claim.

Strictness defaults to **strict**, so no error type is filtered out of the
report. At the old `standard` default the writer silently deleted Unsupported
claim, Source-year precision, Conceptual/formula precision, Terminology issue
and Language clarity rows unless they were High severity.

### Measuring accuracy

Accuracy was never measurable in this project: the only numbers anyone had came
from grading a run by hand. `tests/benchmark/` is the scoreboard.

```powershell
python testsenchmarkuild.py              # short document, single AI call
python testsenchmarkuild.py --long       # 53 pages, forces multi-chunk
python testsenchmark\score.py              # score it (uses the stored session)
python testsenchmark\score.py --long       # score the multi-chunk document
```

`ground_truth.py` holds 14 deliberate errors — five constitutional, three
historical, two geographical, two conceptual, two language — mixed in with
correct sentences. **Every seeded error is durable**: an article number, the
year of an enacted law, settled history. Not one depends on a figure that could
have been revised or a scheme that could have been renamed, so "the notes are
newer than my training data" is never available as an excuse. A miss is a real
miss.

The scorer buckets each reported row three ways. MATCHED names a seeded error.
FALSE fires on a sentence known to be correct, and is the only thing counted
against precision. UNSEEDED is a plausible finding the benchmark did not seed —
reported for a human to glance at, never scored, because guessing either way
would corrupt the number.

Measured on this benchmark (4 short runs, 4 long runs, `strictness=strict`):

| Case | Recall | Precision |
| --- | --- | --- |
| Seeded, 6 pages, single AI call | 100% | 100% |
| Seeded, 53 pages, 4 chunks | 100% | 100% |
| Clean control, 11 pages, no errors at all | — | 2 spurious rows |

The long case matters most: at 10,269 words the document is split into four
chunks, which is the path where the original losses happened. All four parts
returned complete, and errors seeded into the final chunk were still found.

**What this does not prove.** The benchmark is 14 errors of my construction, not
the 40 from your trial, and a checker can be tuned to a benchmark. Treat these
numbers as evidence the machinery no longer loses or invents findings — not as a
guarantee about a document nobody has tested.

### The rule set: the PW OnlyIAS Notes QC Framework

The framework is this app's policy, kept in three places that are checked
against each other by `tests/test_framework_alignment.py`:

| File | What it holds |
| --- | --- |
| [`reference_documents/notes_qc_framework.md`](reference_documents/notes_qc_framework.md) | the readable original — **edit this first** |
| `config/upsc_qc_rules.yaml` | the 13 checks, machine-readable, with rule ids |
| `config/upsc_qc_sources.yaml` | the per-subject standard books, official domains and the blocked-source list |

**Every AI call that produces the Error Report now carries both**: the checklist
of rule ids, and the approved sources *for that document's subject*. Subjects
resolve in English and Hindi (`भारतीय राजव्यवस्था` → Polity), so a Polity note is
checked against Laxmikanth, the Bare Acts and `legislative.gov.in`, and an
Ancient History note against R. S. Sharma — not against a generic "reliable
source". Where the subject cannot be identified, the prompt still names the
prescribed book per subject and still forbids the blocked sources.

Wikipedia, blogs, coaching-institute material, anonymous content and
AI-generated summaries are refused every time, per Factual check #2. If the only
support the model can find for a claim is one of those, it is told to flag the
claim as needing an authentic source rather than treat it as verified.

### Coverage: every page, every time

**The content check reads every page, and the visual check now looks at every
page too.** A QC tool that samples can only answer "were the pages I happened to
look at clean?", which is not the question it is asked.

| Check | Coverage |
| --- | --- |
| Page layout (overflow, logo) | every page |
| Page analysis | every page |
| Deterministic / structure / legacy | every page |
| Error Report (AI) | every page, in chunks that overlap so nothing falls between them |
| Visual (AI) | every page, in batches of `VISUAL_BATCH_PAGES` |

Full visual coverage costs real money — every page is rendered and sent to the
model — so `MAX_VISUAL_CHECK_PAGES` can still be set to a positive number to
sample. If you do, the workbook records how many pages of how many it examined,
so a sampled run can never be mistaken for a complete one.

### Why a finding might be missing, and how you would know

A report that quietly covers half a document is worse than one that fails
outright, so every way coverage can be lost is detected and stated:

| What happened | What the report does |
| --- | --- |
| The model's reply hit the output ceiling mid-list | Complete rows before the cut are salvaged and kept; the file is marked **PARTIAL SCAN** and the note says rows after the cut are missing |
| One chunk of a long document failed (timeout, 429) | The other chunks still contribute their rows; the file is marked **PARTIAL SCAN** and the note names the failed part |
| The reply could not be read at all | Reported as `parse_failed` — never as "no issues found" |
| Some parts were readable and some were not | The readable parts' findings are kept; the file is marked **PARTIAL SCAN** |
| A part answered without an error-report block | Counted as unchecked, not as clean |
| Pages could not be rendered (image mode) | Named in the note as never checked |
| Strictness removed every finding | Reported as `filtered_out`, with the count — never as "no issues found" |
| The verification pass rejected every finding | Reported as such, with a pointer to `verification.json` — not as "the AI found nothing" |
| The visual check could not read a batch's reply | Those pages are reported as unchecked, not clean |
| The visual check sampled the pages | The note records how many pages of how many it examined |

**A `PARTIAL SCAN` row's error counts are a floor, not a total.** The status is
coloured orange for exactly that reason: the numbers on it cannot be read as a
verdict on the file.

### Why a finding might be wrong, and what stops it

The model checking these notes has a training cutoff; the notes usually do not.
Left unguarded it reports correct, current facts as "outdated" — in one trial,
every finding on a clean document was of that kind. Two things now prevent it:

* The prompt is given **today's date** and told plainly that not recognising
  something is not evidence against it. Figures, scheme names, office-holders
  and counts it cannot confirm are to be left alone. Facts that do not expire —
  constitutional articles, enacted law, history, science, formulae — and any
  place the notes **contradict themselves** remain fully reportable.
* Every surviving claim is then put to the model a second time with the
  opposite instruction: **refute this**. Framing matters — asked to confirm, a
  model confirms. Refuted claims are dropped (and recorded in
  `verification.json`), and claims that cannot be settled either way are kept at
  Low severity and labelled rather than deleted.

Stages fail independently. A network blip in stage 7 does not cost you stages
1–6, and the workbook's "Notes" column names whatever did not complete.

### Hindi PDFs

A Devanagari font embedded as an `Identity-H` subset returns its glyphs, when
copied, in the order they are *drawn* rather than the order they were typed —
`जाति` comes back as `जातत`. The words on the page are correct; the copyable
layer behind them is not. Measured on a real 20-page Hindi notes PDF: **14% of
Devanagari words** come back malformed.

The app detects this (`src/pdf/text_integrity.py`) by looking for letter
sequences Unicode does not allow — a word starting with a matra, two matras in a
row, a word ending in a halant. All three sit at 0% in correctly-typed Hindi.

It then **reads the pages back as text** (`src/pdf/text_recovery.py`): the same
model the visual check uses transcribes the rendered pages at temperature 0,
marking up the structure in the vocabulary the checks already speak. The result
is validated before anything trusts it — it must be free of those impossible
sequences and must be about as long as the page is. On the same 20-page lecture
the recovered text comes back at **0% corruption**, and
`जाति व्यवस्था की उत्पत्ति के सिद्धांत` reads exactly as it was typed.

So on a Hindi PDF every stage runs at full strength and the report carries no
caveats. What is still reported is the underlying defect: *students cannot copy
or search text out of this PDF*, which is worth fixing at the export end.

If the transcription fails validation, nothing is trusted: the app falls back to
sending pages as images for the factual check, marks the text-reading findings
*"Read from a scrambled text layer — confirm against the page before acting on
it"* in the report's own column, and skips the language review rather than
running it over scrambled text.

### PDFs from another template

The structural checks were written for notes whose palette and type scale
(26/16/14/11 pt) are exact. A PDF built on some other house style would be
judged against the wrong numbers and come back structureless — no title, no
headings, and a "heading ladder out of order" that is not there.

So when the expected palette is absent, `measure_type_scale()` reads the scale
off the document instead: the size that sets most of the characters is body
text, and the distinct larger sizes are the heading tiers, largest first.

---

## The report

```
output/
    qc_report_2026-08-19_02-08.xlsx        <- this is the one to open
    individual_reports/
        ethics_qc..._error_report.xlsx     <- one per PDF
```

### `qc_report_<date>.xlsx`

| Sheet | What is on it |
| --- | --- |
| **QC Summary** | one row per file: name, status, pages, total errors, High/Medium/Low, and why a file failed or was only partly checked |
| **Error Report** | seven columns — Error. No., Original statement, Error type, Severity, Error / concern, Correct information / correction, Source URL(s) — in that order. A **Source PDF** column is appended when the run carried more than one file |
| *one per PDF* | the seven columns again, for that file alone (only when the run had several) |

An empty report never means "clean". Three different rows say three different
things:

- `No issue` — checked, found nothing
- `Report parsing` — the model answered, but its answer could not be read
- `Check not run` — nobody checked (no sign-in, or the call failed)

---

## Project structure

```
upsc_file_QC/
├── app.py                      Streamlit UI: upload, run, download
├── run_cli.py                  the same QC without a browser
├── requirements.txt
├── .env.example
├── .streamlit/
│   └── secrets.toml.example    Google sign-in config
├── config/
│   ├── upsc_qc_rules.yaml      the QC rule set
│   └── upsc_qc_sources.yaml    the approved-source registry
├── pw_access.py                the PW proxy client
├── src/
│   ├── config.py               every setting, read from .env
│   ├── pipeline.py             batch: N PDFs in, one workbook out
│   ├── models.py               SlideData, PdfQcResult
│   ├── ai_client.py            GeminiClient
│   ├── auth_session.py         7-day sign-in expiry
│   ├── proxy_session.py        the stored proxy pass
│   ├── error_report_writer.py  the Error Report XLSX
│   ├── qc_prompts.py           the ERROR REPORT contract
│   ├── pdf_layout_check.py     content-column overflow scan
│   ├── quality_checker.py      content completeness
│   ├── upsc_quality_checker.py structural checks over marker text
│   ├── dtp_parser.py           DTP note parsing
│   ├── language_detect.py      Hindi / English
│   ├── title_utils.py          lecture-title rules
│   ├── table_sanitizer.py      the 5.5 in table limit
│   ├── subject_detect.py       subject guess, for the log
│   ├── utils.py
│   ├── pdf/
│   │   ├── extractor.py        text, pages, spans, rendered images
│   │   ├── reconstruct.py      PDF -> marker text; measures the type scale
│   │   ├── analyzer.py         page-level checks
│   │   ├── text_integrity.py   is the text layer readable?
│   │   └── text_recovery.py    read the pages back when it is not
│   ├── qc/
│   │   ├── engine.py           the ten stages, for one PDF
│   │   ├── deterministic_checks.py
│   │   ├── ai_review.py
│   │   ├── format_check.py
│   │   ├── qc_config.py
│   │   ├── models.py
│   │   └── paths.py
│   └── report/
│       └── workbook.py         the QC workbook
├── tests/
│   ├── test_pdf_qc.py          the checks, end to end
│   ├── test_ai_stages.py       the AI stages, with a stub model
│   ├── test_regression.py      pinned findings on known lectures
│   ├── make_fixtures.py        builds PDFs with one known defect each
│   └── check_copies.py         optional provenance check (see Tests)
├── runs/                       per-run working files
└── output/                     the reports
```

A run leaves this behind, which is where to look when a report says something
surprising:

```
runs/qc_<timestamp>_<id>/
    input/                      the uploaded PDFs (deleted at the end)
    <pdf stem>/
        rendered/               one PNG per page
        reconstructed_notes.txt structure read off the page geometry
        recovered_notes.txt     the model's transcription, raw (Hindi PDFs)
        recovered_marker_notes.txt  ...validated, and what the checks then read
        error_report_prompt_*.txt / error_report_response_*.txt
        qc_result.json          every finding, every stage, every warning
        page_analysis.json  layout_check.json  visual_check.json  ...
    run_log.json
```

---

## Tests

```powershell
.venv\Scripts\Activate.ps1
python -m pytest tests -q
```

46 tests, none of which call the model. `tests/test_ai_stages.py` drives the AI
stages with a stub client, so prompt building, response parsing, chunking, the
image fallback and the failure paths are all covered without a sign-in.

> **Known flakiness.** Three tests fail intermittently, all from one cause:
> PyMuPDF's `find_tables()` is not thread-safe. Run on concurrent threads it
> raises `ValueError: not a textpage of this page` or `ReferenceError: weakly-
> referenced object no longer exists`; `src/pdf/extractor.py` swallows the
> exception and the page silently reports no tables. Because the session-wide
> test fixture runs a batch at the default `MAX_PARALLEL_PDFS=3`, table findings
> come and go between runs. `MAX_PARALLEL_PDFS=1 python -m pytest tests -q`
> leaves only `test_parallel_and_serial_runs_agree`, which hardcodes
> parallelism and is the test that demonstrates the bug. Fixing it needs one
> lock around the `find_tables()` call.

`tests/check_copies.py` is an optional provenance check: much of the QC logic
was lifted from an earlier project, and that script diffs those files against it
to confirm nothing drifted. It needs that project checked out next door and is
not part of the normal test run.

To exercise the real model end to end, sign in once in the browser app and then:

```powershell
python run_cli.py testdata --session
```

---

## Known limitations

**Checks a finished PDF cannot support**

- Claim verification and correction application. Those exist to *edit* notes,
  and this app must not modify anything. The factual checking they would support
  is done by stage 9, which reports rather than rewrites.
- Registry checks on diagrams — whether each selected diagram was inserted once,
  in the right section. That needs a registry that only exists while the notes
  are being written. The DPI readability rule is kept and measured off the PDF.
- `#FLOW_START` / `#FLOW_END` marker balance, **on an offline run only**. A
  flowchart renders as arrows in ordinary text, and geometry alone cannot tell
  them from prose that happens to contain "↓" — so it is reported as *"not
  applicable to a PDF"* rather than passed silently. When the pages are read
  back by the model (stage 0), the flow zones come back with them and the check
  runs normally.

**Table detection is unreliable above `MAX_PARALLEL_PDFS=1`**

Verified, and the cause of the test flakiness above. Concurrent `find_tables()`
calls raise, the error is swallowed, and the page reports no tables — so a table
overflowing the content column can lose both its "Table clipping" and "Layout"
findings, leaving only a Low "table detection did not run on page N" note. Set
`MAX_PARALLEL_PDFS=1` until a lock is added around that call.

**On the model's own knowledge.** Stage 9 reports what the model believes to be
wrong, and its knowledge has a cutoff. On an English test lecture it flagged
"total installed electricity capacity 513.73 GW" as overstated and offered
441.96 GW "as of March 2024" — the notes' newer figure was the right one. Treat
an "Outdated data" row about a very recent figure as a prompt to check the date,
not as a verdict.

---

## Troubleshooting

**"Authentication credentials in `.streamlit/secrets.toml` are missing for the
authentication provider google."**
There is no `[auth.google]` section in the secrets file actually being loaded.
Two common causes: you have not created `.streamlit/secrets.toml` in this
project yet (so Streamlit fell back to a user-level one somewhere else), or the
file uses the flat single-provider layout — `client_id` directly under `[auth]`
— which only works with a bare `st.login()`. This app calls `st.login("google")`
and needs the nested `[auth.google]` section shown in the example file.

**Sign-in loops, or "Continue with Google" does nothing.**
`.streamlit/secrets.toml` is missing or still has placeholder values. It also
needs `expose_tokens = ["access", "id"]`.

**Google rejects the callback / `redirect_uri_mismatch`.**
The URI registered on the OAuth client must match `redirect_uri` in
`secrets.toml` exactly, port included. Default is
`http://localhost:8501/oauth2callback`.

**"This account isn't authorized for this app."**
The proxy's allowlist is the only source of truth. Ask an admin to add your
email for this app.

**"Couldn't verify your access with the PW proxy."**
The proxy was unreachable or your sign-in lapsed. Wait a moment and retry, or
sign out and back in. The app fails closed on purpose — it will not run paid
calls it could not authorise.

**Every PDF reports "Check not run".**
The AI stages never ran. In the browser app, check you are signed in. From the
terminal, use `--session` (or `--token`); without one the runner says so on its
first line.

**`--session` says "No stored session found" or "has expired".**
The 7-day pass is minted by the browser app, not by the runner. Run
`streamlit run app.py` and sign in once; `--session` then works for a week.

**"This PDF's copyable text is scrambled."**
Expected on Hindi notes PDFs — see "Hindi PDFs". The report itself is
unaffected: the pages are read back as text and every check runs normally. What
the row tells you is that a *student* cannot copy or search text out of that
PDF, which is worth fixing at the export end.

**A big batch is slow.**
Each PDF makes at least three AI calls, and a long or Hindi one is split into
more. `MAX_PARALLEL_AI_CALLS` is the real throttle — it caps AI calls across the
whole run, so raising `MAX_PARALLEL_PDFS` alone will not flood the proxy, and
raising `MAX_PARALLEL_AI_CALLS` past ~6 starts drawing HTTP 429s that are
retried with backoff and cost back the time.

**`ModuleNotFoundError: No module named 'fitz'`**
The virtual environment is not active, or `pip install -r requirements.txt` was
not run in it. `fitz` is the import name of `pymupdf`.

**A report says something surprising.**
Open `runs/qc_<timestamp>_<id>/<pdf stem>/`. `reconstructed_notes.txt` is
literally what the structural checks read; `qc_result.json` has every finding
with the stage that raised it; `error_report_prompt_01.txt` and
`error_report_response_01.txt` are what was sent to the model and what came
back.

---

## What this app will never do

Generate notes. Summarise. Transcribe. Touch YouTube, Google Drive or MP4s.
Draw diagrams. Modify, move or rename your PDF.
