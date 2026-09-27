"""Decode one WAV in a disposable process with bounded audio chunks."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import wave


PROFILES = {
    "quality": {"chunkSec": 20, "beamSize": 5, "vadFilter": False},
    "memory_safe": {"chunkSec": 20, "beamSize": 1, "vadFilter": True},
}


def transcribe_chunks(wav_path: Path, model, *, profile: str, hotwords: str = "") -> dict:
    settings = PROFILES[profile]
    segments = []
    language = "zh"
    with wave.open(str(wav_path), "rb") as source:
        if (source.getnchannels(), source.getframerate(), source.getsampwidth()) != (1, 16000, 2):
            raise ValueError("asr_audio_must_be_pcm16_mono_16khz")
        frame_rate = source.getframerate()
        total_frames = source.getnframes()
        duration = total_frames / frame_rate
        chunk_frames = settings["chunkSec"] * frame_rate
        offset = 0
        with tempfile.TemporaryDirectory(prefix="yuntu-asr-chunks-") as temporary:
            chunk_path = Path(temporary) / "chunk.wav"
            while offset < total_frames:
                count = min(chunk_frames, total_frames - offset)
                audio = source.readframes(count)
                with wave.open(str(chunk_path), "wb") as chunk:
                    chunk.setnchannels(1)
                    chunk.setsampwidth(2)
                    chunk.setframerate(frame_rate)
                    chunk.writeframes(audio)
                decoded, info = model.transcribe(
                    str(chunk_path), language="zh", beam_size=settings["beamSize"],
                    vad_filter=settings["vadFilter"], condition_on_previous_text=False,
                    hotwords=hotwords or None,
                )
                language = str(getattr(info, "language", None) or language)
                for item in decoded:
                    start = max(offset / frame_rate, offset / frame_rate + float(item.start))
                    end = min(duration, (offset + count) / frame_rate, offset / frame_rate + float(item.end))
                    if segments:
                        start = max(start, segments[-1]["end"])
                    content = str(item.text).strip()
                    if content and end > start:
                        segments.append({"start": start, "end": end, "text": content})
                offset += count
    return {
        "durationSec": duration, "language": language, "segments": segments,
        "decodeProfile": {"name": profile, **settings, "computeType": "int8", "cpuThreads": 2},
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        raise SystemExit("usage: asr_worker.py AUDIO_WAV MODEL_DIR OUTPUT_JSON")
    request = json.load(sys.stdin)
    profile = request.get("profile")
    if profile not in PROFILES:
        raise ValueError("invalid_asr_profile")
    from faster_whisper import WhisperModel

    model = WhisperModel(args[1], device="cpu", compute_type="int8", cpu_threads=2, num_workers=1)
    result = transcribe_chunks(Path(args[0]), model, profile=profile, hotwords=str(request.get("hotwords") or ""))
    Path(args[2]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
