import json
from pathlib import Path
import subprocess
import unittest

from devrelay.config import detect, initialize, load
from devrelay.errors import RelayError
from devrelay.git import git, github_slug, pinned_transport, validate_remote

from helpers import RepositoryTest


class ConfigurationTests(RepositoryTest):
    def test_init_preserves_instructions_and_ignore_rules(self):
        (self.root / "AGENTS.md").write_text("Existing project rules")
        before = (self.root / ".gitignore").read_text()
        result = initialize(self.root, "local")
        self.assertEqual(result["provider"], "local")
        self.assertEqual((self.root / "AGENTS.md").read_text(), "Existing project rules")
        self.assertTrue((self.root / ".gitignore").read_text().startswith(before))
        self.assertEqual(load(self.root).base, "main")
        self.assertEqual(load(self.root).data["delivery"], {"auto_review": False, "auto_merge": False,
                                                        "allow_no_ci": False, "required_checks": [],
                                                        "cleanup_branches": False, "cleanup_worktrees": False})
        original = (self.root / ".devrelay/config.toml").read_text()
        with self.assertRaisesRegex(RelayError, "already exists"):
            initialize(self.root)
        self.assertEqual((self.root / ".devrelay/config.toml").read_text(), original)

    def test_node_commands_follow_lockfile_and_existing_scripts(self):
        (self.root / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "build": "vite build"}}))
        (self.root / "pnpm-lock.yaml").write_text("lockfileVersion: 9")
        setup, checks, _ = detect(self.root)
        self.assertEqual(setup[0]["argv"], ["pnpm", "install", "--frozen-lockfile"])
        self.assertEqual([item["id"] for item in checks], ["test", "build"])

    def test_python_runner_is_not_guessed(self):
        (self.root / "pyproject.toml").write_text('[project]\nname="sample"\nversion="0.1.0"\n')
        (self.root / "tests").mkdir()
        setup, checks, notes = detect(self.root)
        self.assertEqual(setup[0]["argv"], ["python3", "-m", "venv", ".venv"])
        self.assertEqual(checks, [])
        self.assertTrue(any("actual runner" in note for note in notes))

    def test_github_ssh_and_https_detection(self):
        for remote in ("https://github.com/owner/repo.git", "git@github.com:owner/repo.git",
                       "ssh://git@github.com/owner/repo"):
            self.assertEqual(github_slug(remote), "owner/repo")
        self.assertEqual(github_slug("https://github.com.evil.test/owner/repo"), "")

    def test_detect_github_without_network(self):
        self.git("remote", "add", "origin", "git@github.com:example/project.git")
        initialize(self.root)
        config = load(self.root)
        self.assertEqual(config.provider, "github")
        self.assertEqual(config.repo, "example/project")

    def test_invalid_command_and_limits_fail_before_execution(self):
        self.configure()
        path = self.root / ".devrelay/config.toml"
        original = path.read_text()
        for change in (original.replace("max_attempts = 2", "max_attempts = 0"),
                       original.replace("timeout_seconds = 10", "timeout_seconds = true"),
                       original.replace("argv = [", "argv = 'shell command' # [")):
            with self.subTest(config=change):
                path.write_text(change)
                with self.assertRaises(RelayError):
                    load(self.root)

    def test_unknown_configuration_fields_are_rejected(self):
        self.configure()
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("max_attempts = 2", "max_attempt = 2"))
        with self.assertRaisesRegex(RelayError, "Unknown"):
            load(self.root)

    def test_symlinked_ignore_file_is_not_modified(self):
        original = self.root / ".gitignore"
        original.unlink()
        outside = self.root.parent / "outside-ignore"
        outside.write_text("preserve me")
        original.symlink_to(outside)
        with self.assertRaisesRegex(RelayError, "symlink"):
            initialize(self.root)
        self.assertEqual(outside.read_text(), "preserve me")
        self.assertFalse((self.root / ".devrelay").exists())

    def test_python_generated_artifacts_are_ignored(self):
        (self.root / "pyproject.toml").write_text('[project]\nname="sample"\nversion="0.1.0"\n')
        initialize(self.root, "local")
        for name in (".venv/lib.py", "sample.egg-info/PKG-INFO", "src/__pycache__/cached.pyc"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("runtime")
            self.assertEqual(self.git("check-ignore", name), name)

    def test_push_remote_must_match_the_configured_repository(self):
        self.configure(provider="github")
        self.git("remote", "add", "origin", "https://github.com/example/project.git")
        self.assertEqual(validate_remote(self.config), "https://github.com/example/project.git")
        self.git("remote", "set-url", "--push", "origin", "https://github.com/example/wrong-project.git")
        with self.assertRaisesRegex(RelayError, "Git push URL does not match project.repository"):
            validate_remote(self.config)
        self.git("remote", "set-url", "--push", "origin", "https://github.com/example/project.git")
        self.git("remote", "set-url", "origin", "https://github.com/example/wrong-project.git")
        with self.assertRaisesRegex(RelayError, "Git fetch URL does not match project.repository"):
            validate_remote(self.config)

    def test_remote_rejects_multiple_fetch_or_push_urls(self):
        self.configure(provider="github")
        self.git("remote", "add", "origin", "git@github.com:example/project.git")
        self.git("remote", "set-url", "--add", "--push", "origin",
                 "https://github.com/example/project.git")
        self.git("remote", "set-url", "--add", "--push", "origin",
                 "git@github.com:example/project.git")
        with self.assertRaisesRegex(RelayError, "exactly one.*push fan-out"):
            validate_remote(self.config)
        self.git("remote", "set-url", "--delete", "--push", "origin",
                 "git@github.com:example/project.git")
        self.git("remote", "set-url", "--add", "origin",
                 "https://github.com/example/project.git")
        with self.assertRaisesRegex(RelayError, "exactly one"):
            validate_remote(self.config)

    def test_remote_validates_resolved_url_rewrites(self):
        self.configure(provider="github")
        original = "https://github.com/example/project.git"
        self.git("remote", "add", "origin", original)
        self.git("config", "url.https://github.com/example/other.git.insteadOf", original)
        with self.assertRaisesRegex(RelayError, "URL rewrite"):
            validate_remote(self.config)

    def test_transport_pin_wins_over_a_later_url_rewrite(self):
        allowed = Path(self.temporary.name) / "pin-allowed.git"
        unapproved = Path(self.temporary.name) / "pin-unapproved.git"
        for remote in (allowed, unapproved):
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        self.git("config", f"url.{unapproved}.pushInsteadOf", str(allowed))
        with pinned_transport(self.root, str(allowed)) as env:
            git(self.root, "push", str(allowed), "refs/heads/main:refs/heads/pinned", env=env)
        self.assertEqual(self.git("--git-dir", str(allowed), "rev-parse", "refs/heads/pinned"),
                         self.git("rev-parse", "main"))
        refs = self.git("--git-dir", str(unapproved), "for-each-ref", "--format=%(refname)")
        self.assertNotIn("refs/heads/pinned", refs)

    def test_remote_validation_uses_the_transport_worktree_context(self):
        self.configure(provider="github")
        allowed = "https://github.com/example/project.git"
        unapproved = "https://github.com/other/unapproved.git"
        self.git("remote", "add", "origin", allowed)
        worktree = Path(self.temporary.name) / "task-context"
        self.git("worktree", "add", "-q", "-b", "devrelay/task-context", str(worktree), "main")
        included = Path(self.temporary.name) / "task-context.gitconfig"
        self.git("config", "--file", str(included), f"url.{unapproved}.insteadOf", allowed)
        self.git("config", "includeIf.onbranch:devrelay/task-*.path", str(included))

        self.assertEqual(validate_remote(self.config), allowed)
        self.assertEqual(self.git("ls-remote", "--get-url", allowed, cwd=worktree), unapproved)
        with self.assertRaisesRegex(RelayError, "URL rewrite"):
            validate_remote(self.config, worktree)

    def test_fetch_and_push_protocols_may_differ_for_the_same_repository(self):
        self.configure(provider="github")
        self.git("remote", "add", "origin", "git@github.com:example/project.git")
        self.git("remote", "set-url", "--push", "origin",
                 "https://github.com/example/project.git")
        self.assertEqual(validate_remote(self.config), "https://github.com/example/project.git")

    def test_delivery_configuration_is_strict_and_github_only(self):
        self.configure()
        path = self.root / ".devrelay/config.toml"
        original = path.read_text()
        for policy in ('auto_merge = "true"', 'auto_review = 1', 'allow_no_ci = "yes"',
                       'required_checks = ["test", "test"]', 'required_checks = [""]',
                       'required_checks = "test"', 'auto_merg = true', 'auto_merge = true',
                       'cleanup_worktrees = "true"', 'cleanup_branches = true',
                       'cleanup_worktrees = true', 'cleanup_worktrees = true\ncleanup_branches = true'):
            with self.subTest(policy=policy):
                path.write_text(original + "\n[delivery]\n" + policy + "\n")
                with self.assertRaises(RelayError):
                    load(self.root)


if __name__ == "__main__":
    unittest.main()
