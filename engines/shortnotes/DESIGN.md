# Notes PDF Design System (Instagram-toppers-notes style)

Reference: neet.preparation2027-style hand-drawn NEET notes. All rendering in
`notesbot/notes_pdf.py` (fpdf2, Patrick Hand font), icons in `notesbot/doodles.py`.

## Page
- Cream background `PAGE_CREAM` + faint DOTTED GRID (bullet-journal paper).
- Top-left "Page N" inside a lavender RIBBON banner.
- Diagonal signature watermark; footer = signature only.

## Page-1 header
- Title: UPPERCASE, auto-shrinks 29->17pt, max 2 lines. When a quote bubble is
  present the title is centred in the column LEFT of the bubble (never under
  it); subtitle + squiggle share that same column.
- Subtitle: `( Full Notes for <audience> )`, else `( <subject> — Short Notes )`.
- Pink ECG squiggle ending in a tiny heart; star doodle only in a free gutter.
- QUOTE BUBBLE (top right, `QUOTE_BUBBLE_W`): hand-drawn speech bubble with a
  subject quote Gemini writes.
- Signature (left, pink) + lecture no (right) + rule.

## Sections
- Auto-numbered pennant BANNERS: pastel fill + darker border + arrow tip
  (polygon), colour cycles through `MINDMAP_PALETTE` (blue, crimson, green,
  amber, purple, teal). Topic doodle icon at banner's left (schema `icon`
  field; falls back to `doodles.pick_icon(heading)`).
- Bullet dots take the section's edge colour (`_sec_edge`).
- `**word**` in any text renders in `EMPH` marker-red (schema instructs Gemini
  to mark 1-2 key words per point).
- A point that is ONLY `$math$` renders as a centred FORMULA BOX: thin black
  border, faint-yellow fill, gold sparkle beside it.
- Tricks: dashed amber rounded box + bulb doodle.
- "Where," symbol legend under formulas (schema `where`).
- Sticky-note callouts (schema `notes`): dashed grey box + bulb doodle.
- `compare`: two pastel side-by-side cards (Primary vs Secondary style).
- `diagram`: dashed draw-it-yourself box with the instruction.
- `visual{kind,prompt}`: Gemini plans core diagrams; `image_gen.py` draws them
  via `vertex_ai/gemini-3.1-flash-image` (SEQUENTIAL + 429 retry; parallel
  calls trip the gateway limit). In `auto` mode, the coloured-pencil raster
  sketch is attempted first and a labelled SVG is the reliability fallback. A
  visual, SVG, or manual diagram receives a full-width dotted feature section:
  the diagram is centred above its short explanation, while ordinary text
  remains in the two-column flow. Caps: MAX_IMAGES=3, NOTES_IMAGES=false to
  disable.
- Tables: blue header row, alternating row tint, header repeats on page break.
- Flow charts: boxes + colored arrows. Graphs: xkcd-style matplotlib.
- Mind map: last thing in the notes — dark central node, 6-colour branch
  boxes, bezier connectors. It sits under the notes when the whole map fits the
  rest of that page; otherwise it takes its own page, with fonts shrunk to at
  most 88% (`MINDMAP_SCALES`) when that keeps it to one page.

## Space use
- Sections are cards: two short ones share a row, the rest are full width.
- A card that runs past the page end starts in the room left and continues on
  the next page (a fresh dotted frame per page) once its heading and about two
  lines fit there (`CARD_MIN_LEAD`); only a card that would leave just its
  heading behind moves to a fresh page.
- KEY TERMS / शब्दार्थ lists use 2 or 3 columns when every term stays within
  two lines of a column and the box comes out clearly shorter; long
  definitions keep one full-width column.

## Icons (doodles.py)
flask, test_tube, atom, bulb, battery, bolt, drop, fire, magnet, book, gear,
balance, star, heart_ecg — `draw_icon(pdf, name, x, y, size)` centred in a
size×size mm box, fpdf primitives only; unknown name = no-op.

## Known deliberate omissions
- Two-column layout (robustness), inline keyword auto-colouring beyond **marks**,
  question boxes (questions are excluded by prompt).
