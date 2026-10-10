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


def _gitlink_paths(root, *, env=None):
    paths = []
    for entry in git(root, "ls-files", "--stage", "-z", env=env).split("\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition("\t")
        fields = metadata.split()
        if separator and len(fields) == 3 and fields[0] == "160000" and fields[2] == "0":
            paths.append(path)
    return paths


def _submodule_checkout(root, path):
    candidate = root / path
    if candidate.is_symlink():
        return None
    checkout = candidate.resolve()
    if not checkout.is_dir() or not (checkout / ".git").exists():
        return None
    return checkout if Path(git(checkout, "rev-parse", "--show-toplevel")).resolve() == checkout else None


def _skipped_submodule_has_content(root, path):
    candidate = root / path
    if candidate.is_symlink():
        return True
    if not candidate.exists():
        return False
    if not candidate.is_dir():
        return True
    return next(candidate.iterdir(), None) is not None


def _submodule_heads(root, paths):
    heads = {}
    for path in paths:
        checkout = _submodule_checkout(root, path)
        if checkout is not None:
            heads[path] = git(checkout, "rev-parse", "HEAD")
    return heads


def _submodule_content_matches_head(root):
    for entry in git(root, "ls-tree", "-r", "-z", "HEAD").split("\0"):
        if not entry:
            continue
        metadata, _, path = entry.partition("\t")
        mode, _, object_id = metadata.split()
        if mode not in ("100644", "100755"):
            continue
        target = root / path
        if target.is_symlink() or not target.is_file():
            return False
        if git(root, "hash-object", "--no-filters", "--", path) != object_id:
            return False
    return True


def _submodule_snapshot(root, paths, prefix=Path(), seen=None):
    heads, dirty = {}, []
    seen = set() if seen is None else seen
    for path in paths:
        display = (prefix / path).as_posix()
        checkout = _submodule_checkout(root, path)
        if checkout is None:
            if _skipped_submodule_has_content(root, path):
                raise RelayError(f"Submodule path is not initialized but contains files: {display}")
            continue
        if checkout in seen:
            continue
        seen.add(checkout)
        heads[display] = git(checkout, "rev-parse", "HEAD")
        head_tree = git(checkout, "rev-parse", "HEAD^{tree}")
        if (not _submodule_content_matches_head(checkout)
                or git(checkout, "write-tree") != head_tree
                or _isolated_source_tree(checkout, inspect_submodules=False,
                                         neutralize_config=True) != head_tree):
            dirty.append(display)
        nested_heads, nested_dirty = _submodule_snapshot(
            checkout, _gitlink_paths(checkout), prefix / path, seen)
        heads.update(nested_heads)
        dirty.extend(nested_dirty)
    return heads, dirty


def _clean_submodule_snapshot(root, paths):
    heads, dirty = _submodule_snapshot(root, paths)
    if dirty:
        raise RelayError("Submodule worktree has uncommitted changes: " + ", ".join(sorted(set(dirty))))
    return heads


def _isolated_source_tree(root, *, inspect_submodules, neutralize_config=False):
    with tempfile.TemporaryDirectory(prefix="devrelay-index-") as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        git(root, "read-tree", "HEAD", env=env)
        config = (["-c", "core.fileMode=true", "-c", f"core.excludesFile={os.devnull}"]
                  if neutralize_config else [])
        git(root, *config, "add", "-A", env=env)
        paths = _gitlink_paths(root, env=env)
        submodules = (_clean_submodule_snapshot(root, paths) if inspect_submodules
                      else _submodule_heads(root, paths))
        for path in paths:
            if path in submodules:
                git(root, "update-index", "--add", "--cacheinfo", "160000", submodules[path], path, env=env)
        if inspect_submodules:
            git(root, "diff", "--cached", "--check", env=env)
        tree = git(root, "write-tree", env=env)
        if inspect_submodules and _clean_submodule_snapshot(root, paths) != submodules:
            raise RelayError("Submodule HEAD changed while creating source evidence; rerun verification")
        return tree


def source_tree(root):
    return _isolated_source_tree(root, inspect_submodules=True)


def stage_submodule_gitlinks(root):
    paths = [path for path in _gitlink_paths(root) if _submodule_checkout(root, path) is not None]
    if paths:
        git(root, "add", "--force", "--", *paths)


def github_slug(url):
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?",
        url,
    )
    return f"{match[1]}/{match[2]}" if match else ""


def validate_remote(config):
    if config.provider != "github":
        return
    for argv in (("remote", "get-url", config.remote), ("remote", "get-url", "--push", config.remote)):
        slug = github_slug(git(config.root, *argv))
        if slug.casefold() != config.repo.casefold():
            raise RelayError("Git fetch/push remote does not match project.repository; correct the configuration before proceeding")


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
