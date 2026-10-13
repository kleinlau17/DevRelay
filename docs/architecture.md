# Architecture / 架构

## Agent-driven tools / Agent 驱动的工程工具

The primary caller is an external agent. It owns planning, exploration, implementation,
interpretation of evidence and session recovery. It calls `next`, `start`, `prepare`,
`check` and `submit` as separate engineering operations. `prepare` runs configured setup
without starting an implementation agent; each call invalidates previous verification
and preserves setup logs. Failed setup is explicitly retried. `check` and delivery use
the same evidence and authorization gates for every caller.

Configured setup is required under the current configuration at check and delivery
gates. Unmerged tasks can reprepare after PR identity readback, invalidating verification,
review and merge-gate records while retaining their logs. Publication rechecks mutable
requirements at each boundary. Local completion binds the latest accepted source to
the actual delivered commit and its integration, rather than an older ancestor alone.

主要调用方是外层 Agent，负责规划、探索、实现、解释证据和自身会话恢复，逐步调用
`next/start/prepare/check/submit`。`prepare` 执行项目 setup，不启动实现 Agent；每次调用
使旧验收失效并保留准备日志，失败后明确重试。所有调用方共用验收与交付门禁。

配置了 setup 时，验收与交付门禁要求当前配置下的成功准备。未合并任务可在 PR 身份
回读通过后重新准备，使验收、审查和合并门禁记录失效但保留日志。发布边界重新比较可变
要求；本地完成绑定最新验收源码、实际交付提交及集成结果，不能仅凭旧提交的祖先关系。

`run/resume/watch` are optional adapters that launch Codex and manage its implementation
session. They are explicitly invoked, not required for engineering tools. AI review is
also optional and currently uses Codex. `doctor` only checks Codex when requested with
`--with-codex` or required by enabled automatic delivery. No planning scheduler or
second task database is introduced; Issues and task files carry plans and requirements.

`run/resume/watch` 是显式调用的可选 Codex 编排，管理它启动的实现会话；工程工具不依赖
该路径。可选 AI 审查当前也使用 Codex。`doctor` 仅在指定 `--with-codex` 或启用自动 AI
交付时检查 Codex。计划与要求由 Issues 和任务文件承载，不增加规划调度器或第二套任务库。

## Responsibilities / 职责

| Module | Responsibility / 职责 |
|---|---|
| `config` | Validated TOML, command discovery / 配置校验与命令识别 |
| `onboarding` | Add missing GitHub labels and safe issue templates / 补齐标签与不预设执行授权的模板 |
| `providers` | Local and GitHub snapshots, priorities, dependencies / 任务快照、优先级与依赖 |
| `git` | Worktrees and source hashing / 工作树与源码哈希 |
| `state` | Shared SQLite state and POSIX single-worker lock / 状态与单 worker 锁 |
| `codex` | Optional implementation/review adapter, explicit sessions and structured outcomes / 可选实现与审查适配器、精确会话与结构化结果 |
| `process` | Timeout, process groups and unique receipts / 超时、进程组与独立回执 |
| `runner` | Agent-callable claim/setup/check/submit plus optional implementation loop / Agent 工程操作与可选实现循环 |
| `worker` | Optional continuous Codex dispatch through draft PRs / 可选持续 Codex 调度，交付停在 draft PR |
| `delivery` | Fresh AI review, GitHub merge gates, post-merge acceptance / 审查、合并门禁及合并后验收 |
| `cleanup` | Opt-in exact-ref cleanup after acceptance, durable recovery / 验收后清理与恢复 |

## Task lifecycle / 任务生命周期

```text
Primary tools / 主要工具:
queued → claimed → preparing → prepared → [external agent edits] → verifying → verified
                       ↓                                             ↓          ↓
                 failed/interrupted                                failed   committed → pr_open

Optional Codex orchestration / 可选 Codex 编排:
queued → claimed → preparing → prepared → implementing → verifying → verified
                                   ↑             ↓           ↓         ↓
                                resume        blocked      failed   committed → pr_open
                                                                         ↓
                                                              completed (local, integrated)

pr_open → reviewing → reviewed → merge_waiting → merging → merged → cleaning → completed
              ↓                                            ↓          ↓
        review_blocked                               post_merge_failed cleanup_failed
```

State is saved before long-running phases. Main-checkout configuration controls commands even when a task changes its own copy. Verification is independent of any agent's completion report. Optional Codex resume preserves code and targets only the saved session. External sessions remain owned by their host. GitHub remains authoritative for approval and integrated dependencies.

长时间运行前保存状态；主 checkout 配置控制实际执行命令。Agent 的完成报告不等于验收通过；可选 Codex 编排的恢复保留代码且只使用该任务会话，外层会话由宿主管理。GitHub 是授权与依赖交付状态的来源。

## Boundaries / 边界

- One controller, one worker per Git repository; POSIX lock, no distributed leases.
- In agent-driven mode the lock covers each tool call, not external editing between calls. The host must serialize editors and optional orchestration for a repository. External agent sessions are not stored or resumed by DevRelay.
- GitHub onboarding creates only missing repository labels and local template files under the worker lock. It never assigns issue labels or authorization. Existing files/definitions remain intact; uncertain creates are read back and `init --sync-github` repairs partial setup. Templates become public only after normal Git delivery to the default branch.
- One issue delivers one reviewable result; split children only for independent value or acceptance criteria. Templates and implementation/review prompts state this scope policy. Queue eligibility and explicit-ID authorization reject tracking parents marked `scope:plan`, `Task kind: plan`, or the original plan-template child-delivery heading, even with execution approval. This is marker-based enforcement, not semantic classification of arbitrary issue prose. / 一个 Issue 对应一个可审查结果，仅有独立价值或验收标准才拆子项；模板、执行和审查提示词约束粒度。队列与显式 ID 授权检查拒绝已标记的总体计划父任务，即使误加执行授权；检查依据明确标记，不自动理解任意正文。
- Each task has bounded attempts and command timeouts; explicit resume may grant a new bounded attempt window. `watch` is a foreground loop holding the repository worker lock through execution and idle waits; it stops on errors or attention states, and reuses the existing evidence and authorization gates. Submitted/reviewed drafts do not block new dispatch. It requires automatic merge to be disabled, and configuration changes stop it before further dispatch.
- Default Codex sandbox is workspace-write; approval policy is never, so permission failures become actionable failures rather than unattended prompts.
- Delivery defaults to draft PR. Project configuration may opt into fresh read-only AI review and exact-head squash merging; GitHub human approvals and protection still apply.
- Review binds PR URL, head, base, tree, configuration and task requirements; CI is read from the exact head commit. Truncated API data fails closed. Default no-CI policy blocks, with an explicit project opt-out.
- GitHub HEAD matching is atomic, base matching is not. Refresh before merge and verify the merged tree afterwards. Post-merge setup/checks run in a separate detached worktree; failure preserves the merge checkpoint and blocks new automatic dispatch.
- Cleanup defaults off for task resources. Successful acceptance scratch trees are removed; failed trees/evidence remain. Optional task worktree and exact-head branch cleanup checkpoints each step; recovery reads actual refs without assuming the original checkout still exists.
- Remote deletion uses an explicit expected SHA lease; local/remote-tracking ref deletion uses compare-and-swap. Worktree removal is never forced. Git operations outside DevRelay still require coordination with the repository worker.
- Completed GitHub tasks stay claimed in shared state even after their branches/worktrees are removed, preventing repeat dispatch of still-open scoped Issues.
- No OS service installer, remote compute adapter, automatic conflict resolution, release adapter or cross-repository transaction. Continuous issue dispatch is explicitly started with `watch`; it does not create background schedules.
- Project commands may require services, credentials or hardware; DevRelay does not provision them.
- Process markers detect remaining foreground groups after controller crashes. Commands must not daemonize or escape the group. There is a brief spawn-to-marker crash window; this is cooperative recovery, not an OS isolation guarantee.

默认单仓库、单 worker、有限重试并停在 draft PR；项目可明确开启审查与合并。`watch` 持续串行调度，空队列等待期间也保留 worker 锁，要求关闭自动合并；已提交/审查的 draft 不阻止领取下一任务，错误或人工处理状态会停止调度。审查绑定准确提交与 base；CI、GitHub 审查意见和保护条件另行核验。合并后在独立工作树重跑 setup/checks；失败保留远端已合并的事实和本地证据。后台服务安装、远端执行、多仓库事务与发布留给后续适配。项目命令负责业务环境。进程组恢复要求命令不脱离前台进程组；进程启动与标记之间仍有短暂中断窗口，不能宣称操作系统级隔离。
