# Multi-repository autodev MVP / 多仓自动开发 MVP

One authorized task owns one observable outcome across every repository explicitly
listed in a workspace manifest. Repository work units are execution records, not
separate product requirements. Existing single-project commands keep their behavior.

一个明确授权的任务在工作区列出的所有仓库共同交付一个可观察结果。各仓的工作单元是
执行记录，不要求按前后端或文件边界拆成独立需求。原有单仓入口保持行为。

## Configure / 配置

Copy [the example](../examples/workspace.toml) and set actual repository controller
paths, target branches, setup, acceptance and integration commands. Relative paths
resolve against the manifest directory, so a workspace does not need to be a Git
repository. Each member needs at least one check, plus at least one workspace check.
Duplicate Git identities and nested participating repositories are rejected; a
submodule may be prepared as a pinned dependency by its parent's setup command.

复制例子并填写实际控制 checkout、目标分支、准备、验收及联合检查。相对路径以配置文件
目录为起点；工作区本身不必是 Git 仓库。各仓至少一条检查，工作区至少一条联合检查。
重复 Git 仓库和嵌套参与仓库被拒绝；父仓 setup 可准备固定版本子模块依赖。

Commands are argument arrays without shell expansion. Integration `cwd` is a member
key or `workspace` (the manifest directory). Setup and check processes receive:

- `DEVRELAY_WORKTREE_<UPPERCASE_KEY>`: the exact claimed worktree for each member.
- `DEVRELAY_WORKSPACE`: the manifest directory.
- `DEVRELAY_EVIDENCE_DIR`: this task's retained artifact directory.

命令使用参数数组，不做 shell 展开。联合检查的 cwd 选择仓库 key 或 workspace。
环境变量提供每仓实际工作树、配置目录和任务证据目录。脚本由项目负责，可启动本地依赖，
但必须保持前台、有界执行并在退出前关闭服务；DevRelay 不准备业务服务或凭据。

Runtime is `.devrelay/workspace-runtime/<manifest-stem>/` beside the manifest.
Ignore that directory in Git. Keep the manifest and task requirements under normal
version control; do not put credentials in either. The immutable manifest fingerprint,
requirement file and task registration are checked at every mutation boundary.
Changing registered requirements/configuration requires restoring them or registering
a new authorized task; the MVP does not silently transfer authorization.

运行目录位于配置旁的 `.devrelay/workspace-runtime/<配置名>/`，请加入 Git 忽略。
配置和任务要求正常版本管理，不写密钥。每次操作检查配置指纹、要求文件和登记记录；
要求或配置变更需恢复原内容或登记新授权任务，本版不自动转移授权。

## Execute / 执行

```bash
devrelay --workspace /path/workspace.toml doctor --with-codex
devrelay --workspace /path/workspace.toml task add terrain-edit \
  --title "Preserve terrain state during local edits" --file /path/task.md
devrelay --workspace /path/workspace.toml run terrain-edit --submit
devrelay --workspace /path/workspace.toml status
```

`task add` explicitly authorizes the task in ALL configured repositories. Tracking
plan parents are rejected. `run` prepares every worktree, uses one Codex session with
the other worktrees granted through `--add-dir`, runs every member check and joint
check, then starts a fresh read-only Codex review of the complete multi-repository
working source (including newly added files). Check/review findings are returned to
the same implementation session within `max_attempts`. Blocked or interrupted runs
retain code, session IDs and logs. Explicit `resume ID` starts a new bounded attempt
window and resumes only the task-owned implementation session.

`task add` 明确授权所有配置参与仓库，拒绝总体计划父项。`run` 准备全部工作树，使用一个
Codex 实现会话并通过 `--add-dir` 授予其他工作树，执行各仓及联合检查，再用新的只读会话
审查两仓完整工作源码（含新增文件）。检查和审查失败反馈给同一实现会话，在 max_attempts
内回修。阻塞、中断保留代码、会话与日志。显式 resume 开启新的有界尝试窗口。

External agents may call `start ID`, `prepare ID`, edit the returned worktrees,
`check ID`, optional `review ID`, and `submit ID`. These operations share the same
workspace authorization and evidence gates. `submit` does not start a model; a saved
rejected or stale review blocks it. `run` always performs independent review before
successful return/delivery. `submit ID --local` creates accepted member commits
without any push or PR; it keeps the reservation for later delivery/revocation.

外层 Agent 可逐步 start、prepare、编辑、check、可选 review、submit，使用同一授权与
证据门禁。submit 不启动模型；已有拒绝或过期审查会阻止交付。run 成功返回/交付前必须
独立审查。submit --local 只创建验收过的各仓提交，不推送；保留占用直到交付或撤销。

## Verification and recovery / 验收与恢复

The joint receipt binds every member's source tree, HEAD and latest target base,
manifest fingerprint, task requirements and actual command receipts. Source/HEAD/base
changes during checks fail acceptance; later changes block publication until all
checks rerun. Controller-created commits may change HEAD only while preserving the
accepted source tree. Preparation invalidates old verification/review. Each member's
Git common-directory worker lock is acquired in stable order; a persistent workspace
reservation prevents single-repository DevRelay workers or other bundles from claiming
the repositories between commands. Arbitrary external editors still require host
coordination, as in single-project mode. All participating DevRelay workers must use
the upgraded runtime; older installations do not understand workspace reservations.

联合回执绑定全部仓库的源码树、HEAD、最新目标基线、配置指纹、任务要求及真实命令回执。
检查期间变化会失败，之后变化会阻止发布，直到重新联合验收。控制器创建的提交只允许在
源码树一致时推进 HEAD。prepare 使旧验收/审查失效。稳定顺序获取各仓锁，并持久保存
工作区占用，防止跨命令期间其他 DevRelay worker 抢占；外部编辑器仍由宿主协调。
参与的 DevRelay worker 必须使用升级后的运行时，旧安装不识别工作区占用。

`submit` commits all accepted members, pushes each changed branch and creates or
updates associated **draft** PRs. Unchanged members are verified but need no empty PR.
Each published PR is read back against repository, branch, head, base and draft state;
companion drafts are linked after all identities are known. Partial delivery keeps
commit/PR checkpoints and reservations. Retry `submit ID`; if source/base changed,
integrate the new base and call `check/review/submit` first. Branch/PR lookup recovers
an uncertain create without making a duplicate. Existing closed, merged, ready or
mismatched PRs stop recovery. The engine never marks ready, approves, merges, deploys
or cleans up these worktrees. Delivery completion releases reservations; revoke with
`task revoke ID` to stop authorization and release reservations while retaining work.

submit 提交各仓验收源码，为有改动的仓推送分支、创建或更新关联草稿 PR。未改仓保留
验证记录，无空 PR。逐个回读 PR 仓库、分支、HEAD、base、draft 身份，完成后互链。
部分交付保留提交/PR 检查点与占用；重试 submit，源码或基线变化则先集成并 check/review。
不确定创建通过分支回读恢复，不重复创建。已关闭、合并、ready 或身份不符时停止。
本版不自动 ready、批准、合并、部署或清理。全部草稿交付后释放占用；task revoke 撤销
授权、释放占用并保留现场。

MVP limits: local macOS/Linux controllers, explicit local task registration, one
implementation session, bounded `run/resume`, and draft delivery. GitHub multi-repo
Issue queues, persistent multi-repo `watch`, nested participating submodules, automatic
merge and distributed coordination are future work. Existing single-project `watch`
and delivery policies are unchanged. A configured integration script proves only the
behavior it actually executes; typed-action/native-artifact checks do not establish
browser/model-service/robot-traversal acceptance.

MVP 范围：本机 macOS/Linux、显式登记任务、一个实现会话、有界 run/resume、草稿交付。
跨仓 GitHub Issue 队列、多仓 watch、嵌套子模块参与、自动合并和分布式协调留待后续。
现有单仓 watch/交付策略保持原样。联合脚本只证明实际执行的行为，动作契约及原生产物
验证不代表浏览器、产品模型服务或机器人通行验收。
