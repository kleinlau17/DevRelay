# Architecture / 架构

## Responsibilities / 职责

| Module | Responsibility / 职责 |
|---|---|
| `config` | Validated TOML, command discovery / 配置校验与命令识别 |
| `onboarding` | Add missing GitHub labels and safe issue templates / 补齐标签与不预设执行授权的模板 |
| `providers` | Local and GitHub snapshots, priorities, dependencies / 任务快照、优先级与依赖 |
| `git` | Worktrees and source hashing / 工作树与源码哈希 |
| `state` | Shared SQLite state and POSIX single-worker lock / 状态与单 worker 锁 |
| `codex` | Noninteractive CLI, explicit sessions, structured outcomes / CLI、精确会话与结构化结果 |
| `process` | Timeout, process groups and unique receipts / 超时、进程组与独立回执 |
| `runner` | Claim → prepare → implement → verify → submit / 工作流 |
| `delivery` | Fresh AI review, GitHub merge gates, post-merge acceptance / 审查、合并门禁及合并后验收 |
| `cleanup` | Opt-in exact-ref cleanup after acceptance, durable recovery / 验收后清理与恢复 |

## Task lifecycle / 任务生命周期

```text
queued → claimed → preparing → prepared → implementing → verifying → verified
                                   ↑             ↓           ↓         ↓
                                resume        blocked      failed   committed → pr_open
                                                                         ↓
                                                              completed (local, integrated)

pr_open → reviewing → reviewed → merge_waiting → merging → merged → cleaning → completed
              ↓                                            ↓          ↓
        review_blocked                               post_merge_failed cleanup_failed
```

State is saved before long-running phases. Main-checkout configuration controls commands even when a task changes its own copy. Verification is independent of the worker's completion report. Explicit resume preserves code and targets only the saved session. GitHub remains authoritative for approval and integrated dependencies.

长时间运行前保存状态；主 checkout 配置控制实际执行命令。worker 的完成报告不等于验收通过；恢复保留代码且只使用该任务会话。GitHub 是授权与依赖交付状态的来源。

## Boundaries / 边界

- One controller, one worker per Git repository; POSIX lock, no distributed leases.
- GitHub onboarding creates only missing repository labels and local template files under the worker lock. It never assigns issue labels or authorization. Existing files/definitions remain intact; uncertain creates are read back and `init --sync-github` repairs partial setup. Templates become public only after normal Git delivery to the default branch.
- Each invocation has bounded attempts and command timeouts; explicit resume may grant a new bounded attempt window.
- Default Codex sandbox is workspace-write; approval policy is never, so permission failures become actionable failures rather than unattended prompts.
- Delivery defaults to draft PR. Project configuration may opt into fresh read-only AI review and exact-head squash merging; GitHub human approvals and protection still apply.
- Review binds PR URL, head, base, tree, configuration and task requirements; CI is read from the exact head commit. Truncated API data fails closed. Default no-CI policy blocks, with an explicit project opt-out.
- GitHub HEAD matching is atomic, base matching is not. Refresh before merge and verify the merged tree afterwards. Post-merge setup/checks run in a separate detached worktree; failure preserves the merge checkpoint and blocks new automatic dispatch.
- Cleanup defaults off for task resources. Successful acceptance scratch trees are removed; failed trees/evidence remain. Optional task worktree and exact-head branch cleanup checkpoints each step; recovery reads actual refs without assuming the original checkout still exists.
- Remote deletion uses an explicit expected SHA lease; local/remote-tracking ref deletion uses compare-and-swap. Worktree removal is never forced. Git operations outside DevRelay still require coordination with the repository worker.
- Completed GitHub tasks stay claimed in shared state even after their branches/worktrees are removed, preventing repeat dispatch of still-open scoped Issues.
- No scheduler, remote compute adapter, automatic conflict resolution, release adapter or cross-repository transaction in version 0.4.
- Project commands may require services, credentials or hardware; DevRelay does not provision them.
- Process markers detect remaining foreground groups after controller crashes. Commands must not daemonize or escape the group. There is a brief spawn-to-marker crash window; this is cooperative recovery, not an OS isolation guarantee.

默认单仓库、单 worker、有限重试并停在 draft PR；项目可明确开启审查与合并。审查绑定准确提交与 base；CI、GitHub 审查意见和保护条件另行核验。合并后在独立工作树重跑 setup/checks；失败保留远端已合并的事实和本地证据。调度、远端执行、多仓库事务与发布留给后续适配。项目命令负责业务环境。进程组恢复要求命令不脱离前台进程组；进程启动与标记之间仍有短暂中断窗口，不能宣称操作系统级隔离。
