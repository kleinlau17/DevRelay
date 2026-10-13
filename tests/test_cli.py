from contextlib import chdir, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from devrelay import __version__
from devrelay.cli import main

from helpers import RepositoryTest


class VersionTests(unittest.TestCase):
    def test_version_skips_project_initialization(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            for flag in ("--version", "-V"):
                with self.subTest(flag=flag):
                    out, err = io.StringIO(), io.StringIO()
                    with (redirect_stdout(out), redirect_stderr(err),
                          patch("devrelay.cli.repository_root") as repository,
                          patch("devrelay.cli.load") as load,
                          patch("devrelay.cli.State") as state,
                          self.assertRaises(SystemExit) as exit):
                        main([flag])
                    self.assertEqual(exit.exception.code, 0)
                    self.assertEqual(out.getvalue(), f"{__version__}\n")
                    self.assertEqual(err.getvalue(), "")
                    repository.assert_not_called()
                    load.assert_not_called()
                    state.assert_not_called()
                    self.assertEqual(list(Path(directory).iterdir()), [])

    def test_version_process_succeeds_outside_git_repository(self):
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        with tempfile.TemporaryDirectory() as directory:
            for flag in ("--version", "-V"):
                with self.subTest(flag=flag):
                    result = subprocess.run([sys.executable, "-m", "devrelay", flag], cwd=directory,
                                            env=environment, text=True, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, f"{__version__}\n")
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(list(Path(directory).iterdir()), [])


class CliTests(RepositoryTest):
    def invoke(self, *argv, project=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--project", str(project or self.root), *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_onboarding_is_non_destructive_and_reports_missing_acceptance(self):
        code, result = self.invoke("init", "--provider", "local")
        self.assertEqual(code, 0)
        self.assertEqual(result["checks"], 0)
        code, result = self.invoke("doctor")
        self.assertEqual(code, 1)
        self.assertFalse(next(item for item in result["checks"] if item["id"] == "acceptance")["ok"])
        code, result = self.invoke("init")
        self.assertEqual(code, 1)
        self.assertIn("already exists", result["error"])

    def test_cli_runs_an_offline_task_and_routes_calls_from_its_worktree(self):
        self.configure(executable=self.fake_codex())
        task_file = Path(self.temporary.name) / "task.md"
        task_file.write_text("Fix the bounded behavior")
        code, _ = self.invoke("task", "add", "fix", "--title", "Fix value", "--file", str(task_file))
        self.assertEqual(code, 0)
        code, result = self.invoke("run", "fix")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "verified")
        # Record the anchor used by init so a CLI call inside the task uses the controller config.
        self.state.set_metadata("controller_root", str(self.root))
        code, status = self.invoke("status", project=Path(result["worktree"]))
        self.assertEqual(code, 0)
        self.assertEqual(status["project"], str(self.root))
        code, committed = self.invoke("submit", "fix", "--local", project=Path(result["worktree"]))
        self.assertEqual(code, 0)
        self.assertEqual(committed["status"], "committed")

    def test_review_and_merge_commands_reject_local_projects(self):
        self.configure(executable=self.fake_codex())
        for operation in ("review", "merge"):
            with self.subTest(operation=operation):
                code, result = self.invoke(operation, "fix")
                self.assertEqual(code, 1)
                self.assertIn("provider=github", result["error"])


if __name__ == "__main__":
    unittest.main()
