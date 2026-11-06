"""Bounded audio excerpts through the host's configured voice transcription task."""
from __future__ import annotations

import asyncio
from array import array
import io
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
import wave

from .chat_media import AUDIO_LIMIT, ChatMediaError, _ffmpeg, _ffmpeg_run, _video_format, sample_positions
from .unified_expression import _IDENTIFIER, _INSTRUCTION, _SECRET

_CLIP_SECONDS = 20
_WAV_LIMIT = _CLIP_SECONDS * 16000 * 2 + 4096


def _audio_format(data):
    if not isinstance(data, bytes) or not 12 <= len(data) <= AUDIO_LIMIT:
        raise ChatMediaError("invalid_audio_size")
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"fLaC"):
        return "flac"
    if data.startswith(b"#!AMR"):
        return "amr"
    if data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        return "silk"
    if data.startswith(b"ID3") or data[0] == 255 and data[1] & 0xE0 == 0xE0:
        return "mp3"
    if data[4:8] == b"ftyp":
        return "mov"
    raise ChatMediaError("unsupported_audio")


def _windows(duration, positions, video):
    if positions is not None:
        targets = sample_positions(duration, positions)
    elif video:
        targets = sample_positions(duration)
    else:
        targets = [n for n in (0, 20, 40) if not duration or n < duration]
    windows = []
    for target in targets:
        start = max(0.0, target - 5) if positions is not None or video else float(target)
        end = min(start + _CLIP_SECONDS, duration) if duration else start + _CLIP_SECONDS
        if windows and start < windows[-1][1]:
            start = windows[-1][1]
        if end > start:
            windows.append((start, end))
    return windows


async def audio_excerpts(data: bytes, *, positions=None, video=False):
    """Decode at most sixty seconds to mono PCM, with exact excerpt boundaries."""
    fmt = _video_format(data) if video else _audio_format(data)
    binary = _ffmpeg()
    with tempfile.TemporaryDirectory(prefix="mlw-audio-") as directory:
        source, output = Path(directory)/"media", Path(directory)/"clip.wav"
        source.write_bytes(data)
        if fmt == "silk":
            decoded = Path(directory)/"decoded.wav"
            # Plugin dependencies can be provided via the runner's sys.path rather than site-packages.
            environment = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if isinstance(p, str) and p)}
            status = await asyncio.wait_for(_ffmpeg_run(
                [sys.executable, str(Path(__file__).with_name("audio_silk.py")), str(source), str(decoded)],
                env=environment), 10)
            if status != 0 or not decoded.is_file() or not 44 < decoded.stat().st_size <= 60*24000*2+4096:
                raise ChatMediaError("silk_decoder_failed")
            source, fmt = decoded, "wav"
        base = [binary, "-nostdin", "-hide_banner", "-max_alloc", "67108864", "-protocol_whitelist", "file",
                "-probesize", "4194304", "-analyzeduration", "2000000", "-threads", "1", "-max_streams", "16", "-f", fmt]
        if fmt == "mov":
            base += ["-enable_drefs", "0", "-use_absolute_path", "0"]
        async with asyncio.timeout(15):
            duration = await _ffmpeg_run([*base, "-i", str(source)], probe=True)
            duration = duration if math.isfinite(duration) and 0 < duration <= 86400 else 0
            clips = []
            for start, end in _windows(duration, positions, video):
                output.unlink(missing_ok=True)
                status = await _ffmpeg_run([*base, "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(source),
                    "-t", f"{end-start:.3f}", "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000",
                    "-threads", "1", "-c:a", "pcm_s16le", "-fs", str(_WAV_LIMIT), "-f", "wav", "-y", str(output)])
                if status != 0 or not output.is_file() or not 44 < output.stat().st_size <= _WAV_LIMIT:
                    continue
                raw = output.read_bytes()
                with wave.open(io.BytesIO(raw), "rb") as wav:
                    if wav.getparams()[:3] != (1, 2, 16000) or wav.getnframes() > _CLIP_SECONDS * 16000:
                        continue
                    seconds = wav.getnframes() / 16000
                    samples = array("h", wav.readframes(wav.getnframes()))
                # Silent excerpts otherwise invite plausible but fabricated ASR text.
                if seconds >= .1 and any(abs(sample) > 100 for sample in samples):
                    clips.append({"start": round(start, 3), "end": round(start + seconds, 3), "audio": raw})
            return clips


def _transcript(result):
    if not isinstance(result, dict) or result.get("success") is not True or result.get("tool_calls"):
        return ""
    value = result.get("text") or result.get("content")
    if not isinstance(value, str) or not 1 <= len(value) <= 4000:
        return ""
    text = unicodedata.normalize("NFKC", " ".join(value.split()))
    if (any(unicodedata.category(c).startswith("C") for c in text)
            or _IDENTIFIER.search(text) or _SECRET.search(text) or _INSTRUCTION.search(text)
            or any(mark in text for mark in ("[MaiLivingWorld:", "<", ">", "```"))):
        return ""
    return bounded_excerpt(text, 160)


def bounded_excerpt(text, limit):
    """Keep complete sentences or clauses, explicitly withholding an oversized first clause."""
    if len(text) <= limit:
        return text
    candidates = list(re.finditer(r"[。！？!?；;，,]", text[:max(0, limit-1)]))
    if candidates:
        return text[:candidates[-1].end()] + "…"
    return "（该段较长，需进一步指定细节）" if limit >= 19 else "（内容较长）"


async def transcribe_audio(ctx, data, *, reserve, valid, positions=None, video=False):
    """Each real ASR call spends the caller's shared model budget; text is evidence only."""
    transcribe = getattr(getattr(ctx, "llm", None), "transcribe_audio", None)
    if not callable(transcribe) or not valid():
        return []
    try:
        clips = await audio_excerpts(data, positions=positions, video=video)
    except (OSError, ValueError, RuntimeError, TimeoutError, wave.Error):
        return []
    result = []
    for clip in clips:
        if not valid() or not reserve():
            break
        try:
            reply = await asyncio.wait_for(transcribe(clip["audio"], task_name="voice"), 18)
        except Exception:
            continue
        if not valid():
            return []
        text = _transcript(reply)
        if text:
            result.append({"start": clip["start"], "end": clip["end"], "text": text})
    return result


def timeline_text(segments, *, limit=330):
    """Intervals describe supplied clips, never word-level ASR timestamps."""
    labels = [f"{part['start']:.1f}–{part['end']:.1f}秒" for part in segments]
    allowance = max(8, (limit - sum(len(label)+2 for label in labels) - len(labels)+1) // max(1, len(labels)))
    return "；".join(label + "「" + bounded_excerpt(part["text"], allowance) + "」"
                     for label, part in zip(labels, segments))
