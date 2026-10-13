# Agent workflow / Agent 工作流

DevRelay is an engineering toolkit for an external agent. The agent decides what to
investigate, how to implement and how results affect the next task; the tools enforce
project authorization, dependencies, source-bound acceptance and delivery policy.

DevRelay 为外层 Agent 提供工程工具。Agent 决定调查方向、实现方式和结果对后续工作的
影响；工具执行项目授权、依赖、绑定源码的验收和交付策略。

## Plan and explore / 规划与探索

- Inspect the applicable AGENTS.md, owner code, existing Issues, PRs and evidence.
  State unknowns and propose alternatives before turning a goal into tasks.
- Use a tracking parent for an overall plan (`scope:plan` or `Task kind: plan`).
  Each executable child delivers one observable result with its required code,
  configuration, tests and documentation. Split only independently valuable outcomes.
- An exploration task declares its question, resource/time budget, reproducible
  evidence and stopping condition. A negative experimental result can satisfy the
  task when the agreed acceptance requires faithful execution and complete evidence.
- Publish tasks and grant execution labels only within the maintainer's authorization.
  Planning does not itself authorize execution or a change in acceptance. Local
  `task add` explicitly authorizes its task; use it only for already authorized work.
- Update the plan from actual results. Keep superseded conclusions and failed trials
  traceable. A change in scope, requirements or acceptance needs the corresponding
  authorization and new verification.

先读项目指令、所属代码、已有任务和证据，明确未知项与候选方案。总体计划只跟踪子任务；
可执行子任务按独立结果划分，包含该结果所需的代码、配置、测试与文档。有界探索明确
问题、资源与时间预算、复现证据和停止条件；负结果也可满足如实执行与完整证据的验收。
发布和授权均服从维护者已有授权，计划本身不授权执行；本地 `task add` 会直接授权任务。
根据实际结果更新计划并保留历史结论与失败尝试，范围或验收变化需对应授权及重新验证。

## Engineering loop / 工程循环

Run commands from the project controller checkout, keeping `--project` explicit when
working elsewhere. Parse JSON output; use the returned `worktree`, not a guessed path.

从主 checkout 调用；在其他目录操作时显式指定 `--project`。读取 JSON，并使用返回的
`worktree`，不要猜工作树路径。

```bash
devrelay --project /path/to/project doctor
devrelay --project /path/to/project status
devrelay --project /path/to/project next
devrelay --project /path/to/project start 12
devrelay --project /path/to/project prepare 12
# External agent investigates and edits in the returned worktree.
# 外层 Agent 在返回的工作树中调查和实现。
devrelay --project /path/to/project check 12
devrelay --project /path/to/project submit 12
```

| Tool | Agent action / Agent 的处理 |
| --- | --- |
| `status` | Inspect existing work before selecting another task / 先检查已有现场 |
| `next` | Read the suggested eligible task and exclusion reasons; does not claim / 读取候选与排除原因，不领取 |
| `start ID` | Claim or recover a worktree; no setup or model call / 领取或恢复工作树，不准备环境、不调用模型 |
| `prepare ID` | Run configured setup, retaining logs; every invocation invalidates old checks / 执行配置的环境准备并保存日志，每次使旧验收失效 |
| `check ID` | Run acceptance and inspect failures; repair then recheck / 执行验收，失败时读取证据、修复并重验 |
| `submit ID` | Require current evidence, then deliver under project policy / 用有效证据按项目策略交付 |

Preparation operates on a claimed, unfinished task, including a
local commit or an open PR, and never starts an agent. Existing PR identity and state
are checked; closed, merged, actively merging and mismatched tasks are rejected.
Explicit calls rerun setup, so project setup commands should support retries. Fix
failed or interrupted setup and retry `prepare`. When no setup is configured, it
records preparation without running a command. It never resets the worktree.

Configured setup must have succeeded for the current configuration before acceptance
or delivery. Repreparation invalidates verification, AI review and merge gates while
retaining their historical evidence. After environment changes integrate the target
base, prepare, check and submit again; reuse the original PR. Submission rechecks
mutable requirements at publication boundaries and preserves partial delivery on error.

环境准备作用于已领取且未完成交付的任务，包括本地提交和开放 PR，不启动 Agent；已有 PR
会核对身份和状态，已关闭、已合并、正在合并或身份不符时拒绝准备。显式调用会重跑 setup，项目命令
应支持重试；失败或中断后修复原因，再运行 `prepare`。未配置 setup 时仅记录准备状态。
它不重置工作树。

配置了 setup 时，必须在当前配置下成功准备后才能验收与交付。重新准备会使验收、AI
审查和合并门禁失效，历史证据保留。环境变化后集成目标分支，再准备、验收、提交并复用
原 PR。提交在发布边界重新比较可变要求；发生错误时保留已经完成的交付步骤。

Submit defaults to a draft PR. Enabled automatic review/merge may invoke Codex and
advance delivery even in agent-driven mode. Explicit `review` also invokes Codex;
`doctor --with-codex` checks that dependency. Existing human approval and GitHub branch
protection requirements remain applicable. Local projects use `submit ID --local`,
then `task complete ID` after integration into the configured base.

Local completion checks that the latest accepted source is the delivered commit,
the worktree is unchanged, requirements/configuration still match, and the commit is
integrated. A new check alone does not integrate new content or unlock dependencies.

提交默认停在草稿 PR；即使由外层 Agent 驱动，已启用的自动审查或合并策略仍可能调用
Codex 并推进交付。显式 `review` 也调用 Codex，用 `doctor --with-codex` 检查其依赖。
既有人工批准和分支保护要求仍适用。本地项目用 `submit ID --local`，集成到目标分支后
再用 `task complete ID` 标记完成。

本地完成会核对最新验收源码与交付提交一致、工作树未变化、要求与配置一致且提交已集成。
仅重新验收不能交付新增内容或解锁依赖。

## Recovery and optional orchestration / 恢复与可选编排

The external host owns its agent session and a checkpoint linking the goal, task ID,
worktree and evidence. DevRelay stores tool state and receipts, not that conversation.
On recovery inspect `status`, actual files, base and evidence before retrying a tool.
Changed source, base, configuration or requirements require fresh acceptance.

The repository lock covers tool execution only. It does not reserve an external
editor between calls. The host must enforce one active implementation owner and
stop live editors before handing a task to optional `run/resume/watch`. DevRelay does
not detect arbitrary external agents. `resume` resumes only a DevRelay-managed Codex
implementation session; it does not recover the external host's conversation.

外层宿主负责会话和关联目标、任务 ID、工作树与证据的检查点。DevRelay 保存工具状态和
回执，不保存外层对话。恢复先检查 `status`、实际文件、目标分支和证据；源码、base、
配置或要求改变后重新验收。仓库锁仅覆盖工具执行，不保留两次调用之间的编辑独占权。
宿主须保证同一任务只有一个实现者，交给可选 `run/resume/watch` 前停止原编辑者。
DevRelay 不检测任意外层 Agent；`resume` 只恢复 DevRelay 自己启动的 Codex 实现会话。
