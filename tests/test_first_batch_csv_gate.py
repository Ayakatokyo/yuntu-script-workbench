"""Offline scheduler acceptance: responses must land as real business CSV files."""
import json
from pathlib import Path

import pytest

from scripts import yuntu_batch as batch


class CsvDownloader:
    def download_file(self, url, target):
        if url == "download-error":
            raise OSError("download failed")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(url, encoding="utf-8")
        return target


def csv_result(material_id):
    return {"status": "completed", "files": [{"fileUrl":
        f"materialId,dateType,trends\n{material_id},LAST_7_DAYS,[]\n"}]}


class Invoker:
    def __init__(self, result=None):
        self.result = result or (lambda mid: csv_result(mid))
        self.list_calls = 0
        self.detail_calls = []

    def invoke_list(self, *_args):
        self.list_calls += 1
        return {"data": [{"materialId": str(i)} for i in range(1, 7)]}

    def invoke_detail(self, _shop_id, params):
        mid = params["material_id"]
        self.detail_calls.append(mid)
        return self.result(mid)


class AsyncInvoker(Invoker):
    supports_detail_task_recovery = True

    def __init__(self, result=None):
        super().__init__(result)
        self.wait_calls = []

    def submit_detail(self, _shop_id, params):
        mid = params["material_id"]
        self.detail_calls.append(mid)
        return "task-" + mid

    def wait_detail(self, task_group_id):
        self.wait_calls.append(task_group_id)
        return self.result(task_group_id.removeprefix("task-"))


def run(root, invoker):
    status = batch.fetch_batch(shop_id="shop", list_params={},
        detail_date_params={"date_type": "LAST_7_DAYS"},
        target_top_n=6, candidate_top_n=1000, output_dir=root,
        invoker=invoker, downloader=CsvDownloader())
    return status, json.loads((root / "batch-manifest.json").read_text())


@pytest.mark.parametrize("response", [
    {"status": "completed", "files": []},
    {"status": "completed", "data": [{"materialId": "1", "trends": []}]},
    {"status": "completed", "files": [{"fileUrl": "error,message\n400,bad params\n"}]},
    {"status": "completed", "files": [{"fileUrl": "materialId,trends\n"}]},
    {"status": "completed", "files": [{"fileUrl": "download-error"}]},
    csv_result("wrong-id"),
])
def test_no_valid_csv_stops_after_first_wave_and_resume_makes_no_rpa(tmp_path, response):
    invoker = Invoker(lambda _mid: response)
    status, manifest = run(tmp_path, invoker)
    assert len(invoker.detail_calls) == 3
    assert status == 1
    gate = manifest["firstBatchCsvGate"]
    assert gate["status"] == "blocked"
    assert gate["errorCode"] == "first_batch_no_valid_csv"
    assert "入参可能有问题" in gate["message"]
    assert manifest["status"] == "paused"
    assert run(tmp_path, invoker)[0] == 1
    assert len(invoker.detail_calls) == 3
    assert invoker.list_calls == 1
    state = batch.collection_status(output_dir=tmp_path)
    assert state["firstBatchCsvGate"]["status"] == "blocked"
    assert state["ready_for_analysis"] is False


def test_one_valid_csv_allows_later_wave_even_when_postprocess_fails(tmp_path, monkeypatch):
    invoker = Invoker(lambda mid: csv_result(mid) if mid != "2" else {"files": []})
    monkeypatch.setattr(batch, "persist_video_for_detail", lambda *_args: (_ for _ in ()).throw(OSError("video failed")))
    status, manifest = run(tmp_path, invoker)
    assert len(invoker.detail_calls) == 6
    assert status == 1
    assert manifest["firstBatchCsvGate"]["status"] == "passed"
    assert manifest["firstBatchCsvGate"]["validCsvCount"] == 2


@pytest.mark.parametrize("pending_result", [
    {"status": "pending"}, {"status": "new-status"},
])
def test_unconfirmed_first_wave_cannot_submit_next_wave_and_resume_only_polls(tmp_path, pending_result):
    invoker = AsyncInvoker(lambda _mid: pending_result)
    status, manifest = run(tmp_path, invoker)
    assert status == 1
    assert len(invoker.detail_calls) == 3
    assert manifest["firstBatchCsvGate"]["status"] == "unconfirmed"
    invoker.result = csv_result
    _, manifest = run(tmp_path, invoker)
    assert invoker.list_calls == 1
    assert sorted(invoker.detail_calls) == list("123456")
    assert manifest["firstBatchCsvGate"]["status"] == "passed"


def test_async_all_failed_is_blocked_and_not_repolled(tmp_path):
    invoker = AsyncInvoker(lambda _mid: {"status": "failed"})
    _, manifest = run(tmp_path, invoker)
    assert len(invoker.detail_calls) == 3
    assert manifest["firstBatchCsvGate"]["status"] == "blocked"
    run(tmp_path, invoker)
    assert len(invoker.wait_calls) == 3


def test_new_run_with_corrected_parameters_can_probe_again(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    run(old, Invoker(lambda _mid: {"files": []}))
    status, manifest = run(new, Invoker())
    assert status == 0
    assert manifest["firstBatchCsvGate"]["status"] == "passed"


def test_sync_unknown_response_is_unconfirmed_not_a_parameter_failure(tmp_path):
    invoker = Invoker(lambda _mid: {"status": "running"})
    _, manifest = run(tmp_path, invoker)
    assert len(invoker.detail_calls) == 3
    assert manifest["firstBatchCsvGate"]["status"] == "unconfirmed"


def test_poll_timeout_does_not_claim_wrong_parameters_or_submit_more(tmp_path):
    def timeout(_mid):
        raise TimeoutError("poll timeout")
    invoker = Invoker(timeout)
    _, manifest = run(tmp_path, invoker)
    assert manifest["firstBatchCsvGate"]["status"] == "unconfirmed"
    assert len(invoker.detail_calls) == 3


def test_partial_first_batch_success_still_waits_for_unknown_tasks(tmp_path):
    invoker = AsyncInvoker(lambda mid: csv_result(mid) if mid == "1" else {"status": "pending"})
    _, manifest = run(tmp_path, invoker)
    assert len(invoker.detail_calls) == 3
    assert manifest["firstBatchCsvGate"]["status"] == "unconfirmed"
    assert manifest["firstBatchCsvGate"]["validCsvCount"] == 1


def test_valid_csv_then_local_persistence_error_does_not_trigger_input_block(tmp_path, monkeypatch):
    original = batch.write_json_atomically
    def fail_normalized_detail(payload, path):
        if "detail-records" in Path(path).parts:
            raise OSError("disk full")
        return original(payload, path)
    monkeypatch.setattr(batch, "write_json_atomically", fail_normalized_detail)
    invoker = Invoker()
    _, manifest = run(tmp_path, invoker)
    assert len(invoker.detail_calls) == 6
    assert manifest["firstBatchCsvGate"]["status"] == "passed"


def test_cli_receipt_exposes_pause_and_parameter_warning(tmp_path, monkeypatch, capsys):
    from scripts import collector
    run(tmp_path, Invoker(lambda _mid: {"files": []}))
    monkeypatch.setattr(collector, "build_ready_receipt", lambda *_args: {"ready_for_analysis": False})
    collector._print_batch_result(tmp_path, {"coverage": {}})
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["rpa_paused"] is True
    assert "入参可能有问题" in receipt["firstBatchCsvGate"]["message"]


def test_existing_failed_run_without_gate_cannot_reprobe_a_later_batch(tmp_path):
    invoker = Invoker(lambda _mid: {"files": []})
    run(tmp_path, invoker)
    for filename in ("batch-manifest.json", "run-summary.json"):
        path = tmp_path / filename
        state = json.loads(path.read_text())
        state.pop("firstBatchCsvGate", None)
        path.write_text(json.dumps(state))
    _, manifest = run(tmp_path, invoker)
    assert len(invoker.detail_calls) == 3
    assert invoker.list_calls == 1
    assert manifest["firstBatchCsvGate"]["status"] == "blocked"


def test_interrupted_checkpoint_recovers_first_batch_csv_evidence(tmp_path):
    invoker = Invoker()
    run(tmp_path, invoker)
    path = tmp_path / "batch-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["firstBatchCsvGate"].update(status="pending", outcomes={})
    path.write_text(json.dumps(manifest))
    summary_path = tmp_path / "run-summary.json"
    summary = json.loads(summary_path.read_text())
    summary["firstBatchCsvGate"] = manifest["firstBatchCsvGate"]
    summary_path.write_text(json.dumps(summary))
    _, manifest = run(tmp_path, invoker)
    assert manifest["firstBatchCsvGate"]["status"] == "passed"
    summary = json.loads((tmp_path / "run-summary.json").read_text())
    assert summary["firstBatchCsvGate"] == manifest["firstBatchCsvGate"]
    assert len(invoker.detail_calls) == 6
    assert invoker.list_calls == 1


@pytest.mark.parametrize("text", [
    "materialId,dateType,trends\n1,LAST_7_DAYS,\n",
    "materialId,dateType,trends\n1,LAST_7_DAYS,[broken\n",
])
def test_csv_with_only_empty_or_malformed_business_cells_is_invalid(tmp_path, text):
    invoker = Invoker(lambda _mid: {"status": "completed", "files": [{"fileUrl": text}]})
    _, manifest = run(tmp_path, invoker)
    assert manifest["firstBatchCsvGate"]["status"] == "blocked"
    assert len(invoker.detail_calls) == 3


def test_unconfirmed_network_failure_is_not_treated_as_remote_completion(tmp_path):
    def transport_failure(_mid):
        raise ConnectionError("connection lost")
    invoker = Invoker(transport_failure)
    _, manifest = run(tmp_path, invoker)
    assert manifest["firstBatchCsvGate"]["status"] == "unconfirmed"
    assert len(invoker.detail_calls) == 3


def test_multi_record_detail_csv_cannot_hide_another_material(tmp_path):
    invoker = Invoker(lambda mid: {"status": "completed", "files": [{"fileUrl":
        f"materialId,dateType,trends\n{mid},LAST_7_DAYS,[]\n999,LAST_7_DAYS,[]\n"}]})
    _, manifest = run(tmp_path, invoker)
    assert manifest["firstBatchCsvGate"]["status"] == "blocked"
    assert len(invoker.detail_calls) == 3


def test_resume_does_not_repoll_already_persisted_first_batch_material(tmp_path):
    invoker = AsyncInvoker(lambda mid: csv_result(mid) if mid == "1" else {"status": "pending"})
    run(tmp_path, invoker)
    path = tmp_path / "batch-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["taskRuns"]["detail:1"]["attempts"][-1].update(remote_status="submitted", remoteStatus="submitted")
    manifest["firstBatchCsvGate"]["outcomes"].pop("1", None)
    path.write_text(json.dumps(manifest))
    invoker.wait_calls.clear()
    invoker.result = csv_result
    _, manifest = run(tmp_path, invoker)
    assert "task-1" not in invoker.wait_calls
    assert len(manifest["selectedMaterials"]) == 6


@pytest.mark.parametrize("no_download", [False, True])
def test_dynamic_detail_cli_cannot_bypass_batch_gate(tmp_path, monkeypatch, capsys, no_download):
    from scripts import rpa_dynamic_detail_client as client
    monkeypatch.setattr(client, "fetch_connector_detail", lambda: pytest.fail("must stop before network"))
    argv = ["detail-client", "fetch", "--shop-id", "shop", "--params-json", "{}", "--output-dir", str(tmp_path)]
    if no_download:
        argv.append("--no-download")
    monkeypatch.setattr(client.sys, "argv", argv)
    assert client.main() != 0
    assert "collector.py collect" in capsys.readouterr().err


@pytest.mark.parametrize("gate_status", ["blocked", "unconfirmed", "pending"])
def test_dynamic_list_cli_cannot_submit_inside_a_paused_run(tmp_path, monkeypatch, capsys, gate_status):
    from scripts import rpa_dynamic_client as client
    (tmp_path / "batch-manifest.json").write_text(json.dumps({"firstBatchCsvGate": {
        "status": gate_status, "materialIds": ["1"], "message": "当前入参可能有问题",
    }}))
    monkeypatch.setattr(client, "fetch_connector_detail", lambda: pytest.fail("must stop before network"))
    monkeypatch.setattr(client.sys, "argv", ["list-client", "fetch", "--shop-id", "shop",
        "--params-json", "{}", "--output-dir", str(tmp_path / "raw-csv"), "--no-download"])
    assert client.main() != 0
    assert "暂停" in capsys.readouterr().err


def test_dynamic_list_cli_validates_output_directory_before_network(monkeypatch):
    from scripts import rpa_dynamic_client as client
    monkeypatch.setattr(client, "fetch_connector_detail", lambda: pytest.fail("must stop before network"))
    monkeypatch.setattr(client.sys, "argv", ["list-client", "fetch", "--shop-id", "shop",
        "--params-json", "{}", "--output-dir", str(Path(client.__file__).resolve().parents[1])])
    assert client.main() != 0
