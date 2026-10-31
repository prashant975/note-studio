# Short Notes Studio

Password-protected, mobile-first Vercel app that converts a lecture PDF into a
hand-sketched revision PDF. The browser uploads directly to a private Vercel
Blob store, the bounded generation job runs once, and the completed file is
downloaded/opened on the same phone.

## Lecture sources

Choose either **Upload PDF** or **Paste PDF link**. Links support direct public
HTTPS PDFs and Google Drive file links in `/file/d/{id}/view`, `/open?id={id}`
or `/uc?id={id}` form. Drive files must be shared as **Anyone with the link**
with downloads enabled; OAuth, folders, Docs/Sheets/Slides and private links
are intentionally unsupported. Imports are limited to 100 MiB and an
85-second acquisition deadline. The submitted URL is not retained: only the
canonical private input Blob survives and follows normal terminal/TTL cleanup.

## User flow

1. Open the app and enter the shared password.
2. Select a PDF (maximum 100 MB).
3. Enter the teacher name once; the browser remembers approved, nonsecret
   generation preferences only.
4. Choose subject and output language, optionally add instructions, and choose
   the center-logo and teacher-name watermarks independently.
5. Keep the progress page open. If the tab reloads, the saved request ID resumes
   status/download recovery without creating another AI job.
6. When the full PDF has been fetched and validated locally, the app opens or
   downloads it (including the reserved-tab flow for iPhone) and acknowledges
   cleanup.

The output uses soft outlined section cards instead of rigid permanent columns.
Heavy diagrams can use the full width, headings are boxed to their measured
height, related doodles appear on both sides, and an optional final mind map
summarizes conceptual topics. Proof-heavy lectures use numbered question and
solution blocks instead.

Hindi output uses consistent Devanagari terminology and spelling, including
questions, solution steps and conclusions. Both languages retain complete
source definitions, worked examples and proofs. LaTeX matrices render with
their rows, columns and brackets; display delimiters are normalized before
rendering. Duplicate checks preserve mathematical signs and distinct concepts.
The generation cache uses a new revision key so old notes do not bypass these
changes. Previously uploaded PDFs require a fresh generation to gain missing
content; a local re-render can only repair presentation of stored content.

Foundation and NEET-JEE share unit and word-form formula repairs: compound
speed/acceleration units keep their negative exponents attached, and phrases
inside fractions retain spaces. Generation and review include motion/optics
clarity checks (signed slopes, acceleration versus retardation, diffraction
conditions, and distinct subtopic headings).

Percentages in alloy formulas retain their escaped percent signs; comparison
aliases and empty text wrappers are normalized before math rendering. Prose
temperatures retain the real degree symbol (25 °C), including old `degC`
spellings. Long composition lists wrap between components. A local quality
check inspects every rendered PDF page for leaked LaTeX command fragments and
malformed temperatures before delivery, including desktop, web and cached
re-renders, without an additional model call. Existing PDFs need regeneration
or a local re-render from their stored notes to receive these corrections.

Chemistry notes also repair two reported mistakes locally: an extra oxygen in
a standalone double-covalent-bond symbol (`=O` becomes `=`), and the product
state in `Pb(s) + CuCl2(aq) -> PbCl2(s) + Cu(s)`. Complete molecular structures
such as `O=O` and lead chloride dissolution/equilibrium equations stay intact.
Generation, source review and final review share these explicit corrections;
the local repairs run even with model QC disabled, without another model call.
The cache revision prevents reuse of output from before these corrections.

Both generation paths now accept `schematic`: bounded vector lines, arrows,
ellipses and rich-text labels for essential source-taught diagrams. These are
rendered locally and stored with the notes, so cached re-renders retain them
without an image-service call. Graphs may be `qualitative` to illustrate taught
shapes without fabricated numerical ticks. Source coverage checks include
graphs and schematics; complex anatomy still needs the existing image path.
Regenerate existing notes to request missing figures; the sample at
`output/pdf/science-feedback/science-feedback-preview.pdf` is a local rendering
fixture, not a regeneration of the PDFs shown in the feedback screenshots.

For Sanskrit chapters, select **संस्कृतम्** as the output language. Selecting
the Sanskrit subject with Auto language also enables this mode; text PDFs with
clear Sanskrit teaching markers can be detected automatically. Scanned PDFs
should use the explicit language option. Verses, पदच्छेदः, अन्वयः, भावार्थः,
vocabulary and grammar stay in Sanskrit, with complete verses preserved.
All PDF labels use Sanskrit and Devanagari numerals. Latin-script teacher names
use a Sanskrit teacher label; logos and generated images are omitted in this
mode. Both structured content and the rendered PDF are checked for Latin
letters, and mixed output fails instead of being delivered. This script check
does not prove Sanskrit grammar; the generation and review prompts separately
require Sanskrit rather than Hindi. Existing English/Hindi options still work.

The PW notes automation additionally compares each generated page group against
its source PDF for missing definitions, formulas, conditions, examples and proof
steps. A detected gap gets one repair and a fresh review; failed or unreadable
reviews stop the row instead of marking partial notes complete. This adds a
review call per group, plus two calls when a repair is needed. It is a model
check, so human review remains useful. Mind-map formulas are recovered into the
main notes before optional proof-heavy maps are removed. Cache keys include the
selected proxy model and coverage revision.

## Safety and temporary storage

- A signed 12-hour HttpOnly session and per-session CSRF token protect browser
  actions. Passwords and CSRF tokens are never stored in `localStorage`.
- Each request ID maps to one immutable input path and one durable job record.
  A caller-unique dispatch claim prevents duplicate generation after ambiguous
  network responses.
- Generation has no recursive or whole-job retry loop. At most 8 PDF chunks and
  2 generated images are allowed, and every state transition is monotonic.
- Blob access uses Vercel OIDC only; there is no `BLOB_READ_WRITE_TOKEN`.
- App objects are isolated under `shortnotes/v1/`. Inputs are deleted in the
  worker's `finally` block. Outputs are deleted after a successful browser fetch
  acknowledgement, with hourly TTL cleanup as a fallback. Cleanup never scans
  or deletes outside that prefix.

## Local setup

Requirements: Python 3.11+, Node.js 20+.

```powershell
python -m pip install -r requirements.txt
npm install
Copy-Item .env.example .env
```

Fill the local `.env` without committing it. The web app requires
`APP_PASSWORD`, `APP_SESSION_SECRET`, `BLOB_STORE_ID`, and a LiteLLM credential.
Private Blob operations work on Vercel through the project's OIDC connection.
Mathpix is not part of the active generation path. Never expose credentials to
browser code or use a public/client-prefixed environment variable for them.

## Gemini-only generation flow

Gemini reads each bounded PDF chunk directly and returns source-positioned notes
in the same order the teacher taught them. The active flow makes exactly one
structured notes call per chunk and does not run Mathpix, screenshot blocks,
visual inventory/audit, crop extraction, design, QC, or semantic visual
verification calls. Structured graphs and schematics render locally.

Zero images is valid. When the source explicitly supports a core spatial
diagram, the app may make one non-retrying image call with only that exact
one-page source PDF and a bounded allowlist of components and labels. A lecture
can make at most two such image calls. Any image failure leaves the otherwise
valid notes text-only; there is no SVG or manual-diagram fallback in the active
flow. Structured vector schematics are a separate source-taught output field,
not an image-generation fallback.

## Verification

```powershell
python -m pytest -q
npm run test:web
npm run build
python scripts/_smoke_render.py
```

The deployment smoke accepts credentials through environment variables or
interactive input and never prints them:

```powershell
python scripts/_smoke_web.py https://your-production-domain.example path\to\tiny.pdf
```

## Vercel deployment

This directory is linked to the existing `shortnotes` project. Configure these
encrypted Production variables:

- `APP_PASSWORD`
- `APP_SESSION_SECRET`
- `CRON_SECRET`
- `BLOB_STORE_ID` (the connected private store)
- `BLOB_WEBHOOK_PUBLIC_KEY` (provided by the Blob integration)
- `LITELLM_API_KEY`
- `LITELLM_MODEL=vertex_ai/gemini-3.6-flash`
- `LITELLM_REASONING_EFFORT=high`
- `IMAGE_MODEL=vertex_ai/gemini-3.1-flash-image`
- `MATHPIX_ENABLED=false`
- `MAX_CHUNKS=8`
- `MAX_IMAGES=2`
- `NOTES_IMAGES=true`
- `NOTES_DESIGN=false`
- `NOTES_QC=false`

Keep Mathpix and the optional design/QC model passes disabled in production.
Changing a Vercel environment variable affects only fresh deployments, so make
a new candidate deployment before promotion.

Do not add a static Blob read/write token. Keep deployment protection enabled.
Deploy to an unaliased production URL first, run health and full PDF smoke tests,
then promote the verified deployment.

```powershell
vercel.cmd deploy --prod --skip-domain
vercel.cmd inspect <deployment-url> --wait
vercel.cmd curl <deployment-url>/api/web?action=health
vercel.cmd promote <deployment-url>
```

`vercel.json` builds the static client, gives the Python worker a bounded
720-second duration, applies security headers, and runs `/api/cleanup` hourly.
