import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from helpers import RepositoryTest
from devrelay.cli import main
from devrelay.errors import RelayError
from devrelay.state import State
from devrelay import workspace as ws


class WorkspaceTest(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.second = (Path(self.temporary.name) / "second").resolve()
        subprocess.check_call(["git", "clone", "-q", str(self.root), str(self.second)])
        self.git("config", "user.name", "DevRelay Test", cwd=self.second)
        self.git("config", "user.email", "devrelay@example.invalid", cwd=self.second)
        self.manifest = Path(self.temporary.name) / "workspace.toml"
        self.requirements = Path(self.temporary.name) / "task.md"
        self.requirements.write_text("Fix both contract endpoints; preserve the existing controller work.\n")
        command = [sys.executable, "-c", "from pathlib import Path; assert Path('value.txt').read_text() == 'fixed\\n'"]
        joint = [sys.executable, "-c", "import os; from pathlib import Path; assert Path(os.environ['DEVRELAY_WORKTREE_BP'],'value.txt').read_text() == Path(os.environ['DEVRELAY_WORKTREE_PRO'],'value.txt').read_text()"]
        self.fake = self.fake_codex()
        with self.fake.open("a") as stream:
            # The existing fake executable exits only in review/error modes; implementation falls through.
            stream.write("\nif 'verdict' not in schema['properties'] and mode not in ('blocked',):\n    other = [args[i+1] for i,a in enumerate(args) if a == '--add-dir']\n    for path in other:\n        Path(path,'value.txt').write_text(Path('value.txt').read_text())\n")
        text = f'[codex]\nexecutable = {json.dumps(str(self.fake))}\ntimeout_seconds = 15\nmax_attempts = 2\n'
        for key, root in (("bp", self.root), ("pro", self.second)):
            text += f'\n[[repositories]]\nkey = "{key}"\npath = {json.dumps(str(root))}\n[repositories.project]\nprovider = "local"\nbase_branch = "main"\n[[repositories.checks]]\nid = "contract"\nargv = {json.dumps(command)}\ntimeout_seconds = 15\n'
        text += '\n[[checks]]\nid = "integration"\nargv = ' + json.dumps(joint) + '\ntimeout_seconds = 15\n'
        self.manifest.write_text(text)

    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["--workspace", str(self.manifest), *args])
        raw = output.getvalue() or errors.getvalue()
        return code, json.loads(raw)

    def add_task(self, identifier="fix"):
        code, task = self.invoke("task", "add", identifier, "--title", "Repair shared contract", "--file", str(self.requirements))
        self.assertEqual(code, 0, task)
        return task

    def started(self):
        self.add_task()
        code, task = self.invoke("start", "fix")
        self.assertEqual(code, 0, task)
        code, task = self.invoke("prepare", "fix")
        self.assertEqual(code, 0, task)
        return task

    def fixed(self):
        task = self.started()
        for member in task["members"].values():
            Path(member["worktree"], "value.txt").write_text("fixed\n")
        code, task = self.invoke("check", "fix")
        self.assertEqual(code, 0, task)
        return task

    def test_external_tools_joint_commit_preserve_controllers(self):
        (self.root / "unfinished.txt").write_text("Keep original work")
        task = self.fixed()
        self.assertEqual(len(task["verification"]["checks"]), 3)
        code, task = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 0, task)
        self.assertEqual(task["status"], "committed")
        for member in task["members"].values():
            self.assertEqual(self.git("show", "HEAD:value.txt", cwd=member["worktree"]), "fixed")
        self.assertEqual((self.root / "value.txt").read_text(), "broken\n")
        self.assertEqual((self.second / "value.txt").read_text(), "broken\n")
        self.assertEqual((self.root / "unfinished.txt").read_text(), "Keep original work")

    def test_auto_run_repairs_both_repositories_and_reviews(self):
        self.add_task()
        trace = Path(self.temporary.name) / "trace.jsonl"
        with patch.dict(os.environ, DEVRELAY_TEST_MODE="retry", DEVRELAY_TEST_TRACE=str(trace)):
            code, task = self.invoke("run", "fix")
        self.assertEqual(code, 0, task)
        self.assertEqual(task["attempts"], 2)
        self.assertEqual(task["status"], "reviewed")
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(len(events), 3)
        self.assertIn("resume", events[1]["argv"])
        self.assertIn("--add-dir", events[0]["argv"])
        self.assertIn("Review the complete task-relative working source", events[-1]["prompt"])
        self.assertIn("Workspace context", events[0]["prompt"])
        self.assertNotEqual(task["session_id"], task["review"]["session_id"])

    def test_second_member_change_invalidates_all_delivery(self):
        task = self.fixed()
        Path(task["members"]["pro"]["worktree"], "value.txt").write_text("different\n")
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("changed", report["error"])
        for member in task["members"].values():
            self.assertEqual(self.git("show", "HEAD:value.txt", cwd=member["worktree"]), "broken")

    def test_other_base_movement_invalidates_receipt(self):
        task = self.fixed()
        (self.second / "new.txt").write_text("base advanced")
        self.git("add", "new.txt", cwd=self.second)
        self.git("commit", "-qm", "base advanced", cwd=self.second)
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("base advanced", report["error"])

    def test_check_mutation_in_another_member_is_rejected(self):
        original = self.manifest.read_text()
        command = [sys.executable, "-c", "import os; from pathlib import Path; Path(os.environ['DEVRELAY_WORKTREE_PRO'],'changed.txt').write_text('mutation')"]
        self.manifest.write_text(original + '\n[[checks]]\nid = "mutation"\nargv = ' + json.dumps(command) + '\n')
        task = self.started()
        for member in task["members"].values():
            Path(member["worktree"], "value.txt").write_text("fixed\n")
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 1)
        self.assertIn("changed during", report["error"])

    def test_ownership_blocks_single_repository_and_second_bundle(self):
        task = self.started()
        member_state = State(self.root)
        self.addCleanup(member_state.close)
        with self.assertRaisesRegex(RelayError, "reserved"):
            with member_state.lock():
                pass
        self.add_task("other")
        code, report = self.invoke("start", "other")
        self.assertEqual(code, 1)
        self.assertIn("reserved", report["error"])
        code, task = self.invoke("task", "revoke", "fix")
        self.assertEqual(code, 0, task)
        with member_state.lock():
            pass
        self.assertTrue(Path(task["members"]["bp"]["worktree"]).exists())
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 1)
        self.assertIn("revoked", report["error"])

    def test_partial_start_recovers_without_recreating_first_member(self):
        self.add_task()
        real = ws.git
        def failure(root, *args, **kwargs):
            if root == self.second and args[:2] == ("worktree", "add"):
                raise RelayError("Second checkout failed")
            return real(root, *args, **kwargs)
        with patch("devrelay.workspace.git", side_effect=failure):
            code, report = self.invoke("start", "fix")
        self.assertEqual(code, 1)
        _, report = self.invoke("status")
        first = Path(report["tasks"][0]["members"]["bp"]["worktree"])
        (first / "keep.txt").write_text("keep")
        code, task = self.invoke("start", "fix")
        self.assertEqual(code, 0, task)
        self.assertEqual((first / "keep.txt").read_text(), "keep")

    def test_missing_setup_and_missing_joint_check_are_rejected(self):
        self.add_task()
        self.invoke("start", "fix")
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 1)
        self.assertIn("preparation", report["error"])
        self.manifest.write_text(self.manifest.read_text().split('\n[[checks]]')[0])
        code, report = self.invoke("doctor")
        self.assertEqual(code, 1)
        self.assertIn("integration check", report["error"])

    def test_changed_requirements_and_configuration_block(self):
        self.fixed()
        self.requirements.write_text("Different authorization")
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("requirements changed", report["error"])
        self.requirements.write_text("Fix both contract endpoints; preserve the existing controller work.\n")
        self.manifest.write_text(self.manifest.read_text().replace("max_attempts = 2", "max_attempts = 3"))
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 1)
        self.assertIn("configuration changed", report["error"])

    def test_tracking_parent_duplicate_repo_and_auto_merge_are_rejected(self):
        self.requirements.write_text("Task kind: plan\nImplement many unrelated tasks")
        code, report = self.invoke("task", "add", "plan", "--title", "Plan", "--file", str(self.requirements))
        self.assertEqual(code, 1)
        self.assertIn("Tracking", report["error"])
        original = self.manifest.read_text()
        self.manifest.write_text(original.replace(str(self.second), str(self.root)))
        code, report = self.invoke("doctor")
        self.assertEqual(code, 1)
        self.assertIn("Duplicate", report["error"])
        self.manifest.write_text(original + '\n[delivery]\nauto_merge = true\n')
        code, report = self.invoke("doctor")
        self.assertEqual(code, 1)
        self.assertIn("automatic merge", report["error"])

    def test_blocked_model_and_rejected_review_prevent_submission(self):
        self.add_task()
        with patch.dict(os.environ, DEVRELAY_TEST_MODE="blocked"):
            code, task = self.invoke("run", "fix")
        self.assertEqual(code, 1)
        self.assertEqual(task["status"], "blocked")
        with patch.dict(os.environ, DEVRELAY_TEST_MODE="pass", DEVRELAY_TEST_REVIEW="changes_requested"):
            code, report = self.invoke("resume", "fix")
        self.assertEqual(code, 1)
        self.assertIn("review requested changes", report["error"])
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("review is rejected", report["error"])

    def test_review_source_mutation_invalidates_delivery(self):
        self.fixed()
        with patch.dict(os.environ, DEVRELAY_TEST_REVIEW="modify"):
            code, report = self.invoke("review", "fix")
        self.assertEqual(code, 1)
        self.assertIn("changed", report["error"])

    def test_successful_commit_interruption_recovers_exact_source(self):
        task = self.fixed()
        original = ws.git
        interrupted = []
        def commit_then_interrupt(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args[0] == "commit" and not interrupted:
                interrupted.append(root)
                raise RelayError("Commit completed but receipt interrupted")
            return result
        with patch("devrelay.workspace.git", side_effect=commit_then_interrupt):
            code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("receipt interrupted", report["error"])
        head = self.git("rev-parse", "HEAD", cwd=task["members"]["bp"]["worktree"])
        code, task = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 0, task)
        self.assertEqual(task["members"]["bp"]["head_sha"], head)
        self.assertEqual(self.git("rev-list", "--count", "main..HEAD", cwd=task["members"]["bp"]["worktree"]), "1")

    def test_prepare_rejects_a_member_pr_that_was_merged_externally(self):
        task = self.fixed()
        space = ws.load(self.manifest)
        state = State(space.path.parent, directory=space.directory)
        self.addCleanup(state.close)
        task["members"]["bp"]["pr_url"] = "https://github.com/example/project/pull/1"
        with patch("devrelay.workspace.validate_pull", side_effect=RelayError("PR already merged")):
            with self.assertRaisesRegex(RelayError, "already merged"):
                ws.prepare(space, state, task)
        self.assertTrue(state.get("fix")["verification"]["passed"])

    def test_next_honors_priorities_and_stops_on_failed_work(self):
        self.add_task("later")
        code, task = self.invoke("task", "add", "urgent", "--title", "Urgent repair", "--file", str(self.requirements), "--priority", "priority:P0")
        self.assertEqual(code, 0, task)
        code, report = self.invoke("next")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["selected"]["id"], "urgent")
        self.invoke("start", "urgent")
        self.invoke("check", "urgent")  # Preparation is missing; fail without selecting another issue.
        code, report = self.invoke("next")
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "needs_attention")
        self.assertEqual(report["tasks"], ["urgent"])

    def test_base_advance_stops_without_repeated_implementation(self):
        self.add_task()
        calls = []
        def implement(config, state, task, directory, *args, **kwargs):
            calls.append(task["id"])
            for member in task["members"].values():
                Path(member["worktree"], "value.txt").write_text("fixed\n")
            (self.second / "latest.txt").write_text("new base")
            self.git("add", "latest.txt", cwd=self.second)
            self.git("commit", "-qm", "new base", cwd=self.second)
            return {"outcome": "ready", "summary": "Implemented", "blockers": []}
        with patch("devrelay.workspace.codex.implement", side_effect=implement):
            code, report = self.invoke("run", "fix")
        self.assertEqual(code, 1)
        self.assertIn("base advanced", report["error"])
        self.assertEqual(calls, ["fix"])

    def test_explicit_resume_retains_previous_blocker_feedback(self):
        self.add_task()
        with patch.dict(os.environ, DEVRELAY_TEST_MODE="blocked"):
            self.invoke("run", "fix")
        trace = Path(self.temporary.name) / "resume-trace.jsonl"
        with patch.dict(os.environ, DEVRELAY_TEST_MODE="pass", DEVRELAY_TEST_TRACE=str(trace)):
            code, task = self.invoke("resume", "fix")
        self.assertEqual(code, 0, task)
        first = json.loads(trace.read_text().splitlines()[0])
        self.assertIn("Provide dataset", first["prompt"])

    def test_remote_partial_delivery_retry_reuses_draft(self):
        task = self.fixed()
        # Member configuration remains local for checks; replace only immutable runtime objects for transport tests.
        space = ws.load(self.manifest)
        for config in space.members.values():
            config.data["project"].update(provider="github", repository="example/" + config.root.name)
        state = State(space.path.parent, directory=space.directory)
        self.addCleanup(state.close)
        task = state.get("fix")
        pulls, calls = {}, []
        def gh(config, *args, **kwargs):
            calls.append((config.root, args))
            if args[:2] == ("pr", "create"):
                if config.root == self.second and not pulls.get("first_failed"):
                    pulls["first_failed"] = True
                    raise RelayError("Second API publication failed")
                url = "https://github.com/" + config.repo + "/pull/1"
                pulls[config.root] = url
                return url
            return ""
        def gh_json(config, *args):
            if args[:2] == ("pr", "list"):
                if config.root not in pulls:
                    return []
                return [{"url": pulls[config.root], "state": "OPEN", "baseRefName": "main", "isDraft": True,
                         "headRefOid": task["members"]["bp" if config.root == self.root else "pro"]["head_sha"]}]
            member = task["members"]["bp" if config.root == self.root else "pro"]
            return {"state": "OPEN", "isDraft": True, "isCrossRepository": False, "baseRefName": "main",
                    "headRefName": member["branch"], "headRefOid": member["head_sha"]}
        @contextlib.contextmanager
        def transport(root, url):
            yield str(self.root), os.environ
        real = ws.git
        def git(root, *args, **kwargs):
            if args[0] == "push":
                return ""
            return real(root, *args, **kwargs)
        with patch("devrelay.workspace.assert_current"), patch("devrelay.workspace.base_commit", return_value=self.git("rev-parse", "HEAD")), patch("devrelay.workspace.validate_remote", return_value="https://github.com/example/project.git"), patch("devrelay.workspace.pinned_transport", side_effect=transport), patch("devrelay.workspace.git", side_effect=git), patch("devrelay.workspace.gh", side_effect=gh), patch("devrelay.workspace.gh_json", side_effect=gh_json):
            with ws.member_locks(space, task) as states:
                with self.assertRaisesRegex(RelayError, "Second API"):
                    ws.submit(space, state, task, states)
                self.assertIn("pr_url", state.get("fix")["members"]["bp"])
                result = ws.submit(space, state, task, states)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(sum(1 for root,args in calls if root == self.root and args[:2] == ("pr", "create")), 1)
        self.assertFalse(any(args[:2] == ("pr", "merge") for _,args in calls))
