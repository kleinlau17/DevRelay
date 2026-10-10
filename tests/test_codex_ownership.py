import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from devrelay.codex import _observe_line, _recover_previous_run, build_command
from devrelay.errors import RelayError
from devrelay.providers import add_local
from devrelay.runner import run_task, start

from helpers import RepositoryTest


class CodexOwnershipTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure(executable=self.fake_codex())
        for identifier in ("fix-1", "fix-other"):
            add_local(self.config, self.state, identifier, "Recovery", "Depends on: none", "priority:P1")
        with self.state.lock():
            self.task = start(self.config, self.state, "fix-1")
        self.task.update(status="interrupted", setup_fingerprint=self.config.fingerprint)
        self.state.save(self.task)
        self.trace = Path(self.temporary.name) / "rejected-review-trace.jsonl"

    def retain_review(self, owner, source):
        session = owner["id"] + "-" + source + "-review-session"
        if source == "current":
            owner["review"] = {"session_id": session, "verdict": "approve"}
        else:
            directory = self.state.directory / "runs" / owner["id"] / "old-review"
            directory.mkdir(parents=True)
            if source == "event-log":
                (directory / "events.jsonl").write_text(
                    "ordinary CLI diagnostic\n" +
                    json.dumps({"type": "thread.started", "thread_id": session}) + "\n")
            else:
                (directory / "review.json").write_text(json.dumps({"session_id": session, "verdict": "approve"}))
            owner.update(review=None, verification=None, status="review_blocked")
            if source == "receipt-path":
                owner["review_path"] = str(directory / "review.json")
            elif source == "run-path":
                owner["review_run"] = str(directory)
            else:
                # A later review replaces both pointers. Earlier ownership must
                # survive in its retained receipt or interrupted review log.
                latest = directory.parent / "latest-review"
                latest.mkdir()
                (latest / "review.json").write_text(json.dumps({"session_id": owner["id"] + "-latest-session"}))
                owner.update(review_run=str(latest), review_path=str(latest / "review.json"))
        self.state.save(owner)
        return session

    def assert_resume_rejected(self, reason):
        with (patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(self.trace)}),
              self.state.lock(), self.assertRaisesRegex(RelayError, reason)):
            run_task(self.config, self.state, self.task["id"], resume=True)
        self.assertFalse(self.trace.exists(), "Codex must not receive a review session's implementation prompt")

    def test_foreign_cached_review_session_is_rejected_before_cli_launch(self):
        session = self.retain_review(self.state.get("fix-other"), "current")
        self.task["session_id"] = session
        self.state.save(self.task)
        self.assert_resume_rejected("another task's review")

    def test_foreign_review_thread_in_previous_log_is_rejected_before_cli_launch(self):
        session = self.retain_review(self.state.get("fix-other"), "current")
        directory = self.state.directory / "runs" / self.task["id"] / "crashed-codex"
        directory.mkdir(parents=True)
        (directory / "events.jsonl").write_text(json.dumps({"type": "thread.started", "thread_id": session}) + "\n")
        self.task.update(last_run=str(directory), event_log=str(directory / "events.jsonl"))
        self.state.save(self.task)
        self.assert_resume_rejected("another task's review")
        self.assertIsNone(self.state.get(self.task["id"]).get("session_id"))

    def test_live_review_thread_is_rejected_without_checkpointing(self):
        session = self.retain_review(self.state.get("fix-other"), "current")
        with self.assertRaisesRegex(RelayError, "another task's review"):
            _observe_line(self.state, self.task, json.dumps({"type": "thread.started", "thread_id": session}),
                          self.state.directory / "events.jsonl")
        self.assertNotIn("session_id", self.task)
        self.assertIsNone(self.state.get(self.task["id"]).get("session_id"))

    def test_implementation_session_remains_usable_alongside_review_records(self):
        self.retain_review(self.task, "current")
        self.retain_review(self.state.get("fix-other"), "old-receipt")
        self.task["session_id"] = "implementation-session"
        self.state.save(self.task)
        _recover_previous_run(self.state, self.task)
        argv = build_command(self.config, self.state.directory / "next-codex", self.task["session_id"])
        self.assertEqual(argv[argv.index("resume") + 1], "implementation-session")
        self.assertNotIn("--last", argv)

    def test_review_ownership_survives_invalidation_and_pointer_replacement(self):
        for source in ("receipt-path", "run-path", "old-receipt", "event-log"):
            with self.subTest(source=source):
                # Separate owners avoid overlapping retained fixtures.
                identifier = "owner-" + source
                add_local(self.config, self.state, identifier, "Review", "Depends on: none", "priority:P1")
                session = self.retain_review(self.state.get(identifier), source)
                self.task["session_id"] = session
                self.state.save(self.task)
                self.assert_resume_rejected("another task's review")
                # Remove the cached ID to exercise checkpoint ownership too.
                self.task.pop("session_id")
                self.state.save(self.task)
                with self.assertRaisesRegex(RelayError, "another task's review"):
                    _observe_line(self.state, self.task,
                                  json.dumps({"type": "thread.started", "thread_id": session}),
                                  self.state.directory / "events.jsonl")
                self.assertIsNone(self.state.get(self.task["id"]).get("session_id"))

    def test_own_review_session_cannot_replace_implementation_session(self):
        for source in ("current", "receipt-path", "old-receipt", "event-log"):
            with self.subTest(source=source):
                identifier = "own-" + source
                add_local(self.config, self.state, identifier, "Recovery", "Depends on: none", "priority:P1")
                with self.state.lock():
                    self.task = start(self.config, self.state, identifier)
                session = self.retain_review(self.task, source)
                self.task.update(session_id=session, status="interrupted", setup_fingerprint=self.config.fingerprint)
                self.state.save(self.task)
                self.assert_resume_rejected("belongs to a review")
                self.task.pop("session_id")
                self.state.save(self.task)
                with self.assertRaisesRegex(RelayError, "belongs to a review"):
                    _observe_line(self.state, self.task,
                                  json.dumps({"type": "thread.started", "thread_id": session}),
                                  self.state.directory / "events.jsonl")

    def test_review_evidence_outside_owned_runs_is_not_read(self):
        outside = Path(self.temporary.name) / "outside-review"
        outside.mkdir()
        (outside / "review.json").write_text(json.dumps({"session_id": "outside-session"}))
        other = self.state.get("fix-other")
        other.update(review=None, review_path=str(outside / "review.json"))
        self.state.save(other)
        self.task["session_id"] = "implementation-session"
        self.state.save(self.task)
        self.assert_resume_rejected("outside this task's run directory")

    def test_review_receipt_symlink_outside_owned_run_blocks_recovery(self):
        outside = Path(self.temporary.name) / "outside-receipt.json"
        outside.write_text(json.dumps({"session_id": "outside-session"}))
        directory = self.state.directory / "runs" / "fix-other" / "old-review"
        directory.mkdir(parents=True)
        (directory / "review.json").symlink_to(outside)
        self.task["session_id"] = "implementation-session"
        self.state.save(self.task)
        self.assert_resume_rejected("outside its run directory")

    def test_runs_symlink_does_not_widen_review_evidence_scope(self):
        outside = Path(self.temporary.name) / "outside-runs"
        directory = outside / "fix-other" / "old-review"
        directory.mkdir(parents=True)
        (directory / "review.json").write_text(json.dumps({"session_id": "outside-session"}))
        runs = self.state.directory / "runs"
        self.assertFalse(runs.exists())
        runs.symlink_to(outside)
        self.task["session_id"] = "implementation-session"
        self.state.save(self.task)
        self.assert_resume_rejected("outside the owned run directory")

    def test_corrupted_retained_review_receipt_blocks_recovery(self):
        directory = self.state.directory / "runs" / "fix-other" / "old-review"
        directory.mkdir(parents=True)
        (directory / "review.json").write_text('{"session_id":')
        self.task["session_id"] = "implementation-session"
        self.state.save(self.task)
        self.assert_resume_rejected("Cannot read saved Codex review evidence")


if __name__ == "__main__":
    unittest.main()
