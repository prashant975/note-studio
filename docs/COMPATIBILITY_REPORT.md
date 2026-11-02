# Compatibility verification — 2 November 2026

Mac (Apple Silicon, Python 3.13.18): full main suite 341 passed / 22 skipped before the additional native Windows sidecar/local-environment tests. Source and packaged app passed six browser page checks with zero JavaScript errors. The child-process test now runs on macOS and verifies that Stop ends both the child and its grandchild. The .app built/import-checked its independent main and three child-engine libraries. The Git engine baseline gate passed with only the listed credential adapters changed; original Windows Downloads folders are absent and were not compared. No pre-existing user data was present on this fresh Mac. Windows results are pending Actions.

## Boundaries

No real Google/PW sign-in, paid generation, real portal posting, private DPP workbook use or live website deployment was performed. Each computer must authenticate independently with its own Google/PW and browser session. External configuration is documented in CROSS_PLATFORM.md.

Mac bundles are unsigned and architecture-specific. Apple signing/notarization, native Windows installer execution and final physical Windows laptop workflows remain separate checks. Source pulls do not update installed applications. Existing Windows installer identities and data-preservation rules remain in place.
