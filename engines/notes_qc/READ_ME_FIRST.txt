UPSC PDF QC — how to use it
============================

WHAT IT DOES
  You give it class notes (PDF, Word, PowerPoint, or images). It reads every
  page and gives you back an Excel error report: what the notes get wrong,
  where, why, and what it should say instead — with source links where it
  checked a fact on the web.


FIRST TIME
  1. Unzip this folder anywhere you like. Keep the files together.
  2. Double-click  UPSC_QC.exe
  3. A black window opens and stays open. That is the app running — leave it
     alone. Your browser opens by itself after a few seconds.
  4. Click "Sign in with Google" and use your @pw.live account.

  If the browser does not open on its own, type this into it:
      http://localhost:8501

  Windows may warn "Windows protected your PC" the first time, because the
  file is not code-signed. Click "More info" then "Run anyway".


EVERY TIME AFTER THAT
  Double-click UPSC_QC.exe, drop your files in, press the button, wait.


HOW LONG IT TAKES
  About 8-12 minutes for a 17-20 page document. It is not stuck. It reads
  every two pages three separate times, then double-checks its own findings,
  then looks up facts on the web. That is why it finds what a single read
  misses.

  You can upload several files at once; they are checked one after another.


WHERE YOUR FILES GO
  reports\   the Excel error reports
  runs\      the working files for each run (keep these — see below)

  Both folders sit next to UPSC_QC.exe.


READING THE REPORT
  Each row is one problem. The columns are:

    Original statement   the sentence in your notes, so you can find it
    Error type           what kind of problem it is
    Severity             High / Medium / Low
    Error / concern      what is wrong, and why it matters
    Correct information  what it should say
    Source URL(s)        click it — it opens the page that proves it

  A row starting with "[LEAD — ...]" means the app could not confirm that one
  either way. Treat it as something to check, not as a finding.


IF SOMETHING GOES WRONG
  The app crashed or you closed it mid-run?
    Nothing is lost. Every answer is saved as it arrives. Open a command
    prompt in this folder and run:

        UPSC_QC.exe --rebuild runs\<the run folder>

    It rebuilds the report from what is already on disk, without re-checking
    anything and without costing anything.

  "Port already in use"?
    The app is already running in another window. Close it, or just use the
    window that is open.

  Sign-in fails?
    Your account must be a @pw.live account that is whitelisted for this tool.
    Ask whoever sent you this file.


WHAT IT IS GOOD AND BAD AT
  Good: contradictions inside the document, arithmetic that does not add up,
  wrong dates and centuries, wrong places, spelling and grammar, wrong names
  of schemes and bodies, and current-affairs facts it can look up.

  Weaker: a lone figure with nothing to compare it against. It checks the most
  checkable ones on the web, but it cannot check every number in a document.

  It is a second pair of eyes, not a replacement for yours. Every row tells
  you where to look so you can judge it yourself in seconds.
