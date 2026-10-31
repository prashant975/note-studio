# 07 — Categories × note types: how one app serves every exam

The goal: every note project merged into one app, built so a **new category**
(CLAT, SSC, Banking, CUET, State PSC, ...) can be added **from the Studio,
without new code**, whenever it can reuse an existing kind of notes.

## Three building blocks

| Block | What it is | Examples | Who adds one |
|---|---|---|---|
| **Engine** | Code that makes one family of outputs | `notesbot` (hand-drawn PDF), `upsc` (typed DOCX/PDF from a class recording), `pw_books` (handwritten DOCX), `notes_to_dpp`, `notes_qc` | A developer (Claude), rarely |
| **Note type** | One output an engine can make, with its options | `concise_handdrawn`, `upsc_dcn`, `upsc_simple`, `upsc_mains`, `upsc_prelims`, `upsc_mains_practice`, `pw_books_handwritten`, `dpp_from_notes` | A developer, when a genuinely new output is needed |
| **Category pack** | One exam/audience: its subjects, languages, which note types it offers, its branding and its **requirements** (rule files) | `neet_jee`, `foundation`, `upsc`, later `clat`, `ssc`, `banking`, `cuet` | **The operator, from Settings → Categories → Add category**. No code |

A run is always **category × note type × subject × language** (+ options):

```
New notes:  [Category: UPSC ▾]  [Type: Daily Class Notes ▾]  [Subject: Polity ▾]  [Language: Hindi ▾]
            [Class recording link .....]  [Slides (optional)]           [More options ▸]   [Make notes]
```

The type list shows only the types the category offers; the subject and
language lists come from the category; the options come from the type.

## Requirements are layered text, resolved in one place

What makes notes "different" is plain-language rule text plus a few switches.
It is stacked from general to specific. The **later layer wins** when switches
conflict, and the text is joined in this order:

```
1. engine base prompt            the engine's own prompt (engines/shortnotes/notesbot/prompts.py, engines/<engine>/prompts/...; never edited)
2. note type rules               note_types/<type>/rules.txt         (e.g. "DCN layout: Space for Notes box...")
3. category rules                categories/<cat>/rules/all-subjects.txt
4. subject rules                 categories/<cat>/rules/<subject>.txt
5. category × type rules         categories/<cat>/rules/<type>.txt          (optional)
6. sheet profile / batch rules   instructions saved on the sheet profile    (optional)
7. this run's instructions       the "Extra instructions" box               (optional)
```

The `requirements.resolve(category, note_type, subject, language, profile=None,
extra=None)` function returns three things:

- **`texts`**: the ordered list of `(layer, path, text)`;
- **`switches`**: merged JSON switches, such as `solved_examples`, `mind_map`,
  `one_word_table` and `page_budget`;
- **`fingerprint`**: a hash of every file used. It goes into the engine's cache
  key, so changing a rule never reuses stale notes, and an unchanged rule never
  re-pays.

Lines starting with `#` in a rule file are comments: `resolve()` drops them and
empty files add nothing. The seeded NEET-JEE and Foundation files have no
comment lines; keep them byte-identical so their fingerprint does not move.

Each engine adapter turns the resolved requirements into whatever its engine
already understands:

- **notesbot (engines/shortnotes):** layers 3-4 are the neet_jee and foundation
  packs.
  - On first start they are **seeded byte-for-byte** from the copy's
    `notesbot/format_rules/<category>/` (texts) and `rules.json` (switches,
    subject lists, labels).
  - The copy's `categories.py` (an allowed change) reads them from the packs,
    and the copy's `format_rules/` stays untouched as the seed.
  - For unchanged rules, `prompt_block()` and `fingerprint()` come out
    byte-identical; a snapshot test proves it.
  - Extra layers go in as the existing "instructions" input.
- **upsc and pw_books:** the adapter passes the engine's own prompt file,
  unchanged, and appends layers 2-7 as an extra instruction block at the end.
  The end matters for the same prompt-caching reason as notesbot's ordering.

Settings → Categories shows each pack and its files. **Edit** opens a text box;
Save keeps a `.bak` and the next run uses the new text. **Preview requirements**
shows the exact resolved stack for any category × type × subject × language.

## Category pack layout

```
categories/
  _template/                 copied by "Add category"
    category.json
    rules/all-subjects.txt
    branding/                (logo.png optional)
  neet_jee/
    category.json
    rules/...                seeded byte-for-byte from engines/shortnotes/notesbot/format_rules/neet_jee/
    switches.json            this category's entries from format_rules/rules.json
  foundation/
    category.json
    rules/mathematics.txt, rules/sanskrit.txt ...   seeded from format_rules/foundation/
    switches.json
  upsc/
    category.json
    rules/all-subjects.txt, rules/<subject>.txt (start empty: the UPSC prompts already carry the rules)
    branding/                OnlyIAS DCN look lives in the upsc engine's writer; this only overrides names/footer
```

Pack folders live in Note Studio's data folder, not the install folder:
`~/.note_studio/categories/`. That way an operator's new category and every
rule edit survive updates. Built-in packs ship with the app and are copied
there on first start, only if absent. Rule edits never touch the engine
copies; `drift` keeps the copied `format_rules/` and prompts byte-identical.

### `category.json`

```json
{
  "id": "upsc",
  "label": "UPSC (OnlyIAS)",
  "enabled": true,
  "subjects": [
    {"id": "polity", "label": "Polity"}, {"id": "history", "label": "History"},
    {"id": "geography", "label": "Geography"}, {"id": "economy", "label": "Economy"},
    {"id": "environment", "label": "Environment"}, {"id": "science_tech", "label": "Science & Tech"},
    {"id": "ethics", "label": "Ethics (GS4)"}, {"id": "current_affairs", "label": "Current Affairs"},
    {"id": "general", "label": "Other / mixed"}
  ],
  "languages": ["english", "hindi", "marathi"],
  "language_required": true,
  "note_types": ["upsc_dcn", "upsc_simple", "upsc_mains", "upsc_prelims", "upsc_mains_practice", "dpp_from_notes", "concise_handdrawn"],
  "default_note_type": "upsc_dcn",
  "inputs": ["video_link", "slides_upload", "pdf_upload"],
  "branding": {"signature_label": "Faculty", "footer": "", "logo": "", "watermark": true},
  "rules_dir": "rules",
  "automation": {"sheet_layouts": [], "_comment": "add when a UPSC sheet exists"},
  "output_naming": "{title} - {type_label}.{ext}",
  "models": {}
}
```

The UPSC subject list above is shortened for the example. The real list uses
the UPSC engine's own ids; see `categories/upsc/category.json`. An empty `models` means "use the type's model
order". A category may override the order per stage.

- `inputs` limits what the New notes page offers for this category.
- Every `note_types` entry must exist in `note_types.json` and support at least
  one of the category's languages. **Check category** verifies this.

## Note type registry (`note_types.json`)

Each type names its engine, its options, outputs, accepted inputs, supported
languages, model order per stage, and its cache revision. See
`note_types/README.md`. The UPSC formats are separate types on the same engine:

| Type id | Engine | Output | Comes from |
|---|---|---|---|
| `concise_handdrawn` | notesbot | PDF | shortnotes-core (today's notes) |
| `upsc_dcn` | upsc | DOCX + PDF + error report | Upsc_Notes_2 `dcn` format |
| `upsc_simple` | upsc | DOCX + PDF | `simple` format |
| `upsc_mains` | upsc | DOCX + PDF | `mains` format |
| `upsc_prelims` | upsc | DOCX + PDF | `prelims` format |
| `upsc_mains_practice` | upsc (dmp) | DOCX | DMP tab |
| `pw_books_handwritten` | pw_books | DOCX + PDF | handwritten_notes_automation_complete |
| `dpp_from_notes` | notes_to_dpp | CSV + PDFs | the newer copy inside Upsc_Notes_2 |
| `typeset_notes` | typeset (NoteCraft's own, `note_studio/typeset`) | PDF + quality report | the B.Sc. Wallah sample notes (26 Oct 2026); offered by the `bsc` pack |
| tool `notes_qc` | notes_qc | XLSX report | upsc_file_QC: offered on every finished run, any category |

## Engines: in-process or child process

| Engine | Runs | Why |
|---|---|---|
| notesbot (`engines/shortnotes`) | **in the Studio process** (threads); `engines/shortnotes` on `sys.path` | Built for it already; the upload path and automation call it today. Its packages (`notesbot`, `sheets_bridge`, `api`, `pw_access`) do not clash with the Studio's |
| upsc, pw_books, notes_qc, notes_to_dpp | **child process**, one per run, via the QBG `stream_command` pattern, **cwd and `sys.path` = that engine's folder** | Each project has top-level packages (`src`, `config`, `pw_access`), reads `.env` and keeps globals at import time. Each in its own process, they run **unchanged, with no package renaming**, and can never collide. A child process also makes Stop reliable (kill the process tree) and keeps a crash from taking the Studio down |

The child-process contract (every copied engine gets a small `engine_main.py`):

```
<runtime python.exe> engines\upsc\engine_main.py --request <run_dir>\request.json --out <run_dir>   (cwd = engines\upsc)
stdout, one JSON object per line:
  {"event":"step","text":"Transcribing the class recording","pct":20}
  {"event":"cost","inr":3.10,"model":"gemini-3.6-flash","stage":"notes"}
  {"event":"warning","text":"2 slides could not be read; notes made without them"}
  {"event":"output","path":"<run_dir>/Polity 12 - Daily Class Notes.docx","kind":"docx"}
  {"event":"done","status":"complete"|"needs_attention"|"failed","reason":"..."}
exit code 0 = finished (see status), non-zero = crashed (Studio marks the run failed with the last lines)
```

- `request.json` carries the category, type, subject, language, options, input
  paths/links, the resolved requirements (texts + switches + fingerprint), the
  model order and the settings the engine needs.
- The engine reads **only** `request.json`, plus the sign-in token from its
  environment. It never reads the original project's `.env`, and it sets its
  own config environment before importing its code. It is a separate process,
  so this cannot leak.
- The installed app runs the same contract with its bundled Python runtime.
  That is why Phase 9 recommends QBG's embedded runtime with the code as plain
  folders: two engines with a top-level `src` cannot share one PyInstaller
  archive.
- Old runs from the original projects (`runs/`) are listed in Runs → "From
  older apps", read-only, in place.

## Adding a new category later (the operator's path; no code)

1. **Settings → Categories → Add category.** Fill in the name, subjects (a list
   box), languages, which note types it offers and the default one, plus
   optional logo and footer. Studio copies `_template/` and writes
   `category.json`.
2. **Write the requirements.** Fill `all-subjects.txt` (what is different for
   this exam), then any subject files, in plain sentences such as "Keep every
   date and article number", "Add a 5-line summary at the end" or "No solved
   examples".
3. **Check category.** It verifies the ids, files, languages × types and the
   branding image, and shows the resolved requirements for one choice of
   category × type × subject × language.
4. **Try it.** Make notes for one sample lecture. This is a paid generation,
   so the Studio says so before starting. Compare, edit the rules, and repeat.
5. **Automation (optional).** If the category has a sheet, add the sheet and
   pick this category on the sheet profile.

A developer is needed only when the new category needs **a new kind of
output**: a new note type, and maybe an engine. That is the "Add a note type"
recipe in `note_types/README.md`: adapter, registry entry, tests, tour and
guide.

## Rules for Claude while building this

1. **Seed, don't edit.** The neet_jee and foundation packs are seeded
   byte-for-byte from the **copy's** `notesbot/format_rules/`, which then
   stays untouched (`drift` protects it). Notesbot's cache fingerprint must
   come out identical for the same category and subject (test it).
2. **Read categories from the packs, not from hard-coded lists.** The copy's
   `notesbot/categories.py` hard-codes `CATEGORIES = ("neet_jee",
   "foundation")`, `LABELS`, `DEFAULT = "neet_jee"` and `LEGACY` (neet/jee →
   neet_jee). Change it, minimally and with tests, so the list and the rule
   texts come from the packs. This is one of the few allowed changes in a copy
   (`ENGINE_COPIES` in `data_guard.py`). The following must stay byte-identical
   for the two existing categories:
   - `LABELS[...]` text: it is printed into the prompt by `prompt_block()`
     ("CATEGORY: NEET-JEE notes..."), so the labels stay exactly "NEET-JEE" and
     "Foundation";
   - `fingerprint()` output;
   - `LEGACY` handling;
   - `normalize()` errors for unknown ids.

   Snapshot `prompt_block` and `fingerprint` for every category × subject pair
   before the change, and assert them equal after it.
3. **Validate subject ids.** The copied tests include a cross-check (rules.json,
   `sheets_bridge/mapping.py`, `web_service.py`, `web/state.js`,
   `api/blob-download.js`) for neet_jee/foundation. Copy the files that test
   reads, so it still runs. New categories have their own subject lists and
   need none of those files.
4. **Copy engine code, never edit the originals.** Prompt files stay
   byte-identical in the copies (`drift`), and the originals are frozen
   (`compare`).
