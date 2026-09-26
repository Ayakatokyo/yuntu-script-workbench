import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.fact_packages import build_fact_packages
from scripts.fact_catalog import build_fact_catalog
from scripts.script_report import write_script_report
from scripts.script_transcription import TranscriptionError, _extract_audio, _ffmpeg_binary, transcribe_run


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

    def test_real_ffmpeg_distinguishes_no_audio_track(self):
        video = self.root / "silent.mp4"
        command = [_ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                   "-i", "color=c=blue:s=64x64:d=1", "-an", "-y", str(video)]
        subprocess.run(command, check=True, capture_output=True)
        self.assertEqual(_extract_audio(video, self.root / "silent.wav"), "no_audio_track")


if __name__ == "__main__":
    unittest.main()
