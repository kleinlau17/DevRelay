"""Small JSON-output CLI for project onboarding and bounded task runs."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from . import __version__
from .config import initialize, load
from .delivery import merge, review
from .errors import RelayError
from .git import has_ref, repository_root, validate_remote, worktrees
from .onboarding import setup_github
from .providers import add_local, queue_report, snapshot
from .runner import complete_local, run_task, start, submit, verify
from .state import State


def parser():
    root = argparse.ArgumentParser(prog="devrelay", description="Codex-powered, verified issue-to-PR development")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--project", type=Path, default=Path.cwd(), help="Project directory (default: current directory)")
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create project configuration and initialize GitHub issue management")
    init.add_argument("--provider", choices=("local", "github"))
    init.add_argument("--base", help="Target branch; detected from origin/HEAD, main, master or current branch")
    init.add_argument("--sync-github", action="store_true",
                      help="Add missing GitHub labels/templates using existing configuration; do not overwrite it")
    sub.add_parser("doctor", help="Check configuration, tools, login and base")
    sub.add_parser("next", help="Explain eligible tasks and exclusions; does not claim work")
    sub.add_parser("status", help="Read local task state and evidence locations")
    task = sub.add_parser("task", help="Manage explicit local tasks").add_subparsers(dest="task_command", required=True)
    add = task.add_parser("add", help="Add and approve one local task")
    add.add_argument("id")
    add.add_argument("--title", required=True)
    add.add_argument("--file", required=True, type=Path)
    add.add_argument("--priority", default="priority:P1")
    task.add_parser("list", help="List local task state")
    task.add_parser("complete", help="Mark a local task complete after integration into the base").add_argument("id")
    for name in ("start", "check", "submit", "resume", "review", "merge"):
        item = sub.add_parser(name, help={"start": "Claim a task and create or recover its worktree",
                                         "check": "Run configured acceptance and save exact-source evidence",
                                         "submit": "Commit verified changes and create/update a draft PR",
                                         "resume": "Continue one saved worktree and its exact Codex session",
                                         "review": "Review a submitted PR in a fresh read-only Codex session",
                                         "merge": "Apply enabled merge gates and verify the merged commit"}[name])
        item.add_argument("id")
        if name == "submit":
            item.add_argument("--local", action="store_true", help="Create a local commit without push or PR")
        if name == "resume":
            item.add_argument("--submit", action="store_true", help="Submit a draft PR after checks pass")
    run = sub.add_parser("run", help="Implement and verify one task, recovering interrupted work first")
    run.add_argument("id", nargs="?")
    run.add_argument("--submit", action="store_true", help="Submit a draft PR after checks pass")
    return root


def probe(argv, cwd):
    try:
        return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def doctor(config):
    executable = config.data["codex"]["executable"]
    checks = [{"id": "git", "ok": shutil.which("git") is not None},
              {"id": "base", "ok": has_ref(config.root, config.base_ref), "value": config.base_ref},
              {"id": "acceptance", "ok": bool(config.data["checks"]), "count": len(config.data["checks"])},
              {"id": "codex", "ok": shutil.which(executable) is not None},
              {"id": "codex_login", "ok": probe([executable, "login", "status"], config.root)},
              {"id": "git_identity", "ok": probe(["git", "var", "GIT_AUTHOR_IDENT"], config.root)}]
    if config.provider == "github":
        try:
            validate_remote(config)
            matches = True
        except RelayError:
            matches = False
        checks += [{"id": "gh", "ok": shutil.which("gh") is not None},
                   {"id": "github_login", "ok": probe(["gh", "auth", "status", "--hostname", "github.com"], config.root)},
                   {"id": "remote_matches_repository", "ok": matches}]
    return {"ready": all(item["ok"] for item in checks), "provider": config.provider,
            "checks": checks, "notes": ["Login checks do not call a model or verify model access.",
                                         "Review setup and acceptance commands before execution."]}


def controller(root, state):
    saved = state.metadata("controller_root")
    if not saved:
        return root
    path = Path(saved)
    if not path.is_dir() or not any(Path(item["worktree"]).resolve() == path.resolve() for item in worktrees(root)):
        raise RelayError("Saved controller checkout moved or disappeared; restore it before running tasks")
    return path


def main(argv=None):
    args = parser().parse_args(argv)
    state = None
    try:
        root = repository_root(args.project)
        state = State(root)
        root = controller(root, state)
        if args.command == "init":
            with state.lock():
                if args.sync_github:
                    if args.provider or args.base:
                        raise RelayError("--sync-github uses existing configuration; omit --provider and --base")
                    config = load(root)
                    result = {"config": str(root / ".devrelay/config.toml"), "provider": config.provider}
                else:
                    result = initialize(root, args.provider, args.base)
                    config = load(root)
                state.set_metadata("controller_root", str(root))
                if config.provider == "github" or args.sync_github:
                    result["github"] = setup_github(config)
        else:
            config = load(root)
            if args.command == "doctor":
                result = doctor(config)
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 0 if result["ready"] else 1
            elif args.command in ("status", "task") and (args.command == "status" or args.task_command == "list"):
                result = {"project": str(root), "tasks": state.tasks()}
            elif args.command == "next":
                result = queue_report(config, snapshot(config, state))
            else:
                with state.lock():
                    if args.command == "task" and args.task_command == "add":
                        result = add_local(config, state, args.id, args.title, args.file.read_text(), args.priority)
                    elif args.command == "task":
                        result = complete_local(config, state, args.id)
                    elif args.command == "start":
                        result = start(config, state, args.id)
                    elif args.command == "check":
                        result = verify(config, state, args.id)
                    elif args.command == "submit":
                        result = submit(config, state, args.id, local=args.local)
                    elif args.command == "review":
                        result = review(config, state, args.id)
                    elif args.command == "merge":
                        result = merge(config, state, args.id)
                    else:
                        result = run_task(config, state, args.id, resume=args.command == "resume", submit_after=args.submit)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (RelayError, OSError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"error": "Interrupted; saved work is retained. Use devrelay status and resume."}), file=sys.stderr)
        return 130
    finally:
        if state:
            state.close()
