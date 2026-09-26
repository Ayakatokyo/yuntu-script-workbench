import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.fact_packages import FactPackageError, build_fact_packages, extract_runtime_brands


class FactPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name)
        (self.run_dir / "normalized/detail-records").mkdir(parents=True)
        (self.run_dir / "raw-csv/details").mkdir(parents=True)
        (self.run_dir / "raw-csv/list.csv").write_text("placeholder", encoding="utf-8")
        self.list_path = self.run_dir / "normalized/list-records.json"
        self.list_path.write_text(json.dumps([
            {
                "material_id": "202", "item_id": "202", "rank": 99,
                "title": "Brand B second", "video_duration": 8, "publish_time": "20260820",
                "account_id": "acct", "material_uri": "uri-202", "business_date": "20260828",
                "list_context": {"industry": "食品饮料", "brands": ["Brand A", "Brand B/品牌B"]}, "selection_group": {"id": "default"},
                "metrics": {"show_cnt": 20}, "people_tags": ["女", "未知人群"],
                "material_tags": ["单人口播"], "data_availability": {
                    "metrics": {"available": True, "missing_reason": ""},
                    "people_tags": {"available": True, "missing_reason": ""},
                    "material_tags": {"available": True, "missing_reason": ""},
                }, "source_paths": {"list": "raw-csv/list.csv"},
            },
            {
                "material_id": "101", "item_id": "101", "rank": 1,
                "title": "Brand A first", "video_duration": 10, "publish_time": "20260819",
                "account_id": "acct", "material_uri": "uri-101", "business_date": "20260828",
                "list_context": {"industry": "食品饮料", "brands": ["Brand A", "Brand B/品牌B"]}, "selection_group": {"id": "default"},
                "metrics": {"ctr": 0.2}, "people_tags": ["精致妈妈", "36-40", "女"],
                "material_tags": ["多人情景剧"], "data_availability": {
                    "metrics": {"available": True, "missing_reason": ""},
                    "people_tags": {"available": True, "missing_reason": ""},
                    "material_tags": {"available": True, "missing_reason": ""},
                }, "source_paths": {"list": "raw-csv/list.csv"},
            },
        ], ensure_ascii=False), encoding="utf-8")
        detail = {
            "material_id": "101", "date_range": {"type": "LAST_7_DAYS"},
            "audience_portrait": {"性别": ["女"]},
            "script_text": "", "content_formula": None, "script_segments": [],
            "creative_breakdown": {"raw": {"hook": "痛点"}, "tags": ["痛点"]},
            "trend_series": [{"trend_name": "点击指数", "series": [{"second": 0, "value": 1}]}],
            "high_points": [], "loss_points": [],
            "data_availability": {
                "trends": {"available": True, "missing_reason": ""},
                "script_text": {"available": False, "missing_reason": "source_empty"},
                "content_formula": {"available": False, "missing_reason": "source_empty"},
                "script_segments": {"available": False, "missing_reason": "source_empty"},
            }, "source_paths": {"detail": "raw-csv/details/101.csv"},
        }
        (self.run_dir / "normalized/detail-records/101.json").write_text(
            json.dumps(detail, ensure_ascii=False), encoding="utf-8"
        )
        (self.run_dir / "raw-csv/details/101.csv").write_text("placeholder", encoding="utf-8")
        (self.run_dir / "highlights-manifest.json").write_text(json.dumps({
            "standard": "cover_plus_click_top5",
            "materials": [{"material_id": "101", "frames": [{
                "second": 0, "is_cover": True, "frame_type": "cover",
                "reasons": [{"type": "cover"}], "available": True,
                "frame_ref": "highlight:101:cover:0",
                "relative_path": "highlights/101/01-000s.jpg",
            }]}],
        }, ensure_ascii=False), encoding="utf-8")
        frame = self.run_dir / "highlights/101/01-000s.jpg"
        frame.parent.mkdir(parents=True)
        frame.write_bytes(b"jpeg")

    def tearDown(self):
        self.temp.cleanup()

    def test_build_fact_packages_writes_four_packages_and_classifies_source_facts(self):
        result = build_fact_packages(self.run_dir)
        self.assertEqual(result["coverage"], {
            "ranking_records": 2, "detail_records": 1, "script_records": 1,
            "highlight_materials": 1, "available_highlight_frames": 1,
        })
        manifest = json.loads((self.run_dir / "facts/manifest.json").read_text())
        package_hash = manifest["packages"]["material_ranking"]["sha256"]
        self.assertEqual(
            package_hash,
            hashlib.sha256((self.run_dir / "facts/material-ranking.json").read_bytes()).hexdigest(),
        )
        ranking = json.loads((self.run_dir / "facts/material-ranking.json").read_text())
        self.assertEqual([row["rank"] for row in ranking["records"]], [1, 99])
        self.assertEqual(result["brand_classification"]["candidate_brands"], ["Brand A", "Brand B/品牌B"])
        self.assertEqual(result["brand_classification"]["rule_version"], "runtime-title-rule-v1")
        self.assertEqual(ranking["records"][0]["brand_classification"]["matchedBrands"], ["Brand A"])
        self.assertEqual(ranking["records"][1]["brand_classification"]["matchedBrands"], ["Brand B/品牌B"])
        self.assertEqual(ranking["records"][0]["brand_rule_version"], "runtime-title-rule-v1")
        audience = json.loads((self.run_dir / "facts/audience.json").read_text())
        self.assertEqual(
            [(x["tag"], x["dimension"]) for x in audience["records"][0]["observations"]],
            [("精致妈妈", "crowd_group"), ("36-40", "age"), ("女", "gender")],
        )
        unknown = audience["records"][1]["observations"][1]
        self.assertEqual((unknown["tag"], unknown["dimension"]), ("未知人群", "unclassified"))
        self.assertEqual(
            audience["records"][0]["brand_classification"],
            ranking["records"][0]["brand_classification"],
        )
        script = json.loads((self.run_dir / "facts/script.json").read_text())
        self.assertEqual(script["records"][0]["audience_portrait"], {"性别": ["女"]})
        self.assertEqual(script["records"][0]["creative_breakdown"]["tags"], ["痛点"])
        self.assertFalse(script["records"][0]["data_availability"]["script_text"]["available"])
        self.assertEqual(
            script["records"][0]["brand_classification"],
            ranking["records"][0]["brand_classification"],
        )
        highlights = json.loads((self.run_dir / "facts/highlight-frames.json").read_text())
        self.assertEqual(highlights["records"][0]["frames"][0]["relative_path"], "highlights/101/01-000s.jpg")
        self.assertEqual(
            highlights["records"][0]["brand_classification"],
            ranking["records"][0]["brand_classification"],
        )

    def test_complete_fact_build_stops_at_ready_for_analysis(self):
        result = build_fact_packages(self.run_dir)

        summary = json.loads((self.run_dir / "run-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["workflow"]["stage"], "ready_for_analysis")
        self.assertEqual(result["run_id"], summary["runId"])

    def test_partial_collection_cannot_be_promoted_to_ready(self):
        (self.run_dir / "run-summary.json").write_text(
            json.dumps({"runId": "run-partial", "status": "partial"}), encoding="utf-8"
        )

        result = build_fact_packages(self.run_dir)

        summary = json.loads((self.run_dir / "run-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["workflow"]["stage"], "partial")
        self.assertNotEqual(summary["workflow"]["stage"], "ready_for_analysis")
        self.assertEqual(result["run_id"], "run-partial")

    def test_fact_build_error_preserves_existing_recoverable_stage(self):
        self.list_path.write_text("{}", encoding="utf-8")

        for stage in ("partial", "failed"):
            with self.subTest(stage=stage):
                (self.run_dir / "run-summary.json").write_text(
                    json.dumps({"runId": f"run-{stage}", "status": stage}), encoding="utf-8"
                )

                with self.assertRaises(FactPackageError):
                    build_fact_packages(self.run_dir)

                summary = json.loads((self.run_dir / "run-summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["status"], stage)
                self.assertEqual(summary["workflow"]["stage"], stage)

    def test_extract_runtime_brands_deduplicates_in_first_seen_order(self):
        records = [
            {"list_context": {"brands": [" 品牌A ", "品牌B/Brand B"]}},
            {"list_context": {"brands": ["品牌B/Brand B", "品牌C"]}},
            {"list_context": {"industry": "食品饮料"}},
        ]

        self.assertEqual(extract_runtime_brands(records), ["品牌A", "品牌B/Brand B", "品牌C"])

    def test_missing_runtime_brand_scope_writes_unknown_without_default_fallback(self):
        records = json.loads(self.list_path.read_text(encoding="utf-8"))
        for record in records:
            record["list_context"].pop("brands", None)
        self.list_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

        result = build_fact_packages(self.run_dir)
        self.assertEqual(result["brand_classification"]["candidate_brands"], [])
        ranking = json.loads((self.run_dir / "facts/material-ranking.json").read_text())
        self.assertTrue(all(
            row["brand_classification"]["value"] == "未知品牌"
            for row in ranking["records"]
        ))

    def test_builder_rejects_missing_or_invalid_inputs_without_partial_output(self):
        self.list_path.unlink()
        with self.assertRaises(FactPackageError):
            build_fact_packages(self.run_dir)
        self.assertFalse((self.run_dir / "facts").exists())

        self.list_path.write_text("[]", encoding="utf-8")
        (self.run_dir / "highlights-manifest.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(FactPackageError):
            build_fact_packages(self.run_dir)
        self.assertFalse((self.run_dir / "facts").exists())

    def test_builder_rejects_detail_filename_and_id_conflict(self):
        (self.run_dir / "normalized/detail-records/202.json").write_text(
            json.dumps({"material_id": "999"}), encoding="utf-8"
        )
        with self.assertRaisesRegex(FactPackageError, "conflicts with filename"):
            build_fact_packages(self.run_dir)
        self.assertFalse((self.run_dir / "facts").exists())

    def test_builder_does_not_modify_input_files(self):
        tracked = [self.list_path, self.run_dir / "normalized/detail-records/101.json", self.run_dir / "highlights-manifest.json"]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked}
        build_fact_packages(self.run_dir)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked})


if __name__ == "__main__":
    unittest.main()
