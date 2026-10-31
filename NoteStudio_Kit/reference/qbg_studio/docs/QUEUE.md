# Smart Queue: many papers and tasks, one after another

Added 3 October 2026, on the operator's request: "add multiple papers at once
and then run one time, then one by one all start reading and move ahead to
complete the task" - in New batch, in Autopilot and in the other tasks - and
"each queue is a different batch".

## Using it

**Several papers in one go.** On Autopilot or New batch, choose all the files
and tick **Each file is a separate paper**. Every file becomes its own batch
(in Autopilot, its own run too) with the settings on the form. The first
starts at once; the others wait in the queue. Without the tick the files are
one paper, as before (pages of one paper, or a paper and its answer key).

**One at a time, with different settings.** Fill the form and press Start as
usual. If something is already running, the new one is no longer refused: it
takes a place in the queue and starts by itself. This is also how a paper
with its own separate answer file, or an English + Hindi pair, is queued.

**The Queue card** (on Autopilot, New batch and the Queue page in the side
menu) lists what is running and what waits, in order. A waiting entry can be
removed; "Hold the queue" stops new starts and "Continue the queue" lets them
go on.

The same queue takes bulk translation, solutions, Tag questions (AI
suggestions and the Excel sheet), video tagging and Verify questions: a start
while another task is running waits its turn instead of being refused.

## Rules

- One entry runs at a time, in the order added. A task that writes to QBG
  also waits until the portal is free, as it always had to.
- Autopilot takes each paper all the way to QBG. New batch reads each paper
  and leaves it at its Preview for the operator's Upload.
- A paper that fails, or cannot start, is reported on the card and the queue
  moves on.
- The queue goes on hold, with the reason on the card, when:
  - Stop is pressed on the task that is running (the next one is not started
    over the operator's Stop);
  - the PW sign-in or the QBG session has gone when the next entry is due
    (the following papers would only fail the same way);
  - Studio is closed and opened again.
- A paper read before (same file, by content) is not read again by the queue:
  its batch is made, the card says so, and its Preview offers "Reuse" or
  "Read again", as New batch always did.

## How it is built

`qbg_studio/job_queue.py`. What waits is the request itself - its settings
and files, in `data/queue/<entry>/` - not a batch or a run. Nothing is created
until the entry's turn comes, so removing a waiting entry leaves nothing
behind. At its turn the entry's launcher does exactly what the screen's own
Start did before (`autopilot_routes.launch`, `paper_routes.launch_paper`, and
`job_queue.task(...)` for the tasks that start with `create_run` + `run`).

Every start on those screens now goes through `job_queue.submit`: with nothing
before it, it starts at once and the person lands on its page as before. A
background look at the line every four seconds (`job_queue.start_pump`) starts
the next entry when the one before it has finished. Retry, Resume and "Finish
leftovers" on an existing run do not queue; they still need a free portal.

Pages: `/work-queue` (`/queue` is the older "Send to bots" screen).
Tests: `tests/test_job_queue.py`.

## Every QBG button (4 October 2026)

The operator: "if I run any task - question creation, translation, tagging -
even if the portal is busy it goes to the queue and then one by one it starts".
Besides every Start, these now join the queue instead of being refused:

- Question creation: Upload on the paper preview, Create (pilot / all) on a
  batch.
- Translation: Upload approved, Upload checked, Resume upload, Check saved
  drafts, Retry. Solutions: Upload, Check saved drafts, Retry.
- Tagging: Apply approved tags, Upload saved suggestions, refresh the QBG tag
  list. Retry on video tagging, sheet tagging and Verify.
- Autopilot Retry and Finish leftovers; the Translation and Solution bots;
  "Put the Hindi paper into the existing questions".

What a button checks about the request itself (a confirmation, something to
upload, the preview being current) is still said at the click. What depends on
QBG being free is waited for. When its turn comes the request is checked again;
if the paper or run changed meanwhile, the card says why it did not start.
A second click on the same button while the first waits adds nothing ("already
in the queue"). An entry whose own batch is busy waits with the reason shown.

The side menu's **Smart Queue** is highlighted, with a one-line description
and how many are waiting; every task page has a "Start another" button and a
one-line Smart Queue notice while anything waits or runs.

Single-question checks ("Check this save") still run at once.

## The What's new tour

After an update, the first page Studio opens shows a short tour of the release
(Smart Queue first), on at most two Studio starts; "Got it" ends it, and
"What's new" under the version in the side menu opens it again. The steps live
in `qbg_studio/whats_new.py` (TOURS, newest first, each with its own id, so the
next release's tour is shown even to someone who finished this one).

## Two lines side by side (4 October 2026)

The queue was strictly one entry at a time, so an Upload waited behind a paper
that was still being read, though reading never touches QBG. On the
operator's yes it now runs two lines side by side:

- **Reading** - New batch readings (AI only).
- **QBG** - everything that works on QBG: uploads and creation, translation,
  tagging, solutions, videos, Verify, the bots and Autopilot.

Each line runs one entry at a time, in the order added; a waiting entry's
place ("place 2") is counted within its own line. A paper's own Upload still
waits for its own reading: its batch is busy until the reading ends, and the
card shows "Waiting: This paper is busy with another step". Hold, Continue,
Stop and a restart still hold or release both lines together.

