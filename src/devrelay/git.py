"""Git operations that preserve the caller's working tree and index."""

from contextlib import contextmanager
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


def _system_config_path(root):
    nosystem = os.environ.get("GIT_CONFIG_NOSYSTEM")
    if nosystem is not None:
        # Use Git's own boolean parser (including numeric and false spellings),
        # without loading configuration that this variable controls.
        try:
            disabled = git(root, "config", "--file", os.devnull, "--type=bool",
                           "--default", nosystem, "--get", "devrelay.nosystem")
        except RelayError as error:
            raise RelayError("Cannot parse GIT_CONFIG_NOSYSTEM as a Git boolean") from error
        if disabled == "true":
            return None
    configured = os.environ.get("GIT_CONFIG_SYSTEM")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else (Path(root) / path).resolve()
    try:
        result = subprocess.run(
            ["git", "config", "--system", "--includes", "--show-origin", "--null", "--list"],
            cwd=root, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RelayError(f"Cannot inspect Git system configuration: {error}") from error
    if result.returncode:
        if b"No such file or directory" in result.stderr:
            return None  # A missing system file is valid and contributes no settings.
        raise RelayError("Cannot inspect Git system configuration")
    entries = result.stdout.split(b"\0")
    for origin in entries[::2]:
        if origin.startswith(b"file:"):
            return Path(os.fsdecode(origin[5:]))
    return None


@contextmanager
def pinned_transport(root, url):
    """Yield an alias-proof transport argument and its pinned Git environment."""
    root = Path(root)
    system_config = _system_config_path(root)
    with tempfile.TemporaryDirectory(prefix="devrelay-transport-") as temporary:
        path = Path(temporary) / "gitconfig"
        # Git refuses remote configuration subsections beginning with '/'.
        # An absolute argument therefore cannot select a URL-named remote,
        # even if one is added after validation. Bind this non-existent path
        # to the approved endpoint in the same invocation as the transport.
        target = str((Path(temporary) / "endpoint").resolve())
        # System configuration is read before global, local, worktree and
        # command scopes. Git keeps the first equally long rewrite match, so
        # these exact self-mappings cannot be superseded by a later mutation.
        for name in ("insteadOf", "pushInsteadOf"):
            for source in (url, target):
                command(["git", "config", "--file", str(path), "--add",
                         f"url.{url}.{name}", source], root)
        if system_config is not None:
            command(["git", "config", "--file", str(path), "--add", "include.path",
                     str(system_config)], root)
        env = {**os.environ, "GIT_CONFIG_SYSTEM": str(path)}
        env.pop("GIT_CONFIG_NOSYSTEM", None)
        yield target, env


def remote_tracking_ref(root, remote, branch):
    """Map a pushed branch using Git's first matching fetch refspec policy."""
    ref = "refs/heads/" + branch
    specs = _config_values(root, f"remote.{remote}.fetch")

    def match(pattern):
        return re.fullmatch(re.escape(pattern).replace(r"\*", "(.*)"), ref)

    if any(match(spec[1:]) for spec in specs if spec.startswith("^")):
        return None
    for spec in specs:
        if spec.startswith("^"):
            continue
        source, separator, destination = spec.removeprefix("+").partition(":")
        found = match(source)
        if separator and destination and found:
            mapped = destination.replace("*", found[1]) if "*" in source else destination
            git(root, "check-ref-format", mapped)
            return mapped
    return None


def validate_remote(config, root=None):
    if config.provider != "github":
        return None
    root = Path(root or config.root)
    raw_fetch = _config_values(root, f"remote.{config.remote}.url")
    raw_push = _config_values(root, f"remote.{config.remote}.pushurl") or raw_fetch
    raw_urls = {"fetch": raw_fetch, "push": raw_push}
    urls = {}
    for kind, argv in (("fetch", ("remote", "get-url", "--all", config.remote)),
                       ("push", ("remote", "get-url", "--push", "--all", config.remote))):
        # `remote get-url` expands url.*.insteadOf and pushInsteadOf, so these
        # are the endpoints Git would actually use rather than just raw config.
        urls[kind] = git(root, *argv).splitlines()
    if any(len(items) != 1 for group in (raw_urls, urls) for items in group.values()):
        raise RelayError(
            "Git remote must resolve to exactly one fetch URL and one push URL; "
            "multiple URLs and push fan-out are not allowed"
        )
    # Inspect the literal URL in this worktree too, rejecting chained and
    # conditional rewrites before constructing an alias-proof transport.
    literal_push_url = git(root, "ls-remote", "--get-url", urls["push"][0])
    if raw_urls != urls or literal_push_url != urls["push"][0]:
        raise RelayError(
            "Git URL rewrites are not allowed for the configured remote; "
            "use one explicit GitHub fetch URL and push URL"
        )
    for kind, items in urls.items():
        slug = github_slug(items[0])
        if slug.casefold() != config.repo.casefold():
            raise RelayError(
                f"Git {kind} URL does not match project.repository; "
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
