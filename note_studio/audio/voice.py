"""The script spoken by PW AI's Gemini voices and joined into one audio file. All the
speaking is AI; joining needs no program on the computer (no ffmpeg).

    parts   the lines in groups of about PART_CHARS characters (two or three minutes each)
    speak   each group in one native Gemini speech call with two voices
            (speechConfig.multiSpeakerVoiceConfig). When PW AI refuses that request, the
            group is spoken line by line, each line in its friend's voice, through the
            proxy's own speech route instead.
    join    Gemini answers raw 16-bit PCM (24 kHz, mono). The groups are joined with a
            short pause and the episode is written as one MP3 (mono, 44.1 kHz, 96 kbps:
            about 11 MB for 15 minutes) by lameenc, a library bundled in the app. Without
            that library it is a WAV (Python's wave module). A route that answers MP3 is
            joined as MP3 frames.

Each spoken group is kept in audio_parts/ under a fingerprint of what it says and in
which voices, so Try again (and a fixed script) pays only for the groups not spoken yet."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REVISION = "audio-voice-v3"            # v3: a line spoken alone is led by how to say it
PART_CHARS = 1800
PART_LINES = 40
PAUSE = 0.35                           # seconds between groups
LINE_PAUSE = 0.15                      # between lines spoken one at a time
DEFAULT_RATE = 24000
MP3_KBPS = 96
MP3_RATE = 44100                       # MPEG-1, as the team's reference episode: plays everywhere
# Gemini's prebuilt voices offered on the form: name -> plain words.
VOICES = {
    "Puck": "Puck: upbeat, male", "Fenrir": "Fenrir: excitable, male", "Achird": "Achird: friendly, male",
    "Sadachbia": "Sadachbia: lively, male", "Zubenelgenubi": "Zubenelgenubi: casual, male",
    "Charon": "Charon: informative, male", "Orus": "Orus: firm, male",
    "Leda": "Leda: youthful, female", "Laomedeia": "Laomedeia: upbeat, female", "Zephyr": "Zephyr: bright, female",
    "Aoede": "Aoede: breezy, female", "Kore": "Kore: firm, female",
}


class VoiceError(RuntimeError):
    """Audio that came back but cannot be joined, in plain words."""


@dataclass
class Clip:
    kind: str                          # pcm | mp3
    data: bytes
    rate: int = DEFAULT_RATE
    channels: int = 1


# --------------------------------------------------------------- groups and requests
def parts(lines: list[dict], size: int = PART_CHARS, most: int = PART_LINES) -> list[list[dict]]:
    """The lines in order, in groups of about ``size`` characters; a line is never split."""
    groups, current, length = [], [], 0
    for line in lines:
        extra = len(line["speaker"]) + len(line["text"]) + 3
        if current and (length + extra > size or len(current) >= most):
            groups.append(current)
            current, length = [], 0
        current.append(line)
        length += extra
    return groups + ([current] if current else [])


def gender(voice_name: str) -> str:
    """ "man", "woman" or "" for one of VOICES."""
    label = VOICES.get(voice_name, "")
    return "woman" if label.endswith("female") else "man" if label.endswith("male") else ""


def style(hosts: tuple[str, str], voices: tuple[str, str] = ("", "")) -> str:
    """How the voices speak, after the team's reference episode: a young man and a young woman,
    brisk and natural, quick reactions and light laughs, urban Indian Hinglish."""
    who = [f"{name} (a young {gender(v)})" if gender(v) else name for name, v in zip(hosts, voices)]
    return (f"Read aloud this conversation between {who[0]} and {who[1]}, two Indian college friends revising a "
            "chapter they have just finished: natural and lively, at a brisk conversational pace, with quick "
            "reactions, light laughs and an urban Indian Hinglish accent. Hindi is written in English letters; say "
            "it as natural spoken Hindi. Say English science terms and formulas clearly in English.")


def line_text(line: dict, hosts: tuple[str, str], voices: tuple[str, str]) -> str:
    """One line for the single-voice route, led by how to say it: that route has no style field,
    and Gemini follows such a lead-in without reading it out (tried on 27 Nov 2026)."""
    name = line["speaker"]
    kind = gender(voices[hosts.index(name)])
    who = f"{name}, a young Indian {kind}" if kind else name
    return (f"Say this as {who} chatting with a college friend: natural, lively spoken Hinglish (Hindi written "
            f"in English letters, said as Hindi), brisk pace, urban Indian accent: {line['text']}")


def request(group: list[dict], hosts: tuple[str, str], voices: tuple[str, str]) -> dict:
    """One native Gemini speech request: the group's lines, each friend in their voice."""
    said = "\n".join(f"{line['speaker']}: {line['text']}" for line in group)
    return {"contents": [{"role": "user", "parts": [{"text": f"{style(hosts, voices)}\n\n{said}"}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"multiSpeakerVoiceConfig": {
                "speakerVoiceConfigs": [
                    {"speaker": hosts[0], "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voices[0]}}},
                    {"speaker": hosts[1], "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voices[1]}}}]}}}}


def fingerprint(group: list[dict], hosts: tuple[str, str], voices: tuple[str, str]) -> str:
    payload = json.dumps([REVISION, style(hosts, voices), list(hosts), list(voices), group], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- audio
def _bare_mp3(data: bytes) -> bytes:
    """MP3 frames without the ID3 tags at the start and end, so files can follow one another."""
    if data[:3] == b"ID3" and len(data) > 10:
        size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
        data = data[10 + size + (10 if data[5] & 0x10 else 0):]
    if len(data) > 128 and data[-128:-125] == b"TAG":
        data = data[:-128]
    return data


def decode(data: bytes, mime: str = "") -> Clip:
    """A speech answer as a clip. Gemini sends raw little-endian PCM (audio/L16;rate=24000)."""
    mime = str(mime or "").lower()
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(data)) as found:
                if found.getsampwidth() != 2:
                    raise VoiceError("PW AI sent audio this app cannot join (not 16-bit).")
                return Clip("pcm", found.readframes(found.getnframes()), found.getframerate(), found.getnchannels())
        except (wave.Error, EOFError):
            raise VoiceError("PW AI sent a broken WAV file.") from None
    if "mpeg" in mime or "mp3" in mime or data[:3] == b"ID3":
        return Clip("mp3", _bare_mp3(data))
    if not mime or any(word in mime for word in ("l16", "pcm", "raw")):
        rate = re.search(r"rate=(\d+)", mime)
        channels = re.search(r"channels=(\d+)", mime)
        clip = Clip("pcm", b"", int(rate.group(1)) if rate else DEFAULT_RATE, int(channels.group(1)) if channels else 1)
        clip.data = data[:len(data) - len(data) % (2 * clip.channels)]
        return clip
    raise VoiceError(f"PW AI sent audio this app cannot join ({mime}).")


def _mono_at(clip: Clip, rate: int) -> bytes:
    """A PCM clip as mono at ``rate`` (first channel, nearest sample): only for a clip that differs."""
    if clip.channels == 1 and clip.rate == rate:
        return clip.data
    samples = array("h")
    samples.frombytes(clip.data)
    if sys.byteorder == "big":
        samples.byteswap()
    if clip.channels > 1:
        samples = samples[::clip.channels]
    if clip.rate != rate and samples:
        step = clip.rate / rate
        samples = array("h", (samples[int(i * step)] for i in range(int(len(samples) / step))))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def mp3_of(pcm: bytes, rate: int, channels: int) -> bytes | None:
    """16-bit PCM as one MP3 (lameenc, bundled in the app), or None when the library is not in this installation."""
    try:
        import lameenc
    except ImportError:
        return None
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(MP3_KBPS)
    encoder.set_in_sample_rate(rate)
    if hasattr(encoder, "set_out_sample_rate"):
        encoder.set_out_sample_rate(MP3_RATE)
    encoder.set_channels(channels)
    encoder.set_quality(2)                       # 2: high quality, still fast
    return bytes(encoder.encode(pcm)) + bytes(encoder.flush())


def join(clips: list[Clip], pause: float = PAUSE, mp3: bool = False) -> tuple[bytes, str, float | None]:
    """(audio file, "wav" or "mp3", seconds or None) of the clips in order. ``mp3``: PCM is written
    as MP3 when the library is here (the episode); otherwise as WAV (a saved part stays lossless)."""
    if not clips:
        raise VoiceError("No audio was made.")
    kinds = {clip.kind for clip in clips}
    if kinds == {"mp3"}:
        return b"".join(clip.data for clip in clips), "mp3", None
    if kinds != {"pcm"}:
        raise VoiceError("PW AI sent the parts in different audio forms, so they cannot be joined.")
    rate = clips[0].rate
    channels = clips[0].channels if all(c.channels == clips[0].channels and c.rate == rate for c in clips) else 1
    silence = b"\x00\x00" * channels * int(rate * pause)
    pcm = silence.join(clip.data if channels == clip.channels and clip.rate == rate else _mono_at(clip, rate)
                       for clip in clips)
    seconds = round(len(pcm) / (2 * channels * rate), 1)
    encoded = mp3_of(pcm, rate, channels) if mp3 else None
    if encoded:
        return encoded, "mp3", seconds
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm)
    return out.getvalue(), "wav", seconds


# --------------------------------------------------------------- saved groups
def _saved(store: Path, key: str) -> Clip | None:
    """A group spoken before, found by what it says (so an edited script reuses every unchanged group)."""
    try:
        found = json.loads((store / f"{key[:32]}.json").read_text(encoding="utf-8"))
        if found.get("identity") != key:
            return None
        return decode((store / found["file"]).read_bytes(), found.get("mime", ""))
    except (OSError, ValueError, KeyError, VoiceError):
        return None


def _save(store: Path, key: str, data: bytes, mime: str, route: str) -> None:
    name = f"{key[:32]}.audio"
    (store / name).write_bytes(data)
    (store / f"{key[:32]}.json").write_text(json.dumps(
        {"identity": key, "file": name, "mime": mime, "route": route}, indent=1), encoding="utf-8")


def speak(session: Callable[[], object], lines: list[dict], hosts: tuple[str, str], voices: tuple[str, str],
          models: list[str], folder: Path, say: Callable[[str, int | None], None]) -> dict:
    """Speak the script into one audio file. ``session`` gives the PW AI session (made only
    when a group must be paid for). Returns {"audio", "ext", "seconds", "parts", "reused_parts",
    "route"}; an AIError (sign-in, budget, no audio) stops it, keeping the groups already made."""
    from ..typeset.ai import AIError

    groups = parts(lines)
    store = Path(folder) / "audio_parts"
    store.mkdir(parents=True, exist_ok=True)
    clips, reused, routes = [], 0, set()
    two_voices = True                   # until PW AI refuses the two-voice request itself
    for number, group in enumerate(groups, start=1):
        key = fingerprint(group, hosts, voices)
        clip = _saved(store, key)
        if clip is not None:
            clips.append(clip)
            reused += 1
            continue
        say(f"Speaking part {number} of {len(groups)}", 40 + round(55 * (number - 1) / len(groups)))
        data = None
        if two_voices:
            try:
                data, mime = session().speech(f"voice part {number}", models, request(group, hosts, voices))
                route = "two_voices"
            except AIError as problem:
                if problem.code in ("budget", "sign_in"):
                    raise
                two_voices = problem.code != "unsupported"
                say(f"Two voices in one call did not work ({problem}); speaking part {number} line by line", None)
        if data is None:
            pieces = []
            for line in group:                 # "voice line": calls the run's steps do not list one by one
                spoken, kind = session().speech_line("voice line", models, line_text(line, hosts, voices),
                                                     voices[hosts.index(line["speaker"])])
                pieces.append(decode(spoken, kind))
            data, ext, _seconds = join(pieces, LINE_PAUSE)
            mime, route = ("audio/wav" if ext == "wav" else "audio/mpeg"), "line_by_line"
        clip = decode(data, mime)
        _save(store, key, data, mime, route)
        clips.append(clip)
        routes.add(route)
    audio, ext, seconds = join(clips, mp3=True)
    return {"audio": audio, "ext": ext, "seconds": seconds, "parts": len(groups), "reused_parts": reused,
            "route": "+".join(sorted(routes)) or "saved"}
