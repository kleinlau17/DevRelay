from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import unittest

from devrelay.cli import main

from helpers import RepositoryTest


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
