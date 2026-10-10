"""Git operations that preserve the caller's working tree and index."""

import os
from pathlib import Path
import re
import subprocess
import tempfile

from .errors import RelayError


def command(argv, cwd, *, input=None, env=None, timeout=60):
    try:
        result = subprocess.run(argv, cwd=cwd, input=input, env=env, text=True,
                                capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RelayError(f"Cannot run {argv[0]}: {error}") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RelayError(f"{argv[0]} failed ({result.returncode}): {detail}")
    return result.stdout.strip()


def git(root, *argv, **kwargs):
    return command(["git", *argv], root, **kwargs)


def repository_root(path):
    return Path(git(Path(path).resolve(), "rev-parse", "--show-toplevel")).resolve()


def common_dir(root):
    path = Path(git(root, "rev-parse", "--git-common-dir"))
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def has_ref(root, ref):
    return subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                          cwd=root, capture_output=True).returncode == 0


def is_ancestor(root, commit, ref):
    return subprocess.run(["git", "merge-base", "--is-ancestor", commit, ref],
                          cwd=root, capture_output=True).returncode == 0


def source_tree(root):
    with tempfile.TemporaryDirectory(prefix="devrelay-index-") as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        git(root, "read-tree", "HEAD", env=env)
        git(root, "add", "-A", env=env)
        git(root, "diff", "--cached", "--check", env=env)
        return git(root, "write-tree", env=env)


def github_slug(url):
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?",
        url,
    )
    return f"{match[1]}/{match[2]}" if match else ""


def _config_values(root, key):
    try:
        result = subprocess.run(["git", "config", "--get-all", key], cwd=root, text=True,
                                capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RelayError(f"Cannot inspect Git configuration: {error}") from error
    if result.returncode == 1:
        return []
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RelayError(f"git failed ({result.returncode}): {detail}")
    return result.stdout.splitlines()


def validate_remote(config):
    if config.provider != "github":
        return None
    raw_fetch = _config_values(config.root, f"remote.{config.remote}.url")
    raw_push = _config_values(config.root, f"remote.{config.remote}.pushurl") or raw_fetch
    raw_urls = {"fetch": raw_fetch, "push": raw_push}
    urls = {}
    for kind, argv in (("fetch", ("remote", "get-url", "--all", config.remote)),
                       ("push", ("remote", "get-url", "--push", "--all", config.remote))):
        # `remote get-url` expands url.*.insteadOf and pushInsteadOf, so these
        # are the endpoints Git would actually use rather than just raw config.
        urls[kind] = git(config.root, *argv).splitlines()
    if any(len(items) != 1 for group in (raw_urls, urls) for items in group.values()):
        raise RelayError(
            "Git remote must resolve to exactly one fetch URL and one push URL; "
            "multiple URLs and push fan-out are not allowed"
        )
    if raw_urls != urls:
        raise RelayError(
            "Git URL rewrites are not allowed for the configured remote; "
            "use one explicit GitHub fetch URL and push URL"
        )
    for kind, items in urls.items():
        slug = github_slug(items[0])
        if slug.casefold() != config.repo.casefold():
            raise RelayError(
                f"Git {kind} URL does not resolve to project.repository; "
                "correct the remote or URL rewrite before proceeding"
            )
    return urls["push"][0]


def worktrees(root):
    result = []
    for record in git(root, "worktree", "list", "--porcelain").split("\n\n"):
        entry = {}
        for line in record.splitlines():
            key, _, value = line.partition(" ")
            entry[key] = value
        if entry:
            result.append(entry)
    return result
