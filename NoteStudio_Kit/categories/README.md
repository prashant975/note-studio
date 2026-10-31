# Category packs (examples)

One folder per exam/audience. Read `docs/07_CATEGORIES_AND_TYPES.md` first.

| Pack | Engine(s) it uses | Notes |
|---|---|---|
| `_template/` | (none) | Copied by Settings → Categories → **Add category**. Its rule file is a commented example |
| `neet_jee/` | shortnotes (+ pw_books if phase 7 is done) | `rules/` seeded byte-for-byte from `engines/shortnotes/notesbot/format_rules/neet_jee/`. Label must stay "NEET-JEE" |
| `foundation/` | shortnotes | Seeded from `engines/shortnotes/notesbot/format_rules/foundation/`. Label must stay "Foundation" |
| `upsc/` | upsc (notes, Mains practice), notes_to_dpp | Subject ids are the UPSC engine's own; DPP workbooks are read in place |

- **Placeholders.** Values in `<angle brackets>` are placeholders; Claude fills
  them from the real code when it writes the app's built-in packs.
- **Where packs live.** In Note Studio's data folder:
  `~/.note_studio/categories/<id>/`. They are seeded on first start only if
  absent, so operators' edits and new categories survive updates.
- **`seed_from`** names the folder in the engine copy that a built-in pack's
  rules are copied from, once. The engine copy itself is never edited.
- **Rule files.** One plain command per line. Lines starting with `#` are
  comments and are dropped by `requirements.resolve()`. The seeded NEET-JEE
  and Foundation files have no comment lines; keep it that way, so their
  fingerprint is unchanged.

**Adding a future category** (CLAT, SSC, Banking, CUET, State PSC...) is the
operator's path in doc 07: Add category, write the rules, Check category, try
one lecture. No code is needed, as long as an existing note type fits.
