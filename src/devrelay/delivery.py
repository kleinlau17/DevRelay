"""Opt-in PR review, exact-head merge gates and fresh post-merge acceptance."""

import json
from pathlib import Path
import re
import uuid

from . import codex
from .cleanup import run as cleanup
from .errors import RelayError
from .git import command, git, is_ancestor, source_tree, validate_remote
from .process import execute
from .providers import authorized, gh, task_id
from .runner import assert_config, base_commit, claimed_task
from .state import now


PULL_QUERY = """query($owner:String!,$name:String!,$number:Int!){
 repository(owner:$owner,name:$name){pullRequest(number:$number){
  url state headRefName headRefOid baseRefName baseRefOid isCrossRepository
  isDraft mergeable mergeStateStatus reviewDecision mergeCommit{oid}
  reviewThreads(first:100){nodes{isResolved} pageInfo{hasNextPage}}
  latestReviews(first:100){nodes{state} pageInfo{hasNextPage}}
  commits(last:1){nodes{commit{oid statusCheckRollup{contexts(first:100){
   nodes{__typename ... on CheckRun{name status conclusion}
    ... on StatusContext{context state}} pageInfo{hasNextPage}
  }}}}}
 }}
}"""


def pull_snapshot(config, task):
    """Pin host/repository and fail closed on truncated or unavailable API data."""
    url = task.get("pr_url", "")
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/([1-9]\d*)", url)
    if not match or match[1].casefold() != config.repo.casefold():
        raise RelayError("Saved PR URL does not match the configured GitHub repository")
    owner, name = config.repo.split("/")
    raw = command(["gh", "api", "--hostname", "github.com", "graphql", "-f", "query=" + PULL_QUERY,
                   "-f", "owner=" + owner, "-f", "name=" + name, "-F", "number=" + match[2]], config.root)
    try:
        response = json.loads(raw)
        if response.get("errors"):
            raise ValueError("GraphQL errors")
        pull = response["data"]["repository"]["pullRequest"]
        if not isinstance(pull, dict) or pull["url"].casefold() != url.casefold():
            raise ValueError("PR identity")
        for key in ("state", "headRefName", "headRefOid", "baseRefName", "baseRefOid", "mergeable", "mergeStateStatus"):
            if not isinstance(pull[key], str):
                raise ValueError(key)
        for key in ("isCrossRepository", "isDraft"):
            if type(pull[key]) is not bool:
                raise ValueError(key)
        for key in ("reviewThreads", "latestReviews"):
            _connection(pull[key])
        commits = pull["commits"]["nodes"]
        if len(commits) != 1 or commits[0]["commit"]["oid"] != pull["headRefOid"]:
            raise ValueError("head checks")
        rollup = commits[0]["commit"]["statusCheckRollup"]
        pull["checks"] = _connection(rollup["contexts"]) if rollup else []
        pull["reviewDecision"]
        pull["mergeCommit"]
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise RelayError("GitHub returned incomplete or invalid PR gate data") from error
    return pull


def _connection(value):
    if value["pageInfo"]["hasNextPage"] is not False or not isinstance(value["nodes"], list):
        raise ValueError("Truncated connection; at most 100 checks/reviews/threads are supported")
    if any(not isinstance(item, dict) for item in value["nodes"]):
        raise ValueError("Invalid connection node")
    return value["nodes"]


def identity(task, config):
    record = task["verification"]
    return {"head_sha": task["head_sha"], "base_sha": record["base_sha"],
            "source_tree": record["source_tree"], "config_sha256": config.fingerprint,
            "task_body": record["task_body"], "task_title": record["task_title"], "pr_url": task["pr_url"]}


def current_review(task, config):
    record = task.get("review") or {}
    return bool(record) and all(record.get(key) == value for key, value in identity(task, config).items())


def open_evidence(config, state, task, pull):
    assert_config(config)
    fresh = authorized(config, state, task["id"])
    record = task.get("verification") or {}
    path = Path(task["worktree"])
    if (not record.get("passed") or record.get("config_sha256") != config.fingerprint
            or record.get("task_body") != fresh["body"] or record.get("task_title") != fresh["title"]
            or not task.get("head_sha") or git(path, "rev-parse", "HEAD") != task["head_sha"]
            or git(path, "rev-parse", "HEAD^{tree}") != record.get("source_tree")
            or source_tree(path) != record.get("source_tree")):
        raise RelayError("PR verification is missing or stale; check and submit the task again")
    if (pull["state"] != "OPEN" or pull["headRefName"] != task["branch"]
            or pull["headRefOid"] != task["head_sha"] or pull["baseRefName"] != config.base
            or pull["isCrossRepository"]):
        raise RelayError("PR identity or HEAD differs from the verified submission")
    base = base_commit(config)
    if pull["baseRefOid"] != base or record["base_sha"] != base or not is_ancestor(path, base, task["head_sha"]):
        raise RelayError("Target base changed; integrate the base, check, submit and review again")


def review(config, state, identifier):
    if config.provider != "github":
        raise RelayError("PR review requires project.provider=github")
    task = claimed_task(config, state, identifier)
    try:
        pull = pull_snapshot(config, task)
        open_evidence(config, state, task, pull)
        task.update(status="reviewing", review=None, error=None)
        directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-review")
        task["review_run"] = str(directory)
        state.save(task)
        result = codex.review(config, state, task, directory)
        open_evidence(config, state, task, pull_snapshot(config, task))
        record = {**result, **identity(task, config), "finished_at": now()}
        receipt = directory / "review.json"
        receipt.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        task.update(review=record, review_path=str(receipt),
                    status="reviewed" if result["verdict"] == "approve" else "review_blocked")
        if result["verdict"] != "approve":
            task["error"] = "; ".join(result["findings"]) or result["summary"]
        return state.save(task)
    except (RelayError, KeyboardInterrupt) as error:
        task.update(status="review_blocked", review=None, error=str(error) or "Review interrupted")
        state.save(task)
        raise


def gate_reasons(config, pull):
    reasons, seen = [], set()
    if pull["isDraft"]:
        reasons.append("PR is still draft")
    if pull["mergeable"] != "MERGEABLE" or pull["mergeStateStatus"] != "CLEAN":
        reasons.append(f"GitHub merge state: {pull['mergeable']}/{pull['mergeStateStatus']}")
    if pull["reviewDecision"] not in (None, "APPROVED"):
        reasons.append(f"GitHub review decision: {pull['reviewDecision']}")
    for item in pull["reviewThreads"]["nodes"]:
        if item.get("isResolved") is not True:
            reasons.append("Unresolved review thread")
    for item in pull["latestReviews"]["nodes"]:
        if item.get("state") not in ("APPROVED", "COMMENTED", "DISMISSED"):
            reasons.append("Outstanding or unknown GitHub review state")
    for item in pull["checks"]:
        if item.get("__typename") == "CheckRun":
            name = item.get("name")
            passed = item.get("status") == "COMPLETED" and item.get("conclusion") == "SUCCESS"
        elif item.get("__typename") == "StatusContext":
            name = item.get("context")
            passed = item.get("state") == "SUCCESS"
        else:
            name, passed = None, False
        if not isinstance(name, str) or not name:
            reasons.append("Unknown CI check")
        else:
            seen.add(name)
            if not passed:
                reasons.append(f"CI check is not successful: {name}")
    for name in config.data["delivery"]["required_checks"]:
        if name not in seen:
            reasons.append(f"Required CI check is missing: {name}")
    if not pull["checks"] and not config.data["delivery"]["allow_no_ci"]:
        reasons.append("No CI evidence; configure CI or explicitly set delivery.allow_no_ci=true")
    return list(dict.fromkeys(reasons))


def post_merge(config, state, task, pull):
    """Recover a successful remote merge without issuing another merge request."""
    oid = (pull.get("mergeCommit") or {}).get("oid", "")
    if (pull["state"] != "MERGED" or pull["baseRefName"] != config.base
            or pull["headRefOid"] != task.get("head_sha") or pull["headRefName"] != task["branch"]
            or pull["isCrossRepository"] or not re.fullmatch(r"[0-9a-f]{40,64}", oid)):
        raise RelayError("Merged PR does not match the saved submission")
    # This durable checkpoint must precede all local verification: failures cannot undo a merge.
    previous = task.get("post_merge_worktree")
    if previous and not (task.get("post_merge") or {}).get("passed"):
        retained = task.setdefault("retained_worktrees", [])
        if previous not in retained:
            retained.append(previous)
    task.update(status="merged", merge_sha=oid, error=None)
    state.save(task)
    directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-post-merge")
    directory.mkdir(parents=True)
    record = {"passed": False, "merge_sha": oid, "config_sha256": config.fingerprint, "checks": [], "setup": []}
    path = config.root / ".devrelay/worktrees" / (task["id"] + "-post-" + uuid.uuid4().hex)
    try:
        assert_config(config)
        base_commit(config)
        if not is_ancestor(config.root, oid, config.base_ref):
            raise RelayError("Merge commit is absent from the refreshed target branch")
        tree = git(config.root, "rev-parse", oid + "^{tree}")
        record["source_tree"] = tree
        if tree != task["verification"]["source_tree"]:
            raise RelayError("Merged tree differs from the reviewed source; inspect the base/merge race before continuing")
        if not config.data["checks"]:
            raise RelayError("No post-merge acceptance commands configured")
        git(config.root, "worktree", "add", "--detach", str(path), oid)
        task["post_merge_worktree"] = str(path)
        state.save(task)
        for phase in ("setup", "checks"):
            if phase == "checks" and (source_tree(path) != tree or git(path, "rev-parse", "HEAD") != oid):
                raise RelayError("Source or HEAD changed during post-merge setup")
            for entry in config.data[phase]:
                receipt = execute(state, entry["argv"], path, directory / (phase + "-" + entry["id"] + ".log"), entry["timeout_seconds"])
                record[phase].append({"id": entry["id"], **receipt})
                if phase == "checks" and (source_tree(path) != tree or git(path, "rev-parse", "HEAD") != oid):
                    raise RelayError("Source or HEAD changed during post-merge acceptance")
        assert_config(config)
        if source_tree(path) != tree or git(path, "rev-parse", "HEAD") != oid:
            raise RelayError("Source or HEAD changed during post-merge acceptance")
        base_commit(config)
        if not is_ancestor(config.root, oid, config.base_ref):
            raise RelayError("Merge commit disappeared from the target branch during acceptance")
        record.update(passed=True, finished_at=now())
    except (RelayError, KeyboardInterrupt) as error:
        record.update(error=str(error) or "Post-merge acceptance interrupted", finished_at=now())
        task.update(status="post_merge_failed", error=record["error"])
        raise
    finally:
        receipt_path = directory / "verification.json"
        receipt_path.write_text(json.dumps(record, indent=2) + "\n")
        task.update(post_merge=record, post_merge_path=str(receipt_path))
        state.save(task)
    return cleanup(config, state, task)


def merge(config, state, identifier):
    try:
        return _merge(config, state, identifier)
    except (RelayError, KeyboardInterrupt) as error:
        task = state.get(str(identifier))
        if task and (config.data["delivery"]["auto_merge"] or task.get("merge_sha")):
            if task["status"] == "completed":
                task["status"] = "post_merge_failed"
            elif task["status"] not in ("merged", "post_merge_failed", "merge_waiting", "review_blocked", "cleanup_failed"):
                task["status"] = "merge_blocked"
            task["error"] = str(error) or "Merge interrupted; read back GitHub before retrying"
            state.save(task)
        raise


def _merge(config, state, identifier):
    if config.provider != "github":
        raise RelayError("Automatic PR merge requires project.provider=github")
    task = state.get(task_id(identifier))
    if not task or not task.get("pr_url"):
        raise RelayError(f"Task {identifier} has no submitted PR")
    validate_remote(config)
    pull = pull_snapshot(config, task)
    if pull["state"] == "MERGED":
        if ((task.get("post_merge") or {}).get("passed")
                and task["post_merge"].get("config_sha256") == config.fingerprint):
            if (pull["headRefOid"] != task["head_sha"] or pull["headRefName"] != task["branch"]
                    or pull["baseRefName"] != config.base or pull["isCrossRepository"]
                    or (pull["mergeCommit"] or {}).get("oid") != task["merge_sha"]):
                raise RelayError("Merged PR readback differs from the completed task")
            assert_config(config)
            base_commit(config)
            if not is_ancestor(config.root, task["merge_sha"], config.base_ref):
                raise RelayError("Previously verified merge is no longer in the target branch")
            return cleanup(config, state, task)
        return post_merge(config, state, task, pull)
    if not config.data["delivery"]["auto_merge"]:
        raise RelayError("Merge is disabled; opt in with [delivery] auto_merge=true in project configuration")
    open_evidence(config, state, task, pull)
    if not current_review(task, config):
        task = review(config, state, identifier)
    if task["review"]["verdict"] != "approve":
        return task
    pull = pull_snapshot(config, task)
    open_evidence(config, state, task, pull)
    if pull["isDraft"]:
        gh(config, "pr", "ready", task["pr_url"])
    pull = pull_snapshot(config, task)
    open_evidence(config, state, task, pull)
    reasons = gate_reasons(config, pull)
    task["merge_gates"] = {"head_sha": task["head_sha"], "base_sha": pull["baseRefOid"],
                           "checked_at": now(), "reasons": reasons, "checks": pull["checks"]}
    if reasons:
        task.update(status="merge_waiting", error="; ".join(reasons))
        return state.save(task)
    task.update(status="merging", error=None)
    state.save(task)
    assert_config(config)
    # Re-read task authorization and base immediately before the exact-head mutation.
    open_evidence(config, state, task, pull)
    pull = pull_snapshot(config, task)
    open_evidence(config, state, task, pull)
    reasons = gate_reasons(config, pull)
    if reasons:
        task.update(status="merge_waiting", error="; ".join(reasons))
        task["merge_gates"].update(reasons=reasons, checked_at=now(), checks=pull["checks"])
        return state.save(task)
    request_error = None
    try:
        gh(config, "pr", "merge", task["pr_url"], "--squash", "--match-head-commit", task["head_sha"])
    except RelayError as error:
        request_error = error
    pull = pull_snapshot(config, task)
    if pull["state"] == "MERGED":
        return post_merge(config, state, task, pull)
    task.update(status="merge_waiting", error=str(request_error) if request_error else
                "Merge is pending on GitHub; rerun devrelay merge to read back the result")
    state.save(task)
    if request_error:
        raise request_error
    return task


def advance(config, state, identifier):
    """After an explicit submit, run only the delivery phases enabled by this project."""
    settings = config.data["delivery"]
    if settings["auto_merge"]:
        return merge(config, state, identifier)
    if settings["auto_review"]:
        task = claimed_task(config, state, identifier)
        if current_review(task, config):
            try:
                open_evidence(config, state, task, pull_snapshot(config, task))
            except RelayError as error:
                task.update(status="review_blocked", review=None, error=str(error))
                state.save(task)
                raise
            return task
        return review(config, state, identifier)
    return state.get(str(identifier))
