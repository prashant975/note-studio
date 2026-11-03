# Compatibility verification

## Final Mac verification

Full main suite: **359 passed, 19 skipped**. Additional copied-engine domain/rejection tests passed. The self-contained app built/import-checked its main environment and three independent child-engine libraries. Seven packaged pages, including startup sign-in, passed with no JavaScript errors. The single-popup browser behavior and reuse were verified with synthetic Google responses.

Real Google sign-in completed using the same Web OAuth app registration as Doubt Desk; each app retains its own login. Registration and tokens are Keychain-protected, and the login restored after restart without another Google popup. Desktop registration support and original Windows behavior remain available; Web callbacks validate state and use PKCE before credentials can be adopted.

PW Books exported synthetic DOCX files through local Pandoc and Chrome, retaining equations, Hindi, tables and images. No LibreOffice is required on this Mac; the installed copy was moved to Trash. Chrome pagination/styling can differ from Word. Synthetic QC produced a real report, FFmpeg/FFprobe generated/probed audio, and a real Stop check terminated a child plus its grandchild. Speech models were not downloaded.

The Git engine drift gate passed with only explicitly listed changes. Original Windows Downloads projects were absent and were not claimed compared directly. Final data comparison: zero files lost, zero original projects changed. Twenty expected new local files comprise the database, shipped category settings, protected Google registration/login, and the retained administrator Desktop-registration backup. Engine prompts/rules were preserved.

## Windows CI

[Latest compatibility workflows](https://github.com/prashant975/note-studio/actions/workflows/compatibility.yml?query=branch%3Acodex%2Fmacos-windows-compatibility) run both operating systems, full main tests, browser pages, full native executable/installer builds, packaged mock runs for all included engines and fresh-install/reinstall checks with synthetic saved-work preservation. The Windows native launcher opens sign-in first. Artifacts have seven-day retention; exact completed runs are recorded on the PR.

## Remaining checks

Real work-account Google sign-in was verified on this Mac for all three apps. Each app keeps its own protected login; the app registration may be shared. No Windows user login was copied. No paid generation, live portal posting, private DPP workbook workflow or website deployment was exercised.

Mac applications are unsigned and architecture-specific. Public distribution still needs Apple signing/notarization. Final checks on the physical Windows laptop, its existing data, company VPN and real peripherals remain necessary. Source pulls do not update an installed executable or app bundle. See CROSS_PLATFORM.md for exact setup, authentication, data locations, tests, Git workflow and package commands.
