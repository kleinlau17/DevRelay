import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
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

    def test_stderr_diagnostic_does_not_corrupt_events_or_exact_resume(self):
        trace = Path(self.temporary.name) / "stderr-trace.jsonl"
        with (patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "stderr-command-failure",
                                     "DEVRELAY_TEST_TRACE": str(trace)}),
              self.assertRaises(RelayError)):
            self.run_task()
        task = self.state.get("fix-1")
        events = Path(task["event_log"])
        diagnostics = events.with_suffix(events.suffix + ".stderr.log")
        self.assertEqual(task["session_id"], "exact-test-session")
        self.assertNotIn("ordinary CLI diagnostic", events.read_text())
        self.assertIn("ordinary CLI diagnostic", diagnostics.read_text())

        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocation = json.loads(trace.read_text().splitlines()[1])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(invocation["argv"][invocation["argv"].index("resume") + 1],
                         "exact-test-session")

    def test_chunked_unicode_agent_message_preserves_exact_resume(self):
        trace = Path(self.temporary.name) / "unicode-trace.jsonl"
        with (patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "unicode-chunks",
                                     "DEVRELAY_TEST_TRACE": str(trace)}),
              self.assertRaises(RelayError)):
            self.run_task()
        task = self.state.get("fix-1")
        self.assertEqual(task["session_id"], "exact-test-session")
        events = [json.loads(line) for line in Path(task["event_log"]).read_text().split("\n") if line]
        self.assertEqual(events[1]["item"]["text"], "first\u0085second\u2028third\u2029last")
        self.assertEqual(events[-1]["type"], "turn.failed")
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocation = json.loads(trace.read_text().split("\n")[1])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(invocation["argv"][invocation["argv"].index("resume") + 1], "exact-test-session")
        self.assertNotIn("--last", invocation["argv"])

    def test_upgrade_from_legacy_mixed_log_resumes_exact_session(self):
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        directory = self.state.directory / "runs" / task["id"] / "legacy-codex"
        directory.mkdir(parents=True)
        log = directory / "events.jsonl"
        log.write_text("ordinary CLI diagnostic\n" +
                       json.dumps({"type": "thread.started", "thread_id": "legacy-session"}) + "\n" +
                       "2026-01-01 WARN ordinary CLI diagnostic\n")
        task.update(status="interrupted", setup_fingerprint=self.config.fingerprint, session_id="legacy-session",
                    last_run=str(directory), event_log=str(log))
        self.state.save(task)
        trace = Path(self.temporary.name) / "upgrade-trace.jsonl"
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocation = json.loads(trace.read_text().split("\n")[0])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(invocation["argv"][invocation["argv"].index("resume") + 1], "legacy-session")
        self.assertNotIn("--last", invocation["argv"])
        marker = Path(result["event_log"]).with_suffix(".jsonl.format.json")
        self.assertEqual(json.loads(marker.read_text()), {"format": "stdout-jsonl-v1"})

    def test_foreign_truncated_message_does_not_block_exact_session_resume(self):
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        add_local(self.config, self.state, "fix-other", "Other", "Depends on: none", "priority:P1")
        other_directory = self.state.directory / "runs" / "fix-other" / "crashed-codex"
        other_directory.mkdir(parents=True)
        other_log = other_directory / "events.jsonl"
        other_log.write_text(json.dumps({"type": "thread.started", "thread_id": "other-session"}) + "\n" +
                             '{"type":"item.completed","item":')
        other = self.state.get("fix-other")
        other.update(status="interrupted", last_run=str(other_directory), event_log=str(other_log))
        self.state.save(other)
        current = self.state.directory / "runs" / task["id"] / "current-codex"
        current.mkdir(parents=True)
        log = current / "events.jsonl"
        log.write_text(json.dumps({"type": "thread.started", "thread_id": "current-session"}) + "\n")
        task.update(status="interrupted", setup_fingerprint=self.config.fingerprint, session_id="current-session",
                    last_run=str(current), event_log=str(log))
        self.state.save(task)
        trace = Path(self.temporary.name) / "two-task-trace.jsonl"
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocation = json.loads(trace.read_text().split("\n")[0])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(invocation["argv"][invocation["argv"].index("resume") + 1], "current-session")
        self.assertNotIn("--last", invocation["argv"])
        self.assertIsNone(self.state.get("fix-other").get("session_id"))
        task = self.state.get("fix-1")
        task["session_id"] = "other-session"
        self.state.save(task)
        with (patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}),
              self.assertRaisesRegex(RelayError, "another task's implementation log")):
            self.run_task(resume=True)
        self.assertEqual(len(trace.read_text().split("\n")) - 1, 1, "Foreign session must not reach Codex")

    def test_interrupt_after_thread_started_resumes_the_exact_session(self):
        trace = Path(self.temporary.name) / "interrupt-trace.jsonl"
        with (patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "interrupt", "DEVRELAY_TEST_TRACE": str(trace)}),
              self.assertRaises(KeyboardInterrupt)):
            self.run_task()
        task = self.state.get("fix-1")
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["session_id"], "exact-test-session")
        self.assertEqual(task["event_log"], str(Path(task["last_run"]) / "events.jsonl"))

        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocations = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(result["status"], "verified")
        self.assertEqual(invocations[1]["argv"][invocations[1]["argv"].index("resume") + 1],
                         "exact-test-session")
        self.assertNotIn("--last", invocations[1]["argv"])

    def test_controller_crash_log_recovers_session_before_new_codex_run(self):
        trace = Path(self.temporary.name) / "crash-trace.jsonl"
        source = Path(__file__).resolve().parents[1] / "src"
        command = [sys.executable, "-c", "from devrelay.cli import main; raise SystemExit(main())",
                   "--project", str(self.root)]
        environment = {**os.environ, "PYTHONPATH": str(source), "DEVRELAY_TEST_TRACE": str(trace)}
        environment.pop("DEVRELAY_TEST_MODE", None)
        controller = subprocess.Popen([*command, "run", "fix-1"], env={**environment,
                                      "DEVRELAY_TEST_MODE": "controller-crash"},
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        group = None
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                task = self.state.get("fix-1")
                active = self.state.metadata("active_process")
                if task and task.get("last_run") and task.get("event_log") and active:
                    events = Path(task["event_log"])
                    if events.exists() and '"thread_id": "started-session"' in events.read_text():
                        group = active["group"]
                        break
                if controller.poll() is not None:
                    output, error = controller.communicate()
                    self.fail(f"controller exited before crash boundary: {output} {error}")
                time.sleep(0.01)
            else:
                self.fail("controller did not persist its run index and first thread event")

            self.assertEqual(task["event_log"], str(Path(task["last_run"]) / "events.jsonl"))
            self.assertIsNone(task.get("session_id"))
            controller.kill()
            controller.communicate(timeout=5)
            self.assertEqual(controller.returncode, -signal.SIGKILL)

            blocked = subprocess.run([*command, "resume", "fix-1"], env=environment,
                                     capture_output=True, text=True, timeout=5)
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("still runs in process group", blocked.stderr)
            self.assertEqual(len(trace.read_text().splitlines()), 1)

            os.killpg(group, signal.SIGKILL)
            time.sleep(0.1)
            resumed = subprocess.run([*command, "resume", "fix-1"], env=environment,
                                     capture_output=True, text=True, timeout=15)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            invocations = [json.loads(line) for line in trace.read_text().splitlines()]
            argv = invocations[1]["argv"]
            self.assertEqual(argv[argv.index("resume") + 1], "started-session")
            self.assertNotIn("--last", argv)
            self.assertEqual(self.state.get("fix-1")["status"], "verified")
        finally:
            if controller.poll() is None:
                controller.kill()
                controller.communicate(timeout=5)
            active = self.state.metadata("active_process")
            for candidate in {group, active.get("group") if active else None} - {None}:
                try:
                    os.killpg(candidate, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_incomplete_or_conflicting_recovery_events_fail_closed(self):
        cases = [
            ("incomplete", '{"type":"thread.started","thread_id":'),
            ("conflicting", '\n'.join((
                json.dumps({"type": "thread.started", "thread_id": "session-one"}),
                json.dumps({"type": "thread.started", "thread_id": "session-two"})))),
        ]
        for name, content in cases:
            with self.subTest(name=name):
                identifier = "fix-" + name
                add_local(self.config, self.state, identifier, "Recovery", "Depends on: none", "priority:P1")
                with self.state.lock():
                    task = start(self.config, self.state, identifier)
                task["setup_fingerprint"] = self.config.fingerprint
                task["status"] = "implementing"
                directory = self.state.directory / "runs" / identifier / "crashed-codex"
                directory.mkdir(parents=True)
                (directory / "events.jsonl").write_text(content)
                task["last_run"] = str(directory)
                self.state.save(task)
                with self.assertRaisesRegex(RelayError, "incomplete|different session"):
                    self.run_task(identifier, resume=True)

    def test_empty_prelaunch_log_can_be_retried_as_a_new_session(self):
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        task["setup_fingerprint"] = self.config.fingerprint
        task["status"] = "implementing"
        directory = self.state.directory / "runs" / task["id"] / "prelaunch-codex"
        directory.mkdir(parents=True)
        (directory / "events.jsonl").touch()
        task["last_run"] = str(directory)
        self.state.save(task)
        trace = Path(self.temporary.name) / "prelaunch-trace.jsonl"

        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocation = json.loads(trace.read_text().splitlines()[0])
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("resume", invocation["argv"])

    def test_recovery_never_reuses_another_tasks_session(self):
        add_local(self.config, self.state, "fix-other", "Other", "Depends on: none", "priority:P1")
        other = self.state.get("fix-other")
        other["session_id"] = "other-task-session"
        self.state.save(other)
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        task["setup_fingerprint"] = self.config.fingerprint
        task["status"] = "implementing"
        directory = self.state.directory / "runs" / task["id"] / "crashed-codex"
        directory.mkdir(parents=True)
        (directory / "events.jsonl").write_text(
            json.dumps({"type": "thread.started", "thread_id": "other-task-session"}) + "\n")
        task["last_run"] = str(directory)
        self.state.save(task)

        with self.assertRaisesRegex(RelayError, "another task"):
            self.run_task(resume=True)

    def test_cached_session_is_validated_before_cli_launch_without_thread_started(self):
        add_local(self.config, self.state, "fix-other", "Other", "Depends on: none", "priority:P1")
        other = self.state.get("fix-other")
        other["session_id"] = "other-task-session"
        self.state.save(other)
        with self.state.lock():
            original = start(self.config, self.state, "fix-1")
        original["setup_fingerprint"] = self.config.fingerprint
        trace = Path(self.temporary.name) / "rejected-session-trace.jsonl"
        log_cases = ("no-run", "missing", "empty", "turn-only")
        sessions = ("other-task-session", "--last", "", 0, ["session"])
        for log_case in log_cases:
            for index, session in enumerate(sessions):
                with self.subTest(log=log_case, session=session):
                    task = {**original, "status": "interrupted", "session_id": session}
                    directory = self.state.directory / "runs" / task["id"] / f"{log_case}-{index}"
                    if log_case != "no-run":
                        directory.mkdir(parents=True)
                        log = directory / "events.jsonl"
                        if log_case == "empty":
                            log.touch()
                        elif log_case == "turn-only":
                            log.write_text(json.dumps({"type": "turn.started"}) + "\n")
                        task.update(last_run=str(directory), event_log=str(log))
                    self.state.save(task)
                    reason = "another task" if session == "other-task-session" else "session ID is invalid"
                    with (patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}),
                          self.assertRaisesRegex(RelayError, reason)):
                        self.run_task(resume=True)
                    self.assertFalse(trace.exists(), "Codex must not receive a rejected session's prompt")
                    saved = self.state.get(task["id"])
                    self.assertEqual(saved.get("last_run"), task.get("last_run"))
                    self.assertEqual(saved.get("event_log"), task.get("event_log"))

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

    def test_streaming_delivers_large_input_without_a_writer_thread(self):
        seen = []
        payload = "x" * 2_000_000
        script = "import sys; data = sys.stdin.buffer.read(); print(len(data), flush=True)"
        with self.state.lock():
            receipt = execute(self.state, [sys.executable, "-c", script], self.root,
                              self.state.directory / "large-input.log", 5, input=payload,
                              on_output_line=seen.append)
        self.assertEqual(seen, [str(len(payload))])
        self.assertEqual(receipt["returncode"], 0)
        self.assertEqual(Path(receipt["stderr_log"]).read_text(), "")

    def test_streaming_large_input_does_not_wait_on_descendant_held_stdin(self):
        group_file = Path(self.temporary.name) / "held-stdin-group"
        script = """import os, sys, time
path = sys.argv[1]
with open(path, 'w') as stream:
    stream.write(str(os.getpgrp()))
    stream.flush()
if os.fork() == 0:
    time.sleep(30)
    os._exit(0)
os._exit(0)
"""
        started = time.monotonic()
        group = None
        try:
            with self.state.lock(), self.assertRaisesRegex(RelayError, "live descendants"):
                execute(self.state, [sys.executable, "-c", script, str(group_file)], self.root,
                        self.state.directory / "held-stdin.log", 1, input="x" * 2_000_000,
                        on_output_line=lambda line: None)
            self.assertLess(time.monotonic() - started, 3)
            group = int(group_file.read_text())
            self.assertEqual(self.state.metadata("active_process")["group"], group)
        finally:
            if group is None and group_file.exists():
                group = int(group_file.read_text())
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.killpg(group, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail(f"process group {group} survived test cleanup")

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
