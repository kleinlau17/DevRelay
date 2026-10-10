from contextlib import ExitStack
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from devrelay.errors import RelayError
from devrelay.runner import run_task, submit

from helpers import RepositoryTest


class GitHubDeliveryFixture(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure(provider="github", executable=self.fake_codex())
        self.remote = Path(self.temporary.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "origin", "main")
        self.issue = {"number": 12, "title": "Fix value", "body": "Fix the bounded behavior\nDepends on: none",
                      "state": "OPEN", "labels": [{"name": "auto:approved"}, {"name": "priority:P1"}],
                      "createdAt": "2026-10-09T00:00:00Z", "url": "https://github.com/example/project/issues/12"}
        self.created = False
        self.create_count = 0
        self.edit_count = 0
        self.timeout_after_create = False
        self.readback_head = None
        self.last_body = ""
        self.create_argv = ()
        stack = ExitStack()
        self.addCleanup(stack.close)
        # Git transport is real and confined to the temporary bare repository.
        # Mock only the GitHub control plane and its production hostname check.
        stack.enter_context(patch("devrelay.providers.validate_remote"))
        stack.enter_context(patch("devrelay.runner.validate_remote"))
        stack.enter_context(patch("devrelay.cleanup.validate_remote"))
        stack.enter_context(patch("devrelay.providers.gh_json", side_effect=self.inventory))
        stack.enter_context(patch("devrelay.runner.gh_json", side_effect=self.pull_read))
        stack.enter_context(patch("devrelay.runner.gh", side_effect=self.pull_write))

    def inventory(self, config, *argv):
        if argv[0] == "issue":
            return [self.issue]
        return ([{"state": "OPEN", "headRefName": "devrelay/task-12", "body": "Refs #12"}]
                if self.created else [])

    def pull_read(self, config, *argv):
        if argv[1] == "list":
            return [{"number": 1, "url": "https://github.com/example/project/pull/1"}] if self.created else []
        task = self.state.get("12")
        return {"url": "https://github.com/example/project/pull/1", "state": "OPEN",
                "headRefName": task["branch"], "headRefOid": self.readback_head or task["head_sha"], "baseRefName": "main"}

    def pull_write(self, config, *argv, input=None):
        self.last_body = input
        if argv[1] == "create":
            self.created = True
            self.create_count += 1
            self.create_argv = argv
            if self.timeout_after_create:
                raise RelayError("Request timed out after server created the PR")
            return "https://github.com/example/project/pull/1"
        self.edit_count += 1
        return ""

    def implement(self):
        with self.state.lock():
            return run_task(self.config, self.state, "12")

    def add_dependency_submodule(self):
        source = Path(self.temporary.name) / "delivery-dependency"
        source.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
        subprocess.run(["git", "config", "user.name", "DevRelay Test"], cwd=source, check=True)
        subprocess.run(["git", "config", "user.email", "devrelay@example.invalid"], cwd=source, check=True)
        (source / "dependency.txt").write_text("delivered\n")
        subprocess.run(["git", "add", "."], cwd=source, check=True)
        subprocess.run(["git", "commit", "-qm", "dependency"], cwd=source, check=True)
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(source), "vendor/dependency"], cwd=self.root, check=True)
        self.git("commit", "-qam", "add dependency")
        self.git("push", "-q", "origin", "main")


class GitHubDeliveryTests(GitHubDeliveryFixture):
    def test_verified_branch_is_pushed_and_draft_pr_is_read_back(self):
        self.implement()
        with self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertEqual(task["status"], "pr_open")
        pushed = subprocess.check_output(["git", "--git-dir", str(self.remote), "rev-parse", task["branch"]], text=True).strip()
        self.assertEqual(pushed, task["head_sha"])
        self.assertIn("--draft", self.create_argv)
        self.assertIn("Refs #12", self.last_body)
        self.assertIn(task["verification"]["source_tree"], self.last_body)

    def test_api_timeout_retry_reuses_the_commit_and_existing_pr(self):
        self.implement()
        self.timeout_after_create = True
        with self.state.lock(), self.assertRaisesRegex(RelayError, "timed out"):
            submit(self.config, self.state, "12")
        head = self.state.get("12")["head_sha"]
        with self.state.lock():
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(recovered["head_sha"], head)
        self.assertEqual(self.create_count, 1)
        self.assertEqual(self.edit_count, 1)
        self.assertEqual(recovered["status"], "pr_open")

    def test_mismatched_pr_head_is_rejected(self):
        self.implement()
        self.readback_head = "0" * 40
        with self.state.lock(), self.assertRaisesRegex(RelayError, "readback differs"):
            submit(self.config, self.state, "12")
        self.assertNotEqual(self.state.get("12")["status"], "pr_open")

    def test_withdrawn_issue_authorization_prevents_push(self):
        self.implement()
        self.issue["labels"] = [{"name": "priority:P1"}]
        with self.state.lock(), self.assertRaisesRegex(RelayError, "not eligible"):
            submit(self.config, self.state, "12")
        self.assertEqual(self.create_count, 0)
        refs = subprocess.check_output(["git", "--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)"] , text=True)
        self.assertNotIn("devrelay/task-12", refs)

    def test_pre_push_submodule_mutation_blocks_pr_creation(self):
        self.add_dependency_submodule()
        task = self.implement()
        worktree = Path(task["worktree"])
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init",
                        "--recursive"], cwd=worktree, check=True)
        hooks = Path(self.temporary.name) / "push-hooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        hook.write_text("#!/bin/sh\nprintf 'push mutation\\n' > vendor/dependency/from-push.txt\n")
        hook.chmod(0o755)
        self.git("config", "core.hooksPath", str(hooks), cwd=worktree)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree"):
            submit(self.config, self.state, "12")
        saved = self.state.get("12")
        pushed = subprocess.check_output(
            ["git", "--git-dir", str(self.remote), "rev-parse", saved["branch"]], text=True).strip()
        self.assertEqual(pushed, saved["head_sha"])
        self.assertEqual(self.create_count, 0)
        self.assertEqual((worktree / "vendor/dependency/from-push.txt").read_text(), "push mutation\n")


if __name__ == "__main__":
    unittest.main()
