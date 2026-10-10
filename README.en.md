# DevRelay

English | [简体中文](README.md)

Move explicit development tasks to verified pull requests and optional automatic merges with Codex and a small project configuration.

DevRelay owns task selection, Git worktrees, Codex execution, acceptance evidence and PR delivery. Projects own setup commands and acceptance rules. Version 0.4 supports macOS / Linux, Python 3.11+ and Git, with no third-party runtime dependencies.

## Install and onboard

```bash
cd /path/to/DevRelay
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/devrelay --help
```

Alternatively, use `pipx install /path/to/DevRelay` for a global command. AI execution requires an installed, signed-in Codex CLI. GitHub tasks and PR submission also require authenticated `gh`. Existing authentication is reused; credential files are not read or copied.

```bash
devrelay --project /path/to/project init
# Review /path/to/project/.devrelay/config.toml
devrelay --project /path/to/project doctor
```

Initialization detects GitHub origin, the default branch, Node package managers and common scripts, Python installation, Cargo and Go checks. It creates TOML configuration and appends runtime ignores (including Python environments and installation artifacts) without overwriting existing configuration or AGENTS.md. It does not install packages or call a model. Review watch modes, service requirements and actual business acceptance. Python test frameworks are not guessed; configure the real runner.

Commands use argument arrays, with no shell expansion. Setup runs in the task worktree. At least one acceptance command is required before run/check/submit. Commit the project configuration, ignore rules and generated issue templates. Empty repositories need an initial commit. Codex uses the user's default model unless `codex.model` or `codex.profile` is set. See [configuration](examples/config.toml).

```toml
[project]
provider = "github"
repository = "owner/project"
base_branch = "main"

[codex]
sandbox = "workspace-write"
max_attempts = 2

[[checks]]
id = "test"
argv = ["python3", "-m", "unittest", "discover", "-s", "tests"]
```

## GitHub queue

### Labels and issue templates

For GitHub projects, first-time `init` uses authenticated `gh` to create missing repository labels and generates `.github/ISSUE_TEMPLATE/devrelay-{bug,task,experiment,plan}.md`. Label creation requires the corresponding repository permission. Local onboarding remains offline. Labels are created on GitHub immediately; templates are local files and appear in GitHub's issue chooser after being committed to the repository default branch. Initialization does not commit or push files.

Defaults follow DexLab's management rules:

| Dimension | Labels |
|---|---|
| Primary work type | `bug`, `enhancement`, `documentation`, `experiment`, `infrastructure` |
| Relevant area | `area:backend`, `area:benchmark`, `area:cloth`, `area:contact`, `area:evaluation`, `area:hardware` |
| Exactly one priority | `priority:P0` (urgent fixes), `priority:P1` (core delivery), `priority:P2` (extensions) |
| Authorization | `auto:approved` |
| Blockers | `blocked`, `needs-input` |

Templates record evidence, scope, acceptance commands, one `Depends on: none` declaration, resources and recovery conditions. Bug, task and experiment templates pre-apply only the work type. The plan template tracks bounded child deliveries and explicitly excludes execution authorization on the parent. **Generated templates never automatically apply `auto:approved`, a configured authorization label, or a priority to new issues.** Creating a repository label does not authorize an issue. Maintainers separately select one priority and grant execution authorization after confirming the scope and acceptance; dependencies and blockers still apply.

Existing label names, colors and descriptions, template files and other forms are preserved. Preserved project templates retain their own defaults and should be reviewed separately. Add project-specific area labels as needed. For already-onboarded projects or recovery after network/permission errors:

```bash
devrelay --project /path/to/project init --sync-github
```

This uses existing configuration without overwriting configuration, ignore rules or templates, adding only missing items. Custom `[queue]` authorization, priority and blocker labels are honored and described in generated templates; a type label reused as a queue control is not pre-applied. Uncertain creation responses are read back before continuing. Only complete template files are published, so interrupted writes can recover missing files. Required archived labels or queue-label spelling mismatches require a maintainer to restore the label or align configuration.

### Selection and delivery

Tasks must be open, carry `auto:approved`, exactly one `priority:P0/P1/P2`, no `blocked` / `needs-input`, and exactly one declaration: `Depends on: none` or `Depends on: #12, #27`. Dependencies must be closed and linked to merged PRs whose merge commits are ancestors of the refreshed target branch. Existing worktrees and explicitly associated open PRs prevent duplicate claims.

```bash
devrelay next
devrelay start 12
devrelay run 12
devrelay status
devrelay check 12
devrelay submit 12
```

`next` explains selection and exclusions without claiming work; it reads GitHub and fetches the base. `start` creates or recovers a worktree. `run` handles one task, recovering interrupted work first when no ID is given; failed or blocked work returns `needs_attention` until explicitly addressed. Codex implements in `.devrelay/worktrees/ID`; DevRelay runs configured checks. Failed checks feed back into the same saved Codex session within `max_attempts`. External blockers are retained.

`submit` requires current verification evidence, commits, pushes and creates or updates a **draft PR**. `run --submit` combines the steps. By default delivery stops at draft; opt into review and merge below. PRs use `Refs #N`, preserving issue scope. DevRelay does not close whole issues, release, deploy or create background schedules.

## Automatic review and merge

Configure and commit this policy before running tasks:

```toml
[delivery]
auto_review = true
auto_merge = true
required_checks = ["test", "build"] # Actual GitHub check names
allow_no_ci = false
cleanup_worktrees = true
cleanup_branches = true
```

Both automatic switches default to `false`. Review alone retains draft status. Automatic merge includes review, marks an approved draft ready and requests squash merging when gates pass. Named required checks prevent missing or not-yet-created CI from being ignored; all reported checks must also succeed. Skipped, neutral, failing and pending checks do not pass. No CI evidence blocks by default. Explicitly set `allow_no_ci = true` only for projects without GitHub CI; local acceptance, AI review and any named required checks still apply.

```bash
devrelay run 12 --submit   # Implement → check → draft → review → merge → post-merge checks
devrelay review 12         # Explicit fresh, read-only AI review
devrelay merge 12          # Requires auto_merge; recheck gates or recover a merge
devrelay resume 12 --submit
```

Review uses a fresh Codex session with the `read-only` sandbox, preserving the implementation session. Reports remain local; AI review does not submit a GitHub APPROVE or replace required human approvals. Negative/incomplete review, source/head/base/configuration/issue drift, withdrawn authorization, unresolved threads and GitHub merge restrictions block merging. API errors or truncated inventories fail closed; up to 100 checks, reviews and threads are supported per snapshot.

Merge uses `--squash --match-head-commit SHA`, with no admin bypass. A successful request is not completion: DevRelay reads back the merged state and commit. CI waits and merge queues return `merge_waiting`; rerun `merge` or `run --submit` to advance. Each invocation is bounded; there is no background polling.

After merge, DevRelay fetches the base, verifies commit ancestry and tree equality, then runs every setup/check command in a fresh detached worktree. Only successful acceptance records `completed`. API timeout recovery reads GitHub first and never re-merges an already merged PR. Unexpected trees or failed post-merge checks record `post_merge_failed`, retain evidence and failed worktrees, and block automatic dispatch without an ID. GitHub's head condition does not atomically lock the base, so pre-merge refresh and post-merge tree verification both matter. A post-merge failure needs maintainer remediation; DevRelay does not automatically revert a completed remote merge.

To revise a PR, fix its preserved worktree and run `check ID`, then `submit ID`. New commits invalidate old reviews. Configuration changes require fresh acceptance too. Maintainers still own issue closure and dependency delivery links.

### Post-merge cleanup

`cleanup_worktrees` and `cleanup_branches` default to `false`; opt in before running tasks. Worktree cleanup removes the successful task's implementation checkout. Branch cleanup removes its local, remote and remote-tracking branches and requires worktree cleanup too. Enable only worktree cleanup to retain branches.

Cleanup requires a merged PR, its merge commit in the refreshed base, and successful post-merge setup/checks. Failed acceptance retains the task branches, implementation checkout and failed acceptance worktree. Even after a successful retry, previous failed worktrees remain listed in `retained_worktrees`. Successful temporary detached acceptance worktrees are reclaimed as before.

Only recorded task paths and exact commits qualify. Worktrees are never force-removed or broadly pruned. New tracked/non-ignored files, locks, advanced branches and other worktrees using the branch stop cleanup. Removing a worktree also removes its ignored environments, caches and outputs; keep durable files outside it. Verification, review, cleanup logs and shared task state remain in the Git common directory.

Cleanup errors record `cleanup_failed` separately from successful acceptance and pause automatic dispatch without an ID. Some earlier steps may already have removed resources. Resolve permissions, locks or new changes, then run `devrelay merge ID` to inspect actual Git state and resume cleanup without re-merging or repeating still-valid acceptance. A branch already deleted by GitHub is handled idempotently. Remote deletion and local ref deletion match the expected SHA; changed branches are retained. Disable GitHub's own automatic branch deletion if DevRelay must control deletion timing: it cannot delay externally triggered deletion.

## Local tasks

```bash
devrelay init --provider local
devrelay task add fix-1 --title "Fix a bounded issue" --file /path/to/task.md
devrelay run fix-1
devrelay submit fix-1 --local
```

Adding a local task explicitly approves it; a missing dependency declaration becomes `Depends on: none`. Local dependencies may use `Depends on: #fix-1`. Local submission creates only a verified local commit. Integrate it into the configured base, then call `devrelay task complete fix-1` before dependent tasks can run. See the [task template](examples/task.md).

## Evidence and recovery

Verification binds source tree, HEAD, target base, configuration and task requirements. Temporary Git indexes hash tracked changes and non-ignored new files without modifying the original index. Checked-out submodules are inspected recursively for tracked changes and non-ignored untracked files. Isolated-index comparisons neutralize `assume-unchanged`, `skip-worktree`, `core.fileMode=false`, `submodule.*.ignore`, `.gitmodules` ignore settings and status configuration that could hide those changes. Any dirty submodule blocks acceptance, submission and automatic review/merge gates. DevRelay never commits, cleans, resets or updates submodule work.

Clean committed submodule HEAD changes are included in the source tree through the parent gitlink and can be delivered normally. An uninitialized submodule has no worktree to inspect and is represented by the gitlink recorded in its parent; projects whose checks need submodule contents should initialize them explicitly in `[[setup]]`. Source/history/base/configuration/requirements drift invalidates evidence. Integrate a newer base manually and rerun checks; existing work is never reset automatically.

SQLite state, a repository-wide worker lock, command logs, resource receipts, verification and Codex JSON events live under `devrelay/` in the Git common directory. Every attempt gets a new directory. `devrelay resume ID` uses that task's exact session ID, never `--last`. Timeouts terminate process groups; surviving descendants prevent another worker from starting until they finish.

Acceptance commands are trusted project code. A zero exit code does not establish business correctness or independent review. Projects control scoring rules, official engine requirements and delivery authorization.

## Develop

This repository's `.devrelay/config.toml` enables self-maintenance: approved GitHub Issues are implemented in isolated worktrees, verified with the full test suite, submitted as draft PRs, and reviewed in fresh read-only AI sessions. Automatic merge is disabled; run `devrelay merge ID` after merging to perform post-merge acceptance and cleanup.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```

Tests use real temporary Git repositories and a fake Codex CLI. Delivery tests push and squash-merge in temporary repositories, run fresh post-merge checks, and mock GitHub review/CI APIs and timeout recovery. They do not call models or push public repositories. Real target projects still require their own acceptance. See [architecture](docs/architecture.md) and [official Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive).
