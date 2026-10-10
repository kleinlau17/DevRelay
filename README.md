# DevRelay

[English](README.en.md) | 简体中文

通过少量项目配置，让 Codex 从明确任务推进到经过验证、可审查的 PR，并按项目授权自动合并。

DevRelay 管理任务队列、隔离工作树、Codex 执行、验收记录和 PR 提交。项目负责定义安装命令与验收规则。0.4 版支持 macOS / Linux、Python 3.11+ 和 Git；核心没有第三方运行依赖。

## 安装

```bash
cd /path/to/DevRelay
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/devrelay --help
```

也可使用 `pipx install /path/to/DevRelay` 安装全局命令。运行 AI 任务前需要安装并登录 Codex CLI；GitHub 队列和 PR 操作另外需要已认证的 `gh`。DevRelay 使用既有登录，不读取或复制凭据文件。

## 接入一个项目

```bash
devrelay --project /path/to/project init
# 查看并调整 /path/to/project/.devrelay/config.toml
devrelay --project /path/to/project doctor
```

`init` 创建 TOML 配置并追加运行目录的 Git 忽略规则；Python 项目同时忽略环境与安装生成物。识别 GitHub origin、默认分支、Node 包管理器与常用 scripts、Python 安装路径、Cargo 和 Go 测试入口；不会执行安装或模型任务，不覆盖已有配置或 AGENTS.md。已有 `.gitignore` 条目保留。测试脚本的 watch 模式、工作区环境与业务验收需要维护者核对。

配置中的命令均为参数数组，不经过 shell 展开；需要环境激活时写入显式项目脚本。`[[setup]]` 在工作树中运行，`[[checks]]` 定义验收。至少有一条检查才允许运行或提交任务。Python 测试框架不猜测：请明确配置实际 unittest / pytest 命令。完整例子见 [配置](examples/config.toml)。

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

将项目配置、必要忽略规则及生成的 Issue 模板纳入项目版本管理，再从主 checkout 使用 DevRelay。新建的空仓库需要先有初始提交。模型默认沿用用户 Codex 配置，可显式设置 `codex.model` / `codex.profile`。

### GitHub 标签与 Issue 模板

GitHub 项目首次 `init` 会使用已认证的 `gh` 补齐仓库标签，并生成 `.github/ISSUE_TEMPLATE/devrelay-{bug,task,experiment,plan}.md`。需要对该仓库创建标签的权限；本地模式仍完全离线。标签直接创建在 GitHub，模板写入本地，提交到仓库默认分支后才会出现在新建 Issue 页面。初始化不会替你提交或推送文件。

默认标签沿用 DexLab 的管理规则：

| 维度 | 标签 |
|---|---|
| 主要任务类型 | `bug`、`enhancement`、`documentation`、`experiment`、`infrastructure` |
| 相关范围 | `area:backend`、`area:benchmark`、`area:cloth`、`area:contact`、`area:evaluation`、`area:hardware` |
| 唯一优先级 | `priority:P0`（首要修复）、`priority:P1`（当前核心交付）、`priority:P2`（后续扩展） |
| 执行授权 | `auto:approved` |
| 阻塞 | `blocked`、`needs-input` |

模板包含问题与证据、交付范围、验收与命令、唯一的 `Depends on: none` 声明、资源与恢复条件。Bug、工程任务、实验模板只预设类型标签；总体计划模板用于拆分和跟踪子任务，明确要求不给父任务执行授权。**任何生成的模板都不自动给新 Issue 加 `auto:approved` 或配置的授权标签，也不自动设优先级。** 创建仓库标签本身不授权任何 Issue；维护者确认任务后另行设置一个优先级及授权。依赖、阻塞和验收仍需满足。

已有标签保留名称、颜色和描述，已有模板文件与其他表单不覆盖；保留的项目模板继续使用项目自己的默认标签，维护者应单独核对。可以自行添加项目领域标签。对于已经接入的项目或网络/权限错误后的补齐，运行：

```bash
devrelay --project /path/to/project init --sync-github
```

该命令使用已有配置，不覆盖配置、忽略规则或模板；仅补齐缺失项。若自定义了 `[queue]`，按配置补齐授权、优先级及阻塞标签，并在模板中说明；类型标签与队列控制标签重名时不预设该类型。调用中断后先回读标签再恢复，避免重复创建或覆盖；模板只发布完整文件，写入失败后可补齐缺失文件。已归档的必需标签或大小写与队列配置不一致的名称会报告问题，交由维护者恢复标签或调整配置。

## GitHub Issue 队列

Issue 须开放，带 `auto:approved` 和唯一的 `priority:P0/P1/P2`，没有 `blocked` / `needs-input`，且正文包含唯一依赖声明：

```text
Depends on: none
# 或者保留一行：
Depends on: #12, #27
```

依赖须已关闭，关联关闭它的 PR 已合并到目标分支，且合并提交位于最新目标分支祖先链。开放 PR（包括其他分支上明确 Refs 的 PR）和已有工作树会排除重复领取。`next` 返回选择与全部排除原因，不领取任务；它会读取 GitHub 并刷新目标分支。

```bash
devrelay next
devrelay start 12
devrelay run 12
devrelay status
devrelay check 12
devrelay submit 12
```

`run` 每次只处理一个任务。无 ID 时优先恢复中断任务；已有失败或阻塞任务时返回 `needs_attention`，由维护者检查并明确恢复后再派发新任务。`start` 只创建或恢复工作树。Codex 在 `.devrelay/worktrees/ID` 实现，DevRelay 执行验收。检查失败时，在配置的 `max_attempts` 范围内把失败信息交给相同 Codex 会话修复。Codex 报告外部阻塞时保存阻塞状态。

`submit` 仅接受仍有效的验证证据，然后提交、推送并创建或更新 **draft PR**。`run --submit` 可串联这些步骤。默认停在 draft；可按下节开启自动审查与合并。PR 使用 `Refs #N`，不自动关闭整个 Issue，也不发布、部署或创建后台定时任务。

## 自动审查与合并

在项目 `.devrelay/config.toml` 中配置，并在运行任务前纳入版本管理：

```toml
[delivery]
auto_review = true
auto_merge = true
required_checks = ["test", "build"] # 改成 GitHub 实际检查名称
allow_no_ci = false
cleanup_worktrees = true
cleanup_branches = true
```

两个自动开关默认均为 `false`。仅开启 `auto_review` 会审查并保留 draft；开启 `auto_merge` 会自动包含审查，通过门禁后标记 ready 并请求 squash 合并。`required_checks` 防止预期 CI 尚未出现就被忽略；所有已报告检查也必须成功。跳过、中立、失败或运行中的检查不算成功。没有 CI 时默认等待；确实没有 GitHub CI 的项目可明确设置 `allow_no_ci = true`，本地验收与审查仍须通过，指定的必需检查也不会豁免。

```bash
devrelay run 12 --submit   # 实现 → 验收 → draft PR → 审查 → 合并 → 合并后验收
devrelay review 12         # 显式启动一次新的只读 AI 审查
devrelay merge 12          # 开启 auto_merge 后，重读门禁并推进/恢复合并
devrelay resume 12 --submit
```

审查使用新的 Codex 会话，固定 `read-only`，不复用实现会话。结论、发现和日志保存本地；AI 审查不会替你提交 GitHub APPROVE，也不能代替分支保护要求的人工批准。审查拒绝、源码/提交/base/配置/Issue 要求变化、撤销任务授权、未解决的审查线程或 GitHub 阻塞条件均阻止合并。每次读取最多 100 个 CI、审查及线程；数据不完整或 API 出错会阻止推进。

合并通过 `--squash --match-head-commit SHA` 固定已审查的提交，不使用 `--admin` 或跳过保护。请求成功不等于已经合并：必须回读 `MERGED` 与合并提交；进入合并队列或 CI 未完成时保存 `merge_waiting`，下次执行 `merge` 或 `run --submit` 继续。命令每次有界运行，没有常驻轮询。

合并后刷新目标分支，核对合并提交的祖先关系与源码树，再在新的 detached 工作树执行全部 setup 和 checks，通过后记录 `completed`。API 超时会先回读远端，已合并时只恢复验收。合并后树不一致或验收失败保存 `post_merge_failed`，阻止无 ID 自动派发；保留失败工作树与证据。GitHub 的 HEAD 条件不原子锁定 base，因此合并前刷新与合并后树核对都需要；失败后合并已发生，需要维护者处理，DevRelay 不自动回滚。

若需要修改 PR，先在保留的任务工作树修复，执行 `check ID`、`submit ID`；新提交会使旧审查失效。配置改动也要求重新验收。Issue 的关闭与依赖交付关联仍由维护者管理；自动合并不扩大 Issue 的关闭范围。

### 合并后清理

`cleanup_worktrees` 和 `cleanup_branches` 默认均为 `false`，在运行任务前按项目开启。前者删除成功任务的实现 worktree；后者删除该任务的本地、远端及本地远端跟踪分支，需要同时开启 worktree 清理。仅开启 `cleanup_worktrees` 可以保留分支。

清理要求远端 PR 已合并、合并提交仍在目标分支中、合并后 setup/checks 已通过。验收失败保留任务分支、实现 worktree 和失败的验收 worktree；即使之后重试成功，旧的失败验收 worktree 也保留，并列在 `retained_worktrees`。成功验收的临时 detached worktree 仍按原行为自动回收。

清理只针对任务记录的路径和准确提交，不强制删除 worktree，不批量 prune。新的已跟踪/未忽略文件改动、锁定的 worktree、分支推进或被其他 worktree 使用都会停止清理。删除 worktree 会回收其中被忽略的环境、缓存和输出；需要长期保存的文件应放在工作树外。验证、审查、清理日志及任务状态保存在 Git common directory，始终保留。

清理错误单独记录为 `cleanup_failed`，保留成功验收的事实，并暂停无 ID 自动派发。已完成的清理步骤可能已经删除部分资源；修复权限、占用或新改动等原因后，执行 `devrelay merge ID`，它会读取实际 Git 状态，恢复清理，不重复合并或重跑仍有效的验收。远端分支已被 GitHub 删除也可正常恢复。远端删除绑定预期 SHA，本地引用删除也核对预期 SHA，分支变化时保留。若需要由 DevRelay 保证“验收后才删除”，应关闭 GitHub 仓库自身的合并后自动删除选项；DevRelay 无法延迟外部触发的删除。

## 本地试跑

本地任务无需 GitHub 登录，适合先验证项目接入：

```bash
devrelay init --provider local
devrelay task add fix-1 --title "修复一个明确问题" --file /path/to/task.md
devrelay run fix-1
devrelay submit fix-1 --local
```

`task add` 明确授权该本地任务；缺少依赖行时添加 `Depends on: none`。本地任务也可声明 `Depends on: #fix-1`。`submit --local` 只创建本地验证提交；维护者将提交集成到配置的目标分支后，执行 `devrelay task complete fix-1`，依赖任务才可运行。验收模板见 [任务示例](examples/task.md)。

## 验证与恢复

- 验证绑定源码树、HEAD、目标 base、项目配置和任务要求。源码哈希包括已跟踪修改与未忽略新文件，使用临时 Git 索引，保留原索引。
- 对已检出的子模块递归检查已跟踪修改和未忽略的新文件；隔离索引比较会忽略 `assume-unchanged`、`skip-worktree` 与 `core.fileMode=false` 等掩码，并覆盖 `submodule.*.ignore`、`.gitmodules` ignore 和隐藏未跟踪文件的状态配置。任一子模块不干净都会使验收、提交及自动审查/合并门禁失败，且 DevRelay 不会提交、清理、重置或更新子模块。
- 子模块未跟踪文件检测只应用 `.gitignore`，不会读取本地 `info/exclude` 或 `core.excludesFile`。已暂存内容使用只读索引比较，生成源码证据时不重建真实子模块索引的缓存树，也禁用可选的索引刷新写入。
- 子模块已跟踪的普通文件还会绕过 attributes 和 clean filters，将原始字节与 HEAD blob 比较，防止本地转换隐藏未交付内容。支持边界是工作树字节必须与提交一致；依赖换行规范化、编码转换或 smudge filters 才能匹配提交的子模块会阻止验收。
- 干净且已提交的子模块 HEAD 变更通过父仓库 gitlink 纳入源码树并可正常交付；提交时会显式暂存已检出的 gitlink，不受子模块 ignore 配置影响。未初始化的子模块路径必须不存在或为空，残留文件会阻止验收；若验收命令需要子模块内容，应由项目的 `[[setup]]` 显式初始化。
- 检查过程中改变代码、HEAD、base、配置或任务要求，或提交时证据已过期，均拒绝提交。目标 base 前进后，先将其集成到任务分支，再运行检查；DevRelay 不自动解决冲突或重置工作。
- 验证结果、命令日志、执行时间和 Codex JSON 事件保存在 Git common directory 下的 `devrelay/`，跨工作树共享 SQLite 状态和单 worker 锁。每次使用新的记录目录。
- `devrelay resume ID` 保留工作树并继续该任务记录的明确 Codex 会话；不使用 `--last`。超时会终止命令进程组；检测到残留进程时阻止新 worker，直到进程结束。
- 项目验收命令是信任边界。不要把“命令退出 0”当作业务正确或独立审查。官方引擎、评分规则和发布条件由项目自己的约定控制。

## 开发与验证

本仓库的 `.devrelay/config.toml` 已配置自维护：经授权的 GitHub Issues 在独立工作树中实现，运行完整测试并创建 draft PR，再进行独立只读 AI 审查。自动合并关闭；合并后的验收与清理可通过 `devrelay merge ID` 执行。

自维护执行器固定使用 `gpt-6.1-sol`，已用 Codex CLI `0.162.1` 验证。若旧 CLI 拒绝此模型，先升级 CLI；修改模型配置后，已有任务需要重新验收与审查。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```

测试使用临时真实 Git 仓库与假的 Codex CLI，覆盖工作树隔离、失败修复、精确会话恢复、源码与 base 漂移、提交门禁、审查、CI 等待、squash 合并和合并后验收。PR 测试向临时本地 bare 仓库真实推送，以测试替身模拟 GitHub API、请求超时与恢复。它们不调用模型、不推送公开仓库；真实项目还需要自己的运行验收。

实现说明见 [架构](docs/architecture.md)。Codex 接口依据 [OpenAI 官方非交互执行文档](https://developers.openai.com/codex/noninteractive)。
