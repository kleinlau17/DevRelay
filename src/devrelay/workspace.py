"""One authorized outcome across local repositories; bounded runs and draft delivery."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib
import uuid

from . import codex
from .config import validate
from .errors import RelayError
from .git import (common_dir, git, is_ancestor, pinned_transport, repository_root,
                  source_tree, stage_submodule_gitlinks, validate_remote, worktrees)
from .process import execute
from .providers import gh, gh_json, is_tracking_parent, task_id
from .runner import base_commit
from .state import State, now


@dataclass(frozen=True)
class Workspace:
    path: Path
    members: dict
    checks: list
    codex_config: dict
    fingerprint: str

    @property
    def directory(self):
        return self.path.parent / ".devrelay" / "workspace-runtime" / self.path.stem


def load(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise RelayError("Workspace manifest must not be a symlink")
    path = path.resolve()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RelayError(f"Cannot read workspace manifest {path}: {error}") from error
    if set(data) - {"repositories", "checks", "codex"}:
        raise RelayError("Workspace supports repositories, checks and codex only; automatic merge is unavailable")
    repos = data.get("repositories")
    if not isinstance(repos, list) or len(repos) < 2:
        raise RelayError("A workspace needs at least two explicitly configured repositories")
    members, identities = {}, set()
    for entry in repos:
        if not isinstance(entry, dict) or set(entry) - {"key", "path", "project", "setup", "checks"}:
            raise RelayError("Invalid workspace repository entry")
        key, location = entry.get("key"), entry.get("path")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) or key in members:
            raise RelayError("Repository keys must be unique lowercase identifiers")
        if not isinstance(location, str) or not location:
            raise RelayError("Each repository requires an explicit path")
        root = repository_root(path.parent / location)
        identity = common_dir(root)
        if identity in identities or any(root.is_relative_to(c.root) or c.root.is_relative_to(root)
                                         for c in members.values()):
            raise RelayError("Duplicate or nested participating repositories are not supported")
        identities.add(identity)
        member_data = {name: entry[name] for name in ("project", "setup", "checks") if name in entry}
        config = validate(root, member_data)
        if not config.data["checks"]:
            raise RelayError(f"Repository {key} requires at least one acceptance check")
        members[key] = config
    # Reuse command and Codex validation; integration cwd is a member key or manifest directory.
    integration = data.get("checks", [])
    if not isinstance(integration, list) or not integration:
        raise RelayError("Configure at least one workspace integration check")
    commands = []
    for entry in integration:
        if not isinstance(entry, dict) or set(entry) - {"id", "argv", "timeout_seconds", "cwd"}:
            raise RelayError("Invalid workspace integration check")
        cwd = entry.get("cwd", "workspace")
        if not isinstance(cwd, str) or cwd != "workspace" and cwd not in members:
            raise RelayError("Integration cwd must be workspace or a configured repository key")
        commands.append({name: value for name, value in entry.items() if name != "cwd"})
    settings = validate(path.parent, {"codex": data.get("codex", {}), "checks": commands})
    checks = [{**entry, "cwd": original.get("cwd", "workspace")}
              for entry, original in zip(settings.data["checks"], integration)]
    normalized = {"path": str(path), "members": {key: {"root": str(c.root), "data": c.data}
                  for key, c in members.items()}, "checks": checks, "codex": settings.data["codex"]}
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    return Workspace(path, members, checks, settings.data["codex"], digest)


def assert_current(workspace, task):
    if load(workspace.path).fingerprint != workspace.fingerprint or task["config_sha256"] != workspace.fingerprint:
        raise RelayError("Workspace configuration changed; restore the registered configuration or create a new task")
    if not task.get("approved"):
        raise RelayError("Workspace task authorization was revoked")
    try:
        body = Path(task["requirements_path"]).read_text()
    except OSError as error:
        raise RelayError("Registered task requirements are unavailable") from error
    if body != task["body"]:
        raise RelayError("Task requirements changed; restore them or register a new authorized task")


def get_task(state, identifier):
    task = state.get(task_id(identifier))
    if not task:
        raise RelayError("Unknown workspace task; explicitly authorize it with task add")
    return task


def add(workspace, state, identifier, title, requirements, priority="priority:P1"):
    identifier = task_id(identifier)
    if state.get(identifier):
        raise RelayError("Workspace task already exists; preserve its work and evidence")
    requirements = Path(requirements).resolve()
    body = requirements.read_text()
    if not title.strip() or not body.strip():
        raise RelayError("Task title and requirements must be nonempty")
    if priority not in ("priority:P0", "priority:P1", "priority:P2"):
        raise RelayError("Workspace priority must be priority:P0, priority:P1 or priority:P2")
    if is_tracking_parent({"body": body, "labels": []}):
        raise RelayError("Tracking plans cannot authorize a multi-repository implementation")
    token = hashlib.sha256(str(workspace.path).encode()).hexdigest()[:12] + "-" + identifier
    return state.save({"id": identifier, "title": title, "body": body, "requirements_path": str(requirements),
                       "approved": True, "config_sha256": workspace.fingerprint,
                       "owner": token, "priority": priority, "status": "queued", "members": {}, "attempts": 0, "created_at": now()})


@contextmanager
def member_locks(workspace, task):
    # Acquire all locks before reserving or changing anything. Sort by Git identity to avoid deadlocks.
    with ExitStack() as stack:
        states = {}
        for key, config in sorted(workspace.members.items(), key=lambda pair: str(common_dir(pair[1].root))):
            member_state = State(config.root)
            stack.callback(member_state.close)
            stack.enter_context(member_state.lock(owner=task["owner"]))
            states[key] = member_state
        yield states


def reserve(workspace, states, task):
    for key, member_state in states.items():
        # A saved external editor is an unfinished implementation even between CLI calls.
        active = [t for t in member_state.tasks() if t.get("worktree") and t["status"] not in
                  ("completed", "pr_open", "reviewed", "review_blocked")]
        if active:
            raise RelayError(f"Repository {key} has unfinished single-repository work; preserve and finish it first")
    for member_state in states.values():
        member_state.set_metadata("workspace_owner", task["owner"])


def start(workspace, state, task, states):
    assert_current(workspace, task)
    if task["status"] == "delivered":
        raise RelayError("Workspace task already delivered; inspect its draft PRs")
    reserve(workspace, states, task)
    for key, config in workspace.members.items():
        expected = config.root / ".devrelay/worktrees" / ("workspace-" + task["owner"])
        branch = config.prefix + "workspace-" + task["owner"]
        saved = task["members"].get(key)
        if not saved:
            if expected.exists():
                raise RelayError(f"Unregistered worktree at {expected}; left unchanged")
            saved = {"worktree": str(expected), "branch": branch, "base_sha": base_commit(config)}
            task["members"][key] = saved
            state.save(task)
        elif saved["worktree"] != str(expected) or saved["branch"] != branch:
            raise RelayError("Saved workspace member identity differs from configuration")
        if expected.exists():
            inspect_member(config, saved)
            continue
        registered = worktrees(config.root)
        if any(t.get("branch") == "refs/heads/" + branch for t in registered):
            raise RelayError("Task branch belongs to another worktree; left unchanged")
        # An existing branch without our registered checkout may belong to another operation.
        from .git import has_ref
        if has_ref(config.root, "refs/heads/" + branch):
            raise RelayError("Task branch already exists without its worktree; inspect before recovery")
        git(config.root, "worktree", "add", "-b", branch, str(expected), saved["base_sha"])
    task["status"] = "claimed"
    task["error"] = None
    return state.save(task)


def inspect_member(config, member):
    path = Path(member["worktree"])
    if path.is_symlink() or not path.is_dir() or common_dir(path) != common_dir(config.root):
        raise RelayError("Workspace member is missing or belongs to another Git repository")
    if git(path, "branch", "--show-current") != member["branch"] or not any(
            Path(t["worktree"]).resolve() == path.resolve() and t.get("branch") == "refs/heads/" + member["branch"]
            for t in worktrees(config.root)):
        raise RelayError("Workspace member is not registered on the owned task branch")


def paths_env(workspace, task):
    return {**os.environ, "DEVRELAY_WORKSPACE": str(workspace.path.parent),
            "DEVRELAY_EVIDENCE_DIR": str(workspace.directory / "runs" / task["id"]),
            **{"DEVRELAY_WORKTREE_" + key.upper(): member["worktree"] for key, member in task["members"].items()}}


def prepare(workspace, state, task):
    assert_current(workspace, task)
    if set(task["members"]) != set(workspace.members):
        raise RelayError("Claim all workspace members before preparation")
    for key, config in workspace.members.items():
        if task["members"][key].get("pr_url"):
            validate_pull(config, task["members"][key])
    task.update(status="preparing", prepared=False, verification=None, review=None)
    state.save(task)
    directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-setup")
    task["setup_path"] = str(directory)
    state.save(task)
    for key, config in workspace.members.items():
        inspect_member(config, task["members"][key])
        for entry in config.data["setup"]:
            assert_current(workspace, task)
            execute(state, entry["argv"], task["members"][key]["worktree"],
                    directory / (key + "-" + entry["id"] + ".log"), entry["timeout_seconds"],
                    env=paths_env(workspace, task))
    assert_current(workspace, task)
    task.update(status="prepared", prepared=True, setup_path=str(directory), error=None)
    return state.save(task)


def snapshot(workspace, task):
    assert_current(workspace, task)
    if not task.get("prepared") or set(task["members"]) != set(workspace.members):
        raise RelayError("Workspace preparation is missing; run prepare first")
    result = {}
    for key, config in workspace.members.items():
        member = task["members"][key]
        expected = config.root / ".devrelay/worktrees" / ("workspace-" + task["owner"])
        if member["worktree"] != str(expected) or member["branch"] != config.prefix + "workspace-" + task["owner"]:
            raise RelayError("Saved workspace member identity differs from configuration")
        inspect_member(config, member)
        base = base_commit(config)
        if not is_ancestor(member["worktree"], base, "HEAD"):
            raise RelayError(f"Repository {key} base advanced; integrate the latest base and rerun all checks")
        result[key] = {"base_sha": base, "head_sha": git(member["worktree"], "rev-parse", "HEAD"),
                       "source_tree": source_tree(Path(member["worktree"]))}
    return result


def check(workspace, state, task):
    before = snapshot(workspace, task)
    task.update(status="verifying", verification=None, review=None)
    state.save(task)
    directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-checks")
    task["checks_path"] = str(directory)
    state.save(task)
    results = []
    entries = [(key, entry) for key, config in workspace.members.items() for entry in config.data["checks"]]
    entries += [(entry["cwd"], entry) for entry in workspace.checks]
    for key, entry in entries:
        cwd = workspace.path.parent if key == "workspace" else Path(task["members"][key]["worktree"])
        receipt = execute(state, entry["argv"], cwd, directory / (key + "-" + entry["id"] + ".log"),
                          entry["timeout_seconds"], env=paths_env(workspace, task))
        results.append({"repository": key, "id": entry["id"], **receipt})
        if snapshot(workspace, task) != before:
            raise RelayError("Participating source, HEAD or base changed during joint verification; rerun checks")
    record = {"passed": True, "members": before, "config_sha256": workspace.fingerprint,
              "task_body": task["body"], "checks": results, "finished_at": now()}
    directory.mkdir(parents=True, exist_ok=True)
    evidence = directory / "verification.json"
    evidence.write_text(json.dumps(record, indent=2) + "\n")
    for key, value in before.items():
        task["members"][key]["head_sha"] = value["head_sha"]
    task.update(status="verified", verification=record, verification_path=str(evidence), error=None)
    return state.save(task)


def evidence(workspace, task):
    record = task.get("verification")
    current = snapshot(workspace, task)
    if not record or not record.get("passed") or record["config_sha256"] != workspace.fingerprint:
        raise RelayError("Joint verification is missing or stale")
    expected = {key: {**value, "head_sha": task["members"][key].get("head_sha", value["head_sha"])}
                for key, value in record["members"].items()}
    if current != expected or record["task_body"] != task["body"]:
        raise RelayError("Participating source, HEAD or base changed; rerun joint verification")
    return current


def run(workspace, state, task, states, *, submit_after=False):
    if task["status"] == "delivered":
        raise RelayError("Task already delivered")
    if task.get("publication_started"):
        raise RelayError("Task has partial delivery; recover with check/submit, preserve existing PRs")
    start(workspace, state, task, states)
    if not task.get("prepared"):
        prepare(workspace, state, task)
    first = next(iter(workspace.members))
    task["worktree"] = task["members"][first]["worktree"]
    config = validate(Path(task["worktree"]), {"codex": workspace.codex_config, "checks": []})
    context = json.dumps({"members": {key: {"worktree": m["worktree"], "base": m["base_sha"],
                  "checks": workspace.members[key].data["checks"]} for key, m in task["members"].items()},
                  "joint_checks": workspace.checks}, ensure_ascii=False)
    feedback = task.get("error") or ""
    for attempt in range(workspace.codex_config["max_attempts"]):
        assert_current(workspace, task)
        heads = {key: git(member["worktree"], "rev-parse", "HEAD") for key, member in task["members"].items()}
        task.update(status="implementing", attempts=task["attempts"] + 1, verification=None, review=None)
        state.save(task)
        directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-codex")
        result = codex.implement(config, state, task, directory, feedback, context=context,
                                 add_dirs=[m["worktree"] for key, m in task["members"].items() if key != first])
        assert_current(workspace, task)
        if any(git(m["worktree"], "rev-parse", "HEAD") != heads[key] for key, m in task["members"].items()):
            raise RelayError("Implementer changed participating Git history; preserve and inspect its work")
        task["summary"] = result["summary"]
        if result["outcome"] == "blocked":
            task.update(status="blocked", error="; ".join(result["blockers"]) or result["summary"])
            return state.save(task)
        try:
            check(workspace, state, task)
            review(workspace, state, task)
            if task["review"]["verdict"] != "approve":
                raise RelayError("Joint review requested changes: " + ("; ".join(task["review"]["findings"]) or task["review"]["summary"]))
        except RelayError as error:
            feedback = str(error)
            task["error"] = feedback
            state.save(task)
            if attempt + 1 == workspace.codex_config["max_attempts"]:
                raise
            continue
        return submit(workspace, state, task, states) if submit_after else task


def review(workspace, state, task):
    before = evidence(workspace, task)
    first = next(iter(workspace.members))
    task["worktree"] = task["members"][first]["worktree"]
    directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-review")
    proxy = {**task, "head_sha": before[first]["head_sha"], "verification": before[first]}
    config = validate(Path(task["worktree"]), {"codex": workspace.codex_config})
    context = json.dumps({"members": task["members"], "accepted_sources": before,
                          "checks": task["verification"]["checks"],
                          "instruction": "Review the complete task-relative working source in EVERY member: git diff BASE, git status --short and read newly added files. HEAD alone may contain no implementation yet. Check the shared contract and integration evidence."})
    task.update(status="reviewing", review=None, review_run=str(directory))
    state.save(task)
    result = codex.review(config, state, proxy, directory, context=context,
                          add_dirs=[m["worktree"] for key, m in task["members"].items() if key != first])
    if evidence(workspace, task) != before:
        raise RelayError("Participating source changed during read-only joint review")
    for other in state.tasks():
        if other["id"] != task["id"] and (other.get("session_id") == result["session_id"]
                or result["session_id"] in codex._implementation_sessions(state, other)
                or result["session_id"] in codex._review_sessions(state, other)):
            raise RelayError("Review session belongs to another workspace task")
    record = {**result, "members": before}
    receipt = directory / "review.json"
    receipt.write_text(json.dumps(record, indent=2) + "\n")
    task.update(status="reviewed" if result["verdict"] == "approve" else "review_blocked",
                review=record, review_path=str(receipt))
    return state.save(task)


def recover_commits(workspace, state, task):
    """Recover only a direct, source-identical commit following our persisted commit intent."""
    assert_current(workspace, task)
    for key, member in task["members"].items():
        intent = member.get("commit_intent")
        if not intent:
            continue
        inspect_member(workspace.members[key], member)
        path = Path(member["worktree"])
        head = git(path, "rev-parse", "HEAD")
        if head == intent["head_sha"] or head == member.get("head_sha"):
            continue
        if (git(path, "rev-list", "--parents", "-n", "1", head).split() != [head, intent["head_sha"]]
                or git(path, "rev-parse", "HEAD^{tree}") != intent["source_tree"]
                or source_tree(path) != intent["source_tree"]):
            raise RelayError("Interrupted member commit differs from its recorded intent; preserve and recheck")
        member["head_sha"] = head
        state.save(task)


def submit(workspace, state, task, states, *, local=False):
    recover_commits(workspace, state, task)
    evidence(workspace, task)
    if task.get("review") and (task["review"]["verdict"] != "approve" or any(task["review"]["members"][key][field] != value[field] for key, value in snapshot(workspace, task).items() for field in ("source_tree", "base_sha"))):
        raise RelayError("Joint review is rejected or stale; recheck and review before delivery")
    if not local and any(c.provider != "github" for c in workspace.members.values()):
        raise RelayError("Draft PR delivery requires GitHub configuration for every member; use --local for local commits")
    if not local:
        for config in workspace.members.values():
            validate_remote(config)
    task.update(status="submitting", publication_started=True)
    state.save(task)
    # Commit every verified member before any remote mutation. Record each checkpoint for recovery.
    for key, config in workspace.members.items():
        evidence(workspace, task)
        member = task["members"][key]
        path = Path(member["worktree"])
        if member.get("pr_url") and not local:
            validate_pull(config, member)
        git(path, "add", "-A")
        stage_submodule_gitlinks(path)
        if git(path, "write-tree") != task["verification"]["members"][key]["source_tree"]:
            raise RelayError("Staged member differs from jointly verified source")
        if git(path, "diff", "--cached", "--name-only", "--ignore-submodules=none"):
            member["commit_intent"] = {"head_sha": git(path, "rev-parse", "HEAD"),
                                       "source_tree": task["verification"]["members"][key]["source_tree"]}
            state.save(task)
            git(path, "commit", "-m", f"devrelay: {task['title']} ({task['id']}, {key})")
        head = git(path, "rev-parse", "HEAD")
        if source_tree(path) != task["verification"]["members"][key]["source_tree"] or git(path, "rev-parse", "HEAD^{tree}") != task["verification"]["members"][key]["source_tree"]:
            raise RelayError("Commit hooks changed accepted member source; rerun joint checks")
        member["head_sha"] = head
        member["changed"] = head != task["verification"]["members"][key]["base_sha"]
        state.save(task)
    if local:
        task["status"] = "committed"
        return state.save(task)
    links = [{"key": key, "repository": config.repo, "branch": task["members"][key]["branch"]}
             for key, config in workspace.members.items()]
    for key, config in workspace.members.items():
        member = task["members"][key]
        if not member["changed"]:
            continue
        evidence(workspace, task)
        # Persist a submitted SHA before push; uncertain publication is recovered by exact branch lookup.
        url = validate_remote(config, Path(member["worktree"]))
        ref = "refs/heads/" + member["branch"]
        with pinned_transport(Path(member["worktree"]), url) as (target, env):
            git(member["worktree"], "push", target, member["head_sha"] + ":" + ref, env=env)
        evidence(workspace, task)
        pulls = gh_json(config, "pr", "list", "--state", "all", "--head", member["branch"], "--json",
                        "url,state,baseRefName,headRefName,headRefOid,isDraft")
        if len(pulls) > 1:
            raise RelayError("Multiple PRs own this member branch; inspect partial delivery")
        if pulls:
            pull = pulls[0]
            if pull["state"] != "OPEN" or pull["baseRefName"] != config.base or not pull["isDraft"] or pull["headRefOid"] != member["head_sha"]:
                raise RelayError("Existing PR is not the expected open draft; preserve partial delivery")
            member["pr_url"] = pull["url"]
            state.save(task)
            validate_pull(config, member)
        body = (f"{task.get('summary') or task['title']}\n\nWorkspace task `{task['id']}`; member `{key}`.\n\n"
                f"Joint verification: `{task['verification_path']}`\n\n"
                "Participating PR branches:\n" + "\n".join(f"- {m['repository']}: `{m['branch']}`" for m in links)
                + "\n\nChecks:\n" + "\n".join(f"- {c['repository']}/{c['id']}: passed" for c in task["verification"]["checks"])
                + "\n\nDraft delivery only. No automatic merge or deployment.\n")
        directory = state.directory / "runs" / task["id"]
        directory.mkdir(parents=True, exist_ok=True)
        body_file = directory / (key + "-pr.md")
        body_file.write_text(body)
        evidence(workspace, task)
        if member.get("pr_url"):
            gh(config, "pr", "edit", member["pr_url"], "--body-file", str(body_file))
        else:
            member["pr_url"] = gh(config, "pr", "create", "--draft", "--base", config.base, "--head", member["branch"],
                                   "--title", task["title"], "--body-file", str(body_file)).strip()
            state.save(task)
        validate_pull(config, member)
        evidence(workspace, task)
    # Publish clickable companion links only after every draft has an observed identity.
    companions = "\n\nCompanion drafts:\n" + "\n".join(
        f"- [{key}]({member['pr_url']})" for key, member in task["members"].items() if member.get("pr_url")) + "\n"
    for key, config in workspace.members.items():
        member = task["members"][key]
        if member.get("pr_url"):
            evidence(workspace, task)
            validate_pull(config, member)
            body_file = state.directory / "runs" / task["id"] / (key + "-pr.md")
            body_file.write_text(body_file.read_text() + companions)
            gh(config, "pr", "edit", member["pr_url"], "--body-file", str(body_file))
            validate_pull(config, member)
            evidence(workspace, task)
    task.update(status="delivered", error=None)
    state.save(task)
    for member_state in states.values():
        member_state.set_metadata("workspace_owner", None)
    return task


def validate_pull(config, member):
    if not re.fullmatch(r"https://github\.com/" + re.escape(config.repo) + r"/pull/[1-9]\d*", member["pr_url"]):
        raise RelayError("Member PR URL differs from its configured repository")
    pull = gh_json(config, "pr", "view", member["pr_url"], "--json",
                   "state,isDraft,baseRefName,headRefName,headRefOid,isCrossRepository")
    if (pull["state"] != "OPEN" or not pull["isDraft"] or pull["isCrossRepository"]
            or pull["baseRefName"] != config.base or pull["headRefName"] != member["branch"]
            or pull["headRefOid"] != member["head_sha"]):
        raise RelayError("Member PR identity changed; preserve partial delivery and inspect GitHub")


def dispatch(args):
    workspace = load(args.workspace)
    # Avoid following a planted runtime directory into another task's evidence.
    for path in [workspace.directory, workspace.directory / "state.sqlite3", workspace.directory / "worker.lock",
                 *workspace.directory.parents]:
        if path.is_symlink():
            raise RelayError("Workspace runtime must not contain symlink directories")
    state = State(workspace.path.parent, directory=workspace.directory)
    try:
        with state.lock():
            if args.command == "doctor":
                from .cli import doctor
                reports = {key: doctor(config, with_codex=False) for key, config in workspace.members.items()}
                if args.with_codex:
                    from .cli import probe
                    reports["codex"] = {"ready": probe([workspace.codex_config["executable"], "login", "status"], workspace.path.parent)}
                return {"ready": all(r["ready"] for r in reports.values()), "members": reports,
                        "integration_checks": len(workspace.checks), "delivery": "draft-only"}
            if args.command == "status" or args.command == "task" and args.task_command == "list":
                return {"workspace": str(workspace.path), "tasks": state.tasks()}
            if args.command == "next":
                pending = [t for t in state.tasks() if t["status"] != "delivered" and t.get("approved")]
                attention = [t["id"] for t in pending if t["status"] in ("failed", "blocked", "review_blocked")]
                if attention:
                    return {"status": "needs_attention", "tasks": attention, "selected": None}
                active = [t for t in pending if t["status"] != "queued"]
                ordered = sorted(active, key=lambda t: t["created_at"]) if active else sorted(
                    pending, key=lambda t: (t.get("priority", "priority:P1"), t["created_at"]))
                return {"selected": ordered[0] if ordered else None}
            if args.command == "task" and args.task_command == "add":
                return add(workspace, state, args.id, args.title, args.file, args.priority)
            if args.command not in ("start", "prepare", "check", "submit", "run", "resume", "review") and not (args.command == "task" and args.task_command == "revoke"):
                raise RelayError("Workspace MVP supports task add/list/revoke, next/status/doctor, start/prepare/check/review/run/resume/submit; no automatic merge")
            if not args.id:
                raise RelayError("Workspace run requires an explicit authorized task ID")
            task = get_task(state, args.id)
            with member_locks(workspace, task) as states:
                if args.command == "task":
                    task.update(approved=False, status="revoked", verification=None)
                    state.save(task)
                    for member_state in states.values():
                        if member_state.metadata("workspace_owner") == task["owner"]:
                            member_state.set_metadata("workspace_owner", None)
                    return task
                try:
                    assert_current(workspace, task)
                    if args.command not in ("start", "run", "resume"):
                        reserve(workspace, states, task)
                    if args.command == "start":
                        return start(workspace, state, task, states)
                    if args.command == "prepare":
                        return prepare(workspace, state, task)
                    if args.command == "check":
                        return check(workspace, state, task)
                    if args.command == "review":
                        return review(workspace, state, task)
                    if args.command == "submit":
                        return submit(workspace, state, task, states, local=args.local)
                    return run(workspace, state, task, states, submit_after=args.submit)
                except (RelayError, OSError, KeyboardInterrupt) as error:
                    task.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                                error=str(error) or "Interrupted")
                    state.save(task)
                    raise
    finally:
        state.close()
