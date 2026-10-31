"""Failures between a pasted lecture link and a usable transcript.

Every error carries a `user_message` alongside the usual `str(exc)`. They are not
the same thing and conflating them costs one of the two: the person who pasted a
link needs "Unable to access the Google Drive video", while the run log needs
"HTTP 403 on drive.usercontent.google.com for file 1_8w4...". Keeping both means
the UI never shows a stack-trace fragment and the log never loses the reason.
"""
from __future__ import annotations


class VideoSourceError(RuntimeError):
    """Base for anything that stops a lecture video becoming a transcript."""

    #: Shown to the user. Subclasses override; an instance may override again.
    user_message = "The lecture video could not be processed."

    def __init__(self, detail: str = "", *, user_message: str = "") -> None:
        super().__init__(detail or type(self).user_message)
        self.detail = detail
        if user_message:
            self.user_message = user_message


class InvalidVideoURL(VideoSourceError):
    user_message = "The provided lecture video link is invalid or unsupported."


class DirectVideoAccessError(VideoSourceError):
    user_message = "Unable to access the direct video URL."


class DriveAccessError(VideoSourceError):
    # No mention of "configured Google credentials" here: the app ships with
    # none, so telling content staff to check them sent them looking for a
    # setting they do not have.
    user_message = (
        "Unable to access the Google Drive video. Please check that the link is "
        "complete and that the video is shared as \"Anyone with the link\"."
    )


class DriveNotShared(DriveAccessError):
    """Drive asked for a sign-in: the file is private, or limited to the domain.

    This default is for a run whose sign-in carries no Drive permission — a
    sign-in from before the app asked for it, or one where Google's "see your
    Google Drive files" box was left unticked. google_drive.resolve_drive_video
    swaps in a more specific message when the sign-in did carry one.
    """

    user_message = (
        "This Google Drive video is not shared publicly — it is limited to PW "
        "(pw.live) or to named people — and your sign-in does not yet let the app "
        "read Google Drive. Sign out of the app and sign in again, and allow the "
        "app to see your Google Drive files when Google asks; it can then open any "
        "video your PW account can open. Or, in Drive, share the video as "
        "\"Anyone with the link\" (Viewer) and paste that link."
    )


class DriveQuotaExceeded(DriveAccessError):
    user_message = (
        "Google Drive is temporarily blocking downloads of this video because it "
        "has been viewed or downloaded too many times recently. Please try again "
        "later (Google can take up to 24 hours to lift this), or make a copy of the "
        "video in Drive (right-click > Make a copy), share the copy as \"Anyone "
        "with the link\" and paste its link."
    )


class DriveDownloadDisabled(DriveAccessError):
    user_message = (
        "The owner of this Google Drive video has turned off downloading for "
        "viewers, so the app cannot read it. In Drive, open Share > Settings (the "
        "gear icon), tick \"Viewers and commenters can see the option to download, "
        "print, and copy\", then try again."
    )


class DriveUnsupportedContent(VideoSourceError):
    user_message = "The Google Drive file could not be processed as a supported video."


class YouTubeMediaUnavailable(VideoSourceError):
    user_message = (
        "Unable to obtain a transcript or authorized media source for this YouTube lecture."
    )


class FFmpegMissing(VideoSourceError):
    user_message = (
        "FFmpeg is required to read the lecture audio and is not installed or not on "
        "PATH. Install it from https://ffmpeg.org/download.html and restart the app."
    )


class MediaUnreadable(VideoSourceError):
    """Reachable, but not something with a usable audio track."""

    user_message = (
        "The lecture video was reachable but carries no readable audio track, so it "
        "could not be transcribed."
    )


class TranscriptionEngineError(VideoSourceError):
    user_message = (
        "The lecture could not be transcribed. The technical reason is recorded in "
        "the run folder."
    )


class EmptyTranscript(VideoSourceError):
    """Transcription ran but produced nothing usable.

    Raised rather than tolerated: notes built from an empty transcript look like
    notes built from a lecture, which is the one failure a reader cannot detect.
    """

    user_message = (
        "No speech could be transcribed from the lecture video, so no notes were "
        "generated. Please check that the link points at the correct recording."
    )
