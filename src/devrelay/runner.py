"""Single-task orchestration. External delivery is an explicit submit command."""

import json
from pathlib import Path
import uuid

from . import codex
from .config import load
from .errors import RelayError
from .git import git, has_ref, is_ancestor, source_tree, validate_remote, worktrees
from .process import execute
from .providers import authorized, gh, gh_json, queue_report, snapshot, task_id
from .state import now


def assert_config(config):
    if load(config.root).fingerprint != config.fingerprint:
        raise RelayError("Project configuration changed during this operation; rerun with the new configuration")


def base_commit(config):
    if config.provider == "github":
        validate_remote(config)
        git(config.root, "fetch", config.remote, config.base)
    if not has_ref(config.root, config.base_ref):
        raise RelayError(f"Base {config.base_ref} has no commit; create an initial commit or configure the correct base")
    return git(config.root, "rev-parse", config.base_ref)


def claimed_task(config, state, identifier):
    task = state.get(task_id(identifier))
    if not task or not task.get("worktree"):
        raise RelayError(f"Task {identifier} has no worktree; run devrelay start {identifier}")
    path = Path(task["worktree"])
    expected = config.root / ".devrelay/worktrees" / task["id"]
    if path.resolve() != expected.resolve() or not path.is_dir():
        raise RelayError("Saved worktree is missing or outside its expected path; preserve state and repair the checkout")
    if git(path, "branch", "--show-current") != task["branch"]:
        raise RelayError("Existing worktree uses a different branch; left unchanged")
    records = worktrees(config.root)
    if not any(Path(item["worktree"]).resolve() == path.resolve()
               and item.get("branch") == "refs/heads/" + task["branch"] for item in records):
        raise RelayError("Worktree is not registered with this repository")
    return task


def start(config, state, identifier):
    identifier = task_id(identifier)
    task = authorized(config, state, identifier)
    saved = state.get(identifier)
    if saved and saved["status"] == "completed":
        raise RelayError(f"Task {identifier} is already completed; inspect its saved delivery evidence")
    if saved and saved.get("worktree") and Path(saved["worktree"]).is_dir():
        return claimed_task(config, state, identifier)
    recovering_start = saved and saved["status"] in ("claimed", "start_failed")
    view = snapshot(config, state, ignore_state_claim=identifier if recovering_start else None)
    report = queue_report(config, view)
    if identifier not in {item["id"] for item in report["eligible"]}:
        raise RelayError(f"Task {identifier} already has work or a PR; recover it instead")
    base = base_commit(config)
    path = config.root / ".devrelay/worktrees" / identifier
    branch = config.prefix + identifier
    if path.exists():
        raise RelayError(f"Unregistered worktree directory exists: {path}; left unchanged")
    git(config.root, "check-ref-format", "--branch", branch)
    task = {**task, "status": "claimed", "branch": branch, "worktree": str(path),
            "base_sha": base, "attempts": (saved or {}).get("attempts", 0)}
    state.save(task)  # Persist ownership before creating the worktree.
    try:
        if has_ref(config.root, "refs/heads/" + branch):
            git(config.root, "worktree", "add", str(path), branch)
        else:
            git(config.root, "worktree", "add", "-b", branch, str(path), base)
    except RelayError:
        task["status"] = "start_failed"
        state.save(task)
        raise
    return state.save(task)


def prepare(config, state, task):
    task["status"] = "preparing"
    state.save(task)
    directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-setup")
    for entry in config.data["setup"]:
        execute(state, entry["argv"], task["worktree"], directory / (entry["id"] + ".log"), entry["timeout_seconds"])
    assert_config(config)
    task["setup_fingerprint"] = config.fingerprint
    task["status"] = "prepared"
    return state.save(task)


def verify(config, state, identifier):
    if not config.data["checks"]:
        raise RelayError("Configure at least one [[checks]] acceptance command before verification")
    task = claimed_task(config, state, identifier)
    fresh = authorized(config, state, identifier)
    task.update({key: fresh[key] for key in ("body", "title", "labels")})
    path = Path(task["worktree"])
    base = base_commit(config)
    if not is_ancestor(path, base, "HEAD"):
        raise RelayError("Target base moved beyond this worktree; integrate it into the task branch and rerun verification")
    before = source_tree(path)
    head = git(path, "rev-parse", "HEAD")
    directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-checks")
    directory.mkdir(parents=True)
    task["status"] = "verifying"
    task["verification"] = None
    state.save(task)
    results = []
    try:
        for entry in config.data["checks"]:
            receipt = execute(state, entry["argv"], path, directory / (entry["id"] + ".log"), entry["timeout_seconds"])
            results.append({"id": entry["id"], **receipt})
        assert_config(config)
        if source_tree(path) != before or git(path, "rev-parse", "HEAD") != head:
            raise RelayError("Source or HEAD changed during verification; rerun the checks")
        if base_commit(config) != base:
            raise RelayError("Target base changed during verification; integrate it and rerun")
        latest = authorized(config, state, identifier)
        if latest["body"] != fresh["body"] or latest["title"] != fresh["title"]:
            raise RelayError("Task requirements changed during verification; resume and revalidate")
    except RelayError as error:
        task.update(status="failed", error=str(error))
        state.save(task)
        (directory / "verification.json").write_text(json.dumps({"passed": False, "error": str(error), "checks": results}, indent=2))
        raise
    record = {"passed": True, "source_tree": before, "head_sha": head, "base_sha": base,
              "config_sha256": config.fingerprint, "task_body": fresh["body"], "task_title": fresh["title"],
              "checks": results, "finished_at": now()}
    receipt_path = directory / "verification.json"
    receipt_path.write_text(json.dumps(record, indent=2) + "\n")
    task.update(status="verified", verification=record, verification_path=str(receipt_path), base_sha=base, error=None)
    return state.save(task)


def run_task(config, state, identifier=None, *, resume=False, submit_after=False):
    if not config.data["checks"]:
        raise RelayError("Configure at least one [[checks]] before running an AI task")
    if resume and submit_after and (state.get(str(identifier)) or {}).get("pr_url"):
        from .delivery import advance
        return advance(config, state, identifier)
    if identifier is None:
        delivery_attention = [item["id"] for item in state.tasks()
                              if item["status"] in ("review_blocked", "merge_blocked", "post_merge_failed", "cleanup_failed")]
        if delivery_attention:
            return {"status": "needs_attention", "tasks": delivery_attention,
                    "message": "Inspect review/merge/post-merge evidence before dispatching new work"}
        pending = [item for item in state.tasks() if item.get("pr_url") and item["status"] != "completed"]
        if submit_after and pending and (config.data["delivery"]["auto_review"] or config.data["delivery"]["auto_merge"]):
            from .delivery import advance
            return advance(config, state, sorted(pending, key=lambda item: item["created_at"])[0]["id"])
        interrupted = [item for item in state.tasks() if item["status"] in ("claimed", "prepared", "preparing", "implementing", "verifying", "interrupted")]
        if interrupted:
            identifier = sorted(interrupted, key=lambda item: item["created_at"])[0]["id"]
            resume = True
        else:
            attention = [item["id"] for item in state.tasks() if item["status"] in ("failed", "blocked", "start_failed")]
            if attention:
                return {"status": "needs_attention", "tasks": attention,
                        "message": "Inspect saved errors and explicitly resume these tasks before new automatic dispatch"}
            selected = queue_report(config, snapshot(config, state))["selected"]
            if selected is None:
                return {"status": "idle", "message": "No eligible task"}
            identifier = selected["id"]
    if resume and (state.get(str(identifier)) or {}).get("status") in ("claimed", "start_failed"):
        task = start(config, state, identifier)
    else:
        task = claimed_task(config, state, identifier) if resume else start(config, state, identifier)
    if task.get("pr_url"):
        if resume and submit_after:
            from .delivery import advance
            return advance(config, state, identifier)
        raise RelayError(f"Task {identifier} already has a PR; use check/submit, review or merge")
    if task["status"] == "completed":
        raise RelayError(f"Task {identifier} is already {task['status']}; inspect status instead")
    fresh = authorized(config, state, identifier)
    task.update({key: fresh[key] for key in ("body", "title", "labels")})
    feedback = task.get("error", "")
    try:
        if task.get("setup_fingerprint") != config.fingerprint:
            task = prepare(config, state, task)
        for attempt in range(config.data["codex"]["max_attempts"]):
            fresh = authorized(config, state, identifier)
            task.update({key: fresh[key] for key in ("body", "title", "labels")})
            task.update(status="implementing", attempts=task.get("attempts", 0) + 1, verification=None)
            state.save(task)
            directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-codex")
            before_head = git(task["worktree"], "rev-parse", "HEAD")
            result = codex.implement(config, state, task, directory, feedback)
            task["summary"] = result["summary"]
            task["last_run"] = str(directory)
            if git(task["worktree"], "rev-parse", "HEAD") != before_head:
                raise RelayError("Codex changed Git history; preserve its work and review before continuing")
            assert_config(config)
            if result["outcome"] == "blocked":
                task.update(status="blocked", error="; ".join(result["blockers"]) or result["summary"])
                return state.save(task)
            state.save(task)
            try:
                task = verify(config, state, task["id"])
            except RelayError as error:
                feedback = str(error)
                if attempt + 1 >= config.data["codex"]["max_attempts"]:
                    raise
                task = claimed_task(config, state, identifier)
                fresh = authorized(config, state, identifier)
                task.update({key: fresh[key] for key in ("body", "title", "labels")})
                continue
            return submit(config, state, task["id"]) if submit_after else task
    except (RelayError, KeyboardInterrupt) as error:
        task = state.get(str(identifier)) or task
        if not task.get("pr_url"):
            task["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        task["error"] = str(error) or "Interrupted"
        state.save(task)
        raise


def submit(config, state, identifier, *, local=False):
    if config.provider != "github" and not local:
        raise RelayError("Local tasks use submit --local to create a verified local commit")
    saved = state.get(str(identifier))
    if saved and saved.get("pr_url") and saved["status"] in ("completed", "cleaning", "cleanup_failed") and not local:
        from .delivery import advance
        return advance(config, state, identifier)
    task = claimed_task(config, state, identifier)
    if task.get("pr_url") and task["status"] not in ("verified", "committed") and not local:
        from .delivery import advance
        return advance(config, state, identifier)
    fresh = authorized(config, state, identifier)
    record = task.get("verification")
    path = Path(task["worktree"])
    assert_config(config)
    if (not record or not record.get("passed") or record["source_tree"] != source_tree(path)
            or record["config_sha256"] != config.fingerprint or record["base_sha"] != base_commit(config)
            or record["task_body"] != fresh["body"] or record["task_title"] != fresh["title"]):
        raise RelayError("Verification is missing or stale; run devrelay check before submitting")
    # Support retry after a commit/push/API timeout without creating a duplicate commit.
    current = git(path, "rev-parse", "HEAD")
    if current != record["head_sha"] and current != task.get("head_sha"):
        raise RelayError("HEAD changed since verification; rerun checks")
    git(path, "add", "-A")
    if git(path, "write-tree") != record["source_tree"]:
        raise RelayError("Staged source differs from verified source")
    if git(path, "diff", "--cached", "--name-only"):
        git(path, "commit", "-m", f"devrelay: {fresh['title']} ({identifier})")
    head = git(path, "rev-parse", "HEAD")
    if git(path, "rev-parse", "HEAD^{tree}") != record["source_tree"]:
        raise RelayError("Commit hooks changed verified content; rerun checks")
    if not int(git(path, "rev-list", "--count", f"{config.base_ref}..HEAD")):
        raise RelayError("No implementation commit to submit")
    task.update(status="committed", head_sha=head)
    state.save(task)
    if local:
        return task
    assert_config(config)
    authorized(config, state, identifier)
    if base_commit(config) != record["base_sha"]:
        raise RelayError("Base changed before push; integrate it and rerun verification")
    git(path, "push", "--set-upstream", config.remote, task["branch"])
    summary = task.get("summary") or "Implementation verified by configured project checks."
    check_lines = "\n".join(f"- `{item['id']}`: exit {item['returncode']}" for item in record["checks"])
    body = (f"{summary}\n\nVerification\n\n{check_lines}\n\n"
            f"Source tree: `{record['source_tree']}`\nBase: `{record['base_sha']}`\n"
            f"Configuration SHA-256: `{record['config_sha256']}`\n\nRefs #{identifier}\n")
    existing = gh_json(config, "pr", "list", "--head", task["branch"], "--base", config.base,
                       "--state", "open", "--json", "number,url")
    if existing:
        url = existing[0]["url"]
        gh(config, "pr", "edit", str(existing[0]["number"]), "--body-file", "-", input=body)
    else:
        url = gh(config, "pr", "create", "--draft", "--base", config.base, "--head", task["branch"],
                 "--title", fresh["title"], "--body-file", "-", input=body)
    pull = gh_json(config, "pr", "view", url, "--json", "url,state,headRefName,headRefOid,baseRefName")
    if (pull["state"] != "OPEN" or pull["headRefName"] != task["branch"]
            or pull["headRefOid"] != head or pull["baseRefName"] != config.base):
        raise RelayError("PR readback differs from the verified submission; inspect it before retrying")
    task.update(status="pr_open", pr_url=pull["url"])
    state.save(task)
    from .delivery import advance
    return advance(config, state, identifier)


def complete_local(config, state, identifier):
    if config.provider != "local":
        raise RelayError("GitHub task completion is recorded on GitHub after reviewed integration")
    task = claimed_task(config, state, identifier)
    if not task.get("head_sha") or not is_ancestor(config.root, task["head_sha"], config.base_ref):
        raise RelayError("The submitted task commit must be integrated into the configured base before completion")
    task.update(status="completed", open=False)
    return state.save(task)
