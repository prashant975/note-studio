"""Force chunking on a small sample to validate split -> parallel -> merge -> render."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notesbot import config

# Force the small sample to split into multiple chunks.
config.GEMINI_INLINE_MAX_BYTES = 800
config.CHUNK_TARGET_BYTES = 800

from notesbot import gemini_client, notes_pdf, pdf_utils  # noqa: E402
from scripts._smoke_e2e import make_sample_lecture_pdf  # noqa: E402

data = make_sample_lecture_pdf()
chunks = pdf_utils.split_pdf(data)
print(f"source {len(data)} bytes -> {len(chunks)} forced chunks")

notes = gemini_client.generate_short_notes(data)
print("MERGED -> batch:", repr(notes.batch_name), "| lecture:", repr(notes.lecture_no))
print("sections:", len(notes.sections))
for s in notes.sections:
    print("   -", s.heading, f"({len(s.points)} pts)")

out = notes_pdf.build_notes_pdf(notes, config.TEACHER_SIGNATURE)
path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out_chunk.pdf")
open(path, "wb").write(out)
assert out[:4] == b"%PDF"
print("rendered out_chunk.pdf", len(out), "bytes OK")
