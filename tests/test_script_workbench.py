"""Agent transcript handoff, provenance, and first-stage report checks."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.fact_packages import FactPackageError, build_fact_packages
from scripts.fact_catalog import build_fact_catalog
from scripts.script_report import write_script_report
from scripts.script_transcription import TranscriptionError, prepare_audio, import_transcript, mark_unavailable, finalize_transcripts


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        ranking = [{"material_id": mid, "rank": int(mid), "title": f"素材{mid}",
                    "metrics": {}, "people_tags": [], "material_tags": [], "data_availability": {}}
                   for mid in details]
        write_json(self.root / "normalized" / "list-records.json", ranking)
        write_json(self.root / "run-summary.json", {"runId": "fixture", "workflow": {"stage": "ready_for_analysis"},
                                                 "firstBatchCsvGate": {"status": "passed"}})
        write_json(self.root / "facts" / "manifest.json", {"source_artifacts": artifacts})
        for material_id in ("2", "3"):
            path = self.root / "videos" / material_id / "source-video.mp4"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"same-video-content")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def fake_audio(_video, wav, _ffmpeg):
        wav.write_bytes(b"RIFF" + b"a" * 100)
        return "available"

    def prepare(self):
        with mock.patch("scripts.script_transcription._extract_audio", side_effect=self.fake_audio) as extract:
            manifest = prepare_audio(self.root)
        return manifest, extract

    def test_prepare_deduplicates_audio_and_requires_agent_before_report(self):
        manifest, extract = self.prepare()
        self.assertEqual(manifest["counts"], {"platform_script": 1, "pending_agent": 2, "missing_video": 1})
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(Path(extract.call_args.args[1]).parent.parent, (self.root / "audio" / "by-video").resolve())
        self.assertEqual(len(list((self.root / "audio/by-video").glob("*.wav"))), 1)
        with self.assertRaisesRegex(TranscriptionError, "agent_transcripts_pending"):
            finalize_transcripts(self.root)
        self.assertFalse((self.root / "script-report/index.html").exists())

    def test_import_text_keeps_platform_priority_and_verifies_hash_chain(self):
        manifest, _ = self.prepare()
        item = next(item for item in manifest["materials"] if item.get("status") == "pending_agent")
        response = self.root / "agent-response.txt"
        response.write_text("燕之屋鲜炖燕窝，今天有活动。\n", encoding="utf-8")
        imported = import_transcript(self.root, video_sha=item["videoSha256"],
                                     text_file=response, response_file=response)
        self.assertEqual(imported["counts"]["agent_transcribed"], 2)
        finalized = finalize_transcripts(self.root)
        self.assertEqual(finalized["counts"]["available"], 2)
        build_fact_packages(self.root)
        records = json.loads((self.root / "facts/script.json").read_text())["records"]
        by_id = {record["material_id"]: record for record in records}
        self.assertEqual(by_id["1"]["script_source"], "platform")
        self.assertEqual(by_id["2"]["script_source"], "agent_audio")
        self.assertEqual(by_id["2"]["script_text"], "燕之屋鲜炖燕窝，今天有活动。")
        self.assertEqual(by_id["2"]["audio_transcript"]["timestampStatus"], "unavailable")
        self.assertEqual(by_id["4"]["script_source"], "unavailable")
        catalog = build_fact_catalog(self.root).to_dict()["packages"]["script"]
        self.assertIn("audio_transcript.text", catalog["fields"])
        receipt = write_script_report(self.root)
        self.assertEqual(receipt["counts"]["agent_audio"], 2)
        self.assertIn("燕之屋鲜炖燕窝", Path(receipt["reportPath"]).read_text())
        report = json.loads(Path(receipt["dataPath"]).read_text(encoding="utf-8"))
        self.assertEqual(report["scriptFormulaOverview"]["coverage"]["scriptMaterialCount"], 4)
        self.assertEqual(report["brandOverview"]["status"], "available")
        self.assertEqual([(tab["id"], tab["count"]) for tab in report["brandOverview"]["tabs"]],
                         [("all", 4), ("unknown", 4)])
        self.assertEqual(report["brandOverview"]["views"]["unknown"]["scriptFormulaOverview"]["coverage"],
                         {"scriptMaterialCount": 4, "formulaMaterialCount": 0})
        self.assertEqual(len(report["rows"][0]["core"]["defaultFieldIds"]), 7)
        self.assertEqual(report["rows"][1]["formula"]["status"], "unavailable")
        raw = self.root / "transcripts/raw" / f"{item['videoSha256']}.txt"
        raw.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(FactPackageError, "source hash mismatch"):
            build_fact_packages(self.root)

    def test_rejects_modified_detail_paused_gate_and_empty_transcript(self):
        detail = self.root / "normalized/detail-records/2.json"
        detail.write_text('{"material_id":"2","script_text":"tampered"}', encoding="utf-8")
        with self.assertRaisesRegex(TranscriptionError, "detail_source_hash_mismatch"):
            prepare_audio(self.root)
        write_json(self.root / "run-summary.json", {"rpa_paused": True})
        with self.assertRaisesRegex(TranscriptionError, "rpa_gate_not_ready"):
            prepare_audio(self.root)
        write_json(detail, {"material_id": "2", "script_text": "", "data_availability": {}})
        write_json(self.root / "run-summary.json", {"runId": "fixture", "workflow": {"stage": "ready_for_analysis"}})
        manifest, _ = self.prepare()
        response = self.root / "empty.txt"
        response.write_text(" \n", encoding="utf-8")
        with self.assertRaisesRegex(TranscriptionError, "transcript_text_empty"):
            import_transcript(self.root, video_sha=manifest["materials"][1]["videoSha256"],
                              text_file=response, response_file=response)

    def test_reviewed_empty_result_is_a_provenance_checked_gap(self):
        manifest, _ = self.prepare()
        video_sha = manifest["materials"][1]["videoSha256"]
        response = self.root / "empty-response.txt"
        response.write_text("", encoding="utf-8")
        marked = mark_unavailable(self.root, video_sha=video_sha, response_file=response,
                                  review_note="音频理解返回空文本，复核后仍无可用转写")
        self.assertEqual(marked["counts"]["agent_unavailable"], 2)
        finalized = finalize_transcripts(self.root)
        self.assertEqual(finalized["counts"]["agent_unavailable"], 2)
        build_fact_packages(self.root)
        records = json.loads((self.root / "facts/script.json").read_text())["records"]
        by_id = {record["material_id"]: record for record in records}
        self.assertEqual(by_id["2"]["script_source"], "unavailable")
        self.assertEqual(by_id["2"]["audio_transcript"]["reason"], "recognition_empty_after_review")
        self.assertIn("复核后", by_id["2"]["audio_transcript"]["reviewNote"])
        self.assertEqual(by_id["2"]["script_text"], "")
        write_json(self.root / "run-summary.json", {
            "runId": "fixture", "workflow": {"stage": "ready_for_analysis"},
            "collectionContract": {"listParams": {"industry": "食品饮料",
                                                  "brand_scope_type": "SPECIFIED_BRANDS",
                                                  "brands": ["小仙炖", "燕之屋", "五个女博士"]}}
        })
        receipt = write_script_report(self.root)
        self.assertEqual(receipt["counts"]["unavailable"], 3)
        selection = json.loads(Path(receipt["dataPath"]).read_text(encoding="utf-8"))["selection"]
        self.assertEqual(selection["brands"], ["小仙炖", "燕之屋", "五个女博士"])
        self.assertEqual(selection["titleBrandUnmatchedCount"], 4)
        self.assertIn("不据此断定音频无人声", Path(receipt["reportPath"]).read_text(encoding="utf-8"))
        (self.root / "transcripts" / "raw" / f"{video_sha}.txt").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(FactPackageError, "transcript source hash mismatch"):
            build_fact_packages(self.root)

    def test_reviewed_gap_can_be_replaced_by_real_transcript(self):
        manifest, _ = self.prepare()
        video_sha = manifest["materials"][1]["videoSha256"]
        response = self.root / "agent-response.txt"
        response.write_text("", encoding="utf-8")
        mark_unavailable(self.root, video_sha=video_sha, response_file=response,
                         review_note="音频理解返回空文本，复核后仍无可用转写")
        response.write_text("实际口播", encoding="utf-8")
        imported = import_transcript(self.root, video_sha=video_sha, text_file=response, response_file=response)
        self.assertEqual(imported["counts"]["agent_transcribed"], 2)


if __name__ == "__main__":
    unittest.main()
