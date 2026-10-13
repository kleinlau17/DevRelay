"""Recovered approvals must stay inside the task's own successful review run."""

import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from devrelay.config import load
from devrelay.delivery import review
from devrelay.errors import RelayError
from devrelay.runner import run_task
import test_worker_merge as worker_tests


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = worker_tests.AutoMergeWorkerTests(
            "test_completed_review_checkpoint_recovers_without_new_session")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.only_one()

    def checkpoint(self):
        f = self.fixture
        with patch("devrelay.delivery.advance", side_effect=lambda c, s, i: s.get(i)):
            run_task(f.config, f.state, "12", submit_after=True)
        with f.state.lock():
            task = review(f.config, f.state, "12")
        task.update(status="reviewing", review=None, error=None)
        f.state.save(task)
        return task, Path(task["review_run"])

    def reject(self, task, message="outside"):
        f = self.fixture
        before = f.trace_count()
        f.state.save({**task, "status": "reviewing", "review": None, "error": None})
        with f.state.lock(), self.assertRaisesRegex(RelayError, message):
            review(f.config, f.state, "12", recover=True)
        saved = f.state.get("12")
        self.assertEqual(saved["status"], "review_blocked")
        self.assertIsNone(saved["review"])
        self.assertEqual(f.trace_count(), before)
        self.assertEqual(f.merge_count, 0)

    def test_review_directory_symlink_cannot_accept_or_write_external_evidence(self):
        task, directory = self.checkpoint()
        outside = Path(self.fixture.temporary.name) / "unrelated-review"
        outside.mkdir()
        for name in ("events.jsonl", "events.jsonl.receipt.json", "result.json"):
            shutil.copy2(directory / name, outside / name)
        (outside / "result.json").write_text(json.dumps({
            "verdict": "approve", "summary": "Unrelated approval", "findings": []}))
        directory.rename(directory.with_name(directory.name + "-retained"))
        directory.symlink_to(outside, target_is_directory=True)
        with patch("devrelay.codex.read_review") as read:
            self.reject(task)
        read.assert_not_called()
        self.assertFalse((outside / "review.json").exists())

    def test_symlinked_runs_or_task_root_cannot_recover_review(self):
        task, directory = self.checkpoint()
        for root in (directory.parent, directory.parent.parent):
            with self.subTest(root=root):
                retained = root.with_name(root.name + "-retained")
                root.rename(retained)
                root.symlink_to(retained, target_is_directory=True)
                try:
                    with patch("devrelay.codex.read_review") as read:
                        self.reject(task)
                    read.assert_not_called()
                finally:
                    root.unlink()
                    retained.rename(root)

    def test_each_evidence_file_and_approval_output_rejects_symlinks(self):
        task, directory = self.checkpoint()
        for name in ("events.jsonl.receipt.json", "events.jsonl", "result.json", "review.json"):
            with self.subTest(name=name):
                file = directory / name
                outside = Path(self.fixture.temporary.name) / ("external-" + name)
                shutil.copy2(file, outside)
                original = file.read_bytes()
                file.unlink()
                file.symlink_to(outside)
                try:
                    self.reject(task)
                    self.assertEqual(outside.read_bytes(), original)
                finally:
                    file.unlink()
                    file.write_bytes(original)

    def test_other_tasks_review_directory_is_not_read(self):
        task, directory = self.checkpoint()
        other = directory.parent.parent / "another-task" / "saved-review"
        other.mkdir(parents=True)
        for name in ("events.jsonl", "events.jsonl.receipt.json", "result.json"):
            shutil.copy2(directory / name, other / name)
        task["review_run"] = str(other)
        with patch("devrelay.codex.read_review") as read:
            self.reject(task)
        read.assert_not_called()
        self.assertFalse((other / "review.json").exists())

    def test_process_receipt_must_match_this_worktree_log_and_command(self):
        task, directory = self.checkpoint()
        file = directory / "events.jsonl.receipt.json"
        original = file.read_bytes()
        for key, value in (("cwd", "/unrelated/worktree"), ("log", "/unrelated/events.jsonl"),
                           ("argv", ["unrelated-reviewer"]), ("returncode", False),
                           ("timed_out", "false")):
            with self.subTest(key=key):
                receipt = json.loads(original)
                receipt[key] = value
                file.write_text(json.dumps(receipt))
                with patch("devrelay.codex.read_review") as read:
                    self.reject(task, "no complete evidence")
                read.assert_not_called()
        file.write_bytes(original)

    def test_missing_evidence_blocks_without_repeating_review(self):
        task, directory = self.checkpoint()
        for name in ("events.jsonl.receipt.json", "events.jsonl", "result.json"):
            with self.subTest(name=name):
                file = directory / name
                retained = file.with_name(name + ".retained")
                file.rename(retained)
                try:
                    self.reject(task, "review")
                finally:
                    retained.rename(file)

    def test_valid_review_recovery_in_manual_mode_keeps_draft_and_avoids_model_call(self):
        f = self.fixture
        config_path = f.root / ".devrelay/config.toml"
        config_path.write_text(config_path.read_text().replace(
            "auto_merge = true", "auto_merge = false\nauto_review = true"))
        f.git("add", ".devrelay/config.toml")
        f.git("commit", "-qm", "manual review policy")
        f.git("push", "-q", "origin", "main")
        f.config = load(f.root)
        task, _ = self.checkpoint()
        before = f.trace_count()
        result = f.watching(max_tasks=1)
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(f.state.get(task["id"])["status"], "reviewed")
        self.assertTrue(f.remote_pulls[task["id"]]["isDraft"])
        self.assertEqual(f.trace_count(), before)
        self.assertEqual((f.ready_count, f.merge_count), (0, 0))
