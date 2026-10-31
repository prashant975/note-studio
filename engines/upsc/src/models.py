from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SlideData:
    slide_no: int
    heading: str = ""
    text: str = ""
    cleaned_text: str = ""
    image_path: Path | None = None
    filtered: bool = False
    filter_reason: str | None = None
    source_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_text(self) -> str:
        return self.cleaned_text if self.cleaned_text.strip() else self.text


@dataclass
class TranscriptIssue:
    """One thing that went wrong while getting the lecture's transcript.

    Kept separate from the slide/PPT side on purpose: a deck that fails to parse
    stops the run, whereas transcript trouble is survivable — the notes are still
    built from the slides. These are collected rather than raised so a single bad
    audio chunk can't discard the rest of the lecture.
    """
    stage: str                  # captions | audio-setup | audio-download | audio-chunk
    detail: str                 # what went wrong
    action: str = ""            # what the pipeline DID about it — the change this
                                # error caused in the notes
    chunk: str = ""             # "3/5" when one specific audio chunk failed
    time_range: str = ""        # "50:00-1:15:00" — where in the lecture it was lost
    recoverable: bool = True    # False = no transcript could be produced at all
    # Set only on the audio coverage summary record, so the report can state how
    # much of the lecture actually reached the notes without parsing prose.
    chunks_total: int = 0
    chunks_failed: int = 0


@dataclass
class TranscriptSegment:
    """One timed span of speech. `start`/`end` are seconds from the lecture's start."""

    start: float
    end: float
    text: str

    def shifted(self, offset: float) -> "TranscriptSegment":
        """The same segment placed `offset` seconds later.

        Used when merging chunks: an engine timestamps each chunk from zero, so
        without the shift every chunk after the first reports minute 3 of the
        lecture when it means minute 28.
        """
        return TranscriptSegment(self.start + offset, self.end + offset, self.text)


@dataclass
class NormalizedTranscript:
    """What every source produces, whatever it started as.

    YouTube captions, Gemini and faster-whisper disagree about almost everything
    — timing granularity, punctuation, whether segments exist at all — so they
    are converted to this one shape at the edge of the transcription layer.
    Everything downstream (cleaning, prompt building, QC) reads `full_text`,
    which is a plain string, because that is the contract those modules already
    had. `segments` is additional, not a replacement: it is written to the run
    folder so a claim in the notes can be traced back to a minute of the class.
    """

    full_text: str = ""
    source_type: str = ""       # youtube | google_drive | direct_video
    source_label: str = ""      # human: "YouTube captions (hi, manual)"
    language: str = ""
    engine: str = ""            # captions | faster_whisper | gemini
    segments: list[TranscriptSegment] = field(default_factory=list)
    duration_seconds: float = 0.0
    #: Stretches of the lecture no engine could transcribe, as "50:00-1:15:00".
    gaps: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.full_text.strip())

    @property
    def word_count(self) -> int:
        return len(self.full_text.split())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_label": self.source_label,
            "language": self.language,
            "engine": self.engine,
            "duration_seconds": round(self.duration_seconds, 2),
            "word_count": self.word_count,
            "gaps": self.gaps,
            "segments": [
                {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text}
                for s in self.segments
            ],
            "full_text": self.full_text,
        }


def record_issue(issues: list | None, stage: str, detail: str, **kw) -> None:
    """Append a TranscriptIssue if the caller is collecting them. Never raises —
    failing to record an error must not take down the run it is describing."""
    if issues is None:
        return
    try:
        issues.append(TranscriptIssue(stage=stage, detail=detail, **kw))
    except Exception:
        pass


@dataclass
class PipelineResult:
    run_dir: Path
    docx_path: Path | None
    pdf_path: Path | None
    zip_path: Path | None
    raw_notes_path: Path | None
    warnings: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)
    #: The notes exactly as the documents were built from them. Distinct from
    #: raw_notes_path, which is the model's first draft: between the two sit QC
    #: corrections, layout repair and any page-compression pass. Defaulted like
    #: every other optional artefact below, so existing constructions of this
    #: result keep working untouched.
    notes_final_path: Path | None = None
    # UPSC note-type workflow only. Defaulted so every existing construction of
    # PipelineResult (positional or keyword) keeps working unchanged.
    quality_report_md_path: Path | None = None
    quality_report_docx_path: Path | None = None
    quality_summary: dict[str, Any] = field(default_factory=dict)
    # Error Report — the faculty's factual errors and what each changed in the
    # notes. transcript_issues feeds its "how complete this check is" section.
    error_report_md_path: Path | None = None
    error_report_docx_path: Path | None = None
    transcript_issues: list[TranscriptIssue] = field(default_factory=list)
    # QC stage (src/qc): Error Fixed Report + compact summary for the UI cards
    # and run_metadata. Defaulted so existing constructions keep working.
    error_fixed_report_md_path: Path | None = None
    error_fixed_report_docx_path: Path | None = None
    qc_summary: dict[str, Any] = field(default_factory=dict)
    # Error Report XLSX (src/error_report_writer.py) — the only report shown to
    # a normal user. error_report_status is one of ok / no_issues / parse_failed
    # / missing / failed / disabled, so a sheet with no issue rows can still be
    # told apart from a sheet that was never filled in.
    error_report_xlsx_path: Path | None = None
    #: The same findings written out with their source locations —
    #: timestamp, slide or page — for a human reviewer to check.
    error_report_detailed_path: Path | None = None
    error_report_summary: dict[str, Any] = field(default_factory=dict)
    error_report_status: str = ""
    # Why an output the user expected is not there, kept as its own field rather
    # than left for the UI to find in `warnings`. Searching that prose is worse
    # than it looks: a PDF reason picked out by the substring "word" matches
    # "Transcript source: ... (14,663 words)".
    pdf_error: str = ""
    error_report_error: str = ""
