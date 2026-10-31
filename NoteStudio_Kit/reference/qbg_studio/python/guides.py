"""A short guide at the top of each main screen (the operator's request, 4 October 2026).

"Add instructions or a short guide on top so that they can easily understand
the app and the task." One line saying what the screen is for, then the steps
in the order a person does them. base.html shows it above the page; the person
can fold it away and Studio remembers that on this computer.

Only the screens where work starts are listed; a run's own page explains itself.
"""

GUIDES = {
    '/dashboard': dict(
        title='Overview: your day at a glance',
        about='See what is running, what needs you, and start new work.',
        steps=['Check the connection strip: the PW sign-in and the QBG session must both be ready.',
               'To take a paper all the way to QBG without stopping, use Autopilot. To read a paper and check it '
               'yourself before uploading, use New batch.',
               'Anything you start while QBG is busy waits in Smart Queue and runs by itself.']),
    '/autopilot': dict(
        title='Autopilot: a whole paper to QBG in one go',
        about='It reads the paper, uploads the questions, adds solutions, translates and tags, then gives you one report.',
        steps=['Choose the exam category and the paper. Several papers? Choose them all and tick "Each file is a separate paper".',
               'Say whether the paper has its answer key, and pick solutions, translation and tagging.',
               'Press Start Autopilot. It never stops to ask; anything to look at is in the run\'s report.',
               'Use Retry or Finish leftovers on the run page for what is left.']),
    '/new': dict(
        title='New batch: read a paper, check it, upload it',
        about='Choose how the paper comes in. "Any format" reads PDFs, Word files and images.',
        steps=['Pick the kind of upload below (most papers: Any format).',
               'Read the paper, then check every question on its Preview.',
               'Press Upload on the Preview to create the questions on QBG.']),
    '/new/any-format': dict(
        title='New batch: read a paper, check it, upload it',
        about='Any paper - PDF, Word or images - is read into questions you can check and correct before they go to QBG.',
        steps=['Choose the exam category and the paper language ("Detect from the paper" is right for most papers).',
               'Add the question file, and the answer key if it is separate. Several papers: tick "Each file is a separate paper". '
               'The same paper in English and Hindi: tick "Add the same paper in another language".',
               'Press "Read paper"; the Preview opens. Correct anything marked there.',
               'Press Upload on the Preview. If QBG is busy it waits in Smart Queue.']),
    '/': dict(
        title='Batches: every paper you have read',
        about='Open a batch to see its Preview, upload it, tag it or download it.',
        steps=['Find the paper (newest first) and open it.',
               'On its Preview, check the questions and press Upload.',
               'After upload, use Tag questions, Translate or Solutions for that batch.']),
    '/work-queue': dict(
        title='Smart Queue: one task after another, by itself',
        about='Start as many papers and tasks as you like; whatever cannot start yet waits here.',
        steps=['Start work from any screen as usual.',
               'If it cannot start yet, it is added here and starts by itself when its turn comes.',
               'Papers being read and work on QBG run side by side; each line one at a time.',
               'Use Remove for something you no longer want, Hold to pause, Continue to go on.']),
    '/search': dict(
        title='Search questions: find them on QBG',
        about='Find questions by their words or by their QBG IDs, the way QBG\'s own search does.',
        steps=['Type part of the question (10 characters or more), or paste QBG IDs.',
               'Tick the questions you want - or none, to take every question shown.',
               'Press Tag, Translate, Solutions or Verify: their IDs are filled in for you there.']),
    '/translate': dict(
        title='Translate questions',
        about='Add another language to questions that are already on QBG, under the same QBG IDs.',
        steps=['Use Bulk translation for a list of QBG IDs or a whole batch.',
               'Choose the language and how the drafts go up (automatically, or after your review).',
               'Open the run to see every question and its result.']),
    '/translate/bulk': dict(
        title='Bulk translation: another language on the same QBG IDs',
        about='Each question is translated by AI and saved into the same QBG question as a new language.',
        steps=['Paste the QBG IDs (or choose a batch) and the language.',
               '"Translate and upload automatically" uploads every draft and lists anything worth a second look afterwards. '
               '"Review before upload" waits for you to approve.',
               'Press Start, then open the run to follow each question.']),
    '/tagging': dict(
        title='Tag questions: subject, chapter, topic and difficulty',
        about='Two ways in, at the top: AI suggestions, or an Excel sheet with the tags already chosen.',
        steps=['AI suggestions: paste the QBG IDs (or choose a batch), choose the subject, class and chapters, and Start.',
               'Review the suggestions and Apply them (or choose Direct upload to skip the review).',
               'From an Excel sheet: upload the bulk-tagging sheet; every row\'s tags go on its QBG ID as written.']),
    '/video-tagging': dict(
        title='Video tagging: a solution video on each question',
        about='Puts a YouTube solution video on each question and language, 5 questions at a time.',
        steps=['Upload the video sheet (QBG ID, language, YouTube link), or paste those lines.',
               'Tick "Replace existing videos" only if old videos should be changed.',
               'Start, then open the run; Retry picks up anything that did not save.']),
    '/verify': dict(
        title='Verify questions: the Verified mark on QBG',
        about='Marks questions as Verified (or removes the mark) by their QBG IDs.',
        steps=['Paste the QBG IDs, or upload a sheet with a question id column.',
               'Choose "Mark as Verified" or "Remove the Verified mark", and Start.',
               'Your QBG account must be allowed to verify; if it is not, the run says so.']),
    '/solutions': dict(
        title='Solutions: worked solutions on QBG questions',
        about='AI writes a worked solution for each question and checks the answer.',
        steps=['Paste the QBG IDs and choose the language.',
               'Choose "Generate and upload", or "Save drafts" to review them first.',
               'Open the run to approve and upload, or to see what needs a look.']),
    '/word-export': dict(
        title='Generate paper: a test paper from QBG questions',
        about='Builds a Word (and PDF) paper from questions on QBG.',
        steps=['Choose the subject, chapters, topics and difficulty.',
               'Choose the layout and the paper details (test name, batch, DPP number).',
               'Generate, then download the files.']),
    '/question-bank': dict(
        title='Question bank: a balanced bank from QBG',
        about='Builds a bank with the right weightage per chapter and topic, as PDF and Excel; DPPs come from it.',
        steps=['Choose the subject and the scope.',
               'Build the bank and download the PDF and the matching Excel.',
               'Make DPPs from the bank page.']),
    '/created': dict(
        title='Created questions: what this computer put on QBG',
        about='Every question created from QBG One, with its QBG link.',
        steps=['Search or filter to find a question.',
               'Tick questions to send them to the bots\' sheet for translation or solutions.']),
    '/settings': dict(
        title='Settings',
        about='Your sign-ins, the QBG connection, the AI models and how QBG One behaves.',
        steps=['Sign in with PW first; then connect QBG.',
               'Everything else can stay as it is unless you need to change it.']),
}


def for_path(path):
    """The guide for this screen, or None."""
    guide = GUIDES.get(str(path or '').rstrip('/') or '/')
    return dict(guide, key=str(path or '/')) if guide else None
