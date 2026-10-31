# QBG bridge — live portal editing for DPP questions

The essential machinery of the **QBG Solution Addition Bot**
(`QBG_Complete_Toolkit/1_Existing_Bots_Windows_Ready/qbg_solution_addition_bot.py`),
carried into this project and generalised: instead of "write the sheet's
solution", the caller says which fields to overwrite — question statement,
option texts, correct option letters, solution — on either language tab.

A DPP question's `question_id` **is** the portal's `unique_id` (verified
against the bank export: the `qbgid` and `unique_id` columns are identical),
so every question in a built practice set can be opened on
`qbg-admin.penpencil.co` directly.

## One-time setup

1. Playwright is already in this project's venv.
2. Start debug Chrome and log in to QBG (keep the window open):

   ```powershell
   & "C:\Program Files\Google\Chrome\Application\chrome.exe" `
     --remote-debugging-port=9222 `
     --user-data-dir="$HOME\qbg_chrome_debug_profile"
   ```

   (Same command the toolkit's `START_DEBUG_CHROME.bat` runs.)

That is all: the bridge attaches to that Chrome over CDP and inherits the
login. No credential passes through this code. Alternatively set
`QBG_HEADLESS=1` to use a persistent headless profile at `QBG_CHROME_PROFILE`
that was logged in once by running headed.

## Status: not wired into the app right now

The "Edit on QBG" form this package once powered in the DPP preview was
**reverted on request** — the app currently exposes no QBG editing UI, and
nothing in `app.py` imports this package. The module and its unit tests stay
because the machinery is proven and the integration is expected back; until
then it is used from Python only (below).

A push takes one to two minutes: enter edit mode, type through real keyboard
events, click Update while watching the network for the `/qbg/questions/`
write, then **reopen the question and verify every pushed field was kept**.
Failures retry through the bot's proven ladder (normal save → X Category
tick/untick → AI Guru Personalization) and end in a readable error plus a
screenshot under `runs/_qbg_debug/`.

The local 55 MB workbook still holds the old text until PW's next export —
a push changes the portal, not the bank file.

## Using it from Python

```python
from src.qbg_bridge import QbgEdit, edit_question, fetch_question

snapshot = fetch_question("a8hri1z75sqihd0v9vphoisa5")     # read-only
result = edit_question(
    "a8hri1z75sqihd0v9vphoisa5",
    QbgEdit(language="English",
            solution="The corrected explanation.",
            correct_letters=("B",)),
)
print(result.verified, result.message)
```

## Navigation by id — the one unconfirmed piece

The direct per-question URL format is still unconfirmed (the toolkit's own
config calls it "the LAST OPEN BLOCKER"). Until it is confirmed, the bridge
reaches a question by driving the portal's **question-search** page with the
id, which is written defensively and may need one live run to prove out.

To confirm the fast path once: open any question in the portal, copy its URL,
and set

```
QBG_QUESTION_LINK_TEMPLATE=https://qbg-admin.penpencil.co/<whatever>/{qbg_id}
```

with `{qbg_id}` in place of the question id. Ids then navigate directly and
the search page is never used again.

## Environment variables

Same names the bot documents: `CHROME_CDP_URL`, `QBG_CHROME_PROFILE`,
`QBG_HEADLESS`, `QBG_INITIAL_SETTLE_MS`, `QBG_FORM_SETTLE_MS`,
`QBG_TAB_SETTLE_MS`, `QBG_STABLE_POLL_MS`, `QBG_STABLE_CHECKS`,
`QBG_SAVE_RESPONSE_WAIT_SECONDS`, `QBG_SAVE_GRACE_MS`,
`QBG_PATCH_OBSERVED_GRACE_MS`, `X_CATEGORY_SETTLE_MS`,
`QBG_OVERWRITE_RETRIES`, `QBG_VERIFY_OVERWRITE`, plus `QBG_LONG_TIMEOUT`
(the bot's `LONG_TIMEOUT`, renamed to avoid a generic name),
`QBG_QUESTION_SEARCH_URL`, `QBG_QUESTION_LINK_TEMPLATE`, `QBG_DEBUG_DIR`.

## What was deliberately not copied

- **Google Sheets polling** — the app's own UI supplies the values a sheet
  row used to.
- **`pw_auth` allowlist gate** — toolkit-specific; in this app the per-push
  button behind the login gate is the human authorisation. Do not call
  `edit_question` from any automatic loop.
- **The creation pipeline** (`POST /qbg/questions`) — creating questions is a
  different job with its own confirmed config in the toolkit.
