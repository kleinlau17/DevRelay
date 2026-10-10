import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from devrelay.codex import build_command
from devrelay.config import load
from devrelay.errors import RelayError
from devrelay.git import source_tree
from devrelay.process import execute
from devrelay.providers import add_local, queue_report, snapshot
from devrelay.runner import complete_local, run_task, start, submit, verify
from devrelay.state import State

from helpers import RepositoryTest


class WorkflowTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.executable = self.fake_codex()
        self.configure(executable=self.executable)
        add_local(self.config, self.state, "fix-1", "Fix the value", "Fix value.txt; configured regression must pass.", "priority:P1")

    def run_task(self, identifier="fix-1", **kwargs):
        with self.state.lock():
            return run_task(self.config, self.state, identifier, **kwargs)

    def test_real_worktree_and_verified_local_delivery(self):
        result = self.run_task()
        self.assertEqual(result["status"], "verified")
        self.assertEqual((self.root / "value.txt").read_text(), "broken\n")
        self.assertEqual((Path(result["worktree"]) / "value.txt").read_text(), "fixed\n")
        self.assertTrue(result["verification"]["passed"])
        self.assertTrue(Path(result["verification_path"]).is_file())
        with self.state.lock():
            committed = submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual(committed["status"], "committed")
        with self.assertRaisesRegex(RelayError, "integrated"):
            complete_local(self.config, self.state, "fix-1")
        self.git("merge", "--ff-only", committed["branch"])
        with self.state.lock():
            completed = complete_local(self.config, self.state, "fix-1")
        self.assertFalse(completed["open"])

    def test_failed_check_repairs_in_exact_saved_session(self):
        trace = Path(self.temporary.name) / "trace.jsonl"
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "retry", "DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task()
        invocations = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(invocations), 2)
        argv = invocations[1]["argv"]
        self.assertEqual(argv[argv.index("resume") + 1], "exact-test-session")
        self.assertNotIn("--last", argv)
        self.assertIn("failed", invocations[1]["prompt"])

    def test_codex_failure_retains_session_and_work_for_resume(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "command-failure"}):
            with self.assertRaises(RelayError):
                self.run_task()
        task = self.state.get("fix-1")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["session_id"], "exact-test-session")
        self.assertTrue(Path(task["worktree"]).is_dir())
        result = self.run_task(resume=True)
        self.assertEqual(result["status"], "verified")

    def test_blocked_worker_does_not_run_acceptance(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "blocked"}):
            task = self.run_task()
        self.assertEqual(task["status"], "blocked")
        self.assertIn("dataset", task["error"])
        self.assertNotIn("verification", task if task.get("verification") else {})

    def test_worker_exit_zero_is_insufficient_without_completed_turn_and_result(self):
        for mode in ("bad-result", "no-completed-turn"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"DEVRELAY_TEST_MODE": mode}):
                with self.assertRaises(RelayError):
                    self.run_task(resume=self.state.get("fix-1").get("worktree") is not None)
                self.assertEqual(self.state.get("fix-1")["status"], "failed")

    def test_source_drift_rejects_submission(self):
        task = self.run_task()
        (Path(task["worktree"]) / "new-unverified.txt").write_text("new code")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "stale"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual(self.git("log", "--format=%s", "-1", cwd=task["worktree"]), "project config")

    def test_configuration_drift_rejects_submission(self):
        self.run_task()
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("max_attempts = 2", "max_attempts = 3"))
        config = load(self.root)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "stale"):
            submit(config, self.state, "fix-1", local=True)

    def test_base_movement_requires_combined_reverification(self):
        self.run_task()
        (self.root / "other.txt").write_text("new base content")
        self.git("add", "other.txt")
        self.git("commit", "-qm", "base moved")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "stale"):
            submit(self.config, self.state, "fix-1", local=True)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "integrate"):
            verify(self.config, self.state, "fix-1")

    def test_check_that_mutates_source_is_rejected(self):
        self.config = self.configure(executable=self.executable, attempts=1,
                                     checks=[[sys.executable, "-c", "from pathlib import Path; Path('value.txt').write_text('mutation')"]])
        with self.assertRaisesRegex(RelayError, "changed during verification"):
            self.run_task()

    def test_start_preserves_uncommitted_work_and_excludes_duplicate_dispatch(self):
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
            marker = Path(task["worktree"]) / "ongoing.txt"
            marker.write_text("existing work")
            recovered = start(self.config, self.state, "fix-1")
        self.assertEqual(task["worktree"], recovered["worktree"])
        self.assertEqual(marker.read_text(), "existing work")
        self.assertIsNone(queue_report(self.config, snapshot(self.config, self.state))["selected"])

    def test_no_acceptance_means_no_model_invocation(self):
        self.config = self.configure(executable=self.executable, checks=[])
        with self.assertRaisesRegex(RelayError, "at least one"):
            self.run_task()
        self.assertFalse((self.root / ".devrelay/worktrees/fix-1").exists())

    def test_local_dependencies_wait_for_integration(self):
        add_local(self.config, self.state, "fix-2", "Next fix", "Depends on: #fix-1", "priority:P1")
        self.assertNotIn("fix-2", [item["id"] for item in queue_report(self.config, snapshot(self.config, self.state))["eligible"]])
        task = self.run_task()
        with self.state.lock():
            submit(self.config, self.state, "fix-1", local=True)
        self.git("merge", "--ff-only", task["branch"])
        with self.state.lock():
            complete_local(self.config, self.state, "fix-1")
        self.assertEqual(queue_report(self.config, snapshot(self.config, self.state))["selected"]["id"], "fix-2")

    def test_source_snapshot_preserves_the_real_index(self):
        before = self.git("write-tree")
        (self.root / "value.txt").write_text("changed")
        (self.root / "new.txt").write_text("new")
        computed = source_tree(self.root)
        self.assertEqual(self.git("write-tree"), before)
        self.git("add", "-A")
        self.assertEqual(self.git("write-tree"), computed)

    def test_command_builder_preserves_configured_model_and_profile(self):
        self.config.data["codex"].update(model="requested-model", profile="requested-profile")
        command = build_command(self.config, Path("/log directory"), "session-for-this-task")
        self.assertIn("requested-model", command)
        self.assertIn("requested-profile", command)
        self.assertEqual(command[command.index("resume") + 1], "session-for-this-task")
        self.assertNotIn("--last", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_saved_session_cannot_become_a_cli_option(self):
        with self.assertRaisesRegex(RelayError, "session ID is invalid"):
            build_command(self.config, Path("/logs"), "--last")

    def test_partial_worktree_creation_can_be_retried(self):
        from devrelay import runner
        real_git = runner.git
        def fail_add(root, *argv):
            if argv[:2] == ("worktree", "add"):
                raise RelayError("Interrupted creation")
            return real_git(root, *argv)
        with self.state.lock(), patch.object(runner, "git", side_effect=fail_add):
            with self.assertRaisesRegex(RelayError, "Interrupted creation"):
                start(self.config, self.state, "fix-1")
        self.assertEqual(self.state.get("fix-1")["status"], "start_failed")
        with self.state.lock():
            result = start(self.config, self.state, "fix-1")
        self.assertTrue(Path(result["worktree"]).is_dir())

    def test_submission_error_does_not_restart_the_model(self):
        from devrelay import runner
        trace = Path(self.temporary.name) / "submission-trace"
        with (patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}),
              patch.object(runner, "submit", side_effect=RelayError("PR request timed out")),
              self.assertRaisesRegex(RelayError, "PR request")):
            self.run_task(submit_after=True)
        self.assertEqual(len(trace.read_text().splitlines()), 1)
        self.assertTrue(self.state.get("fix-1")["verification"]["passed"])

    def test_failed_work_requires_explicit_recovery_before_automatic_dispatch(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "command-failure"}):
            with self.assertRaises(RelayError):
                self.run_task()
        with self.state.lock():
            result = run_task(self.config, self.state)
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["tasks"], ["fix-1"])

    def test_task_requirement_drift_rejects_submission(self):
        self.run_task()
        task = self.state.get("fix-1")
        task["body"] += "\nAdditional acceptance requirement"
        self.state.save(task)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "stale"):
            submit(self.config, self.state, "fix-1", local=True)

    def test_withdrawn_approval_rejects_submission(self):
        self.run_task()
        task = self.state.get("fix-1")
        task["labels"].remove("auto:approved")
        self.state.save(task)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "not eligible"):
            submit(self.config, self.state, "fix-1", local=True)


class SubmoduleWorkflowTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.dependency_source = Path(self.temporary.name) / "dependency-source"
        self._initialize_repository(self.dependency_source, "dependency.txt", "broken\n")

    def _initialize_repository(self, path, filename, content):
        path.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "DevRelay Test"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "devrelay@example.invalid"], cwd=path, check=True)
        subprocess.run(["git", "config", "receive.denyCurrentBranch", "updateInstead"], cwd=path, check=True)
        (path / filename).write_text(content)
        subprocess.run(["git", "add", "."], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)

    def _add_nested_submodule(self):
        nested_source = Path(self.temporary.name) / "nested-source"
        self._initialize_repository(nested_source, "nested.txt", "nested\n")
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(nested_source), "nested/inner"], cwd=self.dependency_source, check=True)
        subprocess.run(["git", "commit", "-qam", "add nested dependency"],
                       cwd=self.dependency_source, check=True)

    def _prepare(self, checks=None):
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(self.dependency_source), "vendor/dependency"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "-f", ".gitmodules", "submodule.vendor/dependency.ignore", "all"],
                       cwd=self.root, check=True)
        self.git("commit", "-qam", "add dependency")
        self.configure(checks=checks or [[sys.executable, "-c", "pass"]])
        add_local(self.config, self.state, "fix-1", "Fix dependency", "Update the dependency.\nDepends on: none",
                  "priority:P1")
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        worktree = Path(task["worktree"])
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init",
                        "--recursive"], cwd=worktree, check=True)
        dependency = worktree / "vendor/dependency"
        subprocess.run(["git", "config", "user.name", "DevRelay Test"], cwd=dependency, check=True)
        subprocess.run(["git", "config", "user.email", "devrelay@example.invalid"], cwd=dependency, check=True)
        return worktree, dependency

    def test_tracked_submodule_change_cannot_be_hidden_by_ignore_configuration(self):
        worktree, dependency = self._prepare()
        (dependency / "dependency.txt").write_text("fixed\n")
        self.git("config", "submodule.vendor/dependency.ignore", "all", cwd=worktree)
        self.git("config", "status.showUntrackedFiles", "no", cwd=dependency)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_untracked_submodule_file_cannot_be_hidden_by_status_configuration(self):
        worktree, dependency = self._prepare()
        (dependency / "untracked.txt").write_text("not delivered\n")
        excludes = Path(self.temporary.name) / "global-excludes"
        excludes.write_text("*.txt\n")
        self.git("config", "submodule.vendor/dependency.ignore", "untracked", cwd=worktree)
        self.git("config", "core.excludesFile", str(excludes), cwd=dependency)
        self.git("config", "status.showUntrackedFiles", "no", cwd=dependency)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertTrue((dependency / "untracked.txt").is_file())

    def test_dirty_nested_submodule_is_detected_recursively(self):
        self._add_nested_submodule()
        worktree, dependency = self._prepare()
        nested = dependency / "nested/inner"
        (nested / "nested.txt").write_text("dirty nested source\n")
        self.git("config", "submodule.vendor/dependency.ignore", "all", cwd=worktree)
        self.git("config", "submodule.nested/inner.ignore", "all", cwd=dependency)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "vendor/dependency/nested/inner"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual((nested / "nested.txt").read_text(), "dirty nested source\n")

    def test_submodule_change_during_checks_invalidates_verification(self):
        checks = [[sys.executable, "-c",
                   "from pathlib import Path; Path('vendor/dependency/dependency.txt').write_text('during checks\\n')"]]
        _, dependency = self._prepare(checks)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual(self.state.get("fix-1")["status"], "failed")
        self.assertEqual((dependency / "dependency.txt").read_text(), "during checks\n")

    def test_submodule_change_after_checks_invalidates_submission(self):
        _, dependency = self._prepare()
        with self.state.lock():
            verify(self.config, self.state, "fix-1")
        (dependency / "after-checks.txt").write_text("not verified\n")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertTrue((dependency / "after-checks.txt").is_file())

    def test_submodule_change_from_parent_commit_hook_blocks_submission(self):
        worktree, dependency = self._prepare()
        (worktree / "value.txt").write_text("fixed\n")
        with self.state.lock():
            verify(self.config, self.state, "fix-1")
        hooks = Path(self.temporary.name) / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook mutation\\n' > vendor/dependency/from-hook.txt\n")
        hook.chmod(0o755)
        self.git("config", "core.hooksPath", str(hooks), cwd=worktree)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual((dependency / "from-hook.txt").read_text(), "hook mutation\n")

    def test_committed_submodule_head_is_delivered_by_parent_gitlink(self):
        worktree, dependency = self._prepare([[sys.executable, "-c",
                                               "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').read_text() == 'fixed\\n'"]])
        (dependency / "dependency.txt").write_text("fixed\n")
        self.git("add", "dependency.txt", cwd=dependency)
        self.git("commit", "-qm", "fix dependency", cwd=dependency)
        submodule_head = self.git("rev-parse", "HEAD", cwd=dependency)
        self.git("push", "-q", "origin", "HEAD:main", cwd=dependency)
        self.git("config", "submodule.vendor/dependency.ignore", "all", cwd=worktree)
        with self.state.lock():
            verified = verify(self.config, self.state, "fix-1")
            committed = submit(self.config, self.state, "fix-1", local=True)
        self.assertTrue(verified["verification"]["passed"])
        self.assertEqual(committed["status"], "committed")
        self.assertIn(submodule_head, self.git("ls-tree", "HEAD", "vendor/dependency", cwd=worktree))


class LockAndProcessTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure()

    def test_repository_lock_blocks_a_second_process(self):
        source = Path(__file__).resolve().parents[1] / "src"
        script = "from pathlib import Path; from devrelay.state import State; import sys; s=State(Path(sys.argv[1]));\nwith s.lock(): print('unexpected acquisition')"
        with self.state.lock():
            result = subprocess.run([sys.executable, "-c", script, str(self.root)],
                                    env={**os.environ, "PYTHONPATH": str(source)}, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another DevRelay worker", result.stderr)

    def test_failed_command_retains_nonzero_receipt(self):
        log = self.state.directory / "failure.log"
        with self.state.lock(), self.assertRaises(RelayError):
            execute(self.state, [sys.executable, "-c", "print('failure evidence'); raise SystemExit(7)"], self.root, log, 5)
        self.assertIn("failure evidence", log.read_text())
        self.assertEqual(json.loads(log.with_suffix(".log.receipt.json").read_text())["returncode"], 7)

    def test_timeout_stops_foreground_process(self):
        log = self.state.directory / "timeout.log"
        with self.state.lock(), self.assertRaisesRegex(RelayError, "timed out"):
            execute(self.state, [sys.executable, "-c", "import time; time.sleep(30)"], self.root, log, 0.1)
        receipt = json.loads(log.with_suffix(".log.receipt.json").read_text())
        self.assertTrue(receipt["timed_out"])
        self.assertIsNone(self.state.metadata("active_process"))

    def test_surviving_process_group_blocks_crash_recovery(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            self.state.set_metadata("active_process", {"group": process.pid})
            with self.assertRaisesRegex(RelayError, "still runs"):
                with self.state.lock():
                    self.fail("must not acquire while child survives")
        finally:
            process.terminate()
            process.wait(timeout=5)
        with self.state.lock():
            self.assertIsNone(self.state.metadata("active_process"))


if __name__ == "__main__":
    unittest.main()
