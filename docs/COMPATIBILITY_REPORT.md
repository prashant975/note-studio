# Compatibility verification

## This Mac (Apple Silicon, Python 3.13.18)

Full main regression suite: **349 passed, 19 skipped**. Further build/engine checks: **49 passed, 3 skipped**. Source and native packaged app passed six browser page checks with no JavaScript errors. The self-contained macOS app built/import-checked its independent main and three child-engine libraries. Synthetic QC produced a real report; PW Books converted a synthetic DOCX with spaces in its path to PDF through the installed LibreOffice. FFmpeg/FFprobe generated and probed synthetic audio. Optional transcription/browser libraries import successfully; no speech models were downloaded.

The real process-tree test verifies Stop ends both a child and its grandchild. The Git engine drift gate passed with only explicitly listed compatibility changes, including credentials and LibreOffice discovery. Original Windows Downloads folders are absent; the Git baseline comparison does not claim those folders were compared. Before/after snapshots found no pre-existing local data and no losses on this fresh Mac. Prompts and engine business rules were preserved.

## Windows CI

[Compatibility run](https://github.com/prashant975/note-studio/actions/runs/41538755738) runs the main regression suite, browser pages, full native executable/installer build, packaged mock child-engine runs and temporary fresh-install/reinstall checks. Inspect the linked run for final results.

## Checks requiring the real computers/accounts

Real Google/PW sign-in, VPN/private network access, paid generation, real portal posting, private DPP workbooks and live website deployment were not exercised. Authenticate independently on each computer; see CROSS_PLATFORM.md. No production credentials were copied or committed.

Mac bundles are unsigned and architecture-specific. Apple signing/notarization and the final physical Windows laptop workflows remain outstanding. Windows CI is a clean runner, not the user's existing laptop. Source pulls do not update installed applications. Existing Windows installer identities and data-preservation rules remain in place.
