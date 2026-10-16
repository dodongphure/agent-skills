"""Diagnostic timing never substitutes for workflow evidence."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure.py"
SPEC = importlib.util.spec_from_file_location("measure", SCRIPT)
measure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(measure)


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.task = Path(self.tmp.name) / ".agent-pr" / "tasks" / "timing-test"
        self.task.mkdir(parents=True)

    def test_records_exit_and_duration_without_command_text(self):
        code = measure.measure(self.task, "render", [sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(code, 7)
        text = measure.log_path(self.task).read_text()
        row = json.loads(text)
        self.assertEqual(row["exit_code"], 7)
        self.assertEqual(row["outcome"], "exited")
        self.assertGreaterEqual(row["elapsed_seconds"], 0)
        self.assertTrue(row["started_at"].endswith("Z"))
        self.assertNotIn("SystemExit", text)
        self.assertEqual(set(p.name for p in self.task.iterdir()), {"evidence"})

    def test_summary_does_not_write(self):
        self.assertEqual(measure.summarize(self.task)["categories"], {})
        self.assertEqual(list(self.task.iterdir()), [])

    def test_append_and_summary(self):
        with patch.object(measure.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            measure.measure(self.task, "render", ["approved-command"])
            measure.measure(self.task, "render", ["approved-command"])
        result = measure.summarize(self.task)
        self.assertEqual(result["categories"]["render"]["count"], 2)
        self.assertIn("not task wall time", result["note"])

    def test_launch_error(self):
        with patch.object(measure.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(measure.measure(self.task, "check", ["missing"]), 127)
        self.assertEqual(json.loads(measure.log_path(self.task).read_text())["outcome"], "launch_error")

    def test_invalid_path_never_runs_child(self):
        with patch.object(measure.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                measure.measure(Path(self.tmp.name), "check", ["command"])
            run.assert_not_called()

    def test_log_open_failure_never_runs_child(self):
        with patch.object(Path, "open", side_effect=PermissionError), patch.object(measure.subprocess, "run") as run:
            with self.assertRaises(PermissionError):
                measure.measure(self.task, "check", ["command"])
            run.assert_not_called()

    def test_link_guard(self):
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ValueError):
                measure.log_path(self.task)

    def test_bad_category(self):
        with self.assertRaises(ValueError):
            measure.measure(self.task, "thinking", ["command"])

    def test_bad_duration_rejected(self):
        path = measure.log_path(self.task)
        path.parent.mkdir()
        for value in (-1, float("nan"), True, "1"):
            path.write_text(json.dumps({"category": "render", "elapsed_seconds": value}))
            with self.assertRaises(ValueError):
                measure.summarize(self.task)

    def test_partial_log_reports_missing_coverage(self):
        path = measure.log_path(self.task)
        path.parent.mkdir()
        path.write_text(json.dumps({"category": "render", "elapsed_seconds": 0.5}) + '\n{"category":')
        result = measure.summarize(self.task)
        self.assertEqual(result["categories"]["render"]["count"], 1)
        self.assertEqual(result["unreadable_lines"], 1)

    def test_cli_preserves_child_exit(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--task-dir", str(self.task),
                                 "--category", "check", "--", sys.executable, "-c", "raise SystemExit(6)"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 6, result.stderr)

    def test_cli_rejects_summary_command_mix(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--task-dir", str(self.task),
                                 "--summary", "--", "unexpected"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(self.task.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
