`pw_onlyias_logo.png` is the full PW OnlyIAS lockup (the PW mark plus the
ONLYIAS wordmark) that goes in the header of **every** page of the generated
notes. It is the same image the reference DCN document carries — extracted from
`word/media/image1.jpg` of that file, so the branding matches PW's own notes
exactly — with its `#F7F7F7` background keyed out to transparency, which the
original does not have. At 525x128 it fills the header's 3.0 x 0.735 in box.

`pw_logo.png` is the bare PW circle mark. It is the fallback for installs that
only have that file, and it is what the faint background watermark uses.

The writer picks whichever it finds first, in the order set by `LOGO_CANDIDATES`
in `src/upsc_dcn_docx_writer.py`. Replacing either changes the branding — a wide
lockup fills the box's width, a square mark is constrained by its height.

If this file is missing, the app falls back to detecting the mark in the top-right
corner of the lecture slides; if that also fails, the notes are produced without
a logo or watermark and a warning is shown.

Optional: an `upsc_logo.png` here is shown in the app's login/header UI (see
`LOGO_PATH` in `app.py`).

Do not upload or share font files here. Install the Kalam font on your own
computer for the handwritten style (https://fonts.google.com/specimen/Kalam).
