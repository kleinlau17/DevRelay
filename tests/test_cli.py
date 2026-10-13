from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from devrelay.cli import main, parser

from helpers import RepositoryTest


class WatchArgumentTests(unittest.TestCase):
    def test_poll_interval_alias_uses_same_destination_and_default(self):
        self.assertEqual(parser().parse_args(["watch"]).poll_interval, 60)
        for option in ("--poll-interval", "--interval"):
            with self.subTest(option=option):
                args = parser().parse_args(["watch", option, "30"])
                self.assertEqual(args.poll_interval, 30)

    def test_poll_interval_alias_rejects_non_positive_integers(self):
        for option in ("--poll-interval", "--interval"):
            for value in ("0", "-1", "1.5", "invalid"):
                with self.subTest(option=option, value=value):
                    with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as raised:
                        parser().parse_args(["watch", option, value])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn("must be a positive integer", err.getvalue())

    def test_watch_help_lists_both_options_without_starting_worker(self):
        with patch("devrelay.cli.repository_root") as repository_root, patch("devrelay.cli.watch") as watch:
            with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as raised:
                main(["watch", "--help"])
            self.assertEqual(raised.exception.code, 0)
            self.assertIn("--poll-interval", out.getvalue())
            self.assertIn("--interval", out.getvalue())
            repository_root.assert_not_called()
            watch.assert_not_called()


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
