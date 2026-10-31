# Note formats per category and subject

Every lecture is made in one exam category (NEET-JEE, Foundation) and, where
known, one subject. The files here decide what differs between them. A missing
or empty file adds nothing, so that category and subject make the default
concise notes.

```
<category>/all-subjects.txt   commands for every subject in the category
<category>/<subject>.txt      commands for one subject in that category
rules.json                    switches the code enforces, subject lists, labels
```

For a lecture in category X and subject Y the notes prompt gets
`X/all-subjects.txt`, then `X/Y.txt` (the more specific file last). The final
quality check reads the same text.

`rules.json`:

- `formats["X/Y"]` holds switches for that pair:
  - `solved_examples: true` keeps the teacher's solved examples (question,
    steps, answer) instead of the theory-only default;
  - `mind_map: false` leaves out the revision mind map;
  - `one_word_table: true` keeps only the first शब्दार्थ section;
  - `summary` is the one line the Studio shows under the subject dropdown.
- `category_subjects["X"]` lists the subjects the Studio offers for category X.
- `subject_labels` names each subject id in the Studio.

Subject ids must also be accepted by `sheets_bridge/mapping.py` (column D),
`notesbot/web_service.py`, `web/state.js` and `api/blob-download.js`; a test
checks that they agree.

Sanskrit subject files apply to Sanskrit lectures written as Hindi notes
(verses verbatim, Hindi meanings), not to strict Sanskrit-only notes.

Editing a file changes the next run; the automation's cache refreshes for that
category by itself. The desktop .exe packs these files, so rebuild it after a
change.
