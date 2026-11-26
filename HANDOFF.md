# Note Studio — Handoff (newest first)

## CSIR NET category: B.Sc.-style notes with each lecture PDF's own header name and logo; Mac 2026.11.27.1 — 27 November 2026

The user asked for "more categories for notes generation which follow the B.Sc. style, but the name and logo come by
auto-fetching from the PDF", then to build, install, commit and push. They sent four CSIR NET notes PDFs, made in the
same LaTeX template family:
- `LifeScience_Biomolecules Part 1_ Proteins.pdf`
- `Physics_Potentials.pdf`
- `Mathematical Fundamental Handbook.pdf`
- `Chemistry _Periodicity_ Rules for Electron Filling.pdf`

Each prints a running header: the name on the left (e.g. "CSIR NET - Life Sciences") and the unit on the right
(e.g. "Unit 1: Biochemistry"). None of them has a logo.

- **New** `note_studio/typeset/source_brand.py` reads a lecture PDF's running header. Nothing is guessed:
  - Text in the top 10 % of the page counts when it repeats on the same side on at least 60 % of the first 12 pages,
    is at most 13 pt, and is not a page number. Its left part becomes `header_left` and `footer_left`; its right
    part becomes `header_right`.
  - A logo-sized picture repeated in the top 15 % becomes the corner logo and the faded watermark, drawn as the PW
    logo is (`render.Branding.logo`, `render.own_logos`, `render.logo_box`). A wide logo is 11 mm tall and at most
    3.5× as wide.
  - A one-page PDF counts only small text in the top 4.5 %.
  - The words come out right on all four PDFs. A PW concise-notes PDF gives the PW logo.
- **The notes:** `engines/typeset.make_notes` reads the header first when the category has `branding.from_pdf`.
  - `source.extract(furniture=…)` leaves out of the content the header lines (AI-read or text), bare page numbers at a
    page's top or bottom, and the header logo. They are listed in `source_map.json` `excluded_furniture`.
  - The quality report's first check is "Header name and logo": what was taken, or why the category's words stayed.
  - With no logo in the PDF, the category's PW logo choice applies.
  - A Word input keeps the category's words.
  - Categories without `from_pdf` are unchanged.
- **Category** `note_studio/packs/csir_net` (built in, seeded on start like B.Sc.):
  - subjects Life, Physical, Chemical, Mathematical and Earth Sciences;
  - offers `typeset_notes`, plus Audio notes, which every category gets from the audio session's work;
  - `from_pdf: true` and `pw_logo: false`, so no Google Docs type;
  - fallback words "CSIR NET" | "CSIR NET Notes".
- **Settings → Categories:**
  - per category with B.Sc.-style notes, the switch "Header name and logo: From each lecture PDF / This category's own
    words" (`/settings/categories/{id}/from_pdf`, `category_admin.set_from_pdf`, a `.bak` first);
  - Add a category has the checkbox for it, kept only when B.Sc.-style notes are chosen.
- **Also:**
  - What's new tour `2026-11-27-csir-net`;
  - the New notes and Settings page guides;
  - `tests/test_gdoc_pdf.py` no longer depends on the order of PDFs made in parallel (it failed now and then).
- **Tests:**
  - `tests/test_source_brand.py` (11) and `test_category_packs` (csir_net seeded);
  - full suite in the working tree passed;
  - the committed tree alone (without the audio session's uncommitted files) also passed, except
    `test_gdoc_app::test_a_missing_sign_in_holds_the_queue`, which is old and timing-dependent and passes when rerun.
- **Not checked with AI:** no paid run was made. The default path reads every page with AI; the header lines it
  transcribes are dropped by the same rule, which is tested with saved page answers.
- **Not done:** vector-drawn logos and Word headers are not read.
- **Mac 2026.11.27.1** (No UPSC):
  - The 26.2 output was moved to `dist/kept-20261126-2-noupsc/`.
  - Built with `build_macos.py --without upsc --zip --publish`: zip 691,655,292 bytes, SHA-256 31c506d9…5abe,
    Drive file 1IXh51UHpk5JwOPNctr64hES1x2-vkqUQ. It includes the audio session's voice-step fix as it was in the tree.
  - Installed by self-update at 00:55 (no run active; the audio session was told before and after). It was back on
    3100 as 2026.11.27.1 within a minute. `~/.note_studio/categories/csir_net` was seeded, and New notes and Settings
    show CSIR NET with its switch.
- **Gates:**
  - `data_guard` snapshot `_build/before_csir_net_20261127.json`;
  - compare with `after_csir_net_20261127.json`: STOP only from the update's backup rotation. The 29,429 files of
    `update_backups/NoteCraft-2026.11.26.1.app` were replaced by `NoteCraft-2026.11.26.2.app`; no JSON entries
    lost; no original touched. Changed: `note_studio.db`. New: the `csir_net` pack;
  - drift: STOP only for the known reason (the originals are not on this Mac); `git status engines` is clean.
- **Commit:** only this session's files and hunks. The audio session's work stays uncommitted at its request: `audio/`,
  `audio_service.py`, `engines/audio.py`, its hunks in shared files and its HANDOFF sections.

## "Bsc Nursing Notes": a PDF of every made Doc, its link in Concise Notes Link (853 rows) — 26 November 2026 (night)

The user asked to "convert doc into pdf and paste the link"; they chose all 853 B.Sc.-style Docs, links in column M
"Concise Notes Link" (empty in every row before).
- **New code** `note_studio/gdoc_pdf.py` (+ `gdoc_sheet.write_pdf_link`, `gdoc_sheet.asked_again`, now also used by
  `gdoc_service.write_link`; `gdoc_sheet.read` returns each row's `pdf` and the tab's `pdf_column`):
  - the PDF is the Doc's tabs exported one by one (`gdoc_make.export_tab`) and joined with pypdf, a bookmark per tab
    when there are several. A whole-Doc export breaks pages differently from what the quality check passed
    (row 113: 4 pages instead of 3), so it is not used;
  - one Drive folder per sheet tab, "Bsc Nursing Notes - PDFs (B.Sc. style)" (id 1afLbw9EbCaiCcaaMXPlhCH3ScbHBAYcC),
    in the NoteCraft sign-in's My Drive; folder and PDFs readable by anyone at the account's domain with the link;
  - each Doc is uploaded once: `~/.note_studio/gdoc/pdfs.json` remembers it, so asking again only writes missing links;
  - a link goes into M only while that row's P still links the same Doc, only into an empty cell, never by adding a column.
  - Tests: `tests/test_gdoc_pdf.py` (6). Full suite 472 passed, 19 skipped.
- **Run** (`_build/gdoc_pdfs_20261126.py`, the app's code path with NoteCraft's sign-in; no AI, no cost): rows 113 and
  251 as a pilot (checked by eye: logo, watermark, footer, bookmarks), then the other 851 in 44 min, 3 at a time.
  Result: 853 of 853 rows have M = the PDF of their P Doc; 8,684 pages, 1.33 GB. Results JSON `_build/gdoc_pdfs_20261126_*.json`.
- **Not yet in the app:** no button, and new sheet runs do not make the PDF by themselves. A PDF does not follow later
  edits of its Doc. The 25 rows still without a Doc (equations, drawings) have no PDF either.

## Mac 2026.11.26.2 (No UPSC) built, published and installed by self-update — 26 November 2026 (13:15)

The user said "when it complete then build and install" once the other session's MP3 audio work was finished (its last
message, 13:00: 466 passed, 19 skipped; no file changed after 12:57).
- **Tests:** 466 passed, 19 skipped (full suite, 13:08).
- **Build:** the other session's 2026.11.26.1 output was moved (not deleted) to `dist/kept-20261126-1-noupsc/`, then
  `build_macos.py --without upsc --zip --publish --notes "…MP3…Raju and Riya…"` → 2026.11.26.2, zip 691,633,093 bytes,
  SHA-256 2c3825c6…6c92, Drive file 1LlPAMEVG57h-2CyNuJ0xqJaTnilnLv1a in "NoteCraft Mac updates". `lameenc` is in the bundle's libs.
  Log: `_build/build_macos_20261126_2.log`.
- **Install by the app's own update:** Check for updates (POST /update/check) → downloaded and checked in 2 min →
  no run or job active → Install update at 13:14 → reopened 13:15 as 2026.11.26.2 ("Updated to 2026.11.26.2.");
  backup `~/.note_studio/update_backups/NoteCraft-2026.11.26.1.app`. The startup "Sign in" message cleared on the next check.
- **Gate:** compare says STOP only because the one kept backup rotated (old `NoteCraft-2026.11.25.2.app` replaced by
  the 2026.11.26.1 one, by design); otherwise only `note_studio.db` changed, nothing lost, no original touched,
  engines have no diff against HEAD. Not committed (the tree still holds both sessions' uncommitted work).

## "Bsc Nursing Notes": 16 more rows made on 2026.11.26.1 (853 of 878 rows have a Doc) — 26 November 2026 (midday)

The user shared the 14 unreadable sources and said to make the remaining rows once the other session had built and
installed 2026.11.26.1 (it has the link retry, code-block marker and Drive rate-limit fixes; checked in the bundle).
- **The tab changed overnight:** one row near the top was removed, so every row after it moved up by one (old 114 is
  now 113), and a new row 879 ("Communication and Utilization of Research") was added. All 837 made Docs were still
  in column P (checked by Doc id against every done run item), so nothing was lost; the app writes a link by
  matching the source Doc, not only the row number.
- **Run 14** (app, Sheets lane, 3 at a time; 12:47–12:56; ₹51.62): rows 113, 251, 255, 286, 314, 327–330, 334,
  338–341, 343, 345, 347–349, 352, 376, 383, 384, 398, 410, 416, 431, 461, 462, 469, 470, 478, 486, 688, 691, 706, 817,
  829, 840, 841, 879.
  - **16 made:** 13 of the 14 newly shared rows, the 2 code-block rows (now 410 and 688) and the new row 879.
    Row 251 (Myocardial Disorders) is a whole unit: 13 tabs, 100 pages, ₹30.12.
  - **25 not made, nothing spent:** 23 have a Google Docs equation (327–330, 334, 338–341, 343, 345, 347–349, 352,
    383, 384, 486, 706, 817, 829, 840, 841; row 341 was one of the unshared ones and turned out to have an equation),
    and 2 have a drawing (376, 691).
  - **5 flagged:** 251 ("Mostimportant"), 398 and 469 (raw LaTeX in the source), 478 (source typos `20{-22G`, `→$`)
    are the sources' own text. 462 is a real layout point: the "Dysplasia (Pre-malignant)" table row splits across the
    page and leaves page 2 almost empty.
- The old unlinked failed-check Docs for these two code-block rows (made in run 11/12) are still in Drive; trash them only if the user says so.

## "Bsc Nursing Notes" rows 108–879 made (837 of 877 rows have a Doc); 3 fixes in code — 26 November 2026 (night)

The user asked: "did you remember bsc nursing note continue the remaining note", then "start runing i will charge it".
Everything below ran from the installed app (2026.11.25.3, No UPSC), 3 rows at a time. PW AI, Gemini 3.7, no cap.

| Run | What | Made | ₹ |
|---|---|---|---|
| 11 | rows 108–879 (772) | 513, then stopped at 02:21 (see fix 1) | 1,118.59 |
| 12 | Try again from run 11 (259 rows; row 651's checked Doc reused) | 217 | 558.67 |
| 13 | rows 764 and 835 (Google's rate limit, fix 3) | 2 | 4.71 |

- **Total: 732 notes, ₹1,681.97.** The sheet check (free) shows 837 of 877 rows with a Doc in P. 40 are left.
- **40 rows without a Doc**, none of them paid:
  - **Source Doc cannot be opened by the NoteCraft account** (14): 114, 252, 256, 287, 315, 342, 399, 417, 432,
    462, 463, 470, 471, 479. These need sharing, or a correct link in the sheet.
  - **Google Docs equation in the source** (21): 328–331, 335, 339–341, 344, 346, 348–350, 353, 384, 385, 487,
    707, 818, 830, 841, 842.
    - Google gives no API (not even an export) for what is inside an equation.
    - The gaps sit at things like "High-flow [O₂?] via…" and "Delayed ([?]) / Rapid ([?])".
    - The content team must type them as text.
  - **Google Drawing / object without a picture** (2): 377, 692.
  - **Code block** (2): 411, 689. Fixed in code (fix 2). These need the new app, then Try again.
    - Each left an unlinked, failed-check Doc in Drive: 411 → `1r148YZU…`, and one for 689 in run 12.
- **Quality review: 120 made notes "need attention"** (139 points).
  - 71 "language" points (missing spaces and the like) and 33 "formula_render" points (raw LaTeX such as
    `$30^\circ$` or `\text{ mmHg}`, sometimes pasted twice) are the sources' own text. The Docs passed word for
    word, and 276 and 343 were checked against the source. Content is locked, so they are kept.
  - 35 "layout" points in 29 notes: 160, 169, 179, 249, 269, 276, 402, 405, 420, 426, 431, 449, 460, 464, 475,
    484, 499, 582, 594, 608, 655, 694, 703, 716, 719, 735, 750, 847, 855.
    - 23 are a heading at a page bottom before a table or box. Google ignores keep-with-next before a table, and
      headings already have it.
    - The rest: narrow table columns (269), a trailing empty page (276), a last line alone on a page (426, 484,
      594), a split flowchart (475), a cropped table picture (582), and a bulleted heading (655).
  - The full list is in the run folders' `result.json` → `quality`.
- **Fixes in code** (not in the installed app yet; they need a release):
  1. `gdoc_service.write_link()`: writing a row's link tries 3 times on a dropped connection or a busy Google.
     If it still fails, only that row fails ("Made and checked… Try again writes it"), not the run.
     - Run 11 stopped on "Connection reset by peer" while re-reading the sheet for row 651's link.
  2. `gdoc_source._clean()` drops U+E907, Google Docs' hidden code-block start/end marker. Docs will not insert it,
     so the word-for-word check failed on rows 411 and 689.
  3. `gdoc_writer.rate_limited()`: Drive's 403 "userRateLimitExceeded" waits and retries like a 429. The template
     copy now uses `_execute(..., outages=False)`; after a 5xx a copy may exist, so it is not retried.
  - Tests: +1 in `test_gdoc_app.py`, +2 in `test_typeset_gdoc.py`.
- Gate: nothing lost, no original touched. Changed: the database and token. New: 8,071 run files.
- Claude kept the Mac awake with `caffeinate -i -t 36000` during the run, and stopped it afterwards.

## Mac app: No UPSC edition that updates itself (2026.11.25.2 → .3 proven) — 25 November 2026 (evening)

The user asked: "is auto update is workin on this app", then "i dont want that version it was with upsc exclude it
then make it for auto update".

**Why it did not work on the Mac**
- The updater was QBG One's Windows flow: `NoteCraft_Setup_<v>.exe` from the team release folder, run by PowerShell.
  It switched itself off unless `NoteCraft.exe` was found.
- The team folder (`1vQXzi…`) is not shared with the NoteCraft sign-in on this Mac.
  - Drive answers an empty list for a folder you cannot see, so the app said "Up to date". It now says the folder
    is not shared.

**What was built**
- `version.setup_name(edition, mac=True)` gives `NoteCraft_Mac[_NoUPSC…]`. `version.mac_app_root()` finds the
  `.app` this Python runs inside.
- `updater.py` on the Mac app (`MAC = version.MAC_APP`):
  - looks for `NoteCraft_Mac_<edition>_<v>.zip` (never a Windows `.exe`, never another edition);
  - `installed_root()` is the `.app`, so the 5-minute background check and download run;
  - the download is refused unless Drive's SHA-256 and size match and the file starts `PK`;
  - `install()` → `unpack_mac()` unzips with `ditto` next to the download and checks the version, edition,
    launcher and runtime. Then the zip is deleted.
  - `MAC_HELPER` (`/bin/sh`, its own session) waits for NoteCraft's own programs to end (matched by program path
    via `ps comm`, never by command line) and moves the old app to
    `~/.note_studio/update_backups/NoteCraft-<old>.app` (one backup kept). It then moves the new app in, opens it
    and writes `exit N` to `<log>.result`.
  - Any failure puts the old app back and opens it. Log: `$TMPDIR/notecraft_update/install_<v>.log`.
  - When the first check at start-up runs before the sign-in is back, it tries again in 30 s, not 5 min.
- `update_delivery.py`: the file ending and first bytes come from the updater (`.zip`/`PK` on the Mac).
- Settings → Updates: "The Mac release folder", and a Mac hint.
- What's new tour `2026-11-25-mac-updates`, shown only in the Mac app.
- `packaging/build_macos.py`:
  - `--without upsc` (and/or `pw_books`) uses the Windows builder's `edition_release`. It leaves out that
    engine's code, libraries and wrapper; `packaged()` is the file filter.
  - `--zip` writes `dist/NoteCraft_Mac_NoUPSC_<v>.zip`.
  - `--publish [folder link]` uploads it as the NoteCraft sign-in and checks the SHA-256. The folder id is in
    `packaging/macos_release.json` and is built into the Mac app's `release.json` "releases". The project's
    `release.json` keeps the team (Windows) folder.
  - The build's own temporary folder had overwritten the release-folder variable. It was renamed to
    `release_folder`; 2026.11.25.3 was then uploaded with the same `publish()`.
- Mac release folder: "NoteCraft Mac updates" in the NoteCraft sign-in's My Drive, id
  `1NomCqCouPrJwwkl7UzL-OrMGTc__pUMN`. It is private (owner only).
- Tests: `tests/test_mac_updates.py` (12, with real `ditto`/`sh` on synthetic apps). **442 passed, 19 skipped.**

**Builds and the proof** (the user's go: "Yes, quit it now")
- 2026.11.25.2, No UPSC: 1.8 GB app (the full one is 2.6 GB). Kept as `dist/NoteCraft-20261125-2-noupsc.app`.
  - Installed by hand. The user's own NoteCraft (opened at 12:17, nothing running) was quit with their OK.
  - Backups: `~/NoteStudio_backups/note_studio-before-install-20261125-171316.db` and
    `NoteCraft-installed-20261125-1-full.app`.
  - New notes no longer offers UPSC; Nursing is still there.
- 2026.11.25.3, No UPSC: `dist/NoteCraft.app`, `dist/NoteCraft.dmg` (946,780,596 bytes, SHA-256 `ae8fdc3a…`), and
  `dist/NoteCraft_Mac_NoUPSC_2026.11.25.3.zip`.
  - The zip is 691,389,297 bytes, SHA-256 `a6f99e68…31675`, published as Drive file `1EcoIJKlt2QExII2aMoGz5oBe9O4ftjXP`.
- The installed .2 found .3 by itself at 17:18:53 and downloaded it in 2 min ("ready"). Install update was pressed
  at 17:21:05.
  - The app closed at 17:21:35 after unpacking, the helper swapped it (`exit 0`) and it reopened at 17:23:12.
  - "Updated to 2026.11.25.3." The sign-in came back; .2 is kept in `update_backups`.
- **Bug found by the proof, fixed in code, NOT in the installed .3:** the helper matched processes with `pgrep -f`
  on the app path. Claude's own watch loop named that path, so the helper waited 90 s and then killed that loop.
  - The fix matches by program path, with a test in which a process that only names the app is left alone.
  - The first update from .3 still uses .3's helper. Do not run commands naming
    `/Applications/NoteCraft.app/Contents/` while it installs.
- Gate: nothing lost, no original touched. Changed: the database and token. New: the 29k files of the kept .2
  backup. Engines: no diff against HEAD.

**Another session was working in the tree at the same time** (an "Audio notes" feature, uncommitted, about 20
files, `note_studio/audio/`).
- Both builds above were made before those edits; checked: no `audio/` and `ai.py` equals HEAD.
- `settings.html` and `whats_new.py` hold both sessions' changes.
- **Do not build or commit the whole tree while that work is unfinished.** A Mac build copies the working tree.
  Commit only this section's files and hunks.

**Releasing the next Mac version:**
`.venv/bin/python packaging/build_macos.py --without upsc --dmg --publish --notes "fix one; fix two"`.
Move `dist/NoteCraft.app`, `.dmg` and the old zip aside first.

## Mac build gets its own version (2026.11.25.1); the 24 Nov fixes installed — 25 November 2026

The user sent a screenshot of the installed Mac app's account menu: "Version 2026.11.19.1 · Development copy". They
asked: "after new build also update the version name on frontend just like you are doing on qbg one and note craft".
- Cause: only the Windows builder (`build_installer.py`) gave each build a new `YYYY.M.D.N`.
  - `build_macos.py` copied the project's `release.json` as it was.
  - `version.CHANNEL` knew only the Windows install layout, so the Mac app called itself a development copy.
- `packaging/build_macos.py`:
  - `mac_release()` gives the next version after the project's `note_studio/static/release.json`: the same day is
    N + 1, a new day is .1. It keeps the release folder id.
  - The version goes into the app's `release.json` and `Info.plist` (`CFBundleShortVersionString`/`CFBundleVersion`).
  - It goes into the project's `release.json` only after the package check passes, so the next Mac or Windows build
    counts on from it.
- `note_studio/version.py`: `mac_app()` recognises the app's own runtime
  (`NoteCraft.app/Contents/Resources/runtime`), and `CHANNEL` becomes "Mac app".
  - `INSTALLED` stays False on the Mac. The updater's own check (`installed_root`, NoteCraft.exe) is unchanged.
- What's new tour `2026-11-25-gdoc-again`:
  - Try the unfinished rows again;
  - why a row did not finish;
  - long notes finish.
- The Runs page guide has a step for Try the unfinished rows again.
- Tests: +2 in `tests/test_installed.py`; `test_gdoc_app` updated for the new tour. **430 passed, 19 skipped.**
- Build (user's go: "after new build"):
  - The previous build was kept as `dist/NoteCraft-20261124b.{app,dmg}`.
  - New `dist/NoteCraft.dmg`: 1,357,573,476 bytes, SHA-256
    `225426a38403eaaf5dba2402ca148d2b9a9c0c4ce01129f52df79fcb5dd26fbc`.
  - PACKAGE_OK. Log: `_build/build_macos_20261125.log`.
- Install:
  - Backups: the database (`~/NoteStudio_backups/note_studio-before-install-20261125-105442.db`, integrity ok) and the
    categories.
  - Nothing was waiting or running. Claude quit the copy it had opened itself on 24 Nov at 22:07.
  - The old app is kept as `~/NoteStudio_backups/NoteCraft-installed-20261124b.app`.
  - `ditto` → `/Applications/NoteCraft.app`; `diff -rq` shows it identical to the build.
  - Reopened. The sign-in came back by itself, and the menu shows "Version 2026.11.25.1 · Mac app".
- Gate:
  - Compare: nothing lost, no original touched. Changed: the database and the sign-in token (app start).
  - Engines: no diff against HEAD. Drift still says STOP on this Mac only because the originals are not here.
- This build has every fix from the section below: parts, the complete-PDF check, resume, Try again, failure reasons.

## 100 more "Bsc Nursing Notes" rows (2–107 all done); long tabs, cut-short exports, Try again — 24 November 2026 (night)

The user asked: "make the next 100 rows from the sheet", then "and from the starting". So the run covered the first
100 rows without a Doc from row 2: rows 2–15 and 22–107. Paid PW AI, Gemini 3.7, no spend cap.

**Free size check first:** 15 rows are a whole unit in one tab of 900–3,743 paragraphs: 2–5, 7, 8, 11, 13, 73–76, 78–80.
- The formatting call caps its answer at 12,000 tokens (about 600 choices), so those rows needed a fix.

**Runs** (all show in the app's Runs page)

| Run | What | Made | Time | ₹ (recorded) |
|---|---|---|---|---|
| 5 | 85 normal rows, from the installed app (3 at a time) | 84 + 1 failed (row 86) | 43 min | 180.89 |
| 6 | 15 long rows, project code (`_build/bscn_long_rows_20261124.py`, 2 at a time) | 12 + 3 failed (rows 2, 3, 7) | 69 min | 249.22 |
| 7 | Try again for rows 2, 3, 7 (`_build/bscn_retry_20261124.py`) | 3; the Docs of run 6 reused, no new formatting | 3 min | 24.15 |
| 8 | Try again for row 86 | 1 | 1 min | 1.28 |

- Not recorded in run 6 (old code): rows 2/3/7 formatting, ₹30.16. Not recorded in run 5: row 86's first formatting
  (small).
- **Total ≈ ₹487** for 100 notes.
- Sheet check: rows 2–107 all have a distinct Google Doc link in P (105, with the 5 from earlier). Next: row 108.
- Sign-in: after the reinstall, the app's one-time silent restore failed and a sign-in press timed out. Claude restarted
  its own idle copy of the app and the restore worked.
  - Suggestion: retry the silent restore later instead of only once.

**Fixes in the project code** (NOT yet in the installed app, not committed)
- `word_source.style`: a reference over 500 paragraphs (`STYLE_PART`) is formatted in parts.
  - Each part is saved as `word_styles_part-NN.json`, so a retry pays only for missing parts.
  - Each part may only choose for its own paragraphs; a reference of 500 or fewer still makes the one call it always did.
- `gdoc_make.export_tab`: accepts only a complete PDF (`complete_pdf`: `%%EOF` and pypdf opens it).
  - Google's export of three ~100-page tabs came back cut short, and the quality step failed on "EOF marker not found".
- `gdoc_make.reusable`: a retry in a folder whose earlier attempt wrote and checked the Doc carries on with that Doc
  (same source revision, still in Drive); it is checked word for word again. No duplicate Docs.
- `gdoc_service`:
  - A sheet run's "Try the unfinished rows again" (`retry_sheet`; payload `again_from` copies each row's saved folder).
  - A failed row reports its paid formatting (`spent.json`) and a short reason, e.g. "Google answered 400: …".
  - Full errors go to `~/.note_studio/logs/gdoc.log`.
- Tests: 428 passed, 19 skipped (+3 in `test_typeset_word.py`, +4 in `test_gdoc_app.py`).

**Quality flags, checked by hand.** Content is fine everywhere; the word-for-word check passed on every Doc.
- Checker misreads (the source and the Doc have the spaces): rows 8, 54, 57, 60, 86, 103 and 104, plus row 2's
  "diamond-shaped" (a normal line-end hyphen).
- Source issues, kept: row 2 "opposites.directional" and "human bod"; row 3 heading joined into a bullet and a
  "460 × 300" string; row 11 a bullet ending in ","; row 23 "Microminererals".
- Layout points worth a look:
  - row 31: a small diagram;
  - row 38: a heading left at a page bottom, above its table;
  - row 39: ion signs breaking in narrow table cells, and one bullet alone on the last page;
  - row 57: a flowchart split across pages.
- Gates: compare `_build/before_bscn_100_20261124.json` → `after_…`: nothing lost. 1,709 new files (runs 5–8);
  changed: the database and the token.

## B.Sc.-style Google Docs from the app (New notes + Sheet automation); 5 "Bsc Nursing Notes" rows made — 24 November 2026

The user reported that B.Sc.-style notes "were not made directly from the app", and that Sheet automation had no
option for them either. Both were true: the 14 Nursing Docs of 16–17 Nov were made by `_build/` scripts.
They then gave the sheet ("Notes Testing") and asked for 5 notes first, then the app. Their words: "the doc file
content doesn't miss, that was already a final notes, I have to just design it like you did for B.Sc. Nursing".

**The 5 notes** (paid, Gemini 3.7, no spend cap; ₹13.73 in all)
- The sheet has a new tab, **"Bsc Nursing Notes"** (gid 689523849, rows 2–879).
  - Its links are plain hyperlinks; Sheet1 used smart chips.
  - Every source Doc there has **one tab**.
- Free preflight of rows 2–30 (`_build/nursing_bscn_batch_20261124.py preflight`):
  - rows 9, 12, 14 and 15 have exactly the content of Docs already made from Sheet1;
  - rows 2–8, 10, 11 and 13 are the Sheet1 units merged into one long tab (row 3: ~2,200 paragraphs, ~120 pages);
  - rows 16, 17, 18, 20 and 21 are the first topics never made, so those were made.
- Results:

  | Row | Topic | Pages | Quality | ₹ |
  |---|---|---|---|---|
  | 16 | Clinical Enzymology | 13 | passed | 3.57 |
  | 17 | Introduction to Nutrition | 24 | reviewer misread "ofthe" (the PDF text and source have "of the") | 4.60 |
  | 18 | Carbohydrates & Energy | 9 | passed | 1.97 |
  | 20 | Proteins | 8 | passed | 1.79 |
  | 21 | Fats | 8 | passed | 1.80 |

  - All rows: word-for-word check OK, watermark and logo on every page, link in column P, editable by anyone at pw.live.
  - Summary file: `output/gdoc/bscn_batch_20261124.json`.
- **Lesson:** the batch shared one Google client across threads. httplib2 is not thread-safe, and sharing rows 20–21
  failed with an SSL record-layer error; they were finished one at a time.
  - The app's maker now gives every thread its own clients (`gdoc_make.Google`).

**In the app now** (new note type `typeset_gdoc`, "Google Doc with tabs (B.Sc. style)", Gemini 3.7 only)
- `note_studio/typeset/gdoc_make.py`: the make script's steps as a library.
  - Steps: read the source, AI formatting per tab, copy the template, write, verify, last-page fixes, watermark/logo
    check, quality, optional domain sharing.
  - The template record is `~/.note_studio/gdoc/template.json`, seeded with the 34-tab template.
  - Plain-words `GdocError`; a Doc left half-written by an error is put in the bin.
- `note_studio/gdoc_sheet.py`: reads a tab's rows (hyperlink, chip, linked text or plain link) and writes a link.
  - It never writes over a filled cell or into a row whose link changed.
  - A missing "Google Doc (B.Sc. style)" column is added only if `sheets_bridge.columns.Layout` reads the tab the same.
- `note_studio/gdoc_service.py`:
  - New notes: paste Google Doc links (one Notes-lane run each, source `gdoc`; Try again works).
  - Sheet runs: queue kind `gdoc_sheet` in the Sheets lane; 3 rows at a time; one run with a row table;
    Stop after these rows.
- Pages:
  - New notes asks for "Google Doc links" for this type.
  - Runs shows "Open the Google Doc".
  - Sheet automation has a "Google Docs from a sheet (B.Sc. style)" card: Check rows (free), Make Google Docs.
  - Settings → Categories has "Offer Google Docs" for B.Sc.-style categories (`.bak` first).
  - Tour `2026-11-24-gdoc-bsc`; guides for New notes, Sheet automation and Settings.
- Free real check, read-only: the app code read the real tab (column P; rows 16–21 done) and read the Vitamins source.

**Tests and gates**
- Tests: 421 passed, 19 skipped (404 + `tests/test_gdoc_app.py` 17). `test_note_types` lists the new ready type.
- `data_guard` compare (`_build/before_nursing_5_20261124.json` → `after_…`): nothing lost, no original touched.
  The only change is the Google token refresh.
- `drift` says STOP on this Mac only because the originals (`~/Downloads/shortnotes-core` …) are not here.
  `git diff HEAD -- engines/` is empty.

**Open**
- Rows 2–8, 10, 11 and 13: remake them from the merged one-tab sources, and if so, split them into one tab per topic?
  It is the user's call. One ~120-page tab is a large single formatting/quality call.
- **No Settings step any more** (after the install, the user asked "why it was not directly on sheet automation page").
  - The card said "No category makes Google Docs yet": this Mac had no Nursing category, and a category had to switch
    Google Docs on in Settings first.
  - Nursing was added to this Mac's data the way the Settings form does (`category_admin.add`): 30 subjects from the
    "Bsc Nursing Notes" tab, English/Hindi/Hinglish, both B.Sc. types. The running app showed the card at once.
  - Code: `categories.gdoc_offered()` / `type_ids()`. A category whose B.Sc.-style notes carry the PW logo (Nursing,
    any category added from Settings) offers Google Docs by itself; Settings can switch it off (`"offer_gdoc": false`).
  - The built-in B.Sc. Wallah keeps its unbranded look until switched on, since the Doc template carries the PW watermark.
  - Sheet automation has a "Google Docs from a sheet (B.Sc. style)" button at the top. Tests: 421 passed, 19 skipped.
  - Pushed for the Windows build (`c6a9efb`).
  - Rebuilt and reinstalled on the Mac at the user's request ("yes rebuild and reinstall it"):
    - The earlier build was kept as `dist/NoteCraft-20261124a.{app,dmg}`.
    - New `dist/NoteCraft.dmg`: 1,350,575,867 bytes, SHA-256 `aabac36627138434b3c32ecf1a1ba3fdf3963d9a66e9a12d6cd59b75a4911340`.
      Log: `_build/build_macos_20261124b.log`.
    - Claude quit the idle NoteCraft it had opened itself (PID 75023).
    - Backups: `note_studio-before-install-20261124-214648.db` (ok, 4 runs), its categories, and the replaced app
      `NoteCraft-installed-20261124a.app` in `~/NoteStudio_backups/`.
    - The installed app is identical to the build; PACKAGE_OK; reopened. The live pages show the top button,
      Nursing in the card, and the Google Doc type on New notes.
    - `data_guard` compare: nothing lost. New: the Nursing pack and `gdoc/template.json`; changed: the database and token.
- Template Doc: shared with PW as viewer on 24 Nov, so other installs can copy it.
- **Installed on the Mac (same evening, the user said "yes install it and share the template with pw.live"):**
  - The user quit the old app; it was idle.
  - Backups in `~/NoteStudio_backups/`: `note_studio-before-install-20261124-210308.db` (integrity ok, 4 runs),
    `categories-before-install-…`, and the old app `NoteCraft-installed-before-20261124.app`.
  - `ditto` of `dist/NoteCraft.app` → `/Applications/NoteCraft.app`; `diff -rq` shows it identical; PACKAGE_OK.
  - Opened; port 3100 serves the new card, switch and guide; still 4 runs.
  - `data_guard` compare: nothing lost. Changed: `note_studio.db` (app start) and the token; new: `gdoc/template.json`.
- **Template shared:** the domain is now a viewer (link only, not discoverable). Google lists it under PW's primary
  domain `physicswallah.org`, which covers pw.live accounts.
  - So `share_with_domain`'s "already editable" check, which compares with "pw.live", never matches: it re-creates
    the permission, which is harmless.

**Mac build** (the user asked "build the app")
- The previous build was kept as `dist/NoteCraft-before-20261124.{app,dmg}`.
- `packaging/build_macos.py --dmg` gave `dist/NoteCraft.app` (PACKAGE_OK) and `dist/NoteCraft.dmg`:
  1,353,318,287 bytes, SHA-256 `0156c5ae6b857c289fa3fda9eb3123f9ff7419aed280910073cbdca93e4499e3`.
  Log: `_build/build_macos_20261124.log`.
- The bundled runtime, run on a scratch data folder, showed the new New notes page, Sheet automation card and
  Settings switch (PACKAGED_PAGES_OK).
- The version label is unchanged; only the Windows builder bumps it.
- Committed and pushed to `codex/macos-windows-compatibility` so the user can build the Windows installer.

## All 14 Nursing Google Docs made, shared with PW, last-page fixes — night of 16–17 November 2026

**Where things stand**
- All 14 rows of "Notes Testing" (rows 2–15) have their Doc in column P "Google Doc (B.Sc. style)". Totals: 115 tabs, 994 pages.
- Every tab's exported PDF shows the PW watermark and the header logo on every page.
- The quality check passed 103 of 115 tabs. The other 12 were checked by hand:
  - Source issues, kept exactly because content is locked:
    - UNIT-1 "produce", "human bod";
    - UNIT-2 "Respiratory bronchioles" has no bullet in the source, and the heading "Mechanism of Airflow…" is joined into a bullet in the source;
    - UNIT-3 "Sublingual papill";
    - UNIT-4 "Reply NEXT for .";
    - Unit 9: a missing ")";
    - Unit 10: a bullet ends with ",".
  - Reviewer misreads (the PDF text and the source have the space):
    - "ofthe";
    - "Attachmentfor";
    - "Wallthickness";
    - "Coat(".

**Sharing**
- The user asked that anyone at Physics Wallah can edit.
- `_build/nursing_share_20261116.py` adds a Drive domain permission: pw.live, writer, not discoverable.
- The batch's `finish` now does this after writing the sheet link.
- Report: `output/gdoc/sharing_20261116.json`.

**Edit guard**
- Uses Drive revisions (`make.edited_since`), not modifiedTime. Sharing bumps modifiedTime, which made the first fix run skip every note.

**Fixes after the batch** (formatting only; each was tested on throwaway Drive copies first)
- **Carbohydrates "CₙH₂ₙOₙ".** Lato has no ₙ or ₘ. `glyph_styles` sets characters missing from Lato in Noto Sans.
- **Blank last page** (UNIT-4 tab 10, UNIT-5 tab 1, Unit 9 tab 1).
  - Cause: the page was full to the footer, so Google's required last empty paragraph started a page of its own, even at 1 pt.
  - Text ending: `end_requests` now joins that paragraph to the last text paragraph. Google keeps the text paragraph's style, bullet and spacing.
  - Table ending: `full_page_requests` removes the first of a doubled spacer above the last table (Google refuses to delete the spacer touching the table), or shrinks a single spacer to 1 pt.
- **Last page holding only a table's last row or a box's last line** (UNIT-1 tab 9, UNIT-2 tab 13, UNIT-3 tab 10, Unit-6 tab 5).
  - `last_table_break_requests` sets pageBreakBefore on the heading above that table, or on the spacer above a box.
  - `make.fix_last_page` keeps the break only if no page is added, the last page is no longer a sliver (`last_page_nearly_empty`, under 5% of the page) and the page it left stays at least half full (`page_fill`). Otherwise it undoes the break.
- **Both last-page fixes now run automatically** in `make.quality_all` (`result["blank_last_page"]`).
  - `_build/nursing_pages_20261117.py` applies them to Docs already made.
  - Logs: `_build/nursing_fix_20261116.log`, `_build/nursing_pages_20261117*.log`.
- **Re-check script.**
  - It removes `tab_reviewed.pdf` once used. A stale one from before UNIT-1's logo rebuild made all 12 UNIT-1 tabs be reviewed again (₹11.83).
  - It now re-checks tabs whose earlier check did not finish (UNIT-1 tab 7 lost the network).

**Cost**
- Notes as made: ₹261.92.
- Tonight's fixes and re-checks: ₹21.38 (₹13.30 + ₹5.09 + ₹2.99).

**Gates, tests and what is not done**
- Gates: `data_guard` snapshot `_build/after_nursing_batch_20261117.json` against `before_nursing_gdoc_20261116.json`. Nothing lost; no original touched. Note Studio's own database and sheet token changed. Drift is clean.
- Tests: `test_typeset_gdoc.py` has 32; the full suite has 404 passed, 19 skipped.
- Not done, waiting for the user's go: the note type and Automation page in the app, a `whats_new` tour, and a commit.

## PW watermark on every Nursing Google Doc (template) — 16 November 2026 (evening)

The user asked for the NEET/JEE PW watermark in every note, as the default for all future runs.

- **Google limits.** The Docs API cannot add a watermark or copy a tab (live description, revision 20261109:
  only add, delete and rename tabs). A Word file with the faded PW logo in its header converts into a native
  Docs watermark, but only on that one tab. A tab added through the API has none, and re-importing a Word
  file replaces the whole Doc.
- **What survives (scratch Docs):** the watermark stays through B.Sc. page setup, our header bar written
  into that tab's header, the footer, content, renaming, copying the Doc and deleting other tabs.
- **Design: every note is a copy of a template whose tabs all carry the watermark.**
  - `gdoc_writer.make_template` uses the engine's own `notes_doc.faded_logo` at the
    `pdf_branding.watermark_opacity()` strength, 124 mm, centred.
  - The person duplicates the template's first tab once in Google Docs (tab menu > Duplicate).
  - `create_from_template` copies the template, deletes unused tabs and renames the rest. A note that needs
    more tabs than the template has stops before writing and says how many to add.
  - `header_footer` now empties and reuses an existing header and footer, never deleting them, because
    the watermark goes with the header.
  - `has_watermark` checks every exported page; the quality step records it per tab (`result["watermark"]`).
- **Template:** "NoteCraft – Nursing notes template (PW watermark)",
  https://docs.google.com/document/d/1VOEMzskaheh-nPBz7e7oRFIboLkdSb3q20zJQbvgVek/edit
  (`output/gdoc/template.json`). It has 1 tab, checked to carry the watermark.
- **Waiting for the user:** duplicate the template's tab up to 30 tabs. Then run
  `make.py --check-template` and `make.py --new-doc` (formatting is cached; the quality check about ₹15),
  run `sheet_link.py`, which replaces P2 with the new Doc's link, and bin the old UNIT-1 Doc, which has no
  watermark.
- Tests: `test_typeset_gdoc.py` has 25.
- **Update, same evening.** The user duplicated that template to 34 tabs, but rebuilding UNIT-1 from it lost
  the watermark twice. Scratch copies showed two Google behaviours:
  1. A footer created while the header is empty drops the tab's watermark; the header must be written first.
  2. Any picture inserted into the header through the API drops the watermark.

  The user wants both the corner logo and the watermark. A converted Word header keeps both, so
  **template v2** builds the header bar (labels, navy rule, PW logo), the footer (labels and a live PAGE
  number) and the watermark into the Word file (`watermark_word_file(branding)`). Notes from a branded
  template never touch the header or footer (`template.json` `"branded": true`).
  - Template v2: https://docs.google.com/document/d/1rzEPDvwy1hiDU77yvalRZ6K6nKPczesnmcTMk9bkTKk/edit
  - Checked on scratch copies: watermark, logo and page number survive page setup, content, copy and tab deletion.
  - v1 (34 tabs, no logo) is kept as `output/gdoc/template_v1_no_logo.json` until v2 is duplicated.
  - Writer guards for unbranded templates: header written before the footer; no header picture when watermarked.
  - The title clears the bottom border that Word's "Title" style brings.
  - `has_header_logo` checks the logo on every page.
- **Superseded later that night: no second duplication needed.** The user objected to duplicating again,
  saying NEET/JEE/Foundation get the logo and watermark automatically. More scratch tests found that the
  user's existing 34-tab template (v1) can carry both, written by the writer, if:
  1. the logo goes into the header bar's right cell in its own step, after the bar exists;
  2. the header is written before the footer is created;
  3. the template header's paragraphs are never deleted. Duplicated tabs' headers hold two empty
     paragraphs; deleting the first drops the watermark.

  `_segment` now removes only an earlier bar table. Template v2 (one tab) was binned; template.json is v1
  again (`branded: false`). Reads now retry on 429 too (`_get`), and `_execute` waits up to about 10 minutes.
- **Batch (user: "create all tonight"):** `_build/nursing_batch_20261116.py`.
  - `preflight` is free. All 14 sheet rows passed: up to 20 tabs; nothing that cannot be copied.
  - `run` makes notes 3 at a time with the make script (`--source-id/--out-name`) and writes column P only
    after the read-back, watermark and header-logo checks pass on every page.
  - Summary: `output/gdoc/batch_20261116.json`; logs: `_build/batch_20261116_*.log`.
  - The Lipids canary showed the paragraph-deletion problem; that Doc and the logo-less UNIT-1 Doc were binned.
- Tests: 27 in `test_typeset_gdoc.py`; full suite 399 passed, 19 skipped.

## Nursing Google Doc: closer to the B.Sc. reference, content strictly the source's — 16 November 2026 (later)

The user shared `Lec-14.pdf` (B.Sc. Wallah reference) and `UNIT - 2 Notes.pdf` and asked for more format
improvement, then for a rewrite of the UNIT-1 output. They added: "content must be similar to the reference
doc, only formatting should be implemented".

- **`gdoc_writer.plan()`** decides the presentation; words are never changed.
  - Title and kicker: the tab's first heading becomes the centred navy title; a short line before it
    (the course line) becomes a small grey kicker.
  - Labelled boxes: a source label ("Definition:"), or a short subheading leading straight into a box,
    moves into the box's title bar. This also fixes a heading left alone at the bottom of a page,
    because Google never keeps a paragraph with a table that follows it.
  - Key sections: "Important Points", "Key Points" and "Definition" sections become boxes with real
    bullets.
  - **No template labels.** A box without a source label has no bar. "Note" and "Key Concept" were
    briefly added and then removed at the user's request.
  - Key terms: bold inside a sentence is navy; a bold lead label stays black; headings lose the source
    underline.
  - Formulas: simple `$...$` notation shows with real superscripts and subscripts ($Ca^{2+}$ → Ca²⁺).
    Prices like `$5` are left alone.
  - Tables: booktabs; short columns centred; the header row repeats on each page.
  - Header and footer: split 1×2 bars, with the PW logo on the right of the header.
- **Box layout:** one cell. The bar is the first paragraph, shaded, with side borders in the bar colour
  (Google shades only between indents), and set to keep with the next paragraph.
  - Scratch Docs showed a two-row box leaving its title alone at the bottom of a page; the one-cell box never did.
  - Tables and boxes go into a fresh, unindented paragraph, because Google indents a new table like its
    host paragraph (a following list item pushed a box inwards).
  - The empty paragraph after a tab's last table is made 1 pt, because it caused a blank last page.
- **verify is strict:** besides the word-for-word source check, it rejects anything added except a list
  item's own number or bullet.
- **UNIT-1, same Doc and link** (rewritten in place; Drive modifiedTime checked first):
  - all 12 tabs pass the read-back check;
  - quality: 9 tabs passed. Tabs 4 and 7 have only "ofthe" misreads (the source and the PDF text have
    "of the"); tab 11 has the source typo "human bod".
  - `_build/nursing_unit1_20261116_recheck.py` re-reviews only tabs whose pages changed, compared pixel
    for pixel with the reviewed PDF (`tab_reviewed.pdf`).
- **Cost for UNIT-1 including all improvement passes: ₹66.11.**
  - Formatting: ₹14.43, once; it is cached.
  - Quality: ₹14.09 + ₹15.07 + ₹1.54 + ₹15.08 + ₹5.90.
  - A fresh unit should be about ₹29: one formatting pass and one quality pass.
- **Tests:** `test_typeset_gdoc.py` has 23 tests. Full suite: 395 passed, 19 skipped.
- **Gates:** compare with the session baseline: nothing lost, no original touched.
  - Changed: the Google token (refresh), and `note_studio.db`, through the running installed app's own
    `update_checked` at 16:27:08. No script of this session ran then; integrity OK.
  - Drift against HEAD: 0 changes. Nothing committed or built.

## Nursing notes as editable Google Docs with tabs (B.Sc. style): first unit made — 16 November 2026

The user asked for Nursing notes as editable Google Docs, one tab per topic, in the B.Sc. look, made from the
sheet. They approved one paid note (UNIT-1) using Gemini 3.7 only (3.6 does not work), with no AI spend limit.
Docs must belong to their own account (prashant.kumar15@pw.live, which is NoteCraft's sign-in on this Mac).

- **Why a new writer:** uploading a .docx with conversion (`engines/shortnotes/sheets_bridge/notes_doc.py`)
  makes a single-tab Doc, so tabs need the Google Docs API. The user enabled the Docs API in the sign-in's
  Cloud project 195954488363. The existing Drive scope covers it, so nobody signs in again.
- **`note_studio/typeset/gdoc_source.py`:** reads a tabbed Google Doc literally, one locked source per tab,
  in word_source's shape. It keeps every paragraph, list item (level, numbering), table (merges), 1×1 table
  (as a box) and picture (inline and wrapped). The source's bold/italic/underline/links are kept as spans.
  Layout tables holding pictures are laid out in reading order. An equation, a drawing, a footnote or a
  date chip fails before any AI call.
- **`note_studio/typeset/gdoc_writer.py`:** writes one tab per topic.
  - Each tab is built by inserting its blocks in reverse at index 1, so every range is exact.
  - B.Sc. look from `render.py`: A4, the B.Sc. margins and faint-blue page, Lato, TITLE/HEADING_1/HEADING_2,
    native lists, booktabs-like tables, boxes as one-cell tables (frame colour, tint, 3 pt left rule),
    centred pictures, and a per-tab header ("Nursing | Nursing Notes", navy rule, PW logo) and footer.
  - A numbered list split by other content gets its source numbers as text, because Google would restart
    the numbers of each part.
  - Local pictures (the PW logo) reach Google through a temporary converted Doc's own picture links, so
    nothing is shared publicly. The temporary Doc is binned afterwards.
  - `read_back` and `verify` prove every tab word for word against the source.
  - Retries on 429/5xx; `clear_tab` allows an in-place rewrite.
- **Google facts learnt:**
  - Setting a paragraph's named style resets its runs' text style, so paragraph style must come first.
  - `/document/d/<id>/export?tab=t.0` gives 400; `/document/export?id=<id>&tab=<tab>` exports exactly that tab.
  - A plain request to a picture's contentUri never connects from this Mac; the signed-in request takes 1–2 s.
  - Per-account limits apply to PDF exports (429).
- **AI:** `word_source.style` unchanged (appearance only, words locked), 4 tabs at a time;
  `source_quality.review` runs on each tab's PDF export, 4 at a time.
- **Made:** source "UNIT -1 A&P" (Doc 1U-3hlO2rnFC6HfB9MNAdOR5V5SAckV3yzkVfBQAR7CQ; 12 tabs, 144 pictures).
  - Output: "UNIT -1 A&P – Nursing Notes (B.Sc. style)",
    https://docs.google.com/document/d/16tlGMyIzKDxAIS_fLTHtz0JvNrTA9SbCChfKdhrdtkM/edit
  - Read-back: all 12 tabs OK.
  - Cost: ₹14.43 formatting (12 calls) + ₹14.09 final quality (12 calls, 138 pages) = **₹28.52**.
  - Quality: 6 tabs passed. The real findings are all in the source and were kept: raw `$HCl$`, `$T_3, T_4$`,
    `$Ca^{2+}$`, "human bod", "The pronator teres produce", an "Elbow bending" label in a source picture.
    The 9 "missing space" findings are reviewer misreads (spaces checked in the source, the Doc and the PDF text).
- **Sheet:** "Notes Testing" (1g8hTDHf868PNOWe8A3V3L85PDwla_38sOkg-Cub3GLU), Sheet1.
  - New column P "Google Doc (B.Sc. style)"; P2 holds the link. The row was found by its Docs smart chip
    (the sheet links each unit by chip, not hyperlink).
  - The copied automation's `Layout.from_header` reads the tab identically after the change.
- **Scripts (gitignored `_build/`):** `nursing_unit1_20261116_make.py`, with `--feature-test` (free scratch
  Doc, binned) and `--rewrite`, and saving each paid step per tab under `output/gdoc/nursing-unit1/`.
  Also `nursing_unit1_20261116_sheet_link.py`. Do not rerun the make script just to check: read `result.json`.
- **Tests:** `tests/test_typeset_gdoc.py` adds 13, including a Docs index simulator for the reverse insertion
  and spans. Full suite: 384 passed, 19 skipped, 1 failed. The failure,
  `test_practice::test_a_paid_step_without_sign_in_holds_the_queue`, passes alone (timing).
- **Gates:** compare `_build/{before,after}_nursing_gdoc_20261116.json`: nothing lost, no original touched;
  the only data change is the refreshed Google token in `~/.note_studio/sheets/tokens/`. Drift against HEAD:
  0 changes in all four engine copies.
- **Not done (waits for the user's go):**
  - a note type plus a sheet runner that reads chip links, makes rows 2–3 at a time and writes the link back;
  - Automation page wiring, `whats_new` tour and guide;
  - rendering the source's raw `$…$` formulas as sub/superscripts.

  Nothing committed or built.

## PW logo and watermark on Nursing (B.Sc.-style) notes; Mac app rebuilt and installed — 4 November 2026

User asked: Nursing notes (B.Sc. template) get the PW logo in the corner and the
watermark, as NEET/JEE PDFs; then build and install.

- `typeset/render.py`: `Branding.pw_logo`. When on, every page gets the PW logo
  11 mm in the top-right corner (5 mm in) and the 124 mm faded logo watermark in
  the centre, under the text. Both are made by the shortnotes engine's own
  `sheets_bridge/pdf_branding.py` helpers from `engines/shortnotes/assets/watermarks/center-logo.png`,
  with the same watermark strength (`SHEETS_BRIDGE_WATERMARK_OPACITY`, default 0.10).
  The header's right label and rule end 6 pt before the logo. Words are
  unchanged and renders stay byte-identical, so quality receipts can still be reused.
- Category data decides: `categories.pw_logo(pack)`. `branding.pw_logo` wins; otherwise a
  category added from Settings (Nursing) has it and a built-in pack (B.Sc. Wallah) does not.
  Settings → Categories shows "Add/Remove the PW logo" for packs that make
  `typeset_notes` (POST `/settings/categories/{id}/pw_logo`, `.bak` first).
- What's-new tour `2026-11-04-nursing-pw-logo`; New notes and Settings guides updated.
- Tests: new `tests/test_typeset_pw_logo.py` (12); full suite **372 passed, 19 skipped,
  0 failed** (61 s). Drift gate: engine copies match except the earlier allowed changes.
- Mac: previous build moved to `dist/NoteCraft-before-20261104.app`; new
  `dist/NoteCraft.app` built (PACKAGE_OK) and copied to `/Applications/NoteCraft.app`
  (PACKAGE_OK again from there). The bundled runtime rendered a Nursing PDF with both images.
  Version label is still the repo's `2026.10.26.1` (only the Windows builder bumps it).
  Not committed. No Windows installer built.

## Nursing UNIT-2 completed; NoteCraft 2026.10.30.4 installed — 30 October 2026

Latest explicit user instructions: use the B.Sc. template for Nursing, keep all
reference content, add Nursing as a separate category, make this first before
700+ notes, AI authorized, and apply all relevant NEET/JEE quality checks.
Input: `C:/Users/Laptop/Downloads/UNIT - 2.docx`, Respiratory System, SHA-256
`2aeff711c60b4d9fecc16dca944deed4d70cf9a705f399cea3c5833392463d77`.

**First Nursing PDF DONE and linked in commentary:**
`output/pdf/nursing-unit2/UNIT - 2 Notes.pdf`, 10 pages, all **149 nonempty
paragraphs, 2 complete tables, 7 original embedded images** retained. Source
audit verifies 158 ordered source IDs, rendered wording, table rows and image
hashes. Independent python-docx comparison also confirms all paragraphs and
table cells. All ten PNGs inspected twice after grouping adjacent callouts;
the final paid-reviewed PDF matches all ten inspected PNGs pixel for pixel.
No clinical/source rewriting, summarizing or deletion. Metadata in this sample
predates the subsequent deterministic-PDF metadata change; do not regenerate
it and pay for another visual check unnecessarily.

Actual paid calls (authorized by user): one formatting call ₹1.5034, one final
quality call ₹1.1178, total **₹2.6212**. AI selects source IDs and styles only.
`ai_initial_result.json` retains the first call's result; `result.json` currently
records the later quality call. `word_styles.json` and `final_quality_review.json`
retain both responses with provenance. The final quality report PASSED all
10 pages with zero blocking/advisory issues and no text edits. Checked PDF hash:
`c68868a0f1676c92786e3ce52678a549f39b300fc5b8c2828688a4318d8c6be5`.
Source file was not changed. Sample maker `_build/nursing_unit2_20261030_make.py`
refuses a repeat paid formatting request without a saved cache. Do not run it
again merely to verify output: read the saved artifacts.

**Nursing category added through the actual Settings POST route**, not manual
configuration edits. `~/.note_studio/categories/nursing`, 8 subject choices,
English/Hindi/Hinglish, typeset_notes. Branding derives Nursing/Nursing Notes
from the category label. Helper `_build/nursing_unit2_20261030_category.py`
checks for an existing pack before adding, so does not duplicate it.

**Implemented and installed:**
- `typeset/word_source.py`: ZIP/XML extraction including inline AND anchored
  images, native lists (including inherited styles), source heading roles,
  tables and all text in order. AI can return only IDs/roles/colours. Unsupported
  shapes/equations/text boxes/header/footer academic text fail before billing
  with a PDF-export instruction; nothing silently omitted.
- `typeset/source_quality.py`: same NEET/JEE QualityReview schema/version and
  deterministic notation/chemistry check helpers, plus source coverage/order,
  figures, terminology, language, scientific clarity and every-page visual
  review of the actual final PDF. Full-reference policy overrides concise
  content exclusion: retain questions, worked examples and repetition.
  Concerns/suggestions are reported without changing source words. Receipt
  binds source, notes, PDF and prompt hashes. An incomplete review cannot pass;
  exact cached reviews avoid repeat billing. No edits in copied engines.
- Typeset accepts `.docx` as well as PDF. Word AI chooses appearance only; PDF
  still uses literal page-by-page transcription by default. Full final quality
  check is default for both; no checkbox. New Settings.full_quality=False exists
  only for narrow unit tests/canonical internal controls, not UI.
- Renderer supports literal source tables across pages, audits each row, groups
  adjacent same-style source callouts while keeping each source ID, and omits
  creation-clock PDF metadata so identical renders reuse a paid quality receipt.
- Failed/Needs-attention Retry copies matching transcript/style/quality caches;
  exact source/prompt/PDF checks apply at consumption. Registry/form allow Word
  references and multiple files; each file becomes one Smart Queue run.
- Guides, tour, README and upload explanation include Word inputs and automatic
  NEET/JEE final quality review.

**Tests:** final focused suite **97 passed in 76.37s**: Word uploads, complete
source text/images/tables, table pagination, source order, literal markup, Hindi,
inline formulas, cached failed retries, exact-file quality receipts, incomplete
review rejection, no source rewriting, legacy B.Sc., pages and smaller edition.
Log/XML: `_build/nursing_unit2_20261030_finaltests.*`. Pyflakes clean. An earlier
metadata reproducibility attempt failed; corrected with a stable FPDF output
producer and verified identical rerenders in the final passing suite. An
unrelated queue timing test failed under packaging load, then passed isolated
in 23.24s (`bsc_ai_default_20261030_queue_recheck.*`). No engine copies edited.

**Built and installed .4 (smaller edition, overlay on .2):**
- `dist/team/2026.10.30.4-noupsc-nopwbooks/NoteCraft_Setup_NoUPSC_NoPWBooks_2026.10.30.4.exe`,
  **194,981,258 bytes**, SHA-256
  `221fb79cd8c0a7898221c1312eab02765095628ed8284c170e37446c4a2b7129`.
  Independently verified both aliases/checksum file, staged source hashes and
  all ten unchanged template fonts. All dependency/startup/mock/cleanliness
  build checks passed (48 imports for each included runtime).
- .3 was built but not installed before the Nursing expansion. Its original
  manifest is archived in `_build/bsc_ai_default_20261030_release3_verification.json`.
  .4 reused its staged libraries with `--resume-work`; the work folder remains
  `_build/2026.10.30.3-noupsc-nopwbooks-pvqr79po`, containing the .4 package and
  final verification.json. Use the .4 check helpers, not a glob for a .4 stage.
- Fresh SQLite backup + full local data archive in
  `_build/install_update_20261030_4/` before the idle launcher's normal WM_CLOSE:
  **27 runs, 161 items, 575 events, 27 queue entries and 155 other saved files**.
  Installer finished **17:55:14 IST**, exit 0, no Windows restart.
- Reopened installed NoteCraft: launcher **27100**, server Python **11396**,
  http://127.0.0.1:3100. API/update status report **2026.10.30.4**. All **315 shipped
  app files** match staging byte for byte; 12 live pages passed, including
  Nursing and B.Sc. Word uploads, hidden old mode/transcription choices and
  automatic quality explanation. Existing UPSC/PW Books folders retained.
- Both staged AND installed private Python/libraries processed the actual
  UNIT-2 bytes using the real saved paid style response: all 149 paragraphs,
  two tables and seven images verified, all ten pages pixel-identical to the
  inspected/paid-checked PDF. Its actual final-quality receipt was reused with
  **zero new model calls**. Evidence:
  `_build/install_update_20261030_4/{staged,installed}-nursing-check/packaged_check.json`.
- Immediately after installation all 27 old runs and every saved non-settings
  row matched the fresh backup exactly; all 155 saved files unchanged. Only
  `update_checked` and `whats_new_state` setting values changed. Integrity OK,
  no foreign-key errors. Detailed records in the installation work folder.

**Earlier approved original-lecture test also finished, without charging twice:**
Normal installed-app Retry of failed run27 created **run28**. Reused 20 checked
pages and page21's already-paid proposal; only its review and pages22–25 were
read, followed by one final quality call. All25 original pages accounted for,
79 ordered items, eight output pages, 25 reference boxes, zero source-audit
problems. All eight final PNGs inspected. Final quality reviewed 8/8 pages,
zero blocking/advisory issues. Its overall **Needs attention** is the required
handwriting/source-transcription review flag, not a layout failure or claimed
exact transcription guarantee. Output:
`output/pdf/BSc - Frames of Reference - AI default.pdf`.
Costs: earlier ₹14.1201 + remaining ₹3.0460 = **₹17.1661 total** for this one
approved lecture test (50 source reading/review calls + one final quality call).
Run27 and its raw responses remain unchanged. `_build/bsc_ai_default_20261030_resume.json`
records the one continuation: NEVER run the submit scripts again. Final QA
record: `_build/bsc_ai_default_20261030_final_qa.json`.

**Preservation gate passed:** 5,529 → 5,602 watched files; zero data files lost,
zero JSON entries lost, zero original-project changes. The two changed data
files are the live DB (new authorized run28/update settings) and the existing
Google token refresh (entries retained). All 73 new files belong to the new
Nursing category and run28. Every pre-existing row/file in the fresh backup
still matches after run28; integrity/foreign keys pass. Drift passes, including
all 81 approved PW Books engine hashes and all 19 protected prompts. Original
Nursing DOCX SHA-256 rechecked unchanged. Logs:
`_build/nursing_unit2_20261030_{guard,compare,drift}.log`; snapshot
`_build/after_nursing_unit2_20261030.json` compared with the B.Sc. baseline.

An optional cleanup of an empty test scratch folder was rejected by automatic
approval review (`blocked by policy`); that deletion was not retried. A later
read-only check found the folder absent. The successful packaged checks used
absolute paths and separate scratch folders, with no new paid calls.

All requested implementation, first-sample generation, quality checks, install
and preservation checks are complete. No 700-file production batch was run;
only the supplied Nursing unit and the separately approved earlier lecture
were made. Use Nursing → Typeset notes → add reference files; each becomes its
own Smart Queue run. The Nursing sample's consolidated proof/cost record is
`output/pdf/nursing-unit2/verification.json` (total ₹2.6212).

## B.Sc. AI default implementation — context before the Nursing extension

The user clarified that B.Sc. notes must use AI by default, preserve the lecture
wording/order, and use the B.Sc. template. The opt-in transcription checkbox was
unwanted. This supersedes the earlier default of free local text extraction.

- `reference_ai` is now the B.Sc. default, including digital pages. New notes
  hides content modes, transcription, figure generation and optional review.
  Every page is transcribed and checked separately; the fixed renderer uses the
  reference fonts, A4 dimensions, box colours and category header/footer.
- Source content is locked after the checked transcript. Equations, tables and
  diagrams retain original crops; formula crops receive the reference formula
  frame. Hindi and inline math are drawn correctly, with serialized ActualText
  auditing for shaped text and formula images. Bare arrow commands render as
  symbols while retaining their source logical text. AI content always requires
  human source review; the renderer audit is not proof of perfect transcription.
- Reversed bounding-box corners are normalized without changing the region;
  empty or out-of-range regions still fail. Truncated model answers fail.
- Failed-run Retry copies saved transcripts only for matching PDF and prompt
  hashes. Checked pages need no new calls; a saved unfinished proposal receives
  only its missing review. Raw responses and existing outputs remain intact.

**Already installed before this final clarification:** smaller release
2026.10.30.2, SHA-256
`7985bfcb041dcabc19b674de735f31b5dde2566819689f40aff37dac2cccf881`.
The 26 prior runs and 110 saved files were preserved byte for byte; all 313
shipped app files matched staging and 11 live routes passed. Evidence:
`_build/install_update_20261030_2/`. It still exposed the transcription checkbox.

**Explicitly approved paid test, original lecture once:** run 27, original
`~/.note_studio/runs/23/input/amit sir rec 29.pdf`, 25 source pages, SHA-256
`ed6a5fbc8857707aba29c0d8b8a5eff1a277c5a79903a2b3095e3a30ce3dfad4`.
The .2 test failed on page 21's reversed paragraph bbox after 41 calls, costing
₹14.1201. It saved 20 checked page responses and page 21's proposal. Do not
resubmit the whole lecture. `_build/bsc_ai_default_20261030_cache_proof.json`
binds these responses to the approved PDF and the unchanged .2 system prompt.
The one continuation must reuse those responses and finish only nine calls.
`_build/bsc_ai_default_20261030_resume.py` submits Retry once and refuses a second
submission if its record exists. Inspect the record/run instead of rerunning it.

Preliminary checks: 74 tests passed before the final cache/math regressions were
added. The real saved 20-page subset renders six pages and 23 reference boxes,
zero source-order/wording audit failures. All six PNGs inspected; arrow rendering
then corrected and page 3 inspected again. Diagnostic files are under
`tmp/pdfs/bsc-original-diagnostic/`. Original preview is now `output/pdf/BSc.pdf`;
the IDE tab's former long filename no longer exists. No original PDF was changed.

Release .3 is building, final focused tests running. Baseline:
`_build/before_bsc_ai_default_20261030.json`, 5,529 watched files. Drift already
passes, including all 81 approved PW Books hashes and 19 protected prompts.
Pending: install .3 with fresh backup, finish the approved retry, inspect final
PDF, compare history and run the final preservation gate.

## Installed B.Sc. update 2026.10.30.1 — 30 October 2026

The user explicitly requested installation of the newly built installer. Installed
the **noupsc-nopwbooks** 2026.10.30.1 release over 2026.10.26.1 in
`%LOCALAPPDATA%/Programs/NoteCraft`. Installer SHA-256 was rechecked against the
verified `cfaa7537133242d25b7eeb257e0debf6b25c03b0332db9c08b49acaf3702dbec`.

- The app was idle. Before closing, SQLite backup plus a full data archive were
  taken in `_build/install_update_20261030/`: `installed_before.db`,
  `note_studio_data_before.zip`, and `before_summary.json`. They preserve 24 runs,
  159 items, 450 events, 24 queue entries and 96 other data files.
- Rechecked idle state and unchanged history immediately before requesting the
  existing launcher's own Quit with WM_CLOSE. No unrelated process was stopped.
- Silent per-user installer: **15:35:05–15:37:10 IST**, exit **0**, no Windows
  restart. Log: `_build/install_update_20261030/installer.log`; result JSON beside it.
- Reopened NoteCraft (launcher PID 1296). It serves http://127.0.0.1:3100;
  `/api/instance` and update status report **2026.10.30.1**, installed/current.
- All **312 shipped app files** match the staged package byte for byte. Eleven
  live pages passed, including New notes → B.Sc.; Preserve wording and order is
  selected and the no-sign-in/no-credit explanation appears.
- The **installed runtime and installed libraries** passed the real reference-PDF
  engine check and `/new` → queue → run → download flow in a separate scratch
  home: 2 pages, all 27 source items and rendered wording verified, zero AI calls
  or cost. Evidence:
  `_build/install_update_20261030/installed-source-check/packaged_check.json`.
- Every saved non-settings database row is identical to the fresh backup,
  including all 24 runs. All 96 saved data files are unchanged. Settings changed
  only `update_checked` and `whats_new_state`; no keys removed or added. SQLite
  integrity OK, foreign-key check empty. Details: `history_verification.json`.
- Guard: **5,472 files before and after**, zero lost files/JSON entries or original
  changes. Expected changes: live database and the shared Google token refreshed
  on startup (entries retained). Drift passes. The retained PW Books engine was
  reverified against all 81 original build hashes including 19 protected prompts;
  the new `_build/install_update_20261030_drift.py` uses that same approved
  reference without requiring the outer app release to stay 2026.10.26.1.

The installer overlays files and intentionally deletes nothing: pre-existing
UPSC/PW Books engine folders and libraries remain from the old full installation.
The new release metadata is the smaller edition. No older engine files, original
PDFs or saved outputs were removed, and no live paid generation or upload ran.

Final records: `_build/install_update_20261030/{installed_verification,history_verification}.json`,
`_build/install_update_20261030/preservation.log`, and
`_build/{before,after}_install_update_20261030.json`.

## B.Sc. lecture uploads: preserve source wording and order — 30 October 2026

The user found that the actual B.Sc. outputs did not resemble the earlier preview.
That preview used a hand-transcribed document and proved only the renderer. The
real runs (23 and 24) lost blocks during tidying, and run 23 incorrectly passed.
The user explicitly requested research and implementation, selected **Preserve
wording and order**, and confirmed the existing NoteCraft lecture-upload flow.

**Implemented in source, B.Sc. typeset notes only:**
- New notes → B.Sc. → upload lecture PDFs → Make notes now defaults to source
  preservation. No manually prepared JSON, Google sign-in, model call or AI cost.
  Other note types keep their existing generation paths.
- `typeset/source.py` extracts positioned digital content with PyMuPDF. Prose is
  reflowed using the reference typography and box styles; heading numbers and
  list-item boundaries are retained. Only whitespace, typographic ligatures and
  physical line-wrap hyphenation are normalized. Original wording, errors,
  examples, numbers and source language stay as supplied.
- Formulas, tables and diagrams use lossless original excerpts. Source content
  never uses the renderer's floating-figure queue. A source map records ordered
  item IDs, text, page bounds, PDF/excerpt hashes and known running-furniture
  exclusions. Missing, changed, duplicated, reordered or undrawn items, altered
  excerpts and unverified rendered text block delivery.
- Scanned, annotated and ambiguous vector/multicolumn pages are retained whole
  and produce **Needs attention**, not a claim of newly typed notes. No OCR
  transcription is claimed to be exact. A PDF containing LaTeX source code is
  rejected with instructions to compile it first.
- Optional **Rewrite into study notes with AI** retains the previous workflow.
  Raw JSON is saved before tidying; discarded content and missing Key Takeaways
  now require attention. Figure/review/language/title/instruction controls explain
  which mode uses them. The page no longer says preservation needs sign-in/credit.
- Updated registry, guide, What's new tour, README and explicit PyMuPDF dependency.

**Research and actual inputs:** all six supplied reference PDFs were reprocessed
from their PDF bytes using the real engine. Four passed; `Lect 01.pdf` and
`Lecture 14.pdf` retained unclassified vector pages and need review. The scanned
25-page `amit sir rec 29.pdf` retains all 25 pages as excerpts in a 13-page output
and needs review. `Lec. code 40.pdf` actually contains LaTeX code printed into a
PDF; it is rejected instead of being summarized into an incomplete page.
Evidence: `_build/bsc_fidelity_20261030_samples.{py,json}` and
`output/pdf/bsc-source-fidelity/`. A convenient verified sample is
`output/pdf/BSc - source preserved - Electricity and Magnetism.pdf`.
Primary references: https://pymupdf.readthedocs.io/en/latest/app1.html and
https://ai.google.dev/gemini-api/docs/structured-output .

**Tests so far:** full suite 335 passed, 3 skipped, one existing UPSC DPP timeout
(600s; the large bank was being read while other checks/build ran). That unchanged
test passed alone in 55.30s. Final focused tests: **91 passed in 45.16s**, including
real-PDF preservation, omission/reordering/duplication/edit rejection, literal
markup, lost rendered text, scan retention, changed assets, LaTeX rejection,
source bullet lines and the actual no-sign-in upload/queue flow. Pyflakes clean.
Logs: `_build/bsc_fidelity_20261030_{fulltests,source_finaltests,dpp_recheck}.*`.

**Final upload/release checks:** another **2 passed in 10.39s** after adding an
explicit PDF-download/source-record assertion. The packaged Python and packaged
libraries also processed the actual reference PDF, then performed the real
`/new` → queue → run → download flow in an isolated scratch home. Both passed,
2 pages, 27 source items verified, zero AI calls or cost. The packaged code and
PyMuPDF imports were asserted to come from the staged package, not global Python.
Evidence: `_build/bsc_fidelity_20261030_upload_final.*` and
`_build/2026.10.30.1-noupsc-nopwbooks-8wcbr0l9/source-upload-check/packaged_check.json`.

**Built 2026.10.30.1, smaller edition (UPSC/PW Books omitted as before):**
`dist/team/2026.10.30.1-noupsc-nopwbooks/NoteCraft_Setup_NoUPSC_NoPWBooks.exe`
and `NoteCraft_Setup_NoUPSC_NoPWBooks_2026.10.30.1.exe`, hard links, both
**194,966,297 bytes (195.0 MB)**. SHA-256:
`cfaa7537133242d25b7eeb257e0debf6b25c03b0332db9c08b49acaf3702dbec`.
Independently checked against SHA256.txt; both aliases and MZ headers checked.
Eight changed packaged source files match the workspace byte for byte; all
seven successfully processed input PDFs still match their recorded source hash.
All builder checks passed: NoteCraft imports 47, Notes check imports 48; package
cleanliness; launcher `--check`; 13 pages/version/update button; Notes check mock.
Log: `_build/bsc_fidelity_20261030_build_final.log`; independent verification:
`_build/bsc_fidelity_20261030_artifact_verification.json`.
One preliminary build was stopped before packaging to include the visual-QA list
fix. The normal builder pruned the completed 28 October staged package and its
verification scratch folder, retaining its records and installer.
Optional cleanup of this task's interrupted
`_build/2026.10.30.1-noupsc-nopwbooks-uzxu2mn3/package` was rejected by automatic
approval review ("blocked by policy"). It was left in place; the final installer
comes from the separate `8wcbr0l9` build.

**Preservation:** 5,472 watched files before and after, zero files lost, zero JSON
entries lost, zero original-project changes, zero new data files. The only changed
watched file was the live `~/.note_studio/note_studio.db`; SQLite quick_check is OK,
foreign_key_check is empty, and the newest run remains 24. No run output changed.
The snapshots do not establish why the live database's physical hash changed;
no logical before/after row comparison is claimed. Drift passes with only the
existing allowed differences, using the previously approved, hash-verified
installed PW Books reference (81 files, all 19 protected prompts); the guard
itself and all engine copies were unchanged. Evidence:
`_build/{before,after}_bsc_fidelity_20261030.json` and
`_build/bsc_fidelity_20261030_preservation.log`.

**Not installed or uploaded.** No paid generation. The installed app still needs
this installer before its B.Sc. upload behavior changes. Original PDFs and
existing delivered run outputs were not rewritten.

## Built smaller installer without UPSC or PW Books — 28 October 2026

The user requested a second installer without UPSC, then added “leave pw book
also for now”. Built **2026.10.28.1**, edition **noupsc-nopwbooks**, alongside
the full installer. **Build only: nothing installed or uploaded, no paid
generation.** The installed app was not started, stopped or reinstalled.

**Installer:**
`dist/team/2026.10.28.1-noupsc-nopwbooks/NoteCraft_Setup_NoUPSC_NoPWBooks.exe`
and `NoteCraft_Setup_NoUPSC_NoPWBooks_2026.10.28.1.exe` (a hard link), both
**179,068,745 bytes (179.1 MB)**, **74.6% smaller** than the full 705,006,115-byte
installer. Matching release notes, QUICK_START.txt and SHA256.txt are beside them.
Independent checks of both files match SHA256.txt and the builder record; MZ
headers. SHA-256:
`806331f7bd2e0fbb2b3b962e7c110efae786de138bab575851c4d9c773067934`.

**Included:** hand-drawn notes, B.Sc. typeset notes (all ten fonts), Sheet
automation and Notes check. **Left out:** UPSC notes, Mains practice, DPP,
PW Books, their engine copies/libraries, ffmpeg and the DPP question bank.
The staged package is **651,787,996 bytes**, with only `shortnotes` and
`notes_qc` engine folders and `notecraft` and `notes_qc` library folders.
The UPSC category pack remains data and can be seeded, but is not offered.

**Build command:**
```
python packaging\build_installer.py --without upsc --without pw_books --keep 1000 --keep-package --notes "NoteCraft without UPSC and PW Books: smaller download; hand-drawn, B.Sc. typeset notes, Sheet automation and Notes check included"
```
Started hidden in the background at 14:35 IST; ready by 14:42. Logs:
`_build/noupsc_nopwbooks_20261028_build.{log,err.log}`. Retained work:
`_build/2026.10.28.1-noupsc-nopwbooks-h7ipmb14/` (including verification.json).

**Code and edition isolation:**
- Repeated `--without` selects engine copies, libraries, import probes, startup
  assertions and mock runs. Staged release.json records `edition` and `engines`;
  smaller builds never write the source release.json. Normal full builds keep
  all engines, their original names, installer identity and output folder.
- Each engine selection has distinct installer/release-note names, dist and
  staging folders. Omitting UPSC alone still uses `NoteCraft_Setup_NoUPSC` and
  `-noupsc`; omitting both uses `NoteCraft_Setup_NoUPSC_NoPWBooks` and
  `-noupsc-nopwbooks`. Pruning recognises the edition folders while retaining
  the 27 October cleanup rules and hard links.
- Updater names come from the edition. Drive listings, saved update references,
  cached downloads and install requests reject other editions. A nameless update
  reference is rejected too. The same release folder can contain all editions.
- Note types are offered only when their engine copy exists (typeset is built
  in). New notes and AI models omit unavailable types; direct submissions and
  practice steps are blocked before they create work. `/practice` stays 200 and
  explains that Mains practice and DPP are not in this installation. System
  settings marks absent engines/types accordingly and skips UPSC setup checks.
- Updated page guides, an edition-only What's new tour and the README note.

**Validation:** **325 passed, 3 skipped in 205.02s**, including 22 new edition
tests and the pyflakes guard. The three skips need the absent PW Books original
or its Python environment. All packaged checks passed: NoteCraft imports 46,
Notes check imports 48; package scan; launcher `--check`; 13 pages, version and
Update button; offline Notes check mock run complete. Independent artifact
verification confirms the selected engines/libraries, B.Sc. fonts, checksums
and headers. Tests: `_build/noupsc_nopwbooks_20261028_finaltests.{log,xml}`;
artifact evidence: `_build/noupsc_nopwbooks_20261028_artifact_verification.json`.

**Missing-original gate and the user's approval:** the PW Books original folder
is no longer present. The ordinary drift gate returned STOP because its 19
protected prompts could not be compared. Work paused; the user explicitly
approved the verified installed copy as the reference. All **81 packaged PW
Books files**, including all **19 protected prompts**, in both source and install
match the successful 2026.10.26.1 build record. The wrapper
`_build/noupsc_20261028_gate.py` proves those hashes before substituting that
read-only reference in memory; data_guard.py and its protected-file rules were
not edited. Drift then passes, with only the existing allowed differences for
the other engines. No original or engine copy was changed.

The builder also supports explicit `--engine-libs ENGINE=FOLDER` for libraries
from a preserved package, with the source recorded and all normal checks kept.
This was **not needed for this smaller build**. A future full build on this
machine can use `--engine-libs pw_books=C:\Users\Laptop\AppData\Local\Programs\NoteCraft\_internal\libs\pw_books`
while the original environment is absent. No full rebuild was run.

**Preservation:** guard snapshots contain **5,464 files before and after**:
zero lost files, JSON entries, original changes, data changes or new data.
Snapshots: `_build/before_noupsc_20261028.json` and
`_build/after_noupsc_nopwbooks_20261028.json`; final compare/drift logs:
`_build/noupsc_nopwbooks_20261028_{compare,drift}.log`.
Independent before/after hashes of **566 files** (installed app/code, both
full-installer aliases and source release metadata) all match, recorded in
`_build/noupsc_20261028_preserved_{before,after}.json`. The existing full
installer still has SHA-256
`7f92cf7e835f912f10ec1291ba1c523b5d01c378f73ebb6d2c06c2f8ea1bdaec`;
source release.json is still **2026.10.26.1**, without smaller-edition metadata.
Port 3100 was already unavailable and no installed NoteCraft process was found
when checked during this task; it was left as found.

**Not done:** installer execution, installation, paid generation or uploading.
Putting the versioned `.exe` and matching `.txt` into Drive still needs the
user's go.

## Builds clean up after themselves — 27 October 2026

The project folder was 10.4 GB, 7 GB of it in three staged `_build/<version>-*/package`
copies (2.25 GB each) that `--keep 1000 --keep-package` had kept, plus a second 700 MB copy
of each installer. Source only, nothing built:
- `packaging/build_installer.py` `prune_old_builds(keep, current=work)` now removes the
  `package` and `verification-*` folders of every **finished** build except the current
  one, whatever `--keep` says (records and verification.json stay; unfinished builds keep
  their package for `--resume-work`). It only touches `_build/<version>-xxxxxxxx` and
  `dist/team/<version>` folders, never `install_update_*` (database backups), sync
  backups or gate snapshots: the old prune would have deleted those by age.
- `NoteCraft_Setup_<version>.exe` is a hard link to `NoteCraft_Setup.exe` (`link_or_copy`).
- `tests/test_build_prune.py` (4 tests) + `tests/test_installed.py`: 17 passed.
- The existing leftovers were not deleted (auto mode blocks bulk deletes); the user has a
  preview + YES cleanup script.

## Built and installed 2026.10.26.1 (B.Sc. typeset notes) — 26 October 2026

The user explicitly asked to build and install the B.Sc. update. Built
**2026.10.26.1** with the normal builder, the existing Google registration and
team key, `--keep 1000` and `--keep-package`. Nothing was uploaded and no paid
generation was run.

**Installer:** `dist/team/2026.10.26.1/NoteCraft_Setup.exe` and the versioned
copy, both **705,006,115 bytes**. SHA-256
`7f92cf7e835f912f10ec1291ba1c523b5d01c378f73ebb6d2c06c2f8ea1bdaec`, checked
independently against SHA256.txt; MZ header. Every builder check passed:
- imports: NoteCraft 46, UPSC 152, PW Books 38, Notes QC 48;
- package clean;
- `--check`, 13 pages, version and Update button;
- mock runs of the three child engines.

Log: `_build/install_update_20261026_build.log`; staged package kept in
`_build/2026.10.26.1-hv0lxkzl/`.

**Install:**
- **A fresh backup was needed.** The installed app made run 22 (hand-drawn,
  "Business Law_L1", 16:54–16:56) after the first backup set
  (`_build/install_update_20261026/`, kept). A fresh guard snapshot and SQLite
  backups were taken (`_build/install_update_20261026_2/`, script
  `_build/install_update_20261026_2.py`), and idle state was rechecked just
  before closing.
- **Closing.** The idle launcher (PID 14148, server 8588) was closed through
  its window-close handler (its own Quit); no other process was touched.
- **Installer.** Silent, per-user, over 2026.10.23.1: exit 0, no restart, 19
  minutes (slow machine).
- **Restart.** The new launcher (PID 12324) and server (PID 19144) serve
  http://127.0.0.1:3100. `/api/instance` reports 2026.10.26.1, and update
  status shows it installed and current.

**Checks on the installed copy:**
- 14 pages return 200, including `/new?category=bsc`, which shows the type,
  its options and the B.Sc. rules. Settings → System shows Typeset notes
  ("Part of NoteCraft"), and AI models shows "Drawing the figures".
- All 129 source files are installed and identical, except `release.json`
  (same JSON); the 30 new B.Sc./typeset files are identical.
- The installed runtime and libraries alone made B.Sc. notes (offline stub,
  scratch data folder): complete, 3 pages, verdict passed, Lato and CMU
  embedded, every module from the install folder.
- The B.Sc. pack was seeded into `~/.note_studio/categories/bsc`; the other
  packs are untouched.

**Preservation:**
- Every saved row is identical: installed 22 runs, 157 items, 426 events, 22
  queue entries; local test 10/146/196/10. Integrity OK, no foreign-key
  findings.
- Changed settings are only `update_checked` and `whats_new_state`.
- Guard: 5,700 → 5,707 files, zero lost files or JSON entries, zero original
  changes. Expected changes: the live database, the shared Google token
  (refreshed at start-up, entries kept) and the 7 new B.Sc. pack files.
- Drift: only the allowed differences.
- Evidence: `_build/{before_install_update_20261026_2,after_install_update_20261026}.json`,
  `_build/install_update_20261026_{preservation,drift}.log`, and
  `_build/install_update_20261026_2/history_verification.json`.

**Not done:** the release folder still lists 2026.10.4.4 as its newest. The
team gets this version only when `NoteCraft_Setup_2026.10.26.1.exe` and its
`.txt` go into the release folder (an upload, not done).

**Size question asked during the build:** UPSC is 1,073 MB of the 2,146 MB
package (its libraries 823 MB, ffmpeg 193 MB, the DPP bank 53 MB). Without it
the installer would be about 335 MB, against 705 MB now. No change was made;
the user has to choose.

## B.Sc. category and typeset notes — 26 October 2026

The user supplied six B.Sc. Wallah sample notes (`Downloads\Lec 01 .pdf`,
`Lect 01/13/27/170.pdf`, `Lecture 14.pdf`; LaTeX/pdfTeX) and asked for the same
format in a new category, with a better strategy. No original or engine copy
was edited; no build, install, upload or paid generation was run.

**Format, measured from the samples (PyMuPDF), not guessed:**
- A4 on a faint blue page (250,252,255), with 32.4 pt side margins.
- Lato 2.015 text at 10.91 pt on a 14.35 pt line; Computer Modern formulas;
  CM Sans Bold box titles (`SFSX1095`).
- Six box styles with exact colours: definition, concept, formula, warning,
  note, summary. Corners 8.5 pt (3 mm), frame 0.6 pt, title bar 15.73 pt.
- Booktabs and grid tables; `Figure N:`/`Table N:` captions.
- Header "B.Sc. Wallah | B.Sc. Notes"; footer "B.Sc. Wallah | page | Concept •
  Understand • Apply".

A hand transcription of `Lec 01 .pdf` rendered by the new code tracks the
sample's baselines within about 1.7 pt on page 1 and 4 pt on page 2. The table
columns land on the sample's x positions.

**Engine (`note_studio/typeset/`, adapter `note_studio/engines/typeset.py`),
in process, PW AI through the shortnotes copy's `pw_access`:**
1. **Notes.** One Gemini call with the lecture PDF and a JSON schema
   (`document.schema()`). The engine's prompt is `prompts.py`
   (REVISION `typeset-notes-v1`); every rule layer is added after it.
2. **Tidy.** Local: numbers typed into headings are removed ("3.4 4.
   Pigmentation"; genus abbreviations such as "E. coli" are kept). Open
   brackets are closed, stray markup is dropped, and loops in trees are broken.
3. **Review.** Optional, on by default. A source-grounded call corrects facts,
   names and broken sentences by JSON pointer, applied only where the text is
   unchanged, and lists missing topics. The samples' own slips are the target:
   "Paramecium … prokaryote", "charateristics", "Systemic position".
4. **Figures** (up to 6, operator's choice 0/3/6). Each is drawn from the
   model's precise description through `/api/gemini/image`, the PW Books
   proven route. Its labels are then read back. A misspelt picture is redrawn
   once with the faults named, and the better one is kept. A picture that
   cannot be drawn is left out. A spent budget stops further pictures without
   failing the notes.
5. **PDF** (`render.py`, fpdf2 + HarfBuzz; mathtext for TeX-spaced formulas).
   Pagination is better than the samples':
   - headings stay with their text;
   - a box never leaves fewer than two lines on a page, and short boxes move
     whole (no lone "Unit vectors…" page);
   - a figure that does not fit waits while the text fills the page, and goes
     in at the first later place it fits (no half-empty pages);
   - table headers repeat on each page, and the caption stays with the last
     row.

   Hindi uses Hind for Devanagari (shaped); Hinglish and English use Lato.
6. **Quality report.** `quality_report.json` per run: verdict passed or
   needs_attention. Attention is raised by a review that could not be made,
   missing topics, figures with label faults, formulas printed as text, page
   overflow, or the wrong language.

**Category (data):** built-in pack `note_studio/packs/bsc/`. It is seeded into
`~/.note_studio/categories/bsc` on first start only if absent, then editable in
Settings → Categories.
- Subjects: Physics, Chemistry, Mathematics, Botany, Zoology (plus Auto,
  Other).
- Languages: English, Hindi, Hinglish.
- Rules: `all-subjects.txt` plus one per subject.
- The header, footer and brand come from `branding`, so another category can
  reuse the type with its own names.

**Other changes:**
- `note_types.json`: type `typeset_notes`, engine `typeset`, with stages
  notes/qc/figures.
- `models.py`: "Drawing the figures" stage; the engine is Gemini only.
- `make_service.execute_typeset` and `signin.proxy_token_provider`.
- `requirements.ENGINE_PROMPTS`, `categories.seed` and
  `engine_catalog.ENGINES` ("Part of NoteCraft").
- What's new tour `2026-10-26-bsc-typeset`; New notes guide.
- README, doc 07.
- Fonts (OFL, licences beside them) in `note_studio/static/fonts/typeset/`:
  Lato ×4, CMU Sans ×4 (TeX table removed), Hind ×2.

**Validation:** `tests/test_typeset_notes.py` adds 33 tests, offline with a
stubbed PW AI. They cover:
- the tidy rules, markup and TeX spacing;
- the layout text, fonts and branding;
- box and heading splits at every page position, and Hindi;
- New notes → Smart Queue → run → download;
- review corrections and missing topics, the figure redraw and Needs attention;
- sign-in hold, spent budget, a refused schema retried with the shape, and
  fallback to the second model.

`test_note_types` and `test_category_packs` were updated for the new type and
pack. Full suite: **302 passed in 353.50s** (the 269 of 23 October plus 33),
with pyflakes clean (`_build/bsc_typeset_20261026_tests.{log,xml}`). An
offline run of the whole pipeline and screenshots of New notes (B.Sc.) and
Settings → System were inspected.

**Gates:**
- compare: **5,696 files before and after**; zero lost files or JSON entries,
  zero original-project changes, zero data changed.
- drift: only the existing allowed differences.

Snapshots: `_build/{before,after}_bsc_typeset_20261026.json`; logs:
`_build/bsc_typeset_20261026_{compare,drift}.log`. The installed app (3100)
and the source app were not touched; a temporary preview on port 3197 with a
scratch data folder was started and stopped.

**Not yet verified (needs the user's go):**
- No paid run: the prompt and schema are tested only against the stub, so the
  first B.Sc. lecture run is the real check of content quality and cost.
- Whether the image route and `gemini-3.1-flash-image` are allowed for this
  account is unproven. If refused, figures are left out and the run says so.
- Not yet in an installer: the build copies `note_studio/` (code and fonts)
  as usual.

## Built and installed 2026.10.23.1 — 23 October 2026

The user explicitly requested building and installing the Gujarati correction
update. Built **2026.10.23.1** with the normal builder, existing Google app
registration/team update configuration, `--keep 1000` and `--keep-package`.
Added the Gujarati correction tour and updated the New notes/Automation guides.
No release was uploaded and no paid generation was run.

**Installed:** the normal silent installer updated the existing per-user
installation from **2026.10.15.1**, exit code **0**, with no Windows restart.
The idle old launcher was closed through its normal window-close handler;
no unrelated processes were stopped. The new launcher PID **14332** and bundled
Python PID **5980** serve **http://127.0.0.1:3100**. Release and instance APIs
confirm the installed channel/version. Home, New notes, Automation and System
settings return 200; the new Gujarati guide text is present. All **98 source
application files** match installed bytes; release JSON matches semantically.
The existing OneDrive Desktop shortcut points to the installed executable.

**Installer:** `dist/team/2026.10.23.1/NoteCraft_Setup.exe`, with the versioned
copy, quick start, release notes and SHA256.txt alongside it. Both installers
are **703,105,220 bytes**. Independent SHA-256 checks match both copies and
the builder record:
`48aaca162c131291f011c7e8d31342319675519a6f233125ac1f60c26f051b9c`.

**Validation:** **269 app tests passed in 553.76s**. Packaged third-party/import
checks passed: NoteCraft 43, UPSC 152, PW Books 38, Notes QC 48. Launcher check,
all 13 packaged pages, version/update metadata and all three offline child
engine workflows passed. A separate Gujarati sample used only the packaged
runtime/app and rendered with readable Gujarati and **zero local quality
issues**; the new editorial schema is available in the package. Engine drift
passes with only the existing allowed differences. Staging and packaging
verification: `_build/2026.10.23.1-eij0_77i/verification.json`; Gujarati check:
its `gujarati-offline-qa/verification.json`. Logs/XML are
`_build/install_update_20261023_{build.log,tests.log,tests.xml,drift.log}`.

**Preservation:** SQLite backups and exact row comparisons for the installed
and local-test databases are in `_build/install_update_20261023/`. All saved
run/item/event/queue/sequence rows are identical. Installed history remains
16 runs, 123 items, 314 events and 16 queue entries; local-test history remains
10 runs, 146 items, 196 events and 10 queue entries. Both integrity checks pass
with no foreign-key findings. No setting keys were removed; the only changed
setting at verification was installed `update_checked`. The source/local-test
app was left running with its data unchanged.

Guard snapshots watch **5,684 files before and after**, with zero lost files,
zero JSON entries lost and zero original-project changes. Changed files are
the installed live database and the shared Google token refreshed on startup;
the token's JSON keys/entry counts are preserved. Raw snapshots are
`_build/{before,after}_install_update_20261023.json`, with the comparison in
`_build/install_update_20261023_preservation.log`. Installed verification,
history comparison, checksum and installer-exit reports are retained in the
installation work folder. Earlier builds and this build's staged package
were retained.

## Gujarati reviewer correction patterns — 23 October 2026

The user provided reviewer screenshots across Accountancy, Business Studies,
Economics and Statistics and authorized code changes to prevent recurrence.
Implemented general Gujarati rules, without chapter-specific heading counts or
deletion lists. The copied engines and original projects remain untouched.

**Content:** `note_studio/gujarati_editorial.py` extends the existing Gujarati
prompt and replaces automation's whole-lecture duplicate review with one
source-PDF-backed review. It can group/reorder source topics, merge later
introductions into their proper section, remove covered repetitions or
source-unsupported strings, and correct existing strings using source evidence.
Corrections use validated JSON pointers and reach table cells and schematic
labels. Section plans must account for every input section exactly once;
distinct table/visual slots, signed formulas and unique details survive merging.
Retained repetition references are protected against exclusion. Emptying the
entire lecture is rejected. Missing/incomplete/invalid reviews retain content
and become local findings in the automation quality receipt. Run instructions
are forwarded to the review. Legacy uploads run the same review once; page
groups defer it until automation has merged the whole lecture.

**PDF:** `note_studio/gujarati_layout.py` keeps ordinary inline numbers, years,
ratios, percentages and US abbreviations in the surrounding Gujarati face;
source numerical values and genuine equations remain intact. Normal table rows
stay together and table headers repeat after page turns. Exceptionally tall rows
retain the engine's line-slicing fallback. Glossary continuation panels omit the
duplicate caption and keep ordinary terms whole. Process flows use connected
vertical steps and move together when they fit a fresh page. The formula
composer in `gujarati_math.py` now aligns standalone operators to the math axis,
fixing low subtraction signs. Existing responsive-card handling is regression
tested against title-only opening pages.

**Validation:** full app suite **265 passed in 301.62s**. The final focused suite
is **40 passed in 48.19s**, including all 20 new reviewer regression cases and
20 existing Gujarati generation/math tests; the full run was collected before
the last four focused cases were added. Pyflakes passed. Engine drift passed
with zero unapproved differences. Offline visual QA inspected all five pages of
`output/pdf/gujarati-review-20261023/`, including matching numeral sizes,
centred minus signs, glossary continuation, intact rows and connected steps.
No paid model generation, build, install or upload was performed. Source app
restart is needed; existing PDFs/executables were not rewritten.

**Preservation:** guard snapshots watched **5,684 files before and after**:
zero lost files/JSON entries and zero original-project changes. The only
changed watched file was the live `~/.note_studio/note_studio.db`. Read-only,
immutable SQLite inspection passed integrity/foreign-key checks; counts are
16 runs, 123 items, 314 events, 16 queue entries and 10 settings. The latest
run event is 11:05 IST, before this task; the normal update-check timestamp
14:38:18 IST matches the database modification time. Reports and reproduction
scripts: `_build/gujarati_review_20261023_{tests.xml,focused.xml,preservation.json}`,
`_build/gujarati_review_{smoke,preservation}.py`, and the before/after snapshots.

## Installer 2026.10.17.1 — 17 October 2026

The user requested building an installer of the locally running source.
Built **2026.10.17.1** with the normal installer builder, existing Google
registration/team update configuration, and `--keep 1000` to preserve build
history. No installer was installed or uploaded.

**Installer:** `dist/team/2026.10.17.1/NoteCraft_Setup.exe`, with the versioned
copy, release notes, quick start and SHA256.txt alongside it. Both executable
names are **703,097,875 bytes**. Independent SHA-256 verification of the
versioned installer matches the builder record and checksum file:
`19d6f67fea363a39e1078d082b590a048992a8aea04ff1838c0bcf80bf29e179`.

**Validation:** **249 tests passed** in 477.66 seconds. Packaged imports passed
for NoteCraft (43), UPSC (152), PW Books (38) and Notes QC (48). The desktop
launcher check, all 13 pages, update metadata and all three offline engine
samples passed. Engine drift contains only the existing allowed integration
differences. Reports: `_build/installer_20261017_{tests.xml,tests.log,build.log,drift.log}`;
packaging verification: `_build/2026.10.17.1-j78muhnw/verification.json`.

**Preservation:** the live app finished run **13** during this build. The raw
guard reported STOP when normal `job_queue._finish` cleanup removed its
temporary queue PDF. Read-only investigation confirmed the original upload
is retained byte-for-byte at `~/.note_studio/runs/13/input/`, matching both
raw snapshots. The run is complete, its queue entry is done, SQLite integrity
is **ok**, and there are no foreign-key findings. The database now contains
13 runs, 95 items, 258 events and 13 queue entries. No user data was changed
to clear the alert, and the guard was not modified.

Raw snapshots and the STOP report remain in `_build/`. Documented durable
snapshots exclude only that exact verified queue duplicate: **5,677 files
before, 5,680 after; zero lost files/JSON entries and zero original-project
edits**. Expected changes are the live database and run 13's three new output
files. Evidence: `_build/installer_20261017_preservation_review.json`,
`_build/verify_installer_preservation_20261017.py` and
`_build/installer_20261017_preservation_durable.log`.

## Installed update 2026.10.15.1 — 15 October 2026

The user requested installing and updating after the source app was started
locally. Built the current source with the normal installer builder and
installed **2026.10.15.1** over **2026.10.9.2** in the existing per-user
installation. The installer completed with exit code **0**, without a Windows
restart, and the desktop shortcut is present. No release was uploaded.

**Running:** installed NoteCraft launcher PID **9876**, bundled Python PID
**744**, at `http://127.0.0.1:3100`. Instance and update-status APIs confirm
the new version and installed channel. Home, Automation and System settings
return 200. The source app on 3101 and its separate local-test data were left
running. No paid generation was started.

**Validation:** **249 tests passed** in 542.76 seconds. Packaging verified
43 NoteCraft imports, all child-engine imports (UPSC 152, PW Books 38,
Notes QC 48), the desktop launcher's check, all 13 pages, update metadata,
and the three offline child-engine sample runs. Engine drift passed with
only existing allowed integration differences. All **96 installed app files**
match the source byte-for-byte; release metadata is semantically identical
(source LF versus packaged CRLF).

**Preservation:** SQLite backups and verification are in
`_build/install_update_20261015/`. Every saved row is identical: installed
history has **9 runs, 91 items, 180 events and 9 queue entries**; the local
test history has **10 runs, 146 items, 196 events and 10 queue entries**.
Both integrity checks pass, foreign-key checks are empty, and sequence rows
are unchanged. Changed settings are only `update_checked` in both databases
and installed `whats_new_state`; no setting keys were removed. The shared
Google token refreshed during normal startup/polling, with JSON keys kept.

The raw data guard reported STOP because the installed database's **empty
WAL (0 bytes)** and temporary SHM file disappeared during normal SQLite
cleanup. Raw snapshots are retained. After the read-only history/integrity
investigation, explicitly documented durable snapshots exclude only those
two exact paths: **5,664 files before and after, zero lost files or JSON
entries, zero original-project edits**. The guard itself was not changed.
Snapshots: `_build/{before,after}_install_update_20261015{,_durable}.json`.

**Installer:** `dist/team/2026.10.15.1/NoteCraft_Setup.exe` (703,097,071
bytes), with the versioned copy, notes and SHA256.txt alongside it. SHA-256:
`397700a072271bfc62ea7d9f71862538a542b1715d81b55706bd0b2a47a41394`.
Build verification: `_build/2026.10.15.1-7sj3h17p/verification.json`.
Install log: `_build/install_update_20261015/install.log`. Existing build
history was kept (`--keep 1000`).

## Gujarati words inside formulas — 14 October 2026

The user supplied `ભાગીદારી _ વિષય-પ્રવેશ Concise Notes.pdf` and screenshots
of Gujarati words appearing as circles inside fractions/equations. Short Notes
sync (dry run and actual run) reports **0 files refreshed**, with the three
integration files preserved: no formatting code was missing. Engine drift
passes with only the existing permitted integration differences.

The cause is Matplotlib mathtext's missing Gujarati glyph/shaping support,
which the existing prose-font hook did not cover. New app-level
`note_studio/gujarati_math.py` wraps `notesbot.math_render.render_latex` through
`shortnotes_extensions.install()`. Gujarati text labels use BalooBhai2 and
HarfBuzz in FPDF, composed with fraction bars and the existing symbol renderer,
then rendered to transparent PNG under the shared PDFium lock. Nested
fractions work. Expressions without Gujarati delegate to the original renderer
unchanged; unsupported label syntax fails instead of producing dummy glyphs.
Original and copied engine formatting files, prompts and cache keys remain
unchanged. The page guides and What's new describe the fix.

**Validation:** focused suite **41 passed**; full NoteCraft suite **249 passed**
(241 previously, eight added cases). Tests cover Gujarati labels, fractions,
nested fractions, exact English/numeric PNG and metric parity, rejection of a
malformed formula, and the real notes-PDF/local-quality pipeline. Reports:
`_build/gujarati_formula_focused_20261014.xml` and
`_build/gujarati_formula_full_20261014.xml`.

**Corrected document:** `output/pdf/ભાગીદારી _ વિષય-પ્રવેશ Concise Notes - corrected.pdf`
repairs **11 formula images** in the supplied **six-page** PDF. The source
lecture was read to verify the labels; the capital-interest image also now
includes the multiplication sign between rate and time. All original
non-image text and page dimensions are preserved. Poppler renders of all six
finished pages were inspected for readability, spacing and overlaps. The
original supplied PDF is untouched. No saved notes JSON exists for this run
(caching was off), so this is a direct image repair, with no paid AI generation
or fabricated review receipt. The Drive/Sheet outputs were not replaced.
Reproducible repair and visual evidence live in `_build/gujarati_formula_review/`.

**Preservation:** before/after snapshots each contain **5,666 files**, with
zero lost data files/JSON entries and zero original-project edits. Only the
test database and the shared Google token file changed. The database includes
normal local-app startup/update metadata; every saved row in all **7 runs,
96 run items and 7 queue entries** is identical across the restarts.
`integrity_check` is **ok**, with zero foreign-key findings. The token's write
time is 17:02:19 IST, consistent with the normal Google refresh during startup;
its JSON keys were preserved. Gate snapshots:
`_build/{before,after}_gujarati_formula_fix_20261014.json`; history verification:
`_build/gujarati_formula_review/history-verification.json`.

**Running locally:** detached source app, Python PID **13132**, at
`http://127.0.0.1:3101/automation`, using the existing isolated test home.
Its instance identity, HTTP 200, formula guide and What's new API were verified.
All runs are idle and the user's queue hold remains **stop**. Run 7 was stopped
by the user after two completed lectures; no paid run was started by the agent.
The installed app on 3100 was not changed or stopped. No installer was built.
Metadata: `_build/pdf_chip_local_server_20261014.json`; logs:
`_build/gujarati_formula_local_{stdout,stderr}_20261014.log`.

## Automatic Gujarati in Sheet automation — 14 October 2026

The user requested connecting Gujarati to Sheet automation, running it
locally, and detecting the language automatically like English/Hindi.
`note_studio/automation_languages.py` supplies additive startup hooks:

- Classic and catalog rows recognize **Gujarat**, **Gujarati** and Gujarati
  script batch names and select `gujarati` before generation/cache planning.
- Auto-language sources also detect predominantly Gujarati readable text in
  the first six PDF pages. The original Hindi/Sanskrit detection takes
  precedence; other existing language mappings remain unchanged. Image-only
  or unextractable PDFs need the batch-name signal for reliable selection.
- Source detection promotes Gujarati before the original generation, font,
  rendering and final-review pipeline, including translated sheet headers.
  Gujarati chapter cells are preserved; known subjects and the lecture label
  are localized. Untranslated fields use the generator's Gujarati metadata
  rather than overwrite it with English.
- There is **no language dropdown**: the final implementation is automatic,
  following the user's clarification. The page guide and What's new explain it.
- Original and copied engine files, their prompts, cache-key functions,
  columns, output naming and Drive properties remain unchanged.

**Validation:** final full NoteCraft suite **241 passed** (225 before this
change, 16 new cases), including real Gujarati PDF text detection and the
complete generation/render/final-quality pipeline with the model replaced by
the offline fixture. English/Hindi and original-app parity checks pass.
`_build/gujarati_automatic_full_20261014.xml` records the final result. An
initial PDF extraction assertion needed to ignore shaping-only zero-width
characters, as the existing Gujarati extraction test already does.
Engine drift passes with only the existing allowed differences.

**Data gate:** the raw comparison initially reported STOP because the test
database's transient `-wal` and `-shm` sidecars disappeared during SQLite
checkpointing/restart. Feature work paused for investigation. Read-only
`integrity_check` is **ok**, `foreign_key_check` has zero findings, and every
pre-existing run, row item and queue entry is identical to the saved original
database. The raw snapshots are retained. Explicit durable-file snapshots
exclude only those two named sidecars and compare **5,664 files before and
after**, zero lost files/JSON entries and zero original-project edits.
The test database is the sole changed durable file. The user started new
run/queue item **7** in the local app; all six earlier runs and all 50 earlier
row items remain. Reports are `_build/*gujarati_automation*20261014.json`.
The guard itself and the database were not rewritten to clear the alert.

**Running locally:** the updated source app is detached on
`http://127.0.0.1:3101/automation`, Python PID **5152**. It keeps the existing
test home `~/.note_studio/local_tests/pdf_chips_20261014_153610`, shared sheet
list and Google sign-in. The automation page returns 200, shows the automatic
Gujarati guide and `gujarat note`, and has no sign-in block. It was opened in
the usual browser. Metadata: `_build/pdf_chip_local_server_20261014.json`;
launcher: `_build/restart_gujarati_automation.py`; stdout/stderr:
`_build/gujarati_automation_local_*_20261014.log`. The previous foreground
test process was already absent; the installed app on 3100 was not stopped.
The user's new Gujarat run is active and its live log confirms
`lang=gujarati`. No installer was built or installed, and the agent did not
start a paid sheet run.

## Review and fix for catalog PDF attachments — 14 October 2026

The user's installed Sheet automation run **6**, `gujarat note`, completed
with **0 lectures made, 46 invalid_link failures, INR 0.00**. Connector reads
confirmed `Gujarat Short Notes || Commerce`, tab **Notes** (gid 0), stores
all 46 sources in **J2:J47** as Drive PDF smart chips. The formatted cell
values are PDF filenames; their chip metadata contains valid Drive URLs.
The first attachment's Drive metadata confirms `application/pdf`.

The copied engine's classic sheet path already uses `SheetClient.cell_hyperlink`,
but `CatalogFlow.plan` only parses the displayed value. This reproduces the
reported error without downloads or AI calls. `note_studio/automation_links.py`
now installs the existing hyperlink/chip fallback around catalog planning at
app preparation. It replaces the source value only in an immutable row copy;
plain URL rows retain the original planning behavior. Guide and What's new
text describe PDF chips. Original and copied engine files are unchanged.

**Validation:** NoteCraft suite **225 passed**, including eight attachment
regressions (Drive chips, hyperlinks, linked text, web PDFs, invalid values,
metadata errors and repeated initialization). Copied catalog/sheet engine
tests **299 passed** with a dummy offline spreadsheet ID. The first engine
test run had one missing-profile assertion because the original `.env` is
intentionally absent; rerunning with the documented offline test setting
passes. No real sheet profile was changed. A planning replay of the actual
46 chip URLs verifies identical plans/cache identities to plain URL inputs
using mocked Drive access; it does not verify download access to every PDF.
Reports: `_build/pdf_chip_*_20261014.*`.

**Preservation:** before/after guard snapshots contain **5,650 files** each,
zero lost data/JSON entries and zero original-project edits. Engine drift
passes. The only data hash change is the running installed app's
`~/.note_studio/note_studio.db`; its background update check occurred at
15:06:36 IST between the 15:03:40 and 15:08:32 snapshots. Run 6 and its
46 failed items remain unchanged; tests use isolated data folders.

**Delivery:** this fixes the source project. The installed app is still
**2026.10.9.2**; no build, installation or paid retry was requested or run.
`output/gujarat-note-pdf-links.txt` contains the 46 observed Drive URLs in
row order, ready to paste into Notes!J2:J47 as an immediate workaround.
The sheet itself was inspected read-only.

**Local test server requested by the user:** source NoteCraft is now running
at `http://127.0.0.1:3101/automation` (server PID 25332). The installed app
remains on 3100. The test server uses a SQLite backup and copied category
packs at `~/.note_studio/local_tests/pdf_chips_20261014_153610`, with the
existing shared sheet list and Google sign-in. The saved queue had no active
tasks when copied. Server identity and the automation page's HTTP 200 were
verified; the new PDF-chip guide and `gujarat note` profile are present and
there is no sign-in block. The page was opened in the usual browser. The
in-app Browser backend was unavailable, so there was no browser UI test.
No sheet run or paid generation was started by this launch.
Launch metadata is `_build/pdf_chip_local_server_20261014.json` and the
launcher is `_build/run_pdf_chip_test.py` (exec session 32536).

## Latest Short Notes and UPSC source sync — 12 October 2026

The user requested copying their recent Short Notes / UPSC Notes changes into
Note Studio. Both documented original projects were read only and refreshed
using `NoteStudio_Kit/tools/sync_engines.py` and `shutil.copy2`.

- Short Notes: **13 files refreshed**, including chemistry bonds/states,
  escaped percentages, degree/comparison notation, PDF rendering, quality
  checks, shared prompts and their regression tests.
- UPSC: **11 files refreshed**, including Drive transcript access/error
  handling and use of supplied transcripts before fetching/transcribing video.
- Previous copies are preserved in
  `_build/engine_sync_backups/20261012-105126/{shortnotes,upsc}/`.
  Each engine's `COPIED_FROM.md` lists its refreshed files.
- NoteCraft's permitted category/account/model/extra-instruction hooks remain.
  Their differences were reviewed against the current originals. Gujarati
  support, user category rules, saved runs, sign-ins and queue state are kept.
- The registry now reports the original Short Notes generation revision
  `concise-theory-v13-chemistry-bonds-states` and quality revision
  `concise-theory-quality-v4-chemistry`. Existing cached files were not removed.
  `tests/snapshots/categories_20261012.json` records the current original's
  prompt hashes for 56 category/subject pairs; rule blocks, switches and
  category fingerprints remain identical to the 8 October snapshot, which is
  preserved alongside the Phase 0 snapshot.
- All four UPSC note styles offer **More options → Existing Drive transcript**.
  The full text passes through the form, queue, adapter and `engine_main.py`
  into the original's `transcript_text_override`. Text shorter than the
  original's 25-word minimum is rejected before queueing. The recording link
  stays required. Supplied text bypasses recording retrieval/audio extraction
  and has its own progress message. The guide and What's new tour describe it.

**Validation:** final NoteCraft full suite **217 passed** (the previous 214
plus three supplied-transcript cases); affected Short Notes suite **560
passed**; affected UPSC suite **289 passed, 11 skipped**. UPSC skips are ten
missing sample-lecture fixtures and one standalone sign-in template that the
copy intentionally does not carry. Short Notes tests use a dummy offline sheet
profile because the original `.env` is not copied. No paid generation was run.
The original-app parity helper was updated for its newly added transcript
argument. Empty and supplied transcript paths produce identical prompts,
outputs and relevant metadata in the original and the NoteCraft child; the
supplied path records zero transcript-retrieval time. The form-to-queue test
also verifies the supplied text reaches the generated run unchanged apart
from outer whitespace. Gujarati, automation parity and budget checks pass.
Initial test logs and final XML/log reports remain under `_build/`.

**Gates:** `before_engine_sync_20261012.json` →
`after_engine_sync_20261012.json`: **23,248 files before and after**, zero data
changes/additions/loss, zero JSON entries lost, zero originals touched. Drift
passes with only the previously listed integration differences and NoteCraft
entry files. A subsequent dry run finds no pending source files to refresh.
The machine-readable report is `_build/engine_sync_20261012.json`.

This task updates the source project. The installed desktop bundle receives
these changes after its next build/update; no installer was built or installed.

## Gujarati and manual automation quality — 9 October 2026

The user asked for Gujarati Short Notes and to run NoteCraft locally so they
can compare its output with Short Notes sheet automation. This extends the
authorized engine synchronization and local installation work below.

**Current: NoteCraft 2026.10.9.2 installed and running at
`http://127.0.0.1:3100`.** Open
`/new?category=foundation&type=concise_handdrawn`, select Gujarati (ગુજરાતી),
and leave “Use sheet automation style and quality checks” enabled. NEET-JEE
offers the same language and option. Existing PW sign-in is ready, automatic
updates are enabled, both saved runs remain, and `queue_hold=stop` is preserved.
The 8 October gateway budget limitation below is historical: actual Gujarati
generation succeeded today using the existing authorized sign-in and model.

**Implementation and preservation**

- No original or copied engine source was changed during this extension.
  `note_studio/shortnotes_extensions.py` supplies additive Gujarati prompt,
  renderer, Unicode extraction, and quality-review hooks at app startup.
  Existing-language prompts remain unchanged. Gujarati body, headings,
  subject/audience, page labels, and callouts are supported; formulas, units,
  proper names and useful first-mention English terms retain their meaning.
- Bundled Baloo Bhai 2 regular/bold fonts are shaped with HarfBuzz. Gujarati
  word measurement uses the same shaped face as drawing, so long lines wrap
  within their cards. The original Sanskrit Unicode-map repair is reused for
  Gujarati PDFs. Fonts are registered only for documents that need Gujarati.
  Source: [Google Fonts Baloo Bhai 2](https://github.com/google/fonts/tree/main/ofl/baloobhai2),
  with the accompanying OFL license bundled. `_build/prepare_gujarati_fonts.py`
  records the download and static-font conversion.
- Built-in category migration adds Gujarati only when the existing language
  list is still the stock list. Custom language lists and all other metadata
  are preserved. Each changed category has a `.before-gujarati.bak` copy.
- New manual hand-drawn runs use the unchanged original
  `sheets_bridge.generator.generate_notes_pdf` by default: lean generation,
  grouping, whole-lecture duplicate audit, rendering and final-PDF quality
  review. Per-run instructions and branding controls pass through app hooks.
  The legacy Upload path remains available when the option is disabled.
- Quality receipts are downloadable as `quality_report.json`. Unresolved
  findings produce `needs_attention` while retaining the generated PDF.
  Gujarati local checks also flag English-only metadata/callout labels.
- The original automation defaults per-group source review off; NoteCraft
  mirrors that setting. Turning the original setting on enables those extra
  coverage reviews. Do not claim that each source page was separately reviewed
  in the current default path.
- QBG One background-download/ready-install-popup behavior from 8 October
  remains enabled. No remote release was uploaded or published in this task.

**Quality evidence**

- Final complete NoteCraft suite: **214 passed**, in 352.97 seconds.
  A focused final run also passed all 20 checks. New tests cover Gujarati
  menu/migration, prompt/context isolation, shaped PDF text and layout,
  metadata checks, queue output/receipts, and unresolved-review status.
- `tests/parity/automation_driver.py` compares the actual original automation
  with the NoteCraft adapter using identical controlled model responses for
  NEET physics and Foundation maths. Requests, notes, pages/text, normalized
  PDF hashes, quality receipts and costs match. Existing Upload parity and
  protected prompt checks still pass. Independent live AI calls can vary;
  this proves identical engine behavior with identical model responses.
- Final actual-model Gujarati check: three-page controlled QA lecture about
  reflection and plane mirrors, clearly labelled as a test source; one-page
  Gujarati notes PDF. Existing `gemini-3.7-flash` sign-in made three paid calls
  (generation, duplicate audit, final review), total **₹2.05**. The receipt
  passed, reviewed PDF page 1, with no open/advisory issues and no corrections.
  PDFium rendering was visually inspected: Gujarati glyphs, headings, formula
  `i = r`, and line wrapping are readable and within the page/card bounds.
  Files: `output/pdf/live-gujarati-check/`, including `verification.json`,
  the source, final PDF, notes and `live-gujarati-page-1.png`.
- The first actual-model attempt cost ₹2.51. Its retained artifact exposed
  English metadata and a long-line wrapping issue; both were fixed before the
  final tests/build. It remains under `live-gujarati-check-before-layout-fix/`.
  The separate `Gujarati Short Notes - style sample.pdf` is an offline fixture,
  not the live result. These checks did not add artificial runs to the user's DB.
- Browser skill was loaded, but no in-app browser backend exists in this
  session. Actual browser visual verification is not claimed. Installed HTTP
  checks passed all 13 pages, both Gujarati forms/default quality controls,
  sign-in, database integrity, update settings, and **513** package manifest
  hashes. `_build/installed_verification_20261009.json` records them.
  The app was launched normally. Automatic approval review blocked the extra
  browser-tab command, with only “blocked by policy” provided as its reason.
  The local URL is available to the user for interactive verification.

**Final gates and installer**

- `before_gujarati_20261009.json` → `after_gujarati_20261009.json`:
  **22,702 → 22,704 files**, no data or JSON entries lost, no originals touched.
  Expected changes: the two built-in category JSON files gained Gujarati,
  their two backup files were added, SQLite saved its update state, and the
  existing Google token renewed. Final engine drift passed with only the
  previously listed permitted integration hooks.
- Final installer: `dist/team/2026.10.9.2/NoteCraft_Setup.exe`,
  **768,556,528 bytes**, SHA-256
  `29e94f4172659c0bf62fa0d2fd208f03889fb318f1b666a4f5fcdab31e0ce075`.
  The versioned installer, release notes, quick start and checksums are beside it.
- Package imports, scanner, launcher, packaged HTTP/startup and mocked child
  engine checks all passed. ZIP compression was used; libraries remain bundled.
  Final package report: `_build/2026.10.8.1-3pfmrofv/verification.json`.
  Earlier 8.2 report is preserved as `verification_2026.10.8.2.json`.
- Idle app closed normally; installer returned **0**, no Windows restart.
  Log: `_build/install-local-2026.10.9.2.log`. Installed bundle hashes match
  the final source manifest and the server reports installed version 9.2.
- The unfinished 9.1 installer is explicitly named
  `dist/team/2026.10.9.1/NoteCraft_Setup.exe.incomplete`. Never install or
  distribute it. Only the owned unfinished build/compiler were stopped;
  the completed 8.2 app remained intact until the normal 9.2 upgrade.

## Current engine sync and automatic downloads — 8 October 2026

The user's request authorizes analysis of the current Short Notes and UPSC
projects, refreshing their engines in NoteCraft, reusing available credentials,
and enabling QBG One's background-download / install-prompt workflow.

**Installed: NoteCraft 2026.10.8.2**, running at `http://127.0.0.1:3100`.
Automatic updates are enabled. Existing PW sign-in is ready. Both saved runs
remain, and SQLite integrity is `ok`. Real AI generation is still blocked by
the exhausted PW gateway allowance described below.

**Analysis and engine parity**

- Originals: `Downloads/shortnotes-core` and
  `Downloads/Upsc_Notes_2/upsc_Notes`, the sources already documented by this
  project. They are read only. Initially 22 Short Notes files and 41 UPSC files
  differed; additional new original files were missing from the copies.
- Refreshed 26 Short Notes and 47 UPSC code/assets/test files using
  `shutil.copy2`. Before-image copies are in
  `_build/engine_sync_backups/`; `COPIED_FROM.md` records the refresh.
- Short Notes follows the original's PDF extraction → visual inventory →
  generation → source-coverage and final-quality checks → hand-drawn PDF path.
  Current science-format prompts, diagram/equation/render repairs, Sanskrit
  handling, automation runner and cache revision are now carried together.
- UPSC follows its original `run_pipeline`: class recording and slides →
  transcript/source access → DCN/simple/Mains/Prelims prompts → Word layout,
  quality/error reports and PDF export. Its current DMP/DPP selection/export
  code is also copied. NoteCraft's adapter still supplies the original app's
  defaults; the configured notes/speech/image models match the original `.env`.
- Only the permitted category-pack, per-run account/model, and append-only
  instruction hooks differ. The instructions-empty UPSC prompts match all 18
  original prompt cases. Short Notes parity covers model request hashes,
  normalized PDF bytes, extracted text, page count, filenames and costs.
  Independent live AI calls can vary; this verifies the same engine and
  identical output when given the same model responses.
- The Short Notes original has advanced its own generation cache revision to
  `concise-theory-v11-science-format-diagrams`; the registry now reports that
  same value. Existing caches/outputs were preserved. The 4 October category
  baseline is retained; `categories_20261008.json` records current-original
  prompt hashes and is also checked against the live original in tests.
- The refreshed Drive transcript code separates caches by account and checks
  access on cache reuse. NoteCraft now supplies the signed-in account identity
  alongside the fresh token in the child environment. Token renewal handles
  both naive and timezone-aware credential expiry timestamps.

**Credentials and live limitation**

- Existing authorized PW Google sign-in: `prashant.kumar15@pw.live`.
- Existing team key: `qbg-studio-bot@sage-tribute-501712-e9.iam.gserviceaccount.com`.
  It successfully reads the existing NoteCraft release folder. Credentials
  remain in their established stores; tokens/private keys were never printed.
- Tiny live calls on the configured Short Notes `gemini-3.7-flash` and UPSC
  `gemini-3.5-flash` both returned the
  original's `ModelBudgetError`: the PW gateway allowance is exhausted.
  Real generation remains dependent on the administrator renewing it.
  NoteCraft now shows that actionable message rather than a generic PDF error.
- The previous NoteCraft-only budget-driven provider switch was removed so
  the current original's permanent budget-stop semantics are preserved.
  Legacy alternate-model settings are kept, with accurate UI labels.

**Automatic updates**

- Ported the current QBG One `update_delivery.py` (read only, 8 October):
  startup/five-minute background checks, downloads that allow work to continue,
  `.part` files promoted only after hash/size/Windows-header verification,
  and persistent verified installers in `~/.note_studio/update_downloads/`.
- No installation during an automatic download. The pop-up appears only when
  ready; Install update is explicit, with a ten-minute reminder on dismissal.
  Installation rechecks the cached file and avoids downloading it a second time.
- Active runs and running queue items block installation. Queue claiming and
  the install hold share a lock, preventing new paid work from starting during
  the restart. An install failure releases the update hold. Downloads never
  hold the queue. After restart, an empty queue releases its update hold;
  waiting jobs require Continue. Existing user stop/pause holds are preserved.
  The poller also stops during server shutdown.
- Settings/guide/tour explain automatic downloads and the ready-only prompt.

**Validation**

- 482 Short Notes regression tests passed (a dummy offline sheet id supplies
  the standalone-screen test's expected profile; original `.env` not copied).
- UPSC full copy suite: 1,619 passed, 48 skipped, one inherited assertion
  failure in the old Streamlit screen. It expects an “Open the practice
  builder” button after the original UI moved practice into root tabs. The
  same single test was run read-only against the original and fails identically.
- New tests verify automatic downloads without a browser, duplicate-download
  refusal, cache reuse/tamper checks, invalid/incomplete executable refusal,
  active-work protection, queue holds, and poller shutdown. A JavaScript DOM
  test verifies progress, no early pop-up, reminders and explicit install.
- Browser skill loaded, but this session exposes no browser backend. HTTP,
  JavaScript behavior and packaged-start checks provide verification; no
  visual browser verification is claimed.
- NoteCraft's final complete suite passed: **202 tests**, including the
  packaging scanner and queue-recovery regression checks.
- Gate snapshots: `before_engine_update_20261008.json` and
  `after_install_engine_update_20261008.json`: all 22,702 files remain, no
  data lost, no JSON entries lost, no original touched. Expected changes are
  the saved update check in `note_studio.db` and the existing Google token's
  renewal. The intermediate `after_engine_update_20261008.json` also passed.
  Final engine drift reports only the listed permitted integration hooks.
- The full UPSC test suite generated fixture runs under the engine copy;
  these were preserved in `_build/upsc_engine_suite_artifacts_20261008/`.
  They are excluded from the installer and removed from the engine code tree.
- The current UPSC source contains a public Drive playback identifier used
  by [yt-dlp's official extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/googledrive.py).
  The package secret scanner initially treated it as a private Google API
  key. It now exempts only that exact public literal's SHA-256 in that exact
  source file. Tests prove other key values and paths are still refused;
  no engine source or actual credential was exempted or altered.
- The build supports `--resume-work` under this project's `_build/`, reuses
  staged libraries, re-copies current source and reruns every verification.
  This avoids copying the large dependency folders again after a rejected
  package. Runtime/library imports all passed (NoteCraft 43, UPSC 152,
  PW Books 38, QC 48). Clean packaged startup, all 13 HTTP pages, update
  status, launcher checks and mocked child-engine runs passed. The PDF
  exporter is intentionally stubbed in the mocked generation checks;
  actual machine prerequisites (LibreOffice, fonts, FFmpeg and workbooks)
  were checked separately and are ready.

**Installer and installed verification**

- Final installer: `dist/team/2026.10.8.2/NoteCraft_Setup.exe`,
  767,923,091 bytes. SHA-256:
  `3b8748ddd2063bb57fb3b7c702c686a1df591c5684a019eb0d8d33c3e1e25e1d`.
  The versioned installer, release notes, quick start and checksum file are
  beside it. ZIP compression was used for this release; the default build
  still uses LZMA2. All libraries remain bundled.
- Installation succeeded with exit 0, without restarting Windows. Started
  the installed launcher and verified version, automatic updates, 13 pages,
  release-folder configuration, database integrity and preserved sign-in.
  All 509 installed code/engine-asset manifest hashes match the package.
  Both saved runs and the existing `queue_hold=stop` remain intact.
- `_build/installed_verification_20261008.json` records the installed checks.
  `_build/2026.10.8.1-3pfmrofv/verification.json` records the final .2 package
  checks; its `verification_2026.10.8.1.json` preserves the earlier result.
  `_build/install-local-2026.10.8.2.log` records successful installation.
- The .1 installer is superseded. Its first local installation attempt
  rolled back because Windows Restart Manager could not close all apps.
  The old app then closed normally; no unowned process was force-killed.
  The .2 build also includes the subsequently verified queue-recovery fix.
- This release was installed locally and **has not been uploaded** to the
  team Drive folder. To roll it out, publish the versioned .2 installer and
  matching notes file using the documented release workflow. This PC's
  background updater is enabled for subsequent releases in that folder.


## Phase 10 — Hand-over — 4 October 2026

- **`README.md`** was rewritten for whoever looks after NoteCraft. It covers:
  - what it is and its four engines;
  - running it, installed and from the project folder, and its ports;
  - the folders, and where data lives;
  - adding a category (no code) and adding a note type (code, with the real
    file names);
  - releasing an update (QBG One's workflow);
  - tests and gates.
- **Retiring the old apps.** The old apps stay on disk, untouched; Claude never
  deletes them. Each can stop being used day to day once its paid check in
  NoteCraft has been compared with the old app:

  | Old app | NoteCraft replaces it with | Retire after |
  |---|---|---|
  | Short Notes Studio (`shortnotes-core`, the old exe) | New notes → hand-drawn notes; Automation (the sheet) | one real lecture (the user made one in the installed copy on 4 Oct) **and** one real sheet row. Never run the old exe's sheet automation and NoteCraft's on the same sheet at once |
  | UPSC Notes (`Upsc_Notes_2\upsc_Notes`) | New notes → UPSC; Practice (Mains practice, DPP) | one real class and one real Mains practice set |
  | PW Books handwritten notes (`handwritten_notes_automation_complete`) | New notes → NEET-JEE → PW Books handwritten | one real deck |
  | UPSC file QC (`upsc_file_QC`) | Check notes (on any run, or an upload) | one full UPSC check. The account must be in the PW whitelist's "upsc_file_QC" column |

- **Keep the original folders on this PC even after retiring them:**
  - the project-folder NoteCraft runs the child engines on their `.venv`;
  - Runs → From older apps reads their old runs;
  - the build copies each engine's libraries from there.
- **QBG One is a separate app,** not part of NoteCraft. NoteCraft only copied
  its update workflow and uses its service account to read the release folder.
- **Open items, each needing the user's word:**
  - the paid checks above;
  - the next release, which carries the launcher port fix;
  - optionally a smaller installer (about 550 MB, by dropping the old Streamlit
    screens' packages and PW Books' unused Playwright);
  - optionally teaching `data_guard` to skip SQLite's `-wal`/`-shm` files.

## Phase 9 — Team installer, updates, tours — 4 October 2026

Status: **done.** The user's go covered all of these:
- installed on this PC;
- updated through the button, 2026.10.4.3 → 2026.10.4.4;
- release folder <https://drive.google.com/drive/folders/1vQXzi3kWd-k_LnVcvF-Izeq2tKEND0zO>,
  built into release.json from 2026.10.4.4.

**Install-then-update check** (doc 06's Phase 9 check), 4 Oct 2026, 23:24-23:52:
- `.3` installed silently to `%LOCALAPPDATA%\Programs\NoteCraft`, with desktop
  (OneDrive desktop) and Start-menu shortcuts.
- Its Settings → Updates was pointed at the folder.
- Check for updates found "2026.10.4.4 is ready to install", with its notes.
- The top bar showed "Update to 2026.10.4.4"; pressing it (POST
  `/update/install`, as the button does) started the download.
  - Download through Drive: 702 MB in about 23 min, SHA-256 checked.
  - The helper started the installer silently: `exit 0`, about 3½ min.
  - NoteCraft restarted itself and answers 2026.10.4.4, with the state
    "Updated to 2026.10.4.4."; the button is hidden again.
- `data_guard compare after_phase_9 → after_phase_9_update`: **nothing lost, no
  original touched.** Expected changes:
  - the user's own run (`runs\1`) survived the update;
  - the user's Google sign-in refreshed the shared token;
  - the installed copy's first start upgraded the UPSC and NEET-JEE packs, with
    `.bak` copies kept.

**Publishing:** `--publish` could not upload into this My Drive folder.
- The service account owns no storage.
- prashant.kumar15@pw.live, the NoteCraft sign-in here, has no access to the
  folder.
- The user dropped `NoteCraft_Setup_2026.10.4.4.exe` and `.txt` in by hand
  (QBG's documented way).
- The service account reads the folder, so teammates find updates.

**Launcher fix after `.4` was staged** (the user saw "port clash, opens the
already running app"):
- `launch.pick_port` looks for this NoteCraft first, on any port in 3100-3110.
- Any other app or NoteCraft copy on a port is skipped for the next free port,
  and is never opened.
- The old refusal "Another NoteCraft copy is running" (exit 1, so the desktop
  launcher's "could not start") is gone.
- NoteCraft's 3100-3110 never overlaps 3000 (old Short Notes), 8000-8009 (QBG
  One) or 8501-8510 (Streamlit tools).
- In the next release.

**Size** (the user asked): 702 MB installer, 2.19 GB installed.
- Libraries: UPSC 823 MB, PW Books 450, QC 311, NoteCraft 296.
- ffmpeg 193 MB, DPP bank 53 MB, Python 37 MB, code about 13 MB.
- Each engine carries its original's whole environment, so shared packages
  repeat (numpy ×4; PyMuPDF, pandas, pyarrow ×3), as do the old Streamlit
  screens' packages (streamlit, pyarrow, pandas, pydeck: about 130-160 MB per
  engine).
- PW Books also carries Playwright (103 MB), which its code never imports.
- Dropping those would bring it to about 550 MB, and needs the user's OK (it
  departs from "exact copy of each environment").

- **User decisions:**
  - Inno Setup installer.
  - The DPP bank travels in the package.
  - **QBG One's update workflow, copied** (4 Oct): "show update button beside
    the smart queue in blue, constantly highlighted, with the current version",
    then "copy the same update workflow just like qbg", then "you can copy
    service account too".
- **Copied from QBG One** (`QBG_Complete_Toolkit/QBG_Studio_Vibecraft/backend/studio`,
  read in place, nothing there changed):
  - `updater.py` → `note_studio/updater.py`, with QBG's logic line for line:
    - the Drive folder list and Drive's SHA-256;
    - download with size and hash refusal;
    - the detached PowerShell helper and exit 0;
    - `.result` exit-code read back after the restart;
    - the 15-minute lock;
    - `publish`.
  - `release.js`: a pop-up once per new version, with "Later" remembered in the
    browser. The button starts the update and shows the download %. The page
    reloads when the new version answers.
  - QBG's `.update-dialog` styles and its Settings → Updates card.
  - The build pieces, in `packaging/`:
    - `build_version.py`, `start_installed.py`, `desktop_launcher.cs` (tray,
      "Starting…", single instance), `launcher.manifest`;
    - `notecraft_setup.iss`: per user, a fixed AppId, no deletes;
    - `build_installer.py`, from `build_team_installer.py`.
  - The team's service-account key. The user's go; the copy prints only the
    email, `qbg-studio-bot@sage-tribute-501712-e9.iam.gserviceaccount.com`.
    - It now sits in `~/.note_studio/team_key.json`. Settings → System →
      Updates can add or remove it; only the email is ever shown.
    - The build carries it to `_internal/app/deployment/service_account.json`
      (QBG's `--team-key`; default = the key added in Settings, `--no-team-key`
      leaves it out).
- **Different from QBG, on purpose:**
  - Reading falls back to the person's own Google sign-in, which has the Drive
    scope already.
  - Publishing tries the service account (Shared drive only) and then the
    signed-in person (a person's Drive has storage).
  - An update refuses to start while notes are being made.
  - Errors are redacted (no tokens on a page).
  - `NOTE_STUDIO_OFFLINE` never reads Drive.
- **The top bar:** `v<version>` chip and the blue **Update to X** button beside
  Smart Queue, pulsing all the time it is there. It uses `nav.newer`, not
  `nav.update`: Jinja finds a dict's own `update()` method first, so the button
  never hid.
- **Installed layout:** `%LOCALAPPDATA%\Programs\NoteCraft\NoteCraft.exe`, with
  `_internal\python` (a copy of this PC's Python 3.13.5), `_internal\app` (the
  project, without tests, runs or secrets) and `_internal\libs\<engine>`.
  - NoteCraft's own libraries plus Short Notes are copied from this PC's Python:
    84 packages.
  - Each child engine gets its original's `.venv\Lib\site-packages`, exactly as
    that app runs it.
  - `engine_boot.py` puts an engine's folder first, then runs its unchanged
    `engine_main.py`.
  - `engine_host.command()/probe()` choose that route only when installed. A
    project-folder copy runs exactly as before.
- **The installer carries the Google sign-in registration**
  (`deployment/client_secret.json`), as the old Short Notes exe and QBG do. It
  is checked to be only `installed` client fields; tokens are refused.
- **Build checks:**
  - **QBG's `verify_runtime_imports`, per library folder:** NoteCraft 43; UPSC
    143 and PW Books 38 (third-party modules plus every `src` module); QC 48.
  - **Missing requirement filled in:** the build found the QC tool's
    `pyspellchecker`. Its `requirements.txt` and its released exe have it; its
    development `.venv` does not, which is why its 10 baseline spelling tests
    fail. The build adds a declared requirement from this PC's Python when the
    version fits (0.9.0), and says so.
  - **`verify_clean` + QBG's secret patterns:** only the registration and the
    team key may be secret-shaped, and both are checked.
  - **Clean start, with no sign-in, keys or originals:** `NoteCraft.exe
    --check`, 13 pages, the version, the Update button, `/update/status`, and
    Check setup's UPSC probe on its own libraries.
  - **One mock run per engine on the packaged copy** (`smoke_engines.py`): UPSC
    and PW Books end `needs_attention` (only the PDF step is stubbed); the QC
    basic check ends `complete`.
  - **Compiled with the ISCC from QBG's build cache,** used in place.
- **Tours/guides:** "Welcome to NoteCraft" has 7 steps, including Smart Queue
  and updates. The `/settings` guide covers Updates.
- **Builds** (`dist\team\`, 702.5 MB each):
  - `2026.10.4.2` has no team key: keep it as "A" for the install-then-update
    check.
  - `2026.10.4.3` has the team key and is the one to release.
  - Each has `NoteCraft_Setup_<v>.exe`, `.txt` notes, `SHA256.txt` and
    `QUICK_START.txt`.
- **Also fixed:** `db.recover()` now stops running work and sets the restart
  hold in one transaction. A test's leftover queue pump could start the next
  item in between; in the app the pump starts after `recover()`. Build `.3`
  was staged before this fix; the next build carries it.
- **Tests:** NoteCraft 184 passed (154 at Phase 8). The engine copies are
  unchanged (drift), so their own suites stand as at Phases 5-8.
- **Gates** (`after_phase_9.json` vs `after_phase_8.json`): drift is clean, but
  compare said **STOP**: `note_studio.db-wal` and `-shm` are "gone", and
  `note_studio.db` and the new `team_key.json` are listed.
  - Checked: the WAL was **0 bytes** at Phase 8, the `-shm` file holds no
    data, `note_studio.db` keeps its size (49,152 bytes), and
    `PRAGMA integrity_check` reads `ok` (read-only, `immutable=1`).
  - SQLite deletes both files when the last connection closes, and nothing
    else had the database open: NoteCraft on 3100 was not running, and was not
    stopped by Claude.
  - The one database change is the key copy clearing the stored update-check
    result.
  - Reported to the user, who went on ("yes install it"). `data_guard` is
    unchanged; teaching it to skip SQLite's `-wal`/`-shm` files was offered,
    not done.

## Phase 8 — Check notes (the UPSC file QC tool) — 4 October 2026

Status: **built and tested offline.**
- The basic checks were run for real, with no AI.
- A full, paid UPSC check still needs the user's go. It needs the account in the
  **"upsc_file_QC"** column of the PW whitelist; this tool's `pw_access` APP_NAME
  differs from the notes engines' "Handwritten Notes Automation".

- **User decisions:** every AI call goes through the PW proxy only, and web search
  (Claude through the proxy) is allowed.
- **Copy:** `engines/notes_qc/` (90 files). No engine file changed; drift shows only
  `engine_main.py` and `COPIED_FROM.md`. The copy's own suite matches the original's
  baseline exactly: 209 passed, the same 10 `test_spelling_check` failures.
- **`engines/notes_qc/engine_main.py`:**
  - checks the files with app.py's own `run_pdf_qc_batch` call (app.py:224-230);
  - reports each file in app.py's words (app.py:270-301: PARTIAL SCAN, unreadable AI
    report, check not run, filtered out, all refuted, N errors);
  - outputs the workbook plus the per-file reports;
  - PW-only: `GEMINI_API_KEY` and `GOOGLE_API_KEY` are removed from the child's
    environment, and `src.grounded_lookup._api_key` is replaced at run time to return
    "" (no file edited);
  - if PW refuses the account, the engine quietly skips its AI stages; NoteCraft says
    so in plain words, naming the whitelist column, and holds the queue;
  - `"full": false` runs only the checks that need no AI.
- **Who gets which check:** the tool's AI prompts are written for UPSC class notes.
  UPSC runs and uploads marked "UPSC notes" get the full check. Other notes get the
  basic checks (spelling, names, leftover markers, repeats), with no sign-in and no
  cost, as doc 08 suggests ("basic checks only" until a category has QC rules).
- **NoteCraft:**
  - `check_service.py` / `check_routes.py`: `/check` (upload a PDF, Word, PowerPoint,
    text or image file and choose UPSC or other) and **Check these notes** on every
    finished New notes run (its Word file, else its PDF, copied in).
  - Checks are runs (`source='check'`) in the Smart Queue's Notes lane, with the
    report downloads, Try again / Check again, and Stop.
  - Settings → AI models has a Check notes row (Gemini; the tool's own Claude fallback
    when the Gemini budget is spent).
  - All "coming soon" pages are now built.
- **Tests:** NoteCraft 154 passed (146 at Phase 7). `test_notes_qc_engine.py`:
  - **App-path parity:** the original's own `app.py` call (from its source) against
    NoteCraft's `engine_main`, basic mode, on the tool's test PDF. The same error-report
    workbook row for row, and the same `qc_result.json`.
  - The direct key is never used, even when set.
  - An upload checked end to end; "Check these notes" on a PW Books run.
  - A full check without sign-in holds the queue; upload problems; the AI models row.
- **Gates** (`after_phase_8.json`): nothing lost, no original touched. Drift: notes_qc
  adds only `engine_main.py`.

## Phase 7 — PW Books handwritten notes — 4 October 2026

Status: **built and tested offline (mock mode).** One real deck (paid) still needs the
user's go.

- **Copy:** `engines/pw_books/` from the working tree, including the files git does not
  track yet (`src/image_filter.py`, `src/style_config.py`, `equation_kit/`, two tests),
  plus `pw-app-kit/` for the kit-contract tests. 95 files, no secrets.
- **Copy's own suite:** 176 passed, 5 failed (the original: 181 passed). The 5 are
  `test_auth_refresh.py` refresh cases. They need the Google client settings in the
  original's `.streamlit/secrets.toml`, which is not copied on purpose, so they can
  only pass there. Noted in `copy_engine.py`.
- **`engines/pw_books/engine_main.py`** (new). It runs a deck exactly as the app does:
  - the fixed settings (app.py:1052-1062);
  - the backend processing mode (PROCESSING_MODE, default high_quality; app.py:1248-1261);
  - the model routing (`load_routing_config` / `check_models`, no probes by default /
    `resolve` / `available_chain`; app.py:1321-1341);
  - the `run_pipeline` call (app.py:1383-1406).

  Environment and models:
  - The models come from Settings → AI models as `NOTES_/VISION_/QC_MODEL_PRIMARY/
    FALLBACK_1/FALLBACK_2` and `IMAGE_MODEL_NAME`. The defaults equal the app's.
  - The token is passed as a plain argument, so `src/pw_auth.py` is unchanged and not
    used.

  What it reports:
  - Steps come from the pipeline's own stages, called by name; costs come from
    `gemini_generate` / `mathpix_ocr` / `gemini_image`.
  - Errors use the app's words ("Gemini API failed: …", "Processing failed: …").
  - A missing PDF or a page placeholder means needs attention.
- **Allowed change:** the extra-instructions hook in `src/prompt_builder.py`
  (`_with_extra` on `build_generation_prompt` and `build_merge_prompt`).
- **NoteCraft:**
  - The type `pw_books_handwritten` is ready. NEET-JEE offers it. A built-in NEET-JEE
    pack still seeded with only the hand-drawn type gets it added on start, with a
    `.bak`; an operator's own list is kept.
  - The form:
    - the app's four subjects only, with "Select subject…" required;
    - English or Hindi;
    - Concise/Complete and the exam;
    - under More options: maths rendering, strict equations, extra instructions;
    - PDF or PowerPoint uploads, one run per file.
  - Child engines share `note_studio/engines/child.py`; `make_service.CHILD_ENGINES`
    lists them.
  - PW Books receives only the type, category × type, profile and run layers
    (`requirements.APPLIED`). NEET-JEE's general rule files are the hand-drawn engine's
    own format rules. The New notes card and the run page show only what applies.
  - Settings → AI models has the PW Books rows: notes, slide pictures, checking and
    images; Gemini only.
- **Tests:** NoteCraft 146 passed (135 at Phase 6). `test_pw_books_engine.py`:
  - Prompt parity: the copy is byte-identical to the original for every subject × note
    type × language × exam × maths setting. With extra text: the original prompt, then
    the block.
  - **App-path parity:** the original's own `app.py` settings, mode, routing and call
    (taken from its source by `tests/parity/pw_books_app_driver.py`) against NoteCraft's
    adapter + `engine_main`, in mock mode. Same notes, slides data, filter report, run
    metadata (routing and models included) and output files.
  - One deck end to end through New notes; form problems; a PowerPoint accepted; the
    pack upgrade; only its own rule layers.
- **Gates** (`after_phase_7.json`): nothing lost, no original touched. Drift: pw_books
  `src/prompt_builder.py` (allowed) plus `engine_main.py`.

## Phase 6 — Practice: UPSC Mains practice (DMP) + DPP — 4 October 2026

Status: **built and tested offline.**
- Mains practice is tested with a canned-reply client (no cost).
- DPP is tested with the real question bank (DPP uses no AI).
- A real paid Mains practice set still needs the user's go.

- **Decision taken by default (the user said "start" without answering).** The DPP
  workbooks are read where they are.
  - Default: the UPSC engine copy's `data\` folder (copied in Phase 0), which is the same
    place the old app looks.
  - Settings → System → DPP question bank can point to another folder, for example a
    shared copy for teammates.
  - The installer question (carry the 55 MB bank or not) stays for Phase 9.
- **`engines/upsc/engine_practice.py`** (new Note Studio file; `engine_main.py`
  dispatches every action other than "notes" to it). One step = one child process, as
  one button press is one step on the old app's page:
  - **DMP steps** (app.py:1302-1560):
    - `dmp_questions` → `service.generate_questions`;
    - `dmp_answers` → `service.generate_answers` (only the ticked questions with no
      answer yet);
    - `dmp_export` → `export.export`, into `<copy>/dmp/sheets`, as app.py:1409. The
      question paper gets the ticked questions; the answer sheet gets every answered
      one, as in the app.
    - The client is app.py's `_dmp_client` (`DEFAULT_MODEL`, a new `upsc-dmp` task id
      per step).
  - **DPP steps** (app.py:1678-2325):
    - `dpp_topics`, `dpp_counts`, `dpp_build`, `dpp_eligible`, `dpp_pick`,
      `dpp_replace`, `dpp_remove`, `dpp_export`;
    - the file names, the zip, the DPP No. and the subject follow app.py:1978-2113
      (that logic lives in the page, so it is reproduced there, line-referenced).
- **The old run is never written.** The old app writes DMP and DPP into
  `runs/<lecture>/dmp|dpp/`. Here, starting a practice set copies the lecture's notes
  files and `run_metadata.json` (copy2) into `runs/<id>/source_runs/<lecture>/` in
  NoteCraft. The engine works on that copy with `runs_dir`, the way app.py treats an
  uploaded note.
- **The question bank cache.** Reading the bank takes about 35-50 s. The parsed
  `load_masters()` result is pickled in `~/.note_studio/engines/upsc/dpp_cache/`, keyed
  on both workbooks' path, date and size plus the reader's own code. Later steps take
  about 1.5 s.
- **NoteCraft:**
  - `practice_service.py` and `practice_routes.py` (`/practice`,
    `/practice/<id>`).
  - Each practice set is a run with `source='practice'`. Its page state (topics, set,
    seen, exports) is in `runs/<id>/practice.json`.
  - Paid steps (questions, answers) wait in the Smart Queue (kind `practice`, Notes
    lane; a missing sign-in holds the queue). Free steps run at once, one at a time
    per set.
  - Practice lists NoteCraft's finished UPSC classes and the old app's runs.
  - The DMP page:
    - questions by level, with ticks;
    - answers shown in place;
    - "Make model answers for the ticked", "Question paper (ticked)" and "Model
      answers sheet";
    - several sets per lecture, with a picker.
  - The DPP page:
    - subject, chapter and weaker-match filters, as the app has them;
    - the Marathi notice (English or Hindi only);
    - verified-only;
    - by difficulty (available counts, default min(5, available)) or pick myself (25 per
      page, ticks kept in order);
    - the set with Replace and Remove;
    - "Start a new set" (what was shown stays shown);
    - DPP No. and subject, then six files.
  - New notes shows the two practice types as links to Practice. `/runs/<id>` of a
    practice set opens Practice. Check setup now lists the DPP question bank.
  - `app.js`: the confirm question can come from the pressed button (one form, several
    actions).
- **Fixes found while testing:**
  - A restart test raced the queue pump; the "paused" hold now stays until `recover()`.
    Same in `test_queue.py`.
  - `db.connect()`: two connections turning a brand-new database to WAL at the same
    moment got "database is locked" at once. It now retries briefly. Only tests switch
    data folders under a running pump.
  - Practice question text is Markdown in the engine; its escapes ("1\.") are removed
    for display.
- **Tests:** NoteCraft 135 passed (129 at Phase 5). `test_practice.py`:
  - **Mains practice** on a mock class:
    - questions through the queue, 10 in three levels;
    - 2 answers;
    - both sheets, named `..._DMP_07_...` from the lecture's DCN No.;
    - download;
    - the class's folder byte-for-byte unchanged;
    - path guard.
  - **DPP from an "old app" run**, with the real bank:
    - topics, counts, set, Replace, Remove, six files `..._DPP_07_...`;
    - "Start a new set", then Pick myself: no question already shown is offered again;
    - the old run byte-for-byte unchanged.
  - Guards, New notes → Practice, the Settings folder (refused unless both workbooks
    are there; reset), sign-in holding the queue.
  - The UPSC copy's `src/` is unchanged since Phase 5 (drift), so its own suite stands at
    1392 passed / 8 skipped.
- **Seen in the preview:** a real DPP set built from one of the old app's runs. The
  gates show that run unchanged.
- **Gates** (`after_phase_6.json` against Phase 5):
  - Nothing lost, no original touched, no data changed.
  - Drift as before, plus the new `engine_practice.py`.
- **Not done:**
  - the optional "Edit notes text + Rebuild";
  - the old app's DMP/DPP "Upload notes" route (PDF/TXT/MD from anywhere): Practice
    works from finished runs only.

## Phase 5 — Engine host + UPSC notes — 4 October 2026

Status: **built and tested offline (mock mode: no sign-in, no network, no cost).**
Still waiting on the user:
- the paid check, one real class compared with the old app;
- the paid checks from Phases 2 and 3.

- **The "ask first" question settled itself.** Both copies' `pw_access.py` already use
  APP_NAME "Handwritten Notes Automation", so one whitelist column and one cost line
  cover both. Nothing changed.
- **`note_studio/engine_host.py`** (QBG `stream_command` + `stop_process`):
  - one child process per run: `<python> engines/<id>/engine_main.py --request
    <run>/request.json --out <run>`, cwd = the engine folder;
  - output is JSON lines (step / cost / warning / output / done); any other line goes to
    `<run>/engine.log`;
  - the environment is UTF-8 and unbuffered, with no bytecode written;
    `NOTE_STUDIO_*` / `SHEETS_BRIDGE_*` settings are not passed on;
  - the Google token goes only into the child's environment (`NOTE_STUDIO_PW_TOKEN`),
    never into `request.json`;
  - each child gets its own TEMP (`<run>/tmp`);
  - Stop is `taskkill /T /F` (the whole tree);
  - every child is in a Windows job object with KILL_ON_JOB_CLOSE, so a class cannot
    keep running and spending after NoteCraft is closed or crashes.
- **Runtime.** The UPSC child runs on the original app's own `.venv` Python, read only
  (`PYTHONDONTWRITEBYTECODE`), until Phase 9 bundles a runtime. NoteCraft's Python
  lacks `yt_dlp` and `youtube_transcript_api`. `NOTE_STUDIO_UPSC_PYTHON` overrides it.
- **`engines/upsc/engine_main.py`** (new file; no edit to any engine file except the
  hook below):
  - sets `HANDWRITTEN_NOTES_ROOT` (the copy), `HANDWRITTEN_NOTES_ENV` (a path that
    never exists), `RUNS_DIR` / `OUTPUT_DIR` (inside the run folder) and the model
    names from `request.json` **before** importing `src`;
  - calls `run_pipeline` exactly like app.py: the backend defaults at app.py:2729-2759
    and the call at app.py:3059 (`workflow="upsc"`, `note_type="complete"`,
    `area="auto"`, strict quality, all QC / DCN / source options on, the error report,
    the app's own logo);
  - maps the stage labels to the app's own "Step n/5" words (`STEPS` +
    prefix rules);
  - puts the copy's ffmpeg on PATH only when none is there, as the packaged launcher
    does;
  - points the engine's writable cache (`src/qc/paths.app_data_dir`) at
    `~/.note_studio/engines/upsc`, not the old app's `%LOCALAPPDATA%\UPSC_Notes`;
  - reports each call's proxy-reported `cost_inr` as a cost event, by wrapping
    `pw_access.gemini_generate` / `gpt_generate` at run time;
  - maps errors to the app's plain words (`user_message`, `_FRIENDLY_ERRORS`);
    `PermissionError` is a sign-in problem and holds the queue;
  - status: no PDF or no error report → needs_attention.
- **Allowed change: `engines/upsc/src/prompt_builder.py`.**
  - `EXTRA_INSTRUCTIONS` + `_with_extra()` wrap the returns of `build_upsc_prompt`,
    `build_upsc_dcn_prompt`, `build_dcn_chunk_prompt` and `build_dcn_merge_prompt`.
  - With empty text each prompt is returned unchanged.
  - Requirement layers 2-7 go in as one block at the very end.
- **NoteCraft:**
  - 4 UPSC types ready (DCN, simple, Mains-ready, Prelims-ready).
  - The `upsc` pack is switched on.
    - A seeded pack still byte-identical to Phase 2's is upgraded on start, with a
      `.bak`; an edited one is left alone. The user's own copy is unedited, so it is
      upgraded.
    - First subject: "Not set (as in the old app)" (`upsc`, what the app sends for an
      empty subject box).
    - The language must be chosen (the app's rule).
  - New notes for a class:
    - a recording link (required, as in the app) and optional slides (.pdf / .pptx /
      .ppt);
    - lecture title and DCN No.;
    - under More options: DMP No., DPP No. and extra instructions;
    - one queue item per class, in the Notes lane.
  - Run page:
    - Stop for a running class;
    - Word / PDF / Excel / text downloads;
    - the engine's warnings;
    - the recording link;
    - a needs-attention note.
  - Settings → AI models follows each engine's rules: UPSC has notes, speech and
    pictures stages, two models where the engine has a fallback, and no GPT for speech.
  - Settings → System → Check setup: the engine's Python and packages, ffmpeg/ffprobe,
    LibreOffice or Word, and the fonts (Coming Soon, Kalam). New notes warns when no PDF
    maker is found.
  - Runs → From older apps: the original's `runs\`, read in place, offering only what
    the app's page offered; "Test run" marks mock/fake-only runs.
    - All 88 runs there now are test runs written by the original's own suite during the
      Phase 0 baseline (today 02:58-03:01). They are left as they are; deleting them is
      the user's decision.
- **Tests:**
  - NoteCraft: 129 passed (106 at Phase 4). New:
    - `test_engine_host.py`: events, token only in the environment, own TEMP, crash,
      Stop ends the grandchild.
    - `test_upsc_engine.py`:
      - prompt parity: the copy's 18 builder prompts are byte-identical to the
        original's; with extra text, the original prompt + the block;
      - **app-path parity**: the original's own `app.py` defaults block and
        `run_pipeline(...)` call, taken from its source by `tests/parity/upsc_app_driver.py`,
        against NoteCraft's adapter + `engine_main`, in mock mode with slides and a
        link. Result: identical `prompt_upsc.txt`, the same output files, the same
        run_metadata keys and settings;
      - one class end to end through New notes → Smart Queue → child → run page →
        download;
      - no sign-in, form problems, de-duplication, older runs (read only, path
        guards), Check setup, AI model stages, the pack upgrade, and Stop.
  - UPSC copy, its own suite, run alone: 1392 passed, 8 skipped (reference files and
    the PyInstaller build are not on this computer), 0 failed. Original baseline: 1394
    passed, 6 skipped; Phase 1 copy: 1390 / 9 / 1 failed.
    - A first run that overlapped with another engine test gave 47 skips and 1 failure;
      run alone, it was clean.
    - The suite writes test output into the copy's `runs\`; it was removed afterwards.
  - Shortnotes copy: unchanged this phase (drift).
- **Gates** (`after_phase_5.json` against Phase 4):
  - Nothing lost, no original touched, no data changed or new.
  - Drift: shortnotes `categories.py` and `pw_transport.py`; upsc `src/prompt_builder.py`
    (all allowed) plus the new `engine_main.py`.
- **Not done / open:**
  - The chunked DCN path (long classes) is checked at the prompt-builder level (byte
    identical), not by a full mock run: mock mode turns chunking off.
  - The app's developer-only options (depth, quality level, fact mode, templates) are not
    offered; the app's normal page does not offer them either.
  - UPSC classes share the Notes lane with hand-drawn notes (one at a time).
  - The What's new tour comes in Phase 9. Page guides were updated (Overview, New notes,
    Runs, Settings).

## Phase 4 — Smart Queue, Settings, Add category — 4 October 2026

Status: **built and tested offline (stubbed model and Google, no cost).** Not yet tried
live: a real budget-spent switch (it needs a provider whose budget is actually spent).
The paid checks from Phases 2 and 3 (one real lecture, one real sheet row) still need the
user's go.

- **Per-run model (allowed change, told to the user):**
  - Where: the copy's `sheets_bridge/pw_transport.py`.
  - What: `install(token_provider, model=, fallbacks=)` keeps the model, the account and
    the fallback order in a context variable (`_RUN`). Two runs at once each keep their
    own. A thread that never installed uses the last install, as before.
  - A PW "budget exceeded" answer moves the run to the next model of the *other*
    provider for the rest of that run, and the spent provider rests 30 minutes
    (`_SPENT`). With no fallbacks the error stands, as in the original.
  - The data_guard reason was updated. The engine copy's suite is the same as the
    Phase 1 copy: 1602 passed, 2 known failures.
- **Smart Queue (`job_queue.py`, `/queue`, strip on New notes and Sheet automation):**
  - Two lanes: Notes (one queue item per PDF) and Sheets (sheet runs). Each lane runs one
    item at a time, in order; the two lanes run side by side.
  - The queue holds after a Stop, a lost sign-in, a restart, or Pause, until Continue. A
    failed item does not hold it.
  - On restart, the item that was running is marked stopped and never started again (no
    double payment).
  - Remove works on items that have not started; their files are deleted. The same
    request twice is added once.
  - "Try again" and "Make again" add a new queue item. "Start another" is on every run
    page (New notes, or Sheet automation for sheet runs).
  - The one-at-a-time rule of Phase 3 is gone; lanes replace it.
- **Settings is now five tabs:**
  - Sign-in;
  - AI models: model order per note type and stage. The first model makes the notes;
    the next ones are used only on a spent budget. `models.apply()` gives the sheet run
    the same fallbacks, and the Sheet automation model menu now preselects the first
    model from this tab;
  - Categories:
    - edit rule files, with a `.bak` first;
    - add rules for a subject or a note type;
    - Preview, showing every layer a run follows;
    - Check category, which includes "the engine sees it";
    - switch a category on or off (`category.json.bak`);
    - Add category, a form that copies `_template` (`category_admin.py`);
  - Values: the whitelisted knobs, with labels and checks. They are used the next time
    NoteCraft opens;
  - System: engines (their "For" column now lists the switched-on categories that use
    them), note types, storage, about.
- **Fixes found while testing:**
  - `go()` put the toast after a `#section`, so the browser dropped it. This affected
    Settings → Categories and Sheet automation (#live, #sheets, #checks). The toast now
    goes before the `#`.
  - A New notes run that failed before its first AI call (for example at sign-in) could
    show the cost left over from an earlier task. The cause: the engine's cost counter
    falls back to one shared list before `set_task`. The adapter now reports ₹0 when the
    task never started.
  - On narrow screens the gap between the two lanes was doubled.
- **Checks (Phase 4 plan):**
  - `test_queue.py`: 3 PDFs + 1 sheet run. The lanes run side by side, A, B and C in
    order. Stop holds the queue and the next PDF waits; Continue finishes it; a restart
    stops the running item and holds the rest.
  - `test_category_admin.py`: "Test CLAT" with one rules line, added from the Settings
    form only.
    - Check category says the engine sees it.
    - Preview shows the rule.
    - New notes offers it.
    - The prompt the (stubbed) model received contains "CATEGORY: Test CLAT notes." and
      the rule.
    - Rule edits keep a `.bak`, a path outside the pack is refused, and switching off
      takes it off New notes.
  - `test_per_run_model.py`:
    - two threads with different models and accounts;
    - the budget-spent switch to GPT, skipping the other Gemini model;
    - the 30-minute rest;
    - no fallbacks → the original error;
    - Settings → AI models updates the fallbacks and the sheet model menu.
- **Tests:** NoteCraft 106 passed (89 at Phase 3). The `fake_model` / `lecture_pdf`
  fixtures moved to `tests/conftest.py`.
- **Gates** (`after_phase_4.json` against Phase 3):
  - Nothing lost, no original touched, no data changed or new.
  - Drift: `notesbot/categories.py` and `sheets_bridge/pw_transport.py` (both allowed).
- **Not done:**
  - The What's new tour (`whats_new.TOURS`) is built in Phase 9 per the kit's plan.
    Phase 4 updated the page guides (Settings, Sheet automation, Smart Queue).
  - The user's own NoteCraft on port 3100 still runs Phase 3 code. Close and reopen it
    to get Phase 4. The database adds its new column by itself.

## Phase 3 — Sheet automation — 4 October 2026

Status: **built, tested offline, and the dry-run check matches the old CLI exactly.** A
real (paid) row still needs the user's go.

- **User decisions (this phase):**
  - Sign-in reuses the old project's whitelisted Google sign-in.
  - AI calls go through the old project's PW kit (`pw_access`, app "Handwritten Notes
    Automation").
  - `~/.shortnotes_bridge` is shared (the Phase 0 question). `old_app.adopt_default()`
    sets "Use them" where that folder exists and nothing was chosen; Settings can still
    switch to "Keep separate".
- **Run code: the old app's, unchanged.** NoteCraft drives
  `sheets_bridge.webui.RunController` through its own `api_response()` (run, stop,
  preview, tabs, and its option checks `_clean_options`). There is no second copy of the
  run logic. The remote run-log tab is still written by `batch.run_targets` itself.
- **`note_studio/automation_service.py` + `automation_routes.py` + `/automation`.**
  - Run options:
    - sheet, or "All sheets" (the switched-on ones);
    - category (follows the chosen sheet's saved category);
    - subject (from column D / auto / fixed);
    - AI model (`SELECTABLE_MODELS`);
    - page numbers;
    - under More options: rows limit, one row, redo, spend limit, Sanskrit style.
  - Try it first is a dry run. Run now shows the old app's preview (ready rows per tab),
    then "Yes, make the notes".
  - Live card: made / failed / would be made / not reached / spent, the row position,
    Stop after this row, the outcome table with Drive links, and the full log.
  - History: every sheet run is a `runs` row (`source='sheet'`) with one `run_items` row
    per sheet row (outcome, code, Drive link, cost) and the full log saved. It survives a
    restart, and a run left running is Stopped.
  - Problems are in plain words (`CODE_WORDS`).
  - One notes maker at a time: a sheet run refuses to start while a New notes run is
    being made, and the notes worker waits during a sheet run, because the engine's model
    setting is process-wide until Phase 4.
- **Sheet list: `note_studio/sheet_list.py`.**
  - The engine's own `save_profiles` drops unknown keys and keeps no backup, so NoteCraft
    writes the shared `sheets.json` itself:
    - a fresh read before each change, so a sheet the old app just added is kept;
    - every unknown key kept;
    - checked with the old app's reader before writing;
    - `sheets.json.bak` plus dated copies in `~/.note_studio/backups/sheets/` (last 30).
  - Category and note type per sheet live in NoteCraft's DB (`sheet_extras`), never in
    the shared file, which the old app's save would strip.
  - Add a sheet: name + link + "Load its tabs" (the old tab classifier) or every tab,
    plus a notes folder. It is checked with Google as the signed-in person (the old
    `verify_target` / `verify_workbook` / `verify_output_folder`).
  - Edit: name, signature, spend limit, also-PDF, on/off, notes folder, skipped batches,
    category, note type.
  - Remove: list entry only, with a backup.
  - With no sharing choice, the list is refused rather than written into the engine
    copy's folder.
- **Checks** (background, with output captured per thread by `capture.py`):
  - Check setup (`setup_check.run_check`);
  - Test the AI model (`model_check.test_model`, one tiny call);
  - Try one PDF (`model_check.probe_pdf_file`, paid; asks first).
- **The old app being open** is detected by its `Server: shortnotes-local` header on
  port 3000, and the page warns.
- **Check run (read-only, free):** the test sheet "Short Notes Automation TEST
  2026-08-25", dry run, old CLI (`python -m sheets_bridge --dry-run --no-browser
  --profile …`, run from the original unchanged) against NoteCraft (signed in from the
  shared folder; allowlist passed).
  - Both planned rows 70 and 86, with identical plan lines (file, name, output name,
    folder, language, subject, signature, header) and identical skip counts (77 already
    done, 8 not ready).
  - The logs are identical except NoteCraft's two lines from `RunController`
    ("signed in as…", "profile(s): …").
- **Tests:** NoteCraft 89 passed. The new ones cover the sheet list (unknown keys,
  `.bak`, fresh read, the old app's reader and the original's reader in a subprocess,
  extras, rename) and automation through the real `RunController` with fake Google and
  AI (options mapping, history, dry run, all sheets, Stop, errors, sign-in, one at a
  time, restart, add/edit, checks with thread capture, old-app detection).
- **Gates** (`after_phase_3.json` against Phase 2):
  - Nothing lost, no original touched.
  - Data changed or new: `~/.note_studio/*`, from the user starting the Phase 2 build
    (the packs seeded, settings imported; their copy was running on 3100 and was not
    touched).
  - One Google token in `~/.shortnotes_bridge/tokens/` refreshed by the dry-run
    sign-in.
  - The shared `sheets.json` was not changed.
  - Drift: only `notesbot/categories.py` (allowed).
- **Not in the app yet:**
  - a per-run signature override (`--signature`; the sheet's signature field and the
    imported `SHEETS_BRIDGE_SIGNATURE` cover it);
  - the probe's group, language and verify options;
  - a one-off sheet that is not saved (`--spreadsheet-id/--gid`);
  - changing a saved sheet's link or tab (remove and add it again);
  - sheet-level extra instructions (requirement layer 6: the automation path has no hook
    for them).

## Phase 2 — categories, note types, requirements, New notes, Runs — 4 October 2026

Status: **built and tested offline.** The paid check (one real lecture through NoteCraft
compared with the old exe) is still to do; it needs the user's go.

- **Category packs.** They live in `~/.note_studio/categories/<id>/` and are seeded on
  first start, only if absent:
  - `neet_jee` and `foundation`: rules are copied byte for byte (`shutil.copy2`) from
    `engines/shortnotes/notesbot/format_rules/`, and `switches.json` holds that
    category's `rules.json` formats;
  - `upsc` is switched off until Phase 5;
  - `_template` is copied from the kit.

  Built-in definitions live in `note_studio/categories.py` and `note_studio/packs/`. The
  category × note-type rules go in `rules/types/<type>.txt`, so the engine never reads
  them as a subject.
- **The one allowed change in the copy:** `engines/shortnotes/notesbot/categories.py`.
  - When `NOTE_STUDIO_CATEGORY_PACKS` is set, the category list, labels, legacy ids,
    rule texts and switches come from the packs (enabled packs that offer
    `concise_handdrawn`). `#` lines are dropped.
  - When it is unset, behaviour is exactly as before.
  - The snapshot test (`tests/test_category_packs.py` against
    `tests/snapshots/categories_before.json`, made from the original and the copy before
    the change, which were identical) shows `prompt_block`, `commands`, `switches`,
    `fingerprint`, `normalize` and the full prompt hash are byte-identical for every
    category × subject (aliases included), with and without packs.
- **Registry:** `note_studio/note_types.json` + `note_types.py`.
  - `concise_handdrawn` is ready, with the old Upload page's fields and defaults
    (teacher name, PW logo watermark on, teacher-name watermark off, extra instructions
    of at most 1000 characters).
  - The other 7 types and the `notes_qc` tool are "coming".
  - The cache keys are checked against the engine's constants.
- **`requirements.resolve()`.** Layers 1-7, comments dropped, switches merged and a
  fingerprint, all tested. The hand-drawn engine applies layers 3-4 itself (packs); its
  adapter sends layers 2, 5, 6 and 7 as the engine's own "instructions". With only the
  run box filled, that is exactly the user's text.
- **Adapter:** `note_studio/engines/shortnotes.py` re-composes the old desktop Upload job
  (`web_service.run_job` + `studio_server._local_generate/_local_render`) from the same
  engine functions, in the same order. It does not build `JobRecord`, which only accepts
  the old fixed categories and subjects.
- **Parity proof:** `tests/test_parity_upload.py` + `tests/parity/`.
  - A stub replaces only `pw_access.gemini_generate`, so everything else runs for real,
    with no network and no cost.
  - The original's real `run_job` and the NoteCraft adapter each run in their own
    process, for 4 cases: NEET-JEE auto, Foundation Maths with the teacher watermark,
    Foundation Sanskrit with no logo, and NEET-JEE Physics Hindi with extra instructions.
  - In every case the model requests are byte-identical (SHA-256), and the notes, the
    file name, the page count, the page text and the PDF bytes (minus fpdf's time stamp
    and id) are identical.
  - A guard hashes the original's three upload closures, so a change there is flagged.
- **Sign-in:** `note_studio/signin.py` and the Settings card.
  - It uses the copied `sheets_bridge.auth_session.AutomationAuth` on the folder chosen
    in Settings.
  - A saved sign-in is restored in the background at start, never during a page render.
  - "Sign in with Google" opens the browser once.
  - Model calls go through the signed-in account's PW proxy transport, as in the old app.
- **New notes (`/new`).** Category cards → type cards ("Soon" for types that aren't
  ready) → subject and language (the old page's exact lists) → PDF drop area (more than
  one allowed) → teacher name (remembered in the browser) → options → Make notes.
  - Each PDF becomes one run.
  - Form problems are explained in plain words.
  - "Rules that apply" refreshes when the subject changes.
- **Runs (`/runs`, `/runs/<id>`).**
  - Filters: All / Waiting or running / Done / Did not finish, plus category and search.
  - The run page polls every 2 s while active and shows the steps (engine call lines in
    plain words), the cost the proxy reported, Download, Make again / Try again, Remove
    while waiting, and the rules the run followed.
  - One worker makes runs one at a time.
  - After a restart, a run left running is Stopped and waiting runs hold until Continue.
  - Notes made inside the app process cannot be stopped mid-lecture (the old app could
    not either); the page says so.
- **First-run import** (`settings_store.py`):
  - Copies non-secret knobs once from `shortnotes-core/.env` into NoteCraft's settings;
    there are 32 names on the allow-list (the `KNOBS` tuple in `settings_store.py`), and
    secrets are never read.
  - Precedence: the environment, then imported values, then the old exe's launcher
    defaults, applied before the engine is imported.
  - Today the `.env` knobs and the exe defaults are identical.
- **Pages and guides.** New notes and Runs left the coming-soon list (the tiles and menu
  read it). Page guides were added for `/new` and `/runs`. No release is built, so there
  is no what's-new tour yet.
- **Tests.**
  - NoteCraft: 68 passed (the new ones: packs and the snapshot, requirements, registry,
    parity ×4 + guard, New notes end to end with the stub, settings import, plain words).
  - Engine copy, own suite: 1602 passed, 2 failed, the same two known failures as the
    Phase 0 copy.
- **Gates** (`after_phase_2.json`):
  - `compare` against Phase 1: nothing lost, no original touched. One new data file,
    `~/.note_studio/note_studio.db`, created 03:53 by the Phase 1 build being started
    once (empty; no runs, no settings).
  - `drift`: shortnotes has 1 change, `notesbot/categories.py` (allowed).
- **Docs out of date:** the saved name is now `<lecture> Concise Notes.pdf`
  (`output_naming.OUTPUT_SUFFIX`); " - Concise Notes.pdf" is the legacy form, still
  recognised.
- **Not reachable from the app yet:**
  - the sheet automation (Phase 3);
  - Smart Queue, model choice per run, editing categories and rules, Add category, and
    changing values (Phase 4);
  - UPSC and "From older apps" (Phase 5);
  - practice and check (Phases 6-8).

## Phase 1 (skeleton and the look) — "NoteCraft" — 4 October 2026

Status: **the Studio skeleton is built and tested.** The user asked for QBG One's premium
look and a name; "Notes One" was declined, so the name is **NoteCraft** (in
`note_studio/brand.py`, one edit to change; the package and `~/.note_studio` keep the
project name). The four Phase 0 questions are still open; nothing below depends on them.

- **Look.** QBG One's family: an Apple-like glass top bar with full-width menus (Overview ·
  Make notes ▾ · Results ▾ · Smart Queue; Help on WhatsApp, Settings, account), a slim bar,
  and white cards on a light grey page. The accent is **violet `#6a3cc8`** (white on it
  6.8 : 1), so it is never confused with QBG's blue. The mark is a silver note-page with
  an "N" and a violet nib. Phone width: the menu becomes one sheet. Styles are in
  `note_studio/static/style.css` (tokens in `:root`); HTMX 2.0.10 was copied from QBG
  (`static/vendor`).
- **Pages.**
  - **Overview:** hero, Start tiles, Running now, Needs you, Note engines, Recent.
  - **Settings:**
    - the old app's sheets and sign-in, as a one-click choice that only records the
      choice (it reads counts, never token contents, and never writes the shared folder);
    - Note engines, read from each `COPIED_FROM.md`;
    - Where your work is kept;
    - About.
  - **Coming-soon pages** for New notes, Sheet automation, Runs, Practice, Check notes
    and Smart Queue (`coming_routes.py`). Each says in plain words what it will do; remove
    its entry when the real page lands.
  - The 404 and crash pages are friendly. A crash shows a reference and writes the
    traceback to `~/.note_studio/logs/studio.log`.
- **Plumbing.**
  - `app.py` is wiring only: `render`, `go` with toasts, `asset` with mtime, the
    `_same_site_only` guard and the error pages.
  - `db.py`: SQLite WAL with `settings`, `runs`, `run_items`, `run_events` and `job_queue`.
    `recover()` stops runs left running and holds the queue.
  - `launch.py`: ports 3100 to 3110. It reopens a running copy (`/api/instance` = a hash
    of the DB path) and never opens another copy's database.
  - `engine_catalog.py` appends `engines/shortnotes` to `sys.path`.
  - `old_app.py` sets the `SHEETS_BRIDGE_*` paths and the engine's own launcher defaults
    (read from the copy's `packaging/studio_launcher.py`) only once a choice is made.
- **Start:** `Start NoteCraft.bat` / `run_studio.py`, using the global Python 3.13 (no
  install). `requirements.txt` lists what a Note Studio venv would need.
- **Tests (`python -m pytest` from the project root): 26 passed.**
  - Every page and menu link returns 200 with no traceback.
  - Static files are local; 404 and crash pages work; the same-site guard works.
  - The share choice works (yes / no / invalid), and the shared folder is never written.
  - DB: WAL, settings, `recover()`.
  - Launcher port logic.
  - pyflakes; no bare `src`/`config` while `notesbot` + `sheets_bridge` load from the copy.
  - The name appears in no template.
- **Checked in a real browser** (headless Edge, 1440 px and 520 px, scratch data folder).
  Headless Edge cannot go below 504 px, so 390 px shots are cut off by the tool.
- **Gates:**
  - `compare` against the baseline: nothing lost, no original touched, 0 changed; the
    1226 new files are still only the Phase 0 UPSC test runs.
  - `drift`: 0 changes in both copies.
  - The real `~/.note_studio` was not created (the preview used a scratch folder).
- **Not reachable from the app yet:** everything behind the coming-soon pages (Phases
  2-8), sign-in, and model and category settings.

## Phase 0 — safety net, baselines, engine copies — 4 October 2026

Status: **Phase 0 done, plus the two core engine copies (shortnotes, UPSC).**
The four Phase 0 questions are still open (see the end). Nothing in `note_studio/`
exists yet; Phase 1 has not started.

### Backup and baseline

- Backup: `~/NoteStudio_backups/notes-data-20261004-025153.zip`. It holds 7286 files
  (1295 MB before compression, 1.15 GB zip) and contains secrets. Keep it on this
  computer.
- Baseline snapshot: `NoteStudio_Kit/baseline.json` (7825 files, taken before any
  test or copy).
- Gate after Phase 0 (`NoteStudio_Kit/after_phase_0.json`): `compare` shows no data
  lost and no original touched. It shows 0 changed and 1226 new data files, all made
  by the UPSC original's own test suite (see below). `drift` shows 0 changes in both
  copies.

### Test baselines (each original, inside the original, changing nothing)

Command: `python -m pytest tests -q -rfE -p no:cacheprovider` with
`PYTHONDONTWRITEBYTECODE=1`, run with the original's own `.venv`. shortnotes-core has
no `.venv`, so it used the global Python 3.13.5, which has every package except
`vercel_oidc`. QC also set `MAX_PARALLEL_PDFS=1`.

| Original | Result | Known failures (not regressions) |
|---|---|---|
| shortnotes-core | 1603 passed, 1 failed (2m47s) | `test_visual_pipeline.py::test_exact_source_label_layers_survive_generation_and_pdf_render` (glyph hashes, machine-dependent) |
| Upsc_Notes_2/upsc_Notes | 1394 passed, 6 skipped, 0 failed (4m27s) | none |
| handwritten (pw_books) | 181 passed, 0 failed (18s) | none |
| upsc_file_QC | 209 passed, 10 failed (41s) | all in `test_spelling_check.py`: `test_dropped_letter_typos_are_reported_with_the_right_word`, `test_transposed_letters_are_reported`, `test_a_capitalised_typo_of_a_word_the_notes_use_correctly_is_reported`, `test_a_capitalised_typo_with_a_word_suffix_is_reported_even_if_unique`, `test_the_row_quotes_the_page_and_the_sentence`, `test_a_document_with_a_typo_gets_a_language_row_without_any_ai`, `test_a_typo_is_not_accepted_as_a_british_spelling`, `test_a_typo_is_not_accepted_as_a_plural_of_a_dictionary_fragment`, `test_a_word_beside_an_unknown_word_is_domain_vocabulary`, `test_a_correction_nobody_writes_is_not_offered` |

**Warning: the UPSC suite writes into its project's `runs/`.** Each full run makes
about 31 run folders + zips (`run_<date>_<time>_<id>`). `test_practice_flow_runtime.py`
also writes `dmp/index.json` and `dpp/*` into the **newest real run**. The Phase 0
baseline run added 31 test runs (`runs/run_20261004_0258*`–`0301*`) and 13 files in
`runs/run_20260919_003759_c3ab3721/{dmp,dpp}`, 137 MB in total. Nothing was
overwritten, and `compare` lists them as DATA NEW. The `runs/run_20260917_1736*`
folders look like an earlier test run's leftovers too. **Run the UPSC copy's tests
with `RUNS_DIR` pointed at a temporary folder** so they never write into the copy or
anywhere else.

### Engine copies

Made with `NoteStudio_Kit/tools/copy_engine.py <engine>`. It copies byte for byte from
an allow-list, never overwrites, and writes `COPIED_FROM.md`. All 412 copied files
were verified by SHA-256 against their originals.

| Copy | Files | Notes |
|---|---|---|
| `engines/shortnotes/` | 167 (5.8 MB) | notesbot, sheets_bridge, api, equation_kit, fonts, assets, tests, scripts, docs, packaging + `web/state.js` (read by tests). The original is not a git repo |
| `engines/upsc/` | 249 (263.2 MB) | src (incl. `qbg_bridge`, which tests import), prompts, config, assets, reference_documents, third_party/ffmpeg, tests, scripts + root launchers/spec (read by tests). git `61863cd`, clean. **`data/`** (the DPP workbooks: `DPP and Infinite Practice Questions.xlsx` 55 MB, `UPSC QBG Tags.xlsx`, README) was added later the same day at the user's request with `copy_engine.py upsc --add-missing`. `src/dpp_builder.py:177` finds them in `<engine>/data/`, so DPP works from the copy with no path setting. If the bank is updated in the original later, the copy does not follow; re-copy it on purpose |

Copied tests, run from the copy with the same interpreter as the baseline (no Note
Studio venv yet):

- **shortnotes copy:** 1602 passed, 2 failed. One is the known glyph-hash test. The
  other is `test_sheets_bridge.py::test_without_the_gate_the_standalone_page_behaves_as_before`,
  which expects the sheet from the original's `.env` (`SHEETS_BRIDGE_SPREADSHEET_ID`).
  `.env` is deliberately not copied, so this is an expected environment difference.
- **upsc copy:** 1400 tests, the same count as the baseline. Without the workbooks:
  1350 passed, 49 skipped, 1 failed. With `QUESTION_BANK_PATH` / `QBG_TAGS_PATH`
  pointing at the original's `data/*.xlsx` (read in place): 1390 passed, 9 skipped,
  1 failed.
  - The remaining extra skips and the failure come from things that stay in the
    original by design: `runs/` sample lectures, the `build/` / `dist/` PyInstaller
    output, and the gitignored workbooks for `package_release`.
  - The one failure, `test_dpp_builder.py::test_a_frozen_build_finds_workbooks_beside_the_exe_at_lookup_time`,
    checks the lookup *beside the exe*, which the path override deliberately bypasses.

UPSC per-user reference state was checked and not copied. `%LOCALAPPDATA%\UPSC_Notes\reference_index`
is empty on this PC, `qc_cache` is a 10 KB cache, and `proxy_session.json` is the old
app's sign-in pass (a credential). The copy uses its own state folder from Phase 5.

### Docs found out of date (the code wins)

- The shortnotes cache key is `GENERATION_REVISION = "concise-theory-only-v10-sanskrit-hindi"`
  (`sheets_bridge/output_cache.py:18`), not `concise-theory-only-v6`.
  `QUALITY_VERSION = "concise-theory-quality-v2"` is correct. The rule is unchanged:
  never change either.
- `notesbot/format_rules/` has only `rules.json`, `README.md`,
  `foundation/mathematics.txt` and `foundation/sanskrit.txt`. There is no `neet_jee/`
  folder and no `all-subjects.txt`, so the seeded NEET-JEE pack has no rule files.
- The test counts in doc 06/08 are old. The baselines above replace them.
- The UPSC `.venv` was cloned from the handwritten project's venv
  (`pyvenv.cfg` `command=`). It works and is harmless.
- Every doc 08 UPSC entry point checked out: `pipeline.py:142/494/2228`, `config.py:217/295`,
  `languages.py:23`, `dmp/service.py:349/407`, `dmp/export.py:251`, `app.py:3059`.

### Shown to the user

- **Sheet lists.** `~/.shortnotes_bridge/sheets.json` has 6 profiles and the repo
  `sheets.json` has 5. They share only "Short Notes Automation TEST 2026-08-25" and
  "Class Notes Catalog (Concise Notes)", so there are 9 distinct sheets. Never drop one.
- **Handwritten project.** Commit `eee632a` (2026-08-11). It has 15 modified files,
  including the 3 exam prompts, and untracked `src/image_filter.py`,
  `src/style_config.py`, `equation_kit/`, `tests/test_image_filter.py` and
  `tests/test_notes_formatting.py`.

### Open (Phase 0 questions, waiting for the user)

1. Share `~/.shortnotes_bridge` with the old exe? Recommended: yes.
2. Port 3100? Recommended: yes.
3. One queue lane at a time until the model choice is per run? Recommended: yes.
4. Make `Note_Studio` a local git repo (commits only on request)? Recommended: yes.
5. A Note Studio `.venv` (Python 3.13) for Phase 1? This is an install, so it needs the
   user's go.
6. Should the 31 test-made runs and the 13 test files in the 19 Sep run be removed from
   the UPSC original? Claude does not delete in an original; the user decides.
