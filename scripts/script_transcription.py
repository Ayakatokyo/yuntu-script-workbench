"""Prepare audio for Agent file understanding and record its text-only response."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any


class TranscriptionError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TranscriptionError(f"invalid_json_object:{path.name}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
    os.replace(temporary, path)


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
    try:
        command = [ffmpeg_bin or _ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-i", str(video), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                   "-c:a", "pcm_s16le", "-f", "wav", str(destination)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    except subprocess.TimeoutExpired:
        return "audio_timeout"
    except (OSError, TranscriptionError):
        return "ffmpeg_unavailable"
    if result.returncode != 0:
        stderr = result.stderr.lower()
        if "matches no streams" in stderr or "stream specifier" in stderr and "no streams" in stderr:
            return "no_audio_track"
        return "audio_decode_failed"
    if not destination.is_file() or destination.stat().st_size <= 44:
        return "audio_empty"
    return "available"


def _verified_details(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    summary_path = root / "run-summary.json"
    facts_path = root / "facts" / "manifest.json"
    if not summary_path.is_file() or not facts_path.is_file():
        raise TranscriptionError("verified_collection_facts_required")
    summary = _read_json(summary_path)
    if summary.get("rpa_paused") or summary.get("firstBatchCsvGate", {}).get("status") in {"blocked", "unconfirmed"}:
        raise TranscriptionError("rpa_gate_not_ready")
    if summary.get("workflow", {}).get("stage") != "ready_for_analysis":
        raise TranscriptionError("collection_not_ready_for_scripts")
    facts = _read_json(facts_path)
    verified = {item.get("path"): item.get("sha256") for item in facts.get("source_artifacts", []) if isinstance(item, dict)}
    detail_dir = root / "normalized" / "detail-records"
    if not detail_dir.is_dir():
        raise TranscriptionError("detail_records_missing")
    details = []
    for path in sorted(detail_dir.glob("*.json")):
        if verified.get(path.relative_to(root).as_posix()) != _sha256(path):
            raise TranscriptionError("detail_source_hash_mismatch")
        detail = _read_json(path)
        if not str(detail.get("material_id") or "").isdigit() or str(detail["material_id"]) != path.stem:
            raise TranscriptionError("detail_identity_mismatch")
        details.append((path, detail))
    return details


def prepare_audio(run_dir: Path, *, ffmpeg_bin: str | None = None) -> dict[str, Any]:
    """Extract one persistent WAV per unique video; never run a local recognition model."""
    root = Path(run_dir).resolve()
    previous_path = root / "transcripts" / "manifest.json"
    previous = _read_json(previous_path) if previous_path.is_file() else {}
    previous_audio = {item.get("videoSha256"): item.get("audioSha256")
                      for item in previous.get("materials", []) if isinstance(item, dict) and item.get("audioSha256")}
    materials: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    attempted_audio: dict[str, str] = {}
    for path, detail in _verified_details(root):
        material_id = path.stem
        item: dict[str, Any] = {"materialId": material_id}
        video = root / "videos" / material_id / "source-video.mp4"
        if str(detail.get("script_text") or "").strip():
            item["status"] = "platform_script"
        elif not video.is_file() or video.stat().st_size == 0:
            item["status"] = "missing_video"
        else:
            video_sha = _sha256(video)
            audio = root / "audio" / "by-video" / f"{video_sha}.wav"
            item.update({"videoPath": video.relative_to(root).as_posix(), "videoSha256": video_sha})
            if video_sha in attempted_audio:
                item["status"] = attempted_audio[video_sha]
            elif not audio.is_file() or video_sha not in previous_audio:
                audio.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="yuntu-audio-") as temporary:
                    extracted = Path(temporary) / "audio.wav"
                    item["status"] = _extract_audio(video, extracted, ffmpeg_bin)
                    if item["status"] == "available":
                        os.replace(extracted, audio)
            else:
                item["status"] = "available"
            attempted_audio[video_sha] = item["status"]
            if item["status"] == "available":
                item["audioPath"] = audio.relative_to(root).as_posix()
                item["audioSha256"] = _sha256(audio)
                if video_sha in previous_audio and previous_audio[video_sha] != item["audioSha256"]:
                    raise TranscriptionError("prepared_audio_hash_mismatch")
                transcript_path = root / "transcripts" / "by-video" / f"{video_sha}.json"
                if transcript_path.is_file():
                    transcript = _read_json(transcript_path)
                    if (transcript.get("videoSha256") != video_sha
                            or transcript.get("audioSha256") != item["audioSha256"]
                            or transcript.get("status") != "available"
                            or not str(transcript.get("text") or "").strip()):
                        raise TranscriptionError("transcript_source_mismatch")
                    raw = root / "transcripts" / "raw" / f"{video_sha}.txt"
                    if not raw.is_file() or _sha256(raw) != transcript.get("rawResponseSha256"):
                        raise TranscriptionError("transcript_response_hash_mismatch")
                    item["status"] = "agent_transcribed"
                    item["transcriptPath"] = transcript_path.relative_to(root).as_posix()
                    item["transcriptSha256"] = _sha256(transcript_path)
                else:
                    item["status"] = "pending_agent"
        materials.append(item)
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    manifest = {"schemaVersion": 1, "materials": materials, "counts": counts}
    _write_json(root / "transcripts" / "manifest.json", manifest)
    return manifest


def import_transcript(run_dir: Path, *, video_sha: str, text_file: Path, response_file: Path) -> dict[str, Any]:
    """Bind an Agent-produced transcript to the exact audio and video in this run."""
    root = Path(run_dir).resolve()
    manifest = prepare_audio(root)
    matching = [item for item in manifest["materials"] if item.get("videoSha256") == video_sha]
    if not matching or matching[0]["status"] not in {"pending_agent", "agent_transcribed"}:
        raise TranscriptionError("video_not_pending_agent_transcription")
    source = Path(text_file).resolve()
    if not source.is_file() or source.stat().st_size > 1024 * 1024:
        raise TranscriptionError("transcript_text_file_missing_or_too_large")
    response = Path(response_file).resolve()
    if not response.is_file() or response.stat().st_size > 1024 * 1024:
        raise TranscriptionError("agent_response_file_missing_or_too_large")
    raw_bytes = response.read_bytes()
    try:
        raw_bytes.decode("utf-8")
        text = source.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise TranscriptionError("transcript_text_must_be_utf8") from exc
    if not text:
        raise TranscriptionError("transcript_text_empty")
    raw_path = root / "transcripts" / "raw" / f"{video_sha}.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=raw_path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(raw_bytes)
    os.replace(temporary, raw_path)
    transcript = {"schemaVersion": 1, "status": "available", "provider": "agent_imported_audio_text",
                  "reportedTool": "batch_understand_files", "toolCallVerifiedByScript": False,
                  "modelName": None, "videoSha256": video_sha,
                  "audioSha256": matching[0]["audioSha256"], "rawResponsePath": raw_path.relative_to(root).as_posix(),
                  "rawResponseSha256": _sha256(raw_path), "text": text, "timestampStatus": "unavailable"}
    _write_json(root / "transcripts" / "by-video" / f"{video_sha}.json", transcript)
    return prepare_audio(root)


def finalize_transcripts(run_dir: Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    manifest = prepare_audio(root)
    if manifest["counts"].get("pending_agent"):
        raise TranscriptionError(f"agent_transcripts_pending:{manifest['counts']['pending_agent']}")
    for item in manifest["materials"]:
        if item["status"] == "platform_script":
            continue
        material_id = item["materialId"]
        receipt = {"schemaVersion": 1, "materialId": material_id, **item}
        if item["status"] == "agent_transcribed":
            transcript = _read_json(root / item["transcriptPath"])
            receipt.update({"status": "available", "text": transcript["text"], "provider": transcript["provider"],
                            "audioSha256": transcript["audioSha256"], "timestampStatus": "unavailable",
                            "rawResponsePath": transcript["rawResponsePath"],
                            "rawResponseSha256": transcript["rawResponseSha256"]})
        _write_json(root / "transcripts" / "materials" / f"{material_id}.json", receipt)
        item["transcriptPath"] = f"transcripts/materials/{material_id}.json"
        item["transcriptSha256"] = _sha256(root / item["transcriptPath"])
    manifest["counts"] = {}
    for item in manifest["materials"]:
        item["status"] = "available" if item["status"] == "agent_transcribed" else item["status"]
        manifest["counts"][item["status"]] = manifest["counts"].get(item["status"], 0) + 1
    _write_json(root / "transcripts" / "manifest.json", manifest)
    return manifest
