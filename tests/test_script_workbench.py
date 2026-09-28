import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
import wave

from scripts.asr_worker import transcribe_chunks
from scripts.fact_packages import FactPackageError, build_fact_packages
from scripts.fact_catalog import build_fact_catalog
from scripts.script_report import write_script_report
from scripts.script_transcription import FasterWhisperProvider, TranscriptionError, _extract_audio, _ffmpeg_binary, transcribe_run


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeProvider:
    provider_name = "fixture-asr"
    model_version = "1"

    def __init__(self):
        self.calls = 0

    def transcribe(self, _wav):
        self.calls += 1
        return {"durationSec": 4, "language": "zh", "segments": [
            {"start": 0, "end": 2, "text": "真实口播"},
            {"start": 2, "end": 4, "text": "第二句"},
        ]}


class ScriptWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        details = {
            "1": {"material_id": "1", "script_text": "平台原文", "data_availability": {}},
            "2": {"material_id": "2", "script_text": "", "data_availability": {}},
            "3": {"material_id": "3", "script_text": "", "data_availability": {}},
            "4": {"material_id": "4", "script_text": "", "data_availability": {}},
        }
        artifacts = []
        for material_id, detail in details.items():
            path = self.root / "normalized" / "detail-records" / f"{material_id}.json"
            write_json(path, detail)
            artifacts.append({"path": path.relative_to(self.root).as_posix(), "sha256": digest(path)})
        ranking = [{"material_id": material_id, "rank": int(material_id), "title": f"素材{material_id}",
                    "metrics": {}, "people_tags": [], "material_tags": [], "data_availability": {}}
                   for material_id in details]
        write_json(self.root / "normalized" / "list-records.json", ranking)
        write_json(self.root / "run-summary.json", {"runId": "fixture", "workflow": {"stage": "ready_for_analysis"}, "firstBatchCsvGate": {"status": "passed"}})
        write_json(self.root / "facts" / "manifest.json", {"source_artifacts": artifacts})
        for material_id in ("2", "3"):
            path = self.root / "videos" / material_id / "source-video.mp4"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"same-video-content")

    def tearDown(self):
        self.temp.cleanup()

    def test_platform_bypass_asr_dedupe_and_missing_video(self):
        provider = FakeProvider()

        def fake_audio(_video, wav, _ffmpeg):
            wav.write_bytes(b"RIFF" + b"a" * 100)
            return "available"

        with mock.patch("scripts.script_transcription._extract_audio", side_effect=fake_audio) as extract:
            manifest = transcribe_run(self.root, provider=provider)
        self.assertEqual(manifest["counts"], {"platform_script": 1, "available": 2, "missing_video": 1})
        self.assertEqual(provider.calls, 1)
        self.assertEqual(extract.call_count, 1)
        self.assertFalse((self.root / "asr/materials/1.json").exists())
        self.assertEqual(json.loads((self.root / "normalized/detail-records/2.json").read_text())["script_text"], "")
        build_fact_packages(self.root)
        records = json.loads((self.root / "facts/script.json").read_text())["records"]
        by_id = {item["material_id"]: item for item in records}
        self.assertEqual(by_id["1"]["script_source"], "platform")
        self.assertEqual(by_id["2"]["script_text"], "真实口播第二句")
        self.assertEqual(by_id["2"]["platform_script_text"], "")
        self.assertEqual(by_id["4"]["script_source"], "unavailable")
        catalog = build_fact_catalog(self.root).to_dict()["packages"]["script"]
        self.assertIn("script_source", catalog["fields"])
        self.assertIn("asr_transcript.text", catalog["fields"])
        receipt = write_script_report(self.root)
        self.assertEqual(receipt["counts"]["asr"], 2)
        self.assertIn("真实口播第二句", Path(receipt["reportPath"]).read_text())

    def test_rejects_modified_detail_and_paused_gate(self):
        path = self.root / "normalized/detail-records/2.json"
        path.write_text('{"material_id":"2","script_text":"tampered"}', encoding="utf-8")
        with self.assertRaisesRegex(TranscriptionError, "detail_source_hash_mismatch"):
            transcribe_run(self.root, provider=FakeProvider())
        write_json(self.root / "run-summary.json", {"rpa_paused": True})
        with self.assertRaisesRegex(TranscriptionError, "rpa_gate_not_ready"):
            transcribe_run(self.root, provider=FakeProvider())

    def test_failed_provider_can_retry_without_rpa(self):
        provider = FakeProvider()

        def fake_audio(_video, wav, _ffmpeg):
            wav.write_bytes(b"RIFF" + b"a" * 100)
            return "available"

        with mock.patch("scripts.script_transcription._extract_audio", side_effect=fake_audio), mock.patch.object(provider, "transcribe", side_effect=RuntimeError("timeout")):
            manifest = transcribe_run(self.root, provider=provider)
        self.assertEqual(manifest["counts"]["transcription_failed"], 2)
        self.assertEqual(list((self.root / "asr/by-video").glob("*.json")), [])
        with mock.patch("scripts.script_transcription._extract_audio", side_effect=fake_audio):
            manifest = transcribe_run(self.root, provider=provider)
        self.assertEqual(manifest["counts"]["available"], 2)
        self.assertEqual(provider.calls, 1)

    def test_changed_decode_hints_invalidate_success_cache(self):
        def fake_audio(_video, wav, _ffmpeg):
            wav.write_bytes(b"RIFF" + b"a" * 100)
            return "available"

        first, second = FakeProvider(), FakeProvider()
        first.cache_key, second.cache_key = "terms-a", "terms-b"
        with mock.patch("scripts.script_transcription._extract_audio", side_effect=fake_audio):
            transcribe_run(self.root, provider=first)
            transcribe_run(self.root, provider=second)
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)

    def test_attempt_evidence_is_hash_bound_to_facts(self):
        video_hash = digest(self.root / "videos/2/source-video.mp4")
        attempt = self.root / "asr" / "attempts" / video_hash / "attempt.json"
        write_json(attempt, {"status": "killed", "exitCode": -9})

        def fake_audio(_video, wav, _ffmpeg):
            wav.write_bytes(b"RIFF" + b"a" * 100)
            return "available"

        with mock.patch("scripts.script_transcription._extract_audio", side_effect=fake_audio):
            transcribe_run(self.root, provider=FakeProvider())
        build_fact_packages(self.root)
        attempt.write_text('{"status":"changed"}', encoding="utf-8")
        with self.assertRaisesRegex(FactPackageError, "ASR attempt hash mismatch"):
            build_fact_packages(self.root)

    def test_real_ffmpeg_distinguishes_no_audio_track(self):
        video = self.root / "silent.mp4"
        command = [_ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                   "-i", "color=c=blue:s=64x64:d=1", "-an", "-y", str(video)]
        subprocess.run(command, check=True, capture_output=True)
        self.assertEqual(_extract_audio(video, self.root / "silent.wav"), "no_audio_track")

    def test_chunked_asr_offsets_timestamps_and_bounds_audio(self):
        wav = self.root / "speech.wav"
        with wave.open(str(wav), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 16000 * 21)

        class Model:
            def __init__(self):
                self.calls = []

            def transcribe(self, path, **kwargs):
                with wave.open(path, "rb") as chunk:
                    frames = chunk.getnframes()
                self.calls.append((frames, kwargs))
                return iter([SimpleNamespace(start=0, end=1, text="口播")]), SimpleNamespace(language="zh")

        model = Model()
        checkpoint = self.root / "checkpoint.json"
        result = transcribe_chunks(wav, model, profile="memory_safe", hotwords="燕之屋",
                                   checkpoint_path=checkpoint)
        self.assertEqual([part["start"] for part in result["segments"]], [0, 20])
        self.assertEqual([call[0] for call in model.calls], [20 * 16000, 16000])
        self.assertEqual(model.calls[0][1]["beam_size"], 1)
        self.assertEqual(model.calls[0][1]["hotwords"], "燕之屋")
        self.assertEqual(result["decodeProfile"]["name"], "memory_safe")
        self.assertEqual(json.loads(checkpoint.read_text())["stage"], "chunk_completed")

    def test_worker_memory_kill_retries_safe_profile(self):
        model_dir = self.root / "model"
        model_dir.mkdir()
        (model_dir / "model.bin").write_bytes(b"model")
        (model_dir / "config.json").write_text("{}")
        provider = FasterWhisperProvider(model_dir, hotwords="燕之屋")
        profiles = []
        attempt_dir = self.root / "attempts"

        def run_worker(_command, *, input, **_kwargs):
            request = json.loads(input)
            profiles.append(request["profile"])
            write_json(Path(request["checkpointPath"]), {"stage": "chunk_started", "chunkIndex": 1})
            if request["profile"] == "quality":
                return SimpleNamespace(returncode=-9, stderr="")
            output = Path(_command[-1])
            write_json(output, {"durationSec": 1, "language": "zh", "segments": [],
                                "decodeProfile": {"name": "memory_safe"}})
            return SimpleNamespace(returncode=0, stderr="")

        snapshots = [{"oomKillCount": 3}, {"oomKillCount": 4}, {"oomKillCount": 4}, {"oomKillCount": 4}]
        with mock.patch.object(provider, "preflight"), \
                mock.patch("scripts.script_transcription.subprocess.run", side_effect=run_worker), \
                mock.patch("scripts.script_transcription._memory_snapshot", side_effect=snapshots):
            result = provider.transcribe(self.root / "audio.wav", attempt_dir=attempt_dir,
                                         material_id="2", video_sha="abc")
        self.assertEqual(profiles, ["quality", "memory_safe"])
        self.assertEqual(result["decodeProfile"]["name"], "memory_safe")
        attempts = [json.loads(path.read_text()) for path in attempt_dir.glob("*.json")]
        self.assertEqual({item["status"] for item in attempts}, {"killed", "worker_completed"})
        killed = next(item for item in attempts if item["status"] == "killed")
        self.assertEqual((killed["exitCode"], killed["oomKillDelta"]), (-9, 1))
        self.assertEqual(killed["workerCheckpoint"], {"stage": "chunk_started", "chunkIndex": 1})

    def test_quality_only_retry_does_not_fall_back_to_safe(self):
        model_dir = self.root / "model"
        model_dir.mkdir()
        (model_dir / "model.bin").write_bytes(b"model")
        (model_dir / "config.json").write_text("{}")
        provider = FasterWhisperProvider(model_dir)
        profiles = []

        def killed(_command, *, input, **_kwargs):
            profiles.append(json.loads(input)["profile"])
            return SimpleNamespace(returncode=-9, stderr="")

        with mock.patch.object(provider, "preflight"), \
                mock.patch("scripts.script_transcription.subprocess.run", side_effect=killed):
            with self.assertRaisesRegex(TranscriptionError, "asr_quality_worker_killed"):
                provider.transcribe(self.root / "audio.wav", quality_only=True,
                                    attempt_dir=self.root / "attempts")
        self.assertEqual(profiles, ["quality"])
        self.assertEqual(len(list((self.root / "attempts").glob("*.json"))), 1)

    def test_quality_retry_preserves_safe_cache_on_failure_then_upgrades(self):
        model_dir = self.root / "model"
        model_dir.mkdir()
        (model_dir / "model.bin").write_bytes(b"model")
        (model_dir / "config.json").write_text("{}")
        provider = FasterWhisperProvider(model_dir)
        video_hash = digest(self.root / "videos/2/source-video.mp4")
        cached = self.root / "asr" / "by-video" / f"{video_hash}.json"
        original = {"status": "available", "videoSha256": video_hash,
                    "provider": provider.provider_name, "modelVersion": provider.model_version,
                    "cacheKey": provider.cache_key, "decodeProfile": {"name": "memory_safe"},
                    "durationSec": 4, "language": "zh", "segments": [{"start": 0, "end": 4, "text": "安全档原文"}],
                    "text": "安全档原文"}
        write_json(cached, original)
        original_bytes = cached.read_bytes()

        def fake_audio(_video, wav, _ffmpeg):
            wav.write_bytes(b"RIFF" + b"a" * 100)
            return "available"

        with mock.patch("scripts.script_transcription._extract_audio", side_effect=fake_audio), \
                mock.patch.object(provider, "transcribe", side_effect=TranscriptionError("quality_killed")) as transcribe:
            manifest = transcribe_run(self.root, provider=provider, retry_low_memory=True)
        self.assertEqual(transcribe.call_count, 1)
        self.assertTrue(transcribe.call_args.kwargs["quality_only"])
        self.assertEqual(manifest["qualityRetries"], {"attemptedVideos": 1, "upgradedVideos": 0, "failedVideos": 1})
        self.assertEqual(manifest["counts"]["available"], 2)
        self.assertEqual(cached.read_bytes(), original_bytes)
        self.assertEqual(json.loads((self.root / "asr/materials/2.json").read_text())["text"], "安全档原文")
        build_fact_packages(self.root)
        self.assertEqual(write_script_report(self.root)["counts"]["asr"], 2)

        quality = {"durationSec": 4, "language": "zh", "decodeProfile": {"name": "quality"},
                   "segments": [{"start": 0, "end": 4, "text": "高质量原文"}]}
        with mock.patch("scripts.script_transcription._extract_audio", side_effect=fake_audio), \
                mock.patch.object(provider, "transcribe", return_value=quality) as transcribe:
            manifest = transcribe_run(self.root, provider=provider, retry_low_memory=True)
        self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(manifest["qualityRetries"], {"attemptedVideos": 1, "upgradedVideos": 1, "failedVideos": 0})
        self.assertEqual(json.loads(cached.read_text())["text"], "高质量原文")
        with mock.patch.object(provider, "transcribe") as transcribe:
            manifest = transcribe_run(self.root, provider=provider, retry_low_memory=True)
        transcribe.assert_not_called()
        self.assertEqual(manifest["qualityRetries"]["attemptedVideos"], 0)
        provider.hotwords = "different terms"
        with self.assertRaisesRegex(TranscriptionError, "quality_retry_model_or_hints_mismatch"):
            transcribe_run(self.root, provider=provider, retry_low_memory=True)

    def test_missing_model_preflight_rejects_before_acquisition(self):
        with self.assertRaisesRegex(TranscriptionError, "model_directory_missing_or_incomplete"):
            FasterWhisperProvider(self.root / "missing").preflight()


if __name__ == "__main__":
    unittest.main()
