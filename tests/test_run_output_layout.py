"""Keep the workbench's public output layout aligned with its documented contract."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    spec = importlib.util.spec_from_file_location("yuntu_script_workbench_entry", SCRIPTS / "run.py")
    workbench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workbench)
finally:
    sys.path.pop(0)


class OutputLayoutTests(unittest.TestCase):
    def test_default_root_creates_unique_run_paths_and_explicit_paths_stay_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(workbench.Path, "cwd", return_value=root):
                first = workbench._acquire_run_dir(None, None)
                second = workbench._acquire_run_dir(None, None)
            self.assertEqual(first.parent, root / "云图素材脚本数据")
            self.assertNotEqual(first, second)
            self.assertTrue(first.name.startswith("run-"))
            self.assertEqual(workbench._acquire_run_dir(str(root / "custom"), None).parent, root / "custom")
            self.assertEqual(workbench._acquire_run_dir(None, str(root / "existing")), root / "existing")

    def test_acquire_forwards_generated_run_dir_without_online_rpa(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with mock.patch.object(workbench.Path, "cwd", return_value=Path(temporary)), \
                    mock.patch.object(workbench, "collector_main", return_value=2) as collector, \
                    redirect_stdout(output):
                status = workbench.main(["acquire", "--shop-id", "fixture", "--query-spec", "fixture.json"])
            self.assertEqual(status, 2)
            run_dir = Path(json.loads(output.getvalue())["runDir"])
            self.assertEqual(run_dir.parent, Path(temporary) / "云图素材脚本数据")
            self.assertEqual(collector.call_args.args[0][-2:], ["--output-dir", str(run_dir)])

    def test_acquire_prepares_audio_without_local_model(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(workbench, "collector_main", return_value=0), \
                mock.patch.object(workbench, "prepare_audio", return_value={"counts": {"pending_agent": 2}}) as prepare, \
                redirect_stdout(io.StringIO()):
            status = workbench.main(["acquire", "--shop-id", "fixture", "--query-spec", "fixture.json",
                                     "--output-dir", temporary])
        self.assertEqual(status, 0)
        prepare.assert_called_once_with(Path(temporary))

    def test_acquire_reports_audio_stage_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(workbench, "collector_main", return_value=0), \
                mock.patch.object(workbench, "prepare_audio", side_effect=workbench.TranscriptionError("audio_failed")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            status = workbench.main(["acquire", "--shop-id", "fixture", "--query-spec", "fixture.json",
                                     "--output-dir", temporary])
        self.assertEqual(status, 2)
        failure = json.loads(errors.getvalue())
        self.assertEqual(failure["stage"], "prepare_audio")
        self.assertEqual(failure["runDir"], temporary)

    def test_mark_unavailable_uses_public_entry(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(workbench, "mark_unavailable", return_value={"counts": {"agent_unavailable": 1}}) as mark, \
                redirect_stdout(io.StringIO()):
            status = workbench.main(["mark-unavailable", "--run-dir", temporary,
                                     "--video-sha", "abc", "--response-file", "response.txt",
                                     "--review-note", "复核后没有可用的语音文字"])
        self.assertEqual(status, 0)
        mark.assert_called_once_with(Path(temporary), video_sha="abc", response_file=Path("response.txt"),
                                     review_note="复核后没有可用的语音文字")

    def test_finalize_does_not_recollect(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(workbench, "finalize_transcripts", return_value={
                    "counts": {"available": 1}}) as finalize, \
                mock.patch.object(workbench, "build_fact_packages"), \
                mock.patch.object(workbench, "write_script_report", return_value={}), \
                mock.patch.object(workbench, "collector_main") as collector:
            status = workbench.main(["finalize-scripts", "--run-dir", temporary])
        self.assertEqual(status, 0)
        finalize.assert_called_once()
        collector.assert_not_called()

    def test_workflow_waits_for_final_script_report(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(workbench, "collector_main") as collector:
            status = workbench.main(["workflow", "--run-dir", temporary,
                                     "--business-question", "分析脚本"])
        self.assertEqual(status, 2)
        collector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
