import json
import os
from pathlib import Path
from unittest.mock import patch

from devrelay.codex import EVENT_LOG_FORMAT, _recover_previous_run, build_command, implement
from devrelay.errors import RelayError

from helpers import RepositoryTest


class LegacyCodexLogTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure(executable=self.fake_codex())
        self.directory = self.state.directory / "runs" / "upgrade" / "legacy-codex"
        self.directory.mkdir(parents=True)
        self.log = self.directory / "events.jsonl"
        self.task = {"id": "upgrade", "status": "interrupted", "title": "Upgrade recovery", "body": "Depends on: none",
                     "worktree": str(self.root), "last_run": str(self.directory), "event_log": str(self.log)}
        self.trace = Path(self.temporary.name) / "upgrade-trace.jsonl"

    def test_mixed_legacy_log_recovers_exact_session_with_or_without_checkpoint(self):
        for saved in (False, True):
            with self.subTest(saved=saved):
                task = dict(self.task)
                if saved:
                    task["session_id"] = "legacy-session"
                self.state.save(task)
                self.log.write_text("2026-01-01 WARN ordinary CLI diagnostic\n" +
                                    json.dumps({"type": "thread.started", "thread_id": "legacy-session"}) + "\n" +
                                    "ordinary CLI diagnostic\n" + json.dumps({"type": "turn.failed"}) + "\n")
                _recover_previous_run(self.state, task)
                self.assertEqual(self.state.get(task["id"])["session_id"], "legacy-session")
                argv = build_command(self.config, self.directory, task["session_id"])
                self.assertEqual(argv[argv.index("resume") + 1], "legacy-session")
                self.assertNotIn("--last", argv)

    def test_legacy_diagnostics_do_not_hide_malformed_or_conflicting_events(self):
        cases = (
            ('{"type":"thread.started","thread_id":', "incomplete or invalid"),
            ('{"type":"item.completed",', "incomplete or invalid"),
            ('"type":"thread.started","thread_id":"legacy-session"', "incomplete or invalid"),
            (json.dumps({"type": "thread.started", "thread_id": "--last"}), "invalid session ID"),
            (json.dumps({"type": "thread.started", "thread_id": "different-session"}), "different session"),
            ("null", "invalid event"),
        )
        for index, (event, reason) in enumerate(cases):
            with self.subTest(event=event):
                task = {**self.task, "session_id": "legacy-session"}
                self.state.save(task)
                self.log.write_text("ordinary CLI diagnostic\n" + event + "\n")
                destination = self.directory.parent / f"rejected-{index}-codex"
                with (patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(self.trace)}),
                      self.state.lock(), self.assertRaisesRegex(RelayError, reason)):
                    implement(self.config, self.state, task, destination)
                self.assertFalse(self.trace.exists(), "Malformed legacy events must block CLI launch")
                self.assertFalse(destination.exists())

    def test_diagnostics_only_legacy_run_cannot_silently_start_a_new_session(self):
        self.log.write_text("ordinary CLI diagnostic\n")
        self.state.save(self.task)
        with self.assertRaisesRegex(RelayError, "no valid thread.started"):
            _recover_previous_run(self.state, self.task)

    def test_separated_stdout_remains_strict_during_recovery(self):
        for evidence in ("format", "stderr"):
            with self.subTest(evidence=evidence):
                self.log.write_text("ordinary CLI diagnostic\n" +
                                    json.dumps({"type": "thread.started", "thread_id": "legacy-session"}) + "\n")
                if evidence == "format":
                    marker = self.log.with_suffix(self.log.suffix + ".format.json")
                    marker.write_text(json.dumps({"format": EVENT_LOG_FORMAT}))
                else:
                    marker = self.log.with_suffix(self.log.suffix + ".stderr.log")
                    marker.touch()
                self.state.save(self.task)
                try:
                    with self.assertRaisesRegex(RelayError, "incomplete or invalid"):
                        _recover_previous_run(self.state, dict(self.task))
                finally:
                    marker.unlink()
