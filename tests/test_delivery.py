from contextlib import ExitStack
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from devrelay.errors import RelayError
from devrelay.git import validate_remote as real_validate_remote
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
        stack.enter_context(patch("devrelay.runner.validate_remote", return_value=str(self.remote)))
        stack.enter_context(patch("devrelay.cleanup.validate_remote", return_value=str(self.remote)))
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


class GitHubDeliveryTests(GitHubDeliveryFixture):
    def test_verified_branch_is_pushed_and_draft_pr_is_read_back(self):
        self.implement()
        with self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertEqual(task["status"], "pr_open")
        pushed = subprocess.check_output(["git", "--git-dir", str(self.remote), "rev-parse", task["branch"]], text=True).strip()
        self.assertEqual(pushed, task["head_sha"])
        self.assertEqual(self.git("config", "--get", "branch." + task["branch"] + ".remote",
                                  cwd=task["worktree"]), "origin")
        self.assertEqual(self.git("config", "--get", "branch." + task["branch"] + ".merge",
                                  cwd=task["worktree"]), "refs/heads/" + task["branch"])
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/" + task["branch"]),
                         task["head_sha"])
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

    def test_restricted_fetch_refspec_does_not_break_branch_tracking(self):
        self.git("config", "--replace-all", "remote.origin.fetch",
                 "+refs/heads/main:refs/remotes/origin/main")
        self.implement()
        with self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertEqual(task["status"], "pr_open")
        self.assertEqual(self.git("config", "--get", "branch." + task["branch"] + ".remote",
                                  cwd=task["worktree"]), "origin")
        self.assertEqual(self.git("config", "--get", "branch." + task["branch"] + ".merge",
                                  cwd=task["worktree"]), "refs/heads/" + task["branch"])
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", task["branch"]),
                         task["head_sha"])

    def test_custom_fetch_refspec_updates_the_actual_upstream(self):
        self.git("config", "--replace-all", "remote.origin.fetch",
                 "+refs/heads/main:refs/remotes/origin/main")
        self.git("config", "--add", "remote.origin.fetch",
                 "+refs/heads/devrelay/*:refs/remotes/custom/*")
        self.implement()
        # An existing mapped ref must advance too, rather than leaving a stale upstream.
        self.git("update-ref", "refs/remotes/custom/task-12", self.git("rev-parse", "main"))
        with self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertEqual(task["status"], "pr_open")
        self.assertEqual(self.git("rev-parse", "@{upstream}", cwd=task["worktree"]), task["head_sha"])
        self.assertEqual(self.git("rev-parse", "--symbolic-full-name", "@{upstream}",
                                  cwd=task["worktree"]), "refs/remotes/custom/task-12")
        self.assertEqual(self.git("for-each-ref", "refs/remotes/origin/" + task["branch"]), "")

    def test_negative_fetch_refspec_does_not_suppress_pushed_upstream_update(self):
        self.git("config", "--replace-all", "remote.origin.fetch",
                 "+refs/heads/main:refs/remotes/origin/main")
        self.git("config", "--add", "remote.origin.fetch",
                 "+refs/heads/devrelay/*:refs/remotes/custom/*")
        self.git("config", "--add", "remote.origin.fetch", "^refs/heads/devrelay/*")
        self.implement()
        self.git("update-ref", "refs/remotes/custom/task-12", self.git("rev-parse", "main"))
        with self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertEqual(task["status"], "pr_open")
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", task["branch"]),
                         task["head_sha"])
        self.assertEqual(self.git("rev-parse", "@{upstream}", cwd=task["worktree"]), task["head_sha"])
        self.assertEqual(self.git("rev-parse", "--symbolic-full-name", "@{upstream}",
                                  cwd=task["worktree"]), "refs/remotes/custom/task-12")

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

    def test_multiple_real_push_targets_are_rejected_before_either_receives_the_branch(self):
        self.implement()
        extra = Path(self.temporary.name) / "extra.git"
        subprocess.run(["git", "init", "--bare", "-q", str(extra)], check=True)
        self.git("remote", "set-url", "--add", "--push", "origin", str(self.remote))
        self.git("remote", "set-url", "--add", "--push", "origin", str(extra))
        with (patch("devrelay.runner.validate_remote", side_effect=real_validate_remote),
              self.state.lock(), self.assertRaisesRegex(RelayError, "exactly one")):
            submit(self.config, self.state, "12")
        branch = "refs/heads/devrelay/task-12"
        for remote in (self.remote, extra):
            refs = subprocess.check_output(
                ["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"], text=True
            )
            self.assertNotIn(branch, refs)

    def test_worktree_conditional_rewrite_is_rejected_before_real_push(self):
        task = self.implement()
        extra = Path(self.temporary.name) / "conditional-extra.git"
        subprocess.run(["git", "init", "--bare", "-q", str(extra)], check=True)
        included = Path(self.temporary.name) / "task-transport.gitconfig"
        self.git("config", "--file", str(included), f"url.{extra}.insteadOf", str(self.remote))
        self.git("config", "includeIf.onbranch:devrelay/task-*.path", str(included))
        self.assertEqual(self.git("ls-remote", "--get-url", str(self.remote)), str(self.remote))
        self.assertEqual(self.git("ls-remote", "--get-url", str(self.remote), cwd=task["worktree"]),
                         str(extra))

        def validate_transport(config, root=None):
            return str(self.remote) if root is None else real_validate_remote(config, root)

        with (patch("devrelay.runner.validate_remote", side_effect=validate_transport),
              self.state.lock(), self.assertRaisesRegex(RelayError, "URL rewrite")):
            submit(self.config, self.state, "12")
        branch = "refs/heads/devrelay/task-12"
        for remote in (self.remote, extra):
            refs = subprocess.check_output(
                ["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"], text=True
            )
            self.assertNotIn(branch, refs)

    def test_rewrite_added_after_validation_cannot_redirect_real_push(self):
        self.implement()
        extra = Path(self.temporary.name) / "race-extra.git"
        subprocess.run(["git", "init", "--bare", "-q", str(extra)], check=True)
        changed = False

        def validate_then_rewrite(config, root=None):
            nonlocal changed
            if root is not None and not changed:
                changed = True
                self.git("config", f"url.{extra}.pushInsteadOf", str(self.remote))
            return str(self.remote)

        with patch("devrelay.runner.validate_remote", side_effect=validate_then_rewrite), self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertTrue(changed)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", task["branch"]),
                         task["head_sha"])
        refs = subprocess.check_output(
            ["git", "--git-dir", str(extra), "for-each-ref", "--format=%(refname)"], text=True
        )
        self.assertNotIn("refs/heads/" + task["branch"], refs)

    def test_url_named_remote_added_after_validation_cannot_fan_out_real_push(self):
        self.implement()
        extras = [Path(self.temporary.name) / f"submit-alias-extra-{index}.git" for index in range(2)]
        for extra in extras:
            subprocess.run(["git", "init", "--bare", "-q", str(extra)], check=True)
        url = self.remote.as_uri()
        changed = False

        def validate_then_alias(config, root=None):
            nonlocal changed
            if root is not None:
                changed = True
                self.git("config", f"remote.{url}.url", str(extras[0]))
                for extra in extras:
                    self.git("config", "--add", f"remote.{url}.pushurl", str(extra))
                return url
            return str(self.remote)

        with patch("devrelay.runner.validate_remote", side_effect=validate_then_alias), self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertTrue(changed)
        self.assertEqual(task["status"], "pr_open")
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", task["branch"]),
                         task["head_sha"])
        self.assertEqual(self.git("rev-parse", "@{upstream}", cwd=task["worktree"]), task["head_sha"])
        for extra in extras:
            self.assertEqual(self.git("--git-dir", str(extra), "for-each-ref"), "")


if __name__ == "__main__":
    unittest.main()
