"""Decode one WAV in a disposable process with bounded audio chunks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import sys
import tempfile
from datetime import datetime, timezone
import wave


PROFILES = {
    "quality": {"chunkSec": 20, "beamSize": 5, "vadFilter": False},
    "memory_safe": {"chunkSec": 20, "beamSize": 1, "vadFilter": True},
}


def _checkpoint(path: Path | None, stage: str, chunk_index: int | None = None) -> None:
    if path is None:
        return
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = int(peak if sys.platform == "darwin" else peak * 1024)
    data = {"stage": stage, "at": datetime.now(timezone.utc).isoformat(), "peakRssBytes": peak_bytes}
    if chunk_index is not None:
        data["chunkIndex"] = chunk_index
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)


def transcribe_chunks(wav_path: Path, model, *, profile: str, hotwords: str = "",
                      checkpoint_path: Path | None = None) -> dict:
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
            chunk_index = 0
            while offset < total_frames:
                count = min(chunk_frames, total_frames - offset)
                audio = source.readframes(count)
                with wave.open(str(chunk_path), "wb") as chunk:
                    chunk.setnchannels(1)
                    chunk.setsampwidth(2)
                    chunk.setframerate(frame_rate)
                    chunk.writeframes(audio)
                _checkpoint(checkpoint_path, "chunk_started", chunk_index)
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
                _checkpoint(checkpoint_path, "chunk_completed", chunk_index)
                offset += count
                chunk_index += 1
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
    checkpoint_path = Path(request["checkpointPath"]) if request.get("checkpointPath") else None
    _checkpoint(checkpoint_path, "model_loading")
    from faster_whisper import WhisperModel

    model = WhisperModel(args[1], device="cpu", compute_type="int8", cpu_threads=2, num_workers=1)
    _checkpoint(checkpoint_path, "model_loaded")
    result = transcribe_chunks(Path(args[0]), model, profile=profile,
                               hotwords=str(request.get("hotwords") or ""), checkpoint_path=checkpoint_path)
    Path(args[2]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    _checkpoint(checkpoint_path, "completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
