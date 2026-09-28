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
import time
from typing import Any
import uuid
from datetime import datetime, timezone


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


def _memory_snapshot() -> dict[str, int]:
    """Read optional Linux cgroup v2 counters; absence is not zero usage."""
    root = Path("/sys/fs/cgroup")
    snapshot: dict[str, int] = {}
    for filename, key in (("memory.max", "limitBytes"), ("memory.current", "currentBytes"),
                          ("memory.peak", "peakBytes")):
        try:
            value = (root / filename).read_text(encoding="ascii").strip()
            if value != "max":
                snapshot[key] = int(value)
        except (OSError, ValueError):
            pass
    try:
        events = (root / "memory.events.local").read_text(encoding="ascii")
        for line in events.splitlines():
            name, value = line.split()
            if name in {"oom", "oom_kill", "max"}:
                snapshot[{"oom": "oomCount", "oom_kill": "oomKillCount", "max": "maxEventCount"}[name]] = int(value)
    except (OSError, ValueError):
        pass
    return snapshot


def _record_attempt(directory: Path | None, *, material_id: str | None, video_sha: str | None,
                    profile: str, started_at: str, started_clock: float, returncode: int | None,
                    status: str, stderr: str = "", checkpoint: dict | None = None,
                    memory_before: dict | None = None, memory_after: dict | None = None) -> None:
    if directory is None:
        return
    before, after = memory_before or {}, memory_after or {}
    delta = (after["oomKillCount"] - before["oomKillCount"]
             if "oomKillCount" in before and "oomKillCount" in after else None)
    event = {"schemaVersion": 1, "materialId": material_id, "videoSha256": video_sha,
             "profile": profile, "startedAt": started_at, "finishedAt": datetime.now(timezone.utc).isoformat(),
             "elapsedSec": round(time.monotonic() - started_clock, 3), "status": status,
             "exitCode": returncode, "workerCheckpoint": checkpoint, "cgroupBefore": before,
             "cgroupAfter": after, "oomKillDelta": delta}
    if stderr:
        event["stderrTail"] = stderr[-1000:]
    filename = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:8]}.json"
    _write_json(directory / filename, event)


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

    def transcribe(self, wav_path: Path, *, attempt_dir: Path | None = None,
                   material_id: str | None = None, video_sha: str | None = None,
                   quality_only: bool = False) -> dict[str, Any]:
        self.preflight()
        worker = Path(__file__).with_name("asr_worker.py")
        with tempfile.TemporaryDirectory(prefix="yuntu-asr-worker-") as temporary:
            output = Path(temporary) / "result.json"
            checkpoint_path = Path(temporary) / "checkpoint.json"
            for profile in (("quality",) if quality_only else ("quality", "memory_safe")):
                output.unlink(missing_ok=True)
                checkpoint_path.unlink(missing_ok=True)
                started_at = datetime.now(timezone.utc).isoformat()
                started_clock = time.monotonic()
                before = _memory_snapshot()
                try:
                    completed = subprocess.run(
                        [sys.executable, str(worker), str(wav_path), str(self.model_dir), str(output)],
                        input=json.dumps({"profile": profile, "hotwords": self.hotwords,
                                          "checkpointPath": str(checkpoint_path)}),
                        capture_output=True, text=True, check=False, timeout=900,
                    )
                except subprocess.TimeoutExpired as exc:
                    _record_attempt(attempt_dir, material_id=material_id, video_sha=video_sha, profile=profile,
                                    started_at=started_at, started_clock=started_clock, returncode=None,
                                    status="timeout", checkpoint=_read_json(checkpoint_path) if checkpoint_path.is_file() else None,
                                    memory_before=before, memory_after=_memory_snapshot())
                    raise TranscriptionError("asr_worker_timeout") from exc
                except OSError as exc:
                    _record_attempt(attempt_dir, material_id=material_id, video_sha=video_sha, profile=profile,
                                    started_at=started_at, started_clock=started_clock, returncode=None,
                                    status="launch_failed", stderr=str(exc), memory_before=before,
                                    memory_after=_memory_snapshot())
                    raise TranscriptionError("asr_worker_launch_failed") from exc
                after = _memory_snapshot()
                checkpoint = _read_json(checkpoint_path) if checkpoint_path.is_file() else None
                if completed.returncode == 0 and output.is_file():
                    try:
                        result = _read_json(output)
                    except (OSError, ValueError) as exc:
                        _record_attempt(attempt_dir, material_id=material_id, video_sha=video_sha, profile=profile,
                                        started_at=started_at, started_clock=started_clock, returncode=0,
                                        status="invalid_output", checkpoint=checkpoint, memory_before=before, memory_after=after)
                        raise TranscriptionError("asr_worker_invalid_output") from exc
                    _record_attempt(attempt_dir, material_id=material_id, video_sha=video_sha, profile=profile,
                                    started_at=started_at, started_clock=started_clock, returncode=0,
                                    status="worker_completed", checkpoint=checkpoint, memory_before=before, memory_after=after)
                    return result
                status = "killed" if completed.returncode in {-9, 137} else "failed"
                _record_attempt(attempt_dir, material_id=material_id, video_sha=video_sha, profile=profile,
                                started_at=started_at, started_clock=started_clock, returncode=completed.returncode,
                                status=status, stderr=completed.stderr, checkpoint=checkpoint,
                                memory_before=before, memory_after=after)
                if completed.returncode not in {-9, 137}:
                    raise TranscriptionError(f"asr_worker_failed:{completed.returncode}:{completed.stderr[-500:]}")
            raise TranscriptionError("asr_quality_worker_killed" if quality_only else "asr_worker_killed_both_profiles")


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


def _check_quality_retry_cache(root: Path, detail_dir: Path, provider: Any) -> None:
    """Avoid unexpectedly reprocessing every video when the model or hints changed."""
    provider_name = getattr(provider, "provider_name", provider.__class__.__name__)
    model_version = getattr(provider, "model_version", "unknown")
    cache_key = getattr(provider, "cache_key", None)
    for detail_path in sorted(detail_dir.glob("*.json")):
        detail = _read_json(detail_path)
        if str(detail.get("script_text") or "").strip():
            continue
        video = root / "videos" / detail_path.stem / "source-video.mp4"
        if not video.is_file() or video.stat().st_size == 0:
            continue
        video_hash = _sha256(video)
        cached = root / "asr" / "by-video" / f"{video_hash}.json"
        if not cached.is_file():
            raise TranscriptionError("quality_retry_requires_complete_cache")
        result = _read_json(cached)
        if result.get("videoSha256") != video_hash or result.get("status") not in {
            "available", "no_speech", "no_audio_track"
        }:
            raise TranscriptionError("quality_retry_requires_complete_cache")
        if result["status"] in {"available", "no_speech"} and (
            result.get("provider") != provider_name or result.get("modelVersion") != model_version
            or result.get("cacheKey") != cache_key
        ):
            raise TranscriptionError("quality_retry_model_or_hints_mismatch")


def transcribe_run(run_dir: Path, *, provider: Any, ffmpeg_bin: str | None = None,
                   retry_low_memory: bool = False) -> dict[str, Any]:
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
    if retry_low_memory:
        _check_quality_retry_cache(root, detail_dir, provider)
    verified = {item.get("path"): item.get("sha256") for item in facts.get("source_artifacts", []) if isinstance(item, dict)}
    manifest: dict[str, Any] = {"schemaVersion": 1, "materials": [], "counts": {}}
    retry_attempted: set[str] = set()
    retry_upgraded: set[str] = set()
    retry_failed: dict[str, str] = {}
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
            attempt_dir = root / "asr" / "attempts" / video_hash
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
            profile = result.get("decodeProfile") if isinstance(result, dict) else None
            upgrade = (retry_low_memory and result is not None and result.get("status") in {"available", "no_speech"}
                       and isinstance(profile, dict) and profile.get("name") == "memory_safe")
            retry_error = None
            if upgrade and video_hash in retry_failed:
                retry_error = retry_failed[video_hash]
            elif result is None or upgrade:
                previous_result = result if upgrade else None
                if upgrade:
                    retry_attempted.add(video_hash)
                with tempfile.TemporaryDirectory(prefix="yuntu-asr-") as temporary:
                    wav = Path(temporary) / "audio.wav"
                    audio_status = _extract_audio(video, wav, ffmpeg_bin)
                    if audio_status != "available":
                        if upgrade:
                            retry_error = audio_status
                        else:
                            result = {"status": audio_status, "videoSha256": video_hash}
                    else:
                        try:
                            if isinstance(provider, FasterWhisperProvider):
                                raw = provider.transcribe(wav, attempt_dir=attempt_dir, material_id=material_id,
                                                          video_sha=video_hash, quality_only=upgrade)
                            else:
                                raw = provider.transcribe(wav)
                            normalized = _normalize_result(raw)
                            if upgrade and previous_result.get("status") == "available" and not normalized["text"]:
                                raise TranscriptionError("quality_retry_returned_no_speech")
                            result = {"status": "available" if normalized["text"] else "no_speech",
                                      "videoSha256": video_hash, "audioSha256": _sha256(wav),
                                      "provider": getattr(provider, "provider_name", provider.__class__.__name__),
                                      "modelVersion": getattr(provider, "model_version", "unknown"),
                                      "cacheKey": getattr(provider, "cache_key", None), **normalized}
                        except Exception as exc:
                            if upgrade:
                                retry_error = f"{type(exc).__name__}: {exc}"
                            else:
                                result = {"status": "transcription_failed", "videoSha256": video_hash,
                                          "error": f"{type(exc).__name__}: {exc}"}
                if upgrade and retry_error:
                    result = previous_result
                    retry_failed[video_hash] = retry_error
                elif upgrade:
                    retry_upgraded.add(video_hash)
                if retry_error is None and result["status"] in {"available", "no_speech", "no_audio_track"}:
                    _write_json(cached, result)
            material_path = root / "asr" / "materials" / f"{material_id}.json"
            material_result = {**result, "materialId": material_id, "videoPath": entry["videoPath"]}
            if attempt_dir.is_dir():
                attempt_path = attempt_dir.relative_to(root).as_posix()
                material_result["attemptsPath"] = attempt_path
                entry["attemptsPath"] = attempt_path
                attempts = [{"path": item.relative_to(root).as_posix(), "sha256": _sha256(item)}
                            for item in sorted(attempt_dir.glob("*.json"))]
                material_result["attempts"] = attempts
                entry["attempts"] = attempts
            if upgrade:
                entry["qualityRetryStatus"] = "failed" if retry_error else "upgraded"
                material_result["qualityRetryStatus"] = entry["qualityRetryStatus"]
                if retry_error:
                    material_result["qualityRetryError"] = retry_error
            _write_json(material_path, material_result)
            entry["status"] = result["status"]
            entry["asrPath"] = f"asr/materials/{material_id}.json"
            entry["asrSha256"] = _sha256(material_path)
        manifest["materials"].append(entry)
        manifest["counts"][entry["status"]] = manifest["counts"].get(entry["status"], 0) + 1
    if retry_low_memory:
        manifest["qualityRetries"] = {"attemptedVideos": len(retry_attempted), "upgradedVideos": len(retry_upgraded),
                                      "failedVideos": len(retry_failed)}
    _write_json(root / "asr" / "manifest.json", manifest)
    return manifest
