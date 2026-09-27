"""Turn verified, locally downloaded Yuntu videos into separate ASR facts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any


class TranscriptionError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TranscriptionError(f"invalid JSON object: {path.name}")
    return value


def _ffmpeg_binary() -> str:
    configured = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if configured:
        return configured
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except ImportError:
        found = shutil.which("ffmpeg")
        if found:
            return found
    raise TranscriptionError("ffmpeg_unavailable")


def _extract_audio(video: Path, destination: Path, ffmpeg_bin: str | None = None) -> str:
    command = [ffmpeg_bin or _ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-i", str(video), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "pcm_s16le", "-f", "wav", str(destination)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    except subprocess.TimeoutExpired:
        return "audio_timeout"
    except OSError:
        return "ffmpeg_unavailable"
    if result.returncode != 0:
        stderr = result.stderr.lower()
        if "matches no streams" in stderr or "stream specifier" in stderr and "no streams" in stderr:
            return "no_audio_track"
        return "audio_decode_failed"
    if not destination.is_file() or destination.stat().st_size <= 44:
        return "audio_empty"
    return "available"


class FasterWhisperProvider:
    """Offline provider: model files must already exist in the target environment."""

    provider_name = "faster-whisper"

    decode_policy_version = "chunked-v1"

    def __init__(self, model_dir: Path | None, *, hotwords: str = ""):
        self.model_dir = Path(model_dir).resolve() if model_dir is not None else None
        self._model_version = None
        self.hotwords = hotwords.strip()
        if len(self.hotwords) > 256:
            raise TranscriptionError("asr_hotwords_too_long")

    def preflight(self) -> None:
        if self.model_dir is None or not (self.model_dir / "model.bin").is_file() or not (self.model_dir / "config.json").is_file():
            raise TranscriptionError("model_directory_missing_or_incomplete")
        if importlib.util.find_spec("faster_whisper") is None:
            raise TranscriptionError("faster_whisper_unavailable")
        _ffmpeg_binary()

    @property
    def cache_key(self) -> str:
        material = f"{self.model_version}|{self.decode_policy_version}|{self.hotwords}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @property
    def model_version(self) -> str:
        if self._model_version is None:
            if self.model_dir is None or not self.model_dir.is_dir():
                return "unconfigured"
            digest = hashlib.sha256()
            files = sorted(path for path in self.model_dir.rglob("*") if path.is_file())
            if not files:
                return "unconfigured"
            for path in files:
                digest.update(path.relative_to(self.model_dir).as_posix().encode("utf-8"))
                digest.update(_sha256(path).encode("ascii"))
            self._model_version = f"{self.model_dir.name}:{digest.hexdigest()}"
        return self._model_version

    def transcribe(self, wav_path: Path) -> dict[str, Any]:
        self.preflight()
        worker = Path(__file__).with_name("asr_worker.py")
        with tempfile.TemporaryDirectory(prefix="yuntu-asr-worker-") as temporary:
            output = Path(temporary) / "result.json"
            for profile in ("quality", "memory_safe"):
                try:
                    completed = subprocess.run(
                        [sys.executable, str(worker), str(wav_path), str(self.model_dir), str(output)],
                        input=json.dumps({"profile": profile, "hotwords": self.hotwords}),
                        capture_output=True, text=True, check=False, timeout=900,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise TranscriptionError("asr_worker_timeout") from exc
                if completed.returncode == 0 and output.is_file():
                    return _read_json(output)
                if completed.returncode not in {-9, 137}:
                    raise TranscriptionError(f"asr_worker_failed:{completed.returncode}:{completed.stderr[-500:]}")
            raise TranscriptionError("asr_worker_memory_limit_exceeded")


def _normalize_result(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("segments"), list):
        raise TranscriptionError("invalid_provider_result")
    duration = float(raw.get("durationSec") or 0)
    previous_end = 0.0
    segments = []
    for item in raw["segments"]:
        start, end = float(item["start"]), float(item["end"])
        content = str(item.get("text") or "").strip()
        if not content or start < 0 or end <= start or start < previous_end - 0.001 or (duration and end > duration + 0.5):
            raise TranscriptionError("invalid_asr_segment")
        segments.append({"start": start, "end": end, "text": content})
        previous_end = end
    result = {"durationSec": duration, "language": str(raw.get("language") or "unknown"),
              "segments": segments, "text": "".join(segment["text"] for segment in segments)}
    if isinstance(raw.get("decodeProfile"), dict):
        result["decodeProfile"] = raw["decodeProfile"]
    return result


def transcribe_run(run_dir: Path, *, provider: Any, ffmpeg_bin: str | None = None) -> dict[str, Any]:
    """Process only accepted details; never mutate platform facts or resubmit RPA."""
    root = Path(run_dir).resolve()
    summary_path = root / "run-summary.json"
    facts_path = root / "facts" / "manifest.json"
    if not summary_path.is_file() or not facts_path.is_file():
        raise TranscriptionError("verified_collection_facts_required")
    summary = _read_json(summary_path)
    facts = _read_json(facts_path)
    if summary.get("rpa_paused") or summary.get("firstBatchCsvGate", {}).get("status") in {"blocked", "unconfirmed"}:
        raise TranscriptionError("rpa_gate_not_ready")
    if summary.get("workflow", {}).get("stage") != "ready_for_analysis":
        raise TranscriptionError("collection_not_ready_for_scripts")
    detail_dir = root / "normalized" / "detail-records"
    if not detail_dir.is_dir():
        raise TranscriptionError("detail_records_missing")
    verified = {item.get("path"): item.get("sha256") for item in facts.get("source_artifacts", []) if isinstance(item, dict)}
    manifest: dict[str, Any] = {"schemaVersion": 1, "materials": [], "counts": {}}
    for path in sorted(detail_dir.glob("*.json")):
        relative = path.relative_to(root).as_posix()
        if verified.get(relative) != _sha256(path):
            raise TranscriptionError("detail_source_hash_mismatch")
        detail = _read_json(path)
        material_id = str(detail.get("material_id") or "")
        if not material_id.isdigit() or path.stem != material_id:
            raise TranscriptionError("detail_identity_mismatch")
        platform_text = str(detail.get("script_text") or "").strip()
        video = root / "videos" / material_id / "source-video.mp4"
        entry: dict[str, Any] = {"materialId": material_id, "platformScriptAvailable": bool(platform_text),
                                 "videoPath": f"videos/{material_id}/source-video.mp4"}
        if platform_text:
            entry["status"] = "platform_script"
        elif not video.is_file() or video.stat().st_size == 0:
            entry["status"] = "missing_video"
        else:
            video_hash = _sha256(video)
            entry["videoSha256"] = video_hash
            cached = root / "asr" / "by-video" / f"{video_hash}.json"
            if cached.is_file():
                result = _read_json(cached)
                if result.get("videoSha256") != video_hash:
                    raise TranscriptionError("asr_cache_hash_mismatch")
                if result.get("status") in {"available", "no_speech"} and (
                    result.get("provider") != getattr(provider, "provider_name", provider.__class__.__name__)
                    or result.get("modelVersion") != getattr(provider, "model_version", "unknown")
                    or result.get("cacheKey") != getattr(provider, "cache_key", None)
                ):
                    result = None
            else:
                result = None
            if result is None:
                with tempfile.TemporaryDirectory(prefix="yuntu-asr-") as temporary:
                    wav = Path(temporary) / "audio.wav"
                    audio_status = _extract_audio(video, wav, ffmpeg_bin)
                    if audio_status != "available":
                        result = {"status": audio_status, "videoSha256": video_hash}
                    else:
                        try:
                            normalized = _normalize_result(provider.transcribe(wav))
                            result = {"status": "available" if normalized["text"] else "no_speech",
                                      "videoSha256": video_hash, "audioSha256": _sha256(wav),
                                      "provider": getattr(provider, "provider_name", provider.__class__.__name__),
                                      "modelVersion": getattr(provider, "model_version", "unknown"),
                                      "cacheKey": getattr(provider, "cache_key", None), **normalized}
                        except Exception as exc:
                            result = {"status": "transcription_failed", "videoSha256": video_hash,
                                      "error": f"{type(exc).__name__}: {exc}"}
                if result["status"] in {"available", "no_speech", "no_audio_track"}:
                    _write_json(cached, result)
            material_path = root / "asr" / "materials" / f"{material_id}.json"
            material_result = {**result, "materialId": material_id, "videoPath": entry["videoPath"]}
            _write_json(material_path, material_result)
            entry["status"] = result["status"]
            entry["asrPath"] = f"asr/materials/{material_id}.json"
            entry["asrSha256"] = _sha256(material_path)
        manifest["materials"].append(entry)
        manifest["counts"][entry["status"]] = manifest["counts"].get(entry["status"], 0) + 1
    _write_json(root / "asr" / "manifest.json", manifest)
    return manifest
