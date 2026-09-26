import importlib.util
import csv
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
import json
import subprocess
import sys
import tempfile
from unittest.mock import patch
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
BATCH_PATH = SCRIPTS_DIR / "yuntu_batch.py"


def load_batch():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("yuntu_batch", BATCH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.FileDownloader = FixtureCsvDownloader
    return module


class FixtureCsvDownloader:
    """Make gateway fixtures exercise the actual CSV parser."""
    def download_file(self, file_url, target_path):
        records = json.loads(file_url)
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        headers = list(dict.fromkeys(key for record in records for key in record))
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()
            writer.writerows({key: json.dumps(value) if isinstance(value, (dict, list)) else value
                             for key, value in record.items()} for record in records)
        return path


def fixture_csv_response(records):
    return {"status": "completed", "files": [{"fileUrl": json.dumps(records)}]}


class DetailInvoker:
    def __init__(self, detail_result):
        self.detail_result = detail_result
        self.detail_calls = []

    def invoke_detail(self, shop_id, detail_params):
        self.detail_calls.append((shop_id, detail_params))
        return self.detail_result


class BatchInvoker:
    def __init__(self, list_records, failed_material_ids=()):
        self.list_records = list_records
        self.failed_material_ids = set(failed_material_ids)
        self.detail_calls = []

    def invoke_list(self, _shop_id, _list_params):
        return {"success": True, "data": self.list_records}

    def invoke_detail(self, _shop_id, detail_params):
        self.detail_calls.append(detail_params)
        material_id = detail_params["material_id"]
        if material_id in self.failed_material_ids:
            raise RuntimeError(f"RPA task failed: detail rejected for {material_id}")
        return fixture_csv_response([{
                "materialId": material_id,
                "dateRangeType": detail_params["date_range_type"],
                "trends": [],
                **{
                    field: detail_params[field]
                    for field in (
                        "selectionGroupId",
                        "brandId",
                        "brandName",
                        "touchpointCode",
                        "touchpointName",
                    )
                    if field in detail_params
                },
            }])


class AsyncBatchInvoker(BatchInvoker):
    supports_detail_task_recovery = True

    def __init__(self, list_records, fail_waits=0):
        super().__init__(list_records)
        self.submit_calls = []
        self.wait_calls = []
        self.fail_waits = fail_waits

    def submit_detail(self, shop_id, detail_params):
        self.submit_calls.append((shop_id, dict(detail_params)))
        return f"tg-{detail_params['material_id']}"

    def wait_detail(self, task_group_id):
        self.wait_calls.append(task_group_id)
        if self.fail_waits:
            self.fail_waits -= 1
            raise RuntimeError("temporary poll transport timeout")
        material_id = task_group_id.removeprefix("tg-")
        return fixture_csv_response([{"materialId": material_id, "dateType": "LAST_7_DAYS", "trends": []}])


def list_record(material_id, group_id):
    return {
        "material_id": material_id,
        "selectionGroupId": group_id,
        "brandId": f"brand-{group_id}",
        "brandName": f"Brand {group_id}",
    }


def ranked_list_record(material_id, title, show_cnt, interact_rate):
    return {
        "material_id": material_id,
        "title": title,
        "show_cnt": show_cnt,
        "interact_rate": interact_rate,
    }


def highlight_spec():
    return {
        "include_cover": False,
        "metrics": {
            "点赞指数": {"strategy": "local_peaks", "limit": 1},
            "流失指数": {"strategy": "local_peaks", "limit": 1},
            "点击指数": {"strategy": "local_peaks", "limit": 1},
            "互动指数": {"strategy": "local_peaks", "limit": 1},
            "评论指数": {"strategy": "local_peaks", "limit": 1},
        },
    }


class YuntuBatchTests(unittest.TestCase):
    def test_async_detail_submission_is_accounted_and_resume_polls_without_resubmit(self):
        batch = load_batch()
        batch.generate_highlights_for_detail = lambda *_args: []
        with tempfile.TemporaryDirectory() as directory:
            invoker = AsyncBatchInvoker([
                {"material_id": "101", "title": "素材 A"},
            ], fail_waits=1)
            first_status = batch.fetch_batch(
                shop_id="shop-42",
                list_params={},
                detail_date_params={"date_type": "LAST_7_DAYS"},
                target_top_n=1,
                candidate_top_n=1,
                output_dir=directory,
                invoker=invoker,
            )
            first_manifest = json.loads(
                (Path(directory) / "batch-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first_status, 1)
            self.assertEqual(len(invoker.submit_calls), 1)
            attempt = first_manifest["taskRuns"]["detail:101"]["attempts"][-1]
            self.assertEqual(attempt["task_group_id"], "tg-101")
            self.assertEqual(attempt["error_class"], "poll_timeout")
            self.assertEqual(first_manifest["taskRuns"]["detail:101"]["task_group_id"], "tg-101")
            self.assertEqual(first_manifest["taskRuns"]["detail:101"]["status"], "unresolved")
            self.assertEqual(first_manifest["unresolvedMaterials"][0]["reason"], "poll_unconfirmed")

            second_status = batch.fetch_batch(
                shop_id="shop-42",
                list_params={},
                detail_date_params={"date_type": "LAST_7_DAYS"},
                target_top_n=1,
                candidate_top_n=1,
                output_dir=directory,
                invoker=invoker,
            )
            manifest = json.loads(
                (Path(directory) / "batch-manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(second_status, 0)
        self.assertEqual(len(invoker.submit_calls), 1)
        self.assertEqual(invoker.wait_calls, ["tg-101", "tg-101"])
        self.assertEqual(manifest["unresolvedMaterials"], [])
        self.assertEqual(manifest["selectedMaterials"][0]["material_id"], "101")
        self.assertEqual(
            manifest["taskRuns"]["detail:101"]["functionCode"],
            "rpa.conn.juliang.yt.industry.content.rankings.details",
        )
        self.assertEqual(
            manifest["taskRuns"]["detail:101"]["attempts"][-1]["taskGroupId"],
            "tg-101",
        )
        self.assertEqual(
            manifest["taskRuns"]["detail:101"]["attempts"][-1]["artifact_status"],
            "normalized",
        )

    def test_detail_wave_is_limited_to_three_items(self):
        batch = load_batch()
        candidates = [{"material_id": str(index)} for index in range(4)]

        wave, next_index = batch._next_detail_wave(
            candidates,
            0,
            selected_ids=set(),
            rejected_ids=set(),
            unresolved_ids=set(),
        )

        self.assertEqual(batch.DETAIL_WAVE_CONCURRENCY, 3)
        self.assertEqual([item["material_id"] for item in wave], ["0", "1", "2"])
        self.assertEqual(next_index, 3)

    def test_detail_executor_uses_detail_wave_concurrency_setting(self):
        batch = load_batch()
        configured_limit = 4
        batch.DETAIL_WAVE_CONCURRENCY = configured_limit
        recorded_workers = []

        class RecordingExecutor:
            def __init__(self, max_workers):
                recorded_workers.append(max_workers)
                self._executor = RealThreadPoolExecutor(max_workers=max_workers)

            def __enter__(self):
                self._executor.__enter__()
                return self

            def __exit__(self, *args):
                return self._executor.__exit__(*args)

            def submit(self, *args, **kwargs):
                return self._executor.submit(*args, **kwargs)

        batch.generate_highlights_for_detail = lambda *_args: []
        invoker = BatchInvoker([
            list_record("101", "A"),
            list_record("202", "B"),
            list_record("303", "A"),
            list_record("404", "B"),
        ])

        with patch.object(batch, "ThreadPoolExecutor", RecordingExecutor):
            with tempfile.TemporaryDirectory() as directory:
                status = batch.fetch_batch(
                    shop_id="shop-42",
                    list_params={},
                    detail_date_params={"date_range_type": "LAST_7_DAYS"},
                    target_top_n=4,
                    candidate_top_n=1000,
                    output_dir=Path(directory),
                    invoker=invoker,
                    highlight_spec=highlight_spec(),
                )

        self.assertEqual(status, 0)
        self.assertEqual(recorded_workers, [configured_limit, batch.HIGHLIGHT_CONCURRENCY])

    def _write_collection_state(self, root, status="success"):
        batch = load_batch()
        contract = batch.run_state.collection_contract(
            shop_id="shop-42",
            list_params={},
            detail_date_params={"date_range_type": "LAST_7_DAYS"},
            target_top_n=1,
            candidate_top_n=1000,
        )
        summary = batch.run_state.new_collection_summary(contract)
        summary["status"] = status
        batch.run_state.save_run_state(summary, root / "run-summary.json")
        (root / "batch-manifest.json").write_text(
            json.dumps({"status": status}), encoding="utf-8"
        )

    def _write_complete_facts(self, root):
        facts = root / "facts"
        facts.mkdir(parents=True, exist_ok=True)
        packages = {
            "material_ranking": "material-ranking.json",
            "audience": "audience.json",
            "script": "script.json",
            "highlight_frames": "highlight-frames.json",
        }
        manifest = {"schema_version": 1, "packages": {}}
        for name, filename in packages.items():
            (facts / filename).write_text(
                json.dumps({"schema_version": 1, "records": []}), encoding="utf-8"
            )
            manifest["packages"][name] = {
                "path": f"facts/{filename}", "record_count": 0
            }
        (facts / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_collection_status_reports_complete_facts(self):
        batch = load_batch()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_collection_state(root)
            self._write_complete_facts(root)

            status = batch.collection_status(output_dir=root)

        self.assertEqual(status["facts_status"], "complete")

    def test_collection_status_reports_missing_facts_manifest(self):
        batch = load_batch()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_collection_state(root)

            status = batch.collection_status(output_dir=root)

        self.assertEqual(status["facts_status"], "missing")

    def test_collection_status_reports_invalid_facts_manifest(self):
        batch = load_batch()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_collection_state(root)
            facts = root / "facts"
            facts.mkdir()
            (facts / "manifest.json").write_text("{broken", encoding="utf-8")

            status = batch.collection_status(output_dir=root)

        self.assertEqual(status["facts_status"], "invalid")

    def test_list_context_accepts_platform_ctr_alias(self):
        batch = load_batch()
        result = batch._validate_list_context(
            [{"list_context": {"ranking_limit": "CTRTOP1000"}}],
            {"ranking_limit_type": "CTR_TOP1000"},
        )
        self.assertTrue(result["valid"])

    def test_fetch_batch_requests_global_rank_top_n_across_selection_groups(self):
        batch = load_batch()
        batch.generate_highlights_for_detail = lambda *_args: []
        invoker = BatchInvoker([
            list_record("101", "A"),
            list_record("202", "B"),
            list_record("303", "A"),
        ])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = batch.fetch_batch(
                shop_id="shop-42",
                list_params={},
                detail_date_params={"date_range_type": "LAST_7_DAYS"},
                target_top_n=2,
                candidate_top_n=1000,
                output_dir=root,
                invoker=invoker,
                highlight_spec=highlight_spec(),
            )
            manifest = json.loads((root / "batch-manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual([call["material_id"] for call in invoker.detail_calls], ["101", "202"])
        self.assertEqual(
            [item["material_id"] for item in manifest["candidateMaterials"]], ["101", "202"]
        )
        self.assertEqual(manifest["selectionGroups"], [])

    def test_fetch_batch_uses_local_display_field_selection_before_detail_calls(self):
        batch = load_batch()
        batch.generate_highlights_for_detail = lambda *_args: []
        invoker = BatchInvoker([
            ranked_list_record("1", "普通燕窝", 100, 0.10),
            ranked_list_record("2", "不匹配", 200, 0.90),
            ranked_list_record("3", "高互动燕窝", 300, 0.30),
        ])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = batch.fetch_batch(
                shop_id="shop-42",
                list_params={},
                detail_date_params={"date_range_type": "LAST_7_DAYS"},
                target_top_n=1,
                candidate_top_n=1000,
                material_list={
                    "filters": [
                        {"field": "metrics.show_cnt", "operator": "gte", "value": 100},
                        {"field": "title", "operator": "contains", "value": "燕窝"},
                    ],
                    "sort": {"field": "metrics.interact_rate", "direction": "desc"},
                },
                output_dir=root,
                invoker=invoker,
            )
            manifest = json.loads((root / "batch-manifest.json").read_text(encoding="utf-8"))
            summary = json.loads((root / "run-summary.json").read_text(encoding="utf-8"))
            list_records = json.loads(
                (root / "normalized/list-records.json").read_text(encoding="utf-8")
            )

        self.assertEqual(status, 0)
        self.assertEqual([call["material_id"] for call in invoker.detail_calls], ["3"])
        self.assertEqual(len(list_records), 3)
        self.assertEqual(manifest["fullListCount"], 3)
        self.assertEqual(manifest["matchedMaterialCount"], 2)
        self.assertEqual(
            manifest["materialList"],
            {
                "filters": [
                    {"field": "metrics.show_cnt", "operator": "gte", "value": 100},
                    {"field": "title", "operator": "contains", "value": "燕窝"},
                ],
                "sort": {"field": "metrics.interact_rate", "direction": "desc"},
            },
        )
        self.assertEqual(
            summary["collectionContract"]["materialList"], manifest["materialList"]
        )

    def test_no_local_match_persists_full_list_and_skips_detail_rpa(self):
        batch = load_batch()
        invoker = BatchInvoker([ranked_list_record("1", "普通燕窝", 100, 0.10)])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = batch.fetch_batch(
                shop_id="shop-42",
                list_params={},
                detail_date_params={"date_range_type": "LAST_7_DAYS"},
                target_top_n=1,
                candidate_top_n=1000,
                material_list={
                    "filters": [{"field": "曝光量", "operator": "gt", "value": 9999}],
                    "sort": {"field": "rank", "direction": "asc"},
                },
                output_dir=root,
                invoker=invoker,
            )
            manifest = json.loads((root / "batch-manifest.json").read_text(encoding="utf-8"))
            list_records = json.loads(
                (root / "normalized/list-records.json").read_text(encoding="utf-8")
            )

        self.assertEqual(status, 1)
        self.assertEqual(invoker.detail_calls, [])
        self.assertEqual(len(list_records), 1)
        self.assertEqual(manifest["fullListCount"], 1)
        self.assertEqual(manifest["matchedMaterialCount"], 0)
        self.assertEqual(manifest["error"], "no_materials_matched")

    def test_fetch_batch_records_normalizing_stage_before_fact_packaging(self):
        batch = load_batch()
        batch.generate_highlights_for_detail = lambda *_args: []
        invoker = BatchInvoker([list_record("101", "A")])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = batch.fetch_batch(
                shop_id="shop-42",
                list_params={},
                detail_date_params={"date_range_type": "LAST_7_DAYS"},
                target_top_n=1,
                candidate_top_n=1000,
                output_dir=root,
                invoker=invoker,
                highlight_spec=highlight_spec(),
            )
            summary = json.loads((root / "run-summary.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(summary["workflow"]["stage"], "normalizing")

    def test_fetch_batch_marks_failed_gate_as_partial_workflow(self):
        batch = load_batch()
        batch.generate_highlights_for_detail = lambda *_args: [{"available": False}]
        invoker = BatchInvoker([list_record("101", "A")])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch.fetch_batch(
                shop_id="shop-42",
                list_params={},
                detail_date_params={"date_range_type": "LAST_7_DAYS"},
                target_top_n=1,
                candidate_top_n=1000,
                output_dir=root,
                invoker=invoker,
                highlight_spec=highlight_spec(),
            )
            summary = json.loads((root / "run-summary.json").read_text(encoding="utf-8"))

        self.assertEqual(summary["workflow"]["stage"], "partial")

    def test_fetch_batch_does_not_replace_failed_global_top_n_material(self):
        batch = load_batch()
        batch.generate_highlights_for_detail = lambda *_args: []
        invoker = BatchInvoker(
            [list_record("101", "A"), list_record("202", "B"), list_record("303", "A")],
            failed_material_ids={"101"},
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = batch.fetch_batch(
                shop_id="shop-42",
                list_params={},
                detail_date_params={"date_range_type": "LAST_7_DAYS"},
                target_top_n=2,
                candidate_top_n=1000,
                output_dir=root,
                invoker=invoker,
                highlight_spec=highlight_spec(),
            )
            manifest = json.loads((root / "batch-manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 1)
        self.assertEqual({call["material_id"] for call in invoker.detail_calls}, {"101", "202"})
        self.assertEqual([item["material_id"] for item in manifest["rejectedMaterials"]], ["101"])

    def test_fetch_batch_is_partial_when_a_configured_highlight_frame_is_unavailable(self):
        batch = load_batch()
        batch.generate_highlights_for_detail = lambda *_args: [{"available": False}]
        invoker = BatchInvoker([list_record("101", "A")])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = batch.fetch_batch(
                shop_id="shop-42",
                list_params={},
                detail_date_params={"date_range_type": "LAST_7_DAYS"},
                target_top_n=1,
                candidate_top_n=1000,
                output_dir=root,
                invoker=invoker,
                highlight_spec=highlight_spec(),
            )
            manifest = json.loads((root / "batch-manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 1)
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["selectedMaterials"][0]["postprocessStatus"], "failed")

    def test_fetch_batch_is_partial_when_a_highlight_has_the_wrong_second(self):
        batch = load_batch()
        batch.generate_highlights_for_detail = lambda *_args: [{
            "available": True,
            "frame_type": "custom_highlight",
            "second": 99,
            "relative_path": "highlights/101/01-099s.jpg",
        }]
        invoker = BatchInvoker([list_record("101", "A")])

        def invoke_detail(_shop_id, detail_params):
            response = BatchInvoker.invoke_detail(invoker, _shop_id, detail_params)
            records = json.loads(response["files"][0]["fileUrl"])
            records[0]["trends"] = [{
                "trendName": "点击指数",
                "tendList": [
                    {"x": 0, "y": 0},
                    {"x": 1, "y": 5},
                    {"x": 2, "y": 0},
                ],
            }]
            return fixture_csv_response(records)

        invoker.invoke_detail = invoke_detail

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = root / "highlights" / "101" / "01-099s.jpg"
            frame.parent.mkdir(parents=True)
            frame.write_bytes(b"jpeg")
            status = batch.fetch_batch(
                shop_id="shop-42",
                list_params={},
                detail_date_params={"date_range_type": "LAST_7_DAYS"},
                target_top_n=1,
                candidate_top_n=1000,
                output_dir=root,
                invoker=invoker,
                highlight_spec=highlight_spec(),
            )
            manifest = json.loads((root / "batch-manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 1)
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["selectedMaterials"][0]["postprocessStatus"], "failed")

    def test_retry_highlights_regenerates_only_unavailable_persisted_detail_frames(self):
        batch = load_batch()
        generated_material_ids = []

        def generate_highlights(detail, root, _highlight_spec):
            material_id = detail["material_id"]
            generated_material_ids.append(material_id)
            image = Path(root) / "highlights" / material_id / "01-001s.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"jpeg")
            return [{
                "available": True,
                "frame_type": "custom_highlight",
                "second": 1,
                "relative_path": str(image.relative_to(root)),
            }]

        batch.generate_missing_highlights_for_detail = generate_highlights
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = highlight_spec()
            contract = batch.run_state.collection_contract(
                shop_id="shop-42",
                list_params={},
                detail_date_params={"date_range_type": "LAST_7_DAYS"},
                target_top_n=1,
                candidate_top_n=1000,
            )
            batch.run_state.save_run_state(
                batch.run_state.new_collection_summary(contract),
                root / "run-summary.json",
            )
            batch.set_stage(root, "collecting")
            batch.set_stage(root, "partial", reason="highlight gate incomplete")
            detail_path = root / "normalized" / "detail-records" / "101.json"
            detail_path.parent.mkdir(parents=True)
            detail_path.write_text(json.dumps({
                "material_id": "101",
                "trend_series": [{
                    "trend_name": "点击指数",
                    "series": [
                        {"second": 0, "value": 0},
                        {"second": 1, "value": 5},
                        {"second": 2, "value": 0},
                    ],
                }],
            }), encoding="utf-8")
            (root / "normalized" / "list-records.json").write_text(
                json.dumps([{"material_id": "101", "rank": 1}]), encoding="utf-8"
            )
            (root / "batch-manifest.json").write_text(json.dumps({
                "status": "partial",
                "targetTopN": 1,
                "candidateMaterials": [{"rank": 1, "material_id": "101"}],
                "selectedMaterials": [{
                    "rank": 1,
                    "material_id": "101",
                    "detailRecordPath": "normalized/detail-records/101.json",
                    "postprocessStatus": "failed",
                }],
                "rejectedMaterials": [],
                "unresolvedMaterials": [],
                "cancelledMaterials": [],
            }), encoding="utf-8")

            status = batch.retry_highlights_batch(
                output_dir=root, highlight_spec=spec
            )
            manifest = json.loads((root / "batch-manifest.json").read_text(encoding="utf-8"))

            from scripts.fact_packages import build_fact_packages

            facts = build_fact_packages(root)
            summary = json.loads(
                (root / "run-summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(status, 0)
        self.assertEqual(generated_material_ids, ["101"])
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["selectedMaterials"][0]["postprocessStatus"], "succeeded")
        self.assertEqual(facts["coverage"]["available_highlight_frames"], 1)
        self.assertEqual(summary["workflow"]["stage"], "ready_for_analysis")
        recovery_events = [
            event for event in summary["workflow"]["history"]
            if event.get("recovery") == "retry_highlights"
        ]
        self.assertEqual([event["stage"] for event in recovery_events], ["collecting"])

    def test_scripts_package_imports_from_repository_root(self):
        result = subprocess.run(
            [sys.executable, "-c", "import scripts.yuntu_batch"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
