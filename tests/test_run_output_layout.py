"""Keep the workbench's public output layout aligned with its documented contract."""

from contextlib import redirect_stdout
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
                    mock.patch.object(workbench.FasterWhisperProvider, "preflight"), \
                    mock.patch.object(workbench, "collector_main", return_value=2) as collector, \
                    redirect_stdout(output):
                status = workbench.main(["acquire", "--shop-id", "fixture", "--query-spec", "fixture.json"])
            self.assertEqual(status, 2)
            run_dir = Path(json.loads(output.getvalue())["runDir"])
            self.assertEqual(run_dir.parent, Path(temporary) / "云图素材脚本数据")
            self.assertEqual(collector.call_args.args[0][-2:], ["--output-dir", str(run_dir)])

    def test_missing_model_stops_before_online_rpa(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(workbench, "collector_main") as collector:
            status = workbench.main(["acquire", "--shop-id", "fixture", "--query-spec", "fixture.json",
                                     "--model-dir", str(Path(temporary) / "missing")])
        self.assertEqual(status, 2)
        collector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
