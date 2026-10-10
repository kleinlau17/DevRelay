import errno
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
from devrelay.git import _gitlink_paths, source_tree
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

    def _assert_source_snapshot_accepts_parent_filename(self, filename):
        self.write_filename_fixture(self.root, filename, b"delivered\n")
        self.git("add", "--", os.fsdecode(filename))
        self.git("commit", "-qm", "add filename fixture")
        index = self.root / ".git/index"
        original = index.read_bytes()
        self.assertEqual(source_tree(self.root), self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual(index.read_bytes(), original)

    def test_source_snapshot_accepts_non_utf8_parent_filenames(self):
        self._assert_source_snapshot_accepts_parent_filename(b"parent-\xff.txt")

    def test_source_snapshot_accepts_carriage_returns_in_parent_filenames(self):
        self._assert_source_snapshot_accepts_parent_filename(b"parent-\r.txt")

    def test_non_utf8_fixture_reports_eilseq_as_platform_skip(self):
        with patch.object(Path, "write_bytes", side_effect=OSError(errno.EILSEQ, "Illegal byte sequence")):
            with self.assertRaisesRegex(unittest.SkipTest, "EILSEQ.*end-to-end case unavailable"):
                self.write_filename_fixture(self.root, b"parent-\xff.txt", b"delivered\n")

    def test_filename_fixture_does_not_skip_permission_or_valid_utf8_errors(self):
        for filename, error_number in ((b"parent-\xff.txt", errno.EPERM),
                                       (b"parent-\xff.txt", errno.EACCES),
                                       (b"parent-\r.txt", errno.EILSEQ)):
            with self.subTest(filename=filename, errno=error_number):
                error = OSError(error_number, "fixture creation failed")
                with patch.object(Path, "write_bytes", side_effect=error):
                    with self.assertRaises(OSError) as raised:
                        self.write_filename_fixture(self.root, filename, b"delivered\n")
                self.assertIs(raised.exception, error)

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

    def _prepare(self, checks=None, *, initialize=True, gitmodules_ignore=True):
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(self.dependency_source), "vendor/dependency"], cwd=self.root, check=True)
        if gitmodules_ignore:
            subprocess.run(["git", "config", "-f", ".gitmodules",
                            "submodule.vendor/dependency.ignore", "all"], cwd=self.root, check=True)
        self.git("commit", "-qam", "add dependency")
        self.configure(checks=checks or [[sys.executable, "-c", "pass"]])
        add_local(self.config, self.state, "fix-1", "Fix dependency", "Update the dependency.\nDepends on: none",
                  "priority:P1")
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        worktree = Path(task["worktree"])
        dependency = worktree / "vendor/dependency"
        if initialize:
            subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init",
                            "--recursive"], cwd=worktree, check=True)
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

    def _install_submodule_replacement(self, dependency):
        original = self.git("rev-parse", "HEAD", cwd=dependency)
        (dependency / "dependency.txt").write_text("fixed\n")
        self.git("add", "dependency.txt", cwd=dependency)
        self.git("commit", "-qm", "replacement content", cwd=dependency)
        replacement = self.git("rev-parse", "HEAD", cwd=dependency)
        self.git("checkout", "--detach", "-q", original, cwd=dependency)
        self.git("replace", original, replacement, cwd=dependency)
        self.git("read-tree", replacement, cwd=dependency)
        (dependency / "dependency.txt").write_text("fixed\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=dependency), original)
        return original, replacement

    def test_replacement_refs_cannot_hide_undelivered_submodule_source(self):
        worktree, dependency = self._prepare([[sys.executable, "-c",
            "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').read_text() == 'fixed\\n'"]])
        original, replacement = self._install_submodule_replacement(dependency)
        self.assertEqual(self.git("status", "--porcelain", cwd=dependency), "")
        index = self._git_metadata_path(dependency, "index")
        original_index = index.read_bytes()
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual(index.read_bytes(), original_index)
        self.assertEqual(self.git("rev-parse", f"refs/replace/{original}", cwd=dependency), replacement)
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_replacement_refs_do_not_change_clean_submodule_evidence(self):
        worktree, dependency = self._prepare()
        before = source_tree(worktree)
        original, replacement = self._install_submodule_replacement(dependency)
        self.git("--no-replace-objects", "read-tree", original, cwd=dependency)
        (dependency / "dependency.txt").write_text("broken\n")
        self.assertEqual(source_tree(worktree), before)
        self.assertEqual(self.git("rev-parse", f"refs/replace/{original}", cwd=dependency), replacement)

    def test_replacement_refs_after_verification_block_submission(self):
        _, dependency = self._prepare()
        with self.state.lock():
            verify(self.config, self.state, "fix-1")
        original, replacement = self._install_submodule_replacement(dependency)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual(self.git("rev-parse", f"refs/replace/{original}", cwd=dependency), replacement)
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def _assert_source_snapshot_accepts_submodule_filename(self, filename):
        path = os.fsdecode(filename)
        self.write_filename_fixture(self.dependency_source, filename, b"delivered\n")
        self.git("add", "--", path, cwd=self.dependency_source)
        self.git("commit", "-qm", "add filename fixture", cwd=self.dependency_source)
        worktree, dependency = self._prepare()
        index = self._git_metadata_path(dependency, "index")
        original_index = index.read_bytes()
        self.assertEqual(source_tree(worktree), self.git("rev-parse", "HEAD^{tree}", cwd=worktree))
        self.assertEqual(index.read_bytes(), original_index)
        (dependency / path).write_bytes(b"undelivered\n")
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)

    def test_source_snapshot_accepts_non_utf8_submodule_filenames(self):
        self._assert_source_snapshot_accepts_submodule_filename(b"dependency-\xff.txt")

    def test_source_snapshot_accepts_carriage_returns_in_submodule_filenames(self):
        self._assert_source_snapshot_accepts_submodule_filename(b"dependency-\r.txt")

    def test_gitlink_index_paths_preserve_non_utf8_and_carriage_return_bytes(self):
        worktree, dependency = self._prepare()
        head = self.git("rev-parse", "HEAD", cwd=dependency)
        blob = self.git("rev-parse", "HEAD:value.txt", cwd=worktree)
        self.git("update-index", "--add", "--cacheinfo", "100644", blob,
                 os.fsdecode(b"parent-\xff.txt"), cwd=worktree)
        paths = [b"vendor/dependency-\xff", b"vendor/dependency-\r"]
        for path in paths:
            self.git("update-index", "--add", "--cacheinfo", "160000", head, os.fsdecode(path), cwd=worktree)
        self.assertCountEqual([os.fsencode(path) for path in _gitlink_paths(worktree)],
                              [b"vendor/dependency", *paths])

    def test_submodule_tree_byte_paths_are_inspected_without_checkout(self):
        worktree, dependency = self._prepare()
        head = self.git("rev-parse", "HEAD", cwd=dependency)
        blob = self.git("rev-parse", "HEAD:dependency.txt", cwd=dependency)
        link_paths = [b"child-\xff", b"child-\r"]
        entries = b"".join(b"100644 blob " + blob.encode("ascii") + b"\t" + path + b"\0"
                           for path in (b"dependency.txt", b"missing-\xff.txt", b"missing-\r.txt"))
        entries += b"".join(b"160000 commit " + head.encode("ascii") + b"\t" + path + b"\0"
                            for path in link_paths)
        tree = subprocess.check_output(["git", "mktree", "-z"], input=entries, cwd=dependency).strip().decode("ascii")
        commit = self.git("commit-tree", tree, "-p", head, "-m", "byte paths without checkout", cwd=dependency)
        self.git("update-ref", "HEAD", commit, cwd=dependency)
        self.git("read-tree", tree, cwd=dependency)
        self.assertCountEqual([os.fsencode(path) for path in _gitlink_paths(dependency)], link_paths)
        index = self._git_metadata_path(dependency, "index")
        original = index.read_bytes()
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual(index.read_bytes(), original)

    def _assert_index_mask_cannot_hide_tracked_submodule_change(self, flag):
        checks = [[sys.executable, "-c",
                   "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').read_text() == 'fixed\\n'"]]
        _, dependency = self._prepare(checks)
        self.git("update-index", flag, "dependency.txt", cwd=dependency)
        (dependency / "dependency.txt").write_text("fixed\n")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_assume_unchanged_cannot_hide_tracked_submodule_content(self):
        self._assert_index_mask_cannot_hide_tracked_submodule_change("--assume-unchanged")

    def test_skip_worktree_cannot_hide_tracked_submodule_content(self):
        self._assert_index_mask_cannot_hide_tracked_submodule_change("--skip-worktree")

    def _prepare_symlink_dependency(self, checks=None, *, link_target="dependency.txt"):
        (self.dependency_source / "link.txt").symlink_to(link_target)
        self.git("-c", "core.symlinks=true", "add", "link.txt", cwd=self.dependency_source)
        self.git("commit", "-qm", "add dependency symlink", cwd=self.dependency_source)
        worktree, dependency = self._prepare(checks)
        self.git("config", "core.symlinks", "false", cwd=dependency)
        self.assertTrue((dependency / "link.txt").is_symlink())
        return worktree, dependency

    def test_core_symlinks_false_cannot_hide_replacement_between_source_snapshots(self):
        worktree, dependency = self._prepare_symlink_dependency()
        source_tree(worktree)
        link = dependency / "link.txt"
        link.unlink()
        link.write_text("dependency.txt")
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(), "dependency.txt")

    def test_core_symlinks_false_cannot_hide_replacement_before_verification(self):
        _, dependency = self._prepare_symlink_dependency([[sys.executable, "-c",
            "from pathlib import Path; assert Path('vendor/dependency/link.txt').read_text() == 'dependency.txt'"]])
        link = dependency / "link.txt"
        link.unlink()
        link.write_text("dependency.txt")
        index = self._git_metadata_path(dependency, "index")
        original = index.read_bytes()
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(link.read_text(), "dependency.txt")

    def test_core_symlinks_false_cannot_hide_replacement_after_verification(self):
        _, dependency = self._prepare_symlink_dependency([[sys.executable, "-c",
            "from pathlib import Path; assert Path('vendor/dependency/link.txt').read_text() == 'broken\\n'"]])
        with self.state.lock():
            verify(self.config, self.state, "fix-1")
        link = dependency / "link.txt"
        link.unlink()
        link.write_text("dependency.txt")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual(link.read_text(), "dependency.txt")

    def test_submodule_symlink_targets_are_compared_as_raw_bytes(self):
        original_target = b"missing-\xff \n"
        worktree, dependency = self._prepare_symlink_dependency(link_target=os.fsdecode(original_target))
        source_tree(worktree)
        link = dependency / "link.txt"
        link.unlink()
        link.symlink_to(os.fsdecode(original_target.rstrip()))
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual(os.readlink(os.fsencode(link)), original_target.rstrip())

    def _install_masking_clean_filter(self, dependency, *, info_attributes=False):
        if info_attributes:
            attributes = Path(self.git("rev-parse", "--git-path", "info/attributes", cwd=dependency))
            if not attributes.is_absolute():
                attributes = dependency / attributes
            attributes.parent.mkdir(parents=True, exist_ok=True)
        else:
            attributes = Path(self.temporary.name) / "local-attributes"
            self.git("config", "core.attributesFile", str(attributes), cwd=dependency)
        attributes.write_text("dependency.txt filter=mask\n")
        self.git("config", "filter.mask.clean", "printf 'broken\\n'", cwd=dependency)
        self.git("config", "filter.mask.required", "true", cwd=dependency)

    def test_local_attributes_clean_filter_cannot_hide_source_during_verification(self):
        _, dependency = self._prepare([[sys.executable, "-c",
            "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').read_text() == 'fixed\\n'"]])
        self._install_masking_clean_filter(dependency)
        (dependency / "dependency.txt").write_text("fixed\n")
        self.assertEqual(self.git("hash-object", "--path=dependency.txt", "dependency.txt", cwd=dependency),
                         self.git("rev-parse", "HEAD:dependency.txt", cwd=dependency))
        index = Path(self.git("rev-parse", "--git-path", "index", cwd=dependency))
        if not index.is_absolute():
            index = dependency / index
        original_index = index.read_bytes()
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual(index.read_bytes(), original_index)
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_local_attributes_clean_filter_after_verification_blocks_submission(self):
        _, dependency = self._prepare()
        with self.state.lock():
            verify(self.config, self.state, "fix-1")
        self._install_masking_clean_filter(dependency)
        (dependency / "dependency.txt").write_text("fixed\n")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_info_attributes_clean_filter_cannot_hide_changes_between_source_snapshots(self):
        worktree, dependency = self._prepare()
        self._install_masking_clean_filter(dependency, info_attributes=True)
        source_tree(worktree)
        (dependency / "dependency.txt").write_text("fixed\n")
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_line_ending_conversion_cannot_hide_submodule_content(self):
        worktree, dependency = self._prepare()
        self.git("config", "core.autocrlf", "true", cwd=dependency)
        (dependency / "dependency.txt").write_bytes(b"broken\r\n")
        self.assertEqual(self.git("hash-object", "--path=dependency.txt", "dependency.txt", cwd=dependency),
                         self.git("rev-parse", "HEAD:dependency.txt", cwd=dependency))
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual((dependency / "dependency.txt").read_bytes(), b"broken\r\n")

    def test_filemode_configuration_cannot_hide_submodule_mode_change(self):
        checks = [[sys.executable, "-c",
                   "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').stat().st_mode & 0o111"]]
        _, dependency = self._prepare(checks)
        target = dependency / "dependency.txt"
        self.git("config", "core.fileMode", "false", cwd=dependency)
        target.chmod(0o755)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertTrue(target.stat().st_mode & 0o111)

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

    def _git_metadata_path(self, root, name):
        path = Path(self.git("rev-parse", "--git-path", name, cwd=root))
        return path if path.is_absolute() else root / path

    def test_info_exclude_cannot_hide_untracked_submodule_source_between_snapshots(self):
        worktree, dependency = self._prepare()
        source_tree(worktree)
        excluded = dependency / "repair.txt"
        excluded.write_text("fixed\n")
        exclude = self._git_metadata_path(dependency, "info/exclude")
        exclude.write_text("repair.txt\n")
        self.assertEqual(self.git("ls-files", "--others", "--exclude-standard", cwd=dependency), "")
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual(excluded.read_text(), "fixed\n")
        self.assertEqual(exclude.read_text(), "repair.txt\n")

    def test_info_exclude_cannot_hide_untracked_submodule_source_from_verification(self):
        _, dependency = self._prepare([[sys.executable, "-c",
            "from pathlib import Path; assert Path('vendor/dependency/repair.txt').read_text() == 'fixed\\n'"]])
        (dependency / "repair.txt").write_text("fixed\n")
        self._git_metadata_path(dependency, "info/exclude").write_text("repair.txt\n")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual((dependency / "repair.txt").read_text(), "fixed\n")

    def test_info_exclude_cannot_hide_untracked_nested_submodule_source(self):
        self._add_nested_submodule()
        worktree, dependency = self._prepare()
        nested = dependency / "nested/inner"
        (nested / "repair.txt").write_text("fixed\n")
        self._git_metadata_path(nested, "info/exclude").write_text("repair.txt\n")
        with self.assertRaisesRegex(RelayError, "vendor/dependency/nested/inner"):
            source_tree(worktree)
        self.assertEqual((nested / "repair.txt").read_text(), "fixed\n")

    def test_committed_gitignore_still_excludes_submodule_build_artifacts(self):
        (self.dependency_source / ".gitignore").write_text("build/\n")
        self.git("add", ".gitignore", cwd=self.dependency_source)
        self.git("commit", "-qm", "ignore build artifacts", cwd=self.dependency_source)
        worktree, dependency = self._prepare()
        before = source_tree(worktree)
        build = dependency / "build"
        build.mkdir()
        (build / "output.txt").write_text("generated\n")
        self.assertEqual(source_tree(worktree), before)
        self.assertEqual((build / "output.txt").read_text(), "generated\n")

    def test_source_snapshot_preserves_submodule_indexes_with_invalid_cache_trees(self):
        self._add_nested_submodule()
        worktree, dependency = self._prepare()
        indexes = []
        for checkout, filename in ((dependency, "dependency.txt"), (dependency / "nested/inner", "nested.txt")):
            object_id = self.git("rev-parse", f"HEAD:{filename}", cwd=checkout)
            self.git("update-index", "--cacheinfo", "100644", object_id, filename, cwd=checkout)
            index = self._git_metadata_path(checkout, "index")
            original = index.read_bytes()
            self.assertIn(b"\0-1 ", original)
            indexes.append((index, original))
        source_tree(worktree)
        for index, original in indexes:
            self.assertEqual(index.read_bytes(), original)

    def test_staged_submodule_changes_are_rejected_without_rewriting_the_index(self):
        worktree, dependency = self._prepare()
        (dependency / "staged.txt").write_text("undelivered\n")
        self.git("add", "staged.txt", cwd=dependency)
        (dependency / "staged.txt").unlink()
        index = self._git_metadata_path(dependency, "index")
        original = index.read_bytes()
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual(index.read_bytes(), original)

    def test_uninitialized_submodule_with_files_blocks_verification(self):
        checks = [[sys.executable, "-c",
                   "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').read_text() == 'fixed\\n'"]]
        worktree, dependency = self._prepare(checks, initialize=False)
        source_tree(worktree)
        dependency.mkdir(parents=True, exist_ok=True)
        (dependency / "dependency.txt").write_text("fixed\n")
        with self.state.lock(), self.assertRaisesRegex(
                RelayError, "not initialized.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

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

    def _assert_committed_submodule_head_is_delivered(self, *, gitmodules_ignore, local_ignore):
        worktree, dependency = self._prepare(
            [[sys.executable, "-c",
              "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').read_text() == 'fixed\\n'"]],
            gitmodules_ignore=gitmodules_ignore)
        (dependency / "dependency.txt").write_text("fixed\n")
        self.git("add", "dependency.txt", cwd=dependency)
        self.git("commit", "-qm", "fix dependency", cwd=dependency)
        submodule_head = self.git("rev-parse", "HEAD", cwd=dependency)
        self.git("push", "-q", "origin", "HEAD:main", cwd=dependency)
        if local_ignore:
            self.git("config", "submodule.vendor/dependency.ignore", "all", cwd=worktree)
        (worktree / ".gitignore").write_text("local.cache\n")
        (worktree / "local.cache").write_text("not source\n")
        with self.state.lock():
            verified = verify(self.config, self.state, "fix-1")
            committed = submit(self.config, self.state, "fix-1", local=True)
        self.assertTrue(verified["verification"]["passed"])
        self.assertEqual(committed["status"], "committed")
        self.assertIn(submodule_head, self.git("ls-tree", "HEAD", "vendor/dependency", cwd=worktree))
        self.assertEqual(self.git("ls-tree", "HEAD", "local.cache", cwd=worktree), "")
        self.assertEqual((worktree / "local.cache").read_text(), "not source\n")

    def test_committed_submodule_head_is_delivered_with_gitmodules_ignore(self):
        self._assert_committed_submodule_head_is_delivered(gitmodules_ignore=True, local_ignore=False)

    def test_committed_submodule_head_is_delivered_with_local_ignore(self):
        self._assert_committed_submodule_head_is_delivered(gitmodules_ignore=False, local_ignore=True)


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
