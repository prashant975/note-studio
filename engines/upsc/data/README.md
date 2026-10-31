# Master workbooks for the DPP builder

These two files are the authoritative taxonomy and question bank:

| File | What it is |
| --- | --- |
| `UPSC QBG Tags.xlsx` | The chapter / topic / subtopic taxonomy. Every tag the app assigns must come from a row in here — it never invents one. |
| `DPP and Infinite Practice Questions.xlsx` | The question bank the practice sets are drawn from. |

**They are deliberately not in version control.** The question bank is ~55 MB of
PW's own question content and this repository is public, so committing it would
publish the bank. `.gitignore` covers `data/*`.

The app resolves each file in this order, so a fresh clone runs as soon as one
of them is satisfied:

1. a workbook uploaded in the UI for that session;
2. an absolute path in `QBG_TAGS_PATH` / `QUESTION_BANK_PATH`;
3. a file of the name above sitting in this folder.

To set a machine up, copy both files here. They are not needed to run the notes
pipeline, the QC stage or DMP — only the DPP builder.
