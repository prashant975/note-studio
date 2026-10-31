# Note types — the kinds of output the Studio can make

Read `docs/07_CATEGORIES_AND_TYPES.md` first. In short:

- **Engine:** code that makes a family of outputs (`notesbot`, `upsc`,
  `pw_books`, `notes_to_dpp`, `notes_qc`).
- **Note type:** one output an engine makes, with its options. This folder
  describes them.
- **Category pack:** one exam (`neet_jee`, `foundation`, `upsc`, and later
  any). It picks which types it offers and adds its own requirements. See
  `../categories/`.

A run is **category × note type × subject × language** plus options. The
Studio builds its screens from `note_types.json` and the category packs:

- **New notes:** category → that category's types → the type's options and
  accepted inputs.
- **Automation:** each sheet profile names a category and a note type.
- **Settings:** **Note types** lists every type, its engine, status, inputs and
  outputs. **Categories** holds the requirements.
- **Smart Queue, Runs, History:** show the category and type on every card.

Adding a **category** never needs code (doc 07). Adding a **note type** needs
an engine adapter (below) unless an engine already makes it, e.g. a new UPSC
format is a new `engine_format` on the `upsc` engine.

## Fields

| Field | Meaning |
|---|---|
| `id` | Stable id, stored on every run. Never rename one that has runs |
| `label`, `summary` | What the operator sees: plain words, a one-line summary |
| `engine` | Which engine makes it. See `engines` at the top of the registry: `in_process` (notesbot) or `child_process` (merged projects) |
| `engine_format` | The engine's own name for this output (e.g. upsc `dcn`, `simple`, `mains`, `prelims`, `dmp`) |
| `inputs` | `pdf_upload`, `pdf_link`, `pptx_upload`, `video_link`, `slides_upload`, `sheet_row`, `finished_run`, `docx_upload` |
| `outputs` | `pdf`, `docx`, `xlsx_error_report`, `csv`, ... |
| `languages` | Languages the engine can write. A category offers the overlap of its own list and this one |
| `options` | Form fields: `choices`, `default`, `type` (`text`, `bool`, `textarea`, `file`, `path`), `required`, `advanced` (goes under "More options"), `shown_when` |
| `requirements` | Where the type's own rules live (layer 2 in doc 07). The engine's prompt files (layer 1) are referenced, never edited |
| `models` | Default model order per stage (first = default). Settings and the category may override it |
| `needs` | External programs. When one is missing, the type shows "Needs ..." instead of failing mid-run |
| `settings_needed` | Settings that must be filled first (e.g. the DPP workbooks) |
| `cache_revision` | The engine's existing cache key. Keep it, or finished notes are paid for again |

## The engine contract

Every engine is driven the same way, so the queue, run page, history, costs
and automation work for every type without special cases:

```python
class NoteEngine(Protocol):
    type_ids: tuple[str, ...]
    def validate(self, request: NoteRequest) -> list[str]: ...      # plain-words problems, [] = OK
    def estimate(self, request: NoteRequest) -> Estimate: ...        # pages, calls, rough INR, minutes
    def run(self, request: NoteRequest, progress: Progress,
            stop: StopFlag) -> NoteResult: ...                      # outputs + cost + warnings
```

- **`NoteRequest`:**
  - category, type, subject and language;
  - the chosen options;
  - input files and links;
  - the resolved requirements (texts, switches, fingerprint);
  - the model order;
  - the source (`upload` | `sheet_row` | `finished_run`).
- **`NoteResult`:** output files, cost (INR), model calls, warnings, and
  "needs attention" reasons.
- **notesbot:** `engines/notesbot_engine.py` calls today's code **in the
  Studio process**.
- **Merged engines:** `engines/<engine>.py` writes `request.json` and runs
  `engines/<engine>/engine_main.py` as a **child process**. The
  JSON-lines events (step / cost / warning / output / done) become `progress`
  and `NoteResult`. Stop kills the child. Doc 07 has the event format, and
  doc 08 has the merge recipe and each engine's facts.

## Adding a note type (developer recipe)

1. If an engine already makes it, add a registry entry with that engine and
   `engine_format`, and you are done after tests.
2. If not, copy or write the engine under `engines/<name>/` and
   add `engine_main.py` with the event contract (doc 08 recipe).
3. Add the adapter in `note_studio/engines/<name>.py`.
4. Add the registry entry. Offer the type in the category packs that should
   have it.
5. Tests: the engine's own tests, one mock end-to-end child run, a page test
   for the New notes form, and a prompt-parity test where the type replaces
   an older app.
6. Add a what's-new tour entry and update the page guide.

## The example

`note_types.example.json` shows the eight types and one tool found in your
projects. Ids, option values and model names in it are placeholders taken from
the read-only maps; the real registry is written from the real code.
