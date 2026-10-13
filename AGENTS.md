# DevRelay engineering

- Keep the core project-independent. Project commands and acceptance belong in configuration.
- Make agent-driven use the primary workflow: external agents plan, explore and implement through the engineering tools; run/resume/watch are optional Codex orchestration. Keep authorization and delivery gates shared across both paths.
- One issue defines one observable, reviewable result. Keep the code, configuration, tests and documentation needed for that result together. Split child issues only when they have independent value or acceptance criteria; file, layer or phase boundaries alone do not justify splitting.
- Overall plans are tracking parents, never executable tasks. Mark them with `scope:plan` or `Task kind: plan`; grant execution authorization only to bounded child issues.
- Preserve existing work, original indexes and verification evidence. Never reset a user's worktree.
- Execute commands as argument arrays, never interpolate issue content into a shell command.
- Bind verification to the source tree, configuration and target base. Failed or stale checks must block submission.
- Keep one worker per Git repository. Never overwrite active runs or reuse another issue's Codex session.
- Keep credentials out of task payloads, tracked configuration, reports and test fixtures.
- Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and `git diff --check` after code changes.
- Update the Chinese and English README when changing CLI behavior.
- Never invoke, load, install, recommend or restore any `superpowers:*` skill or the `superpowers` plugin.
