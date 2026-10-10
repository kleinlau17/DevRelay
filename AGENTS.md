# DevRelay engineering

- Keep the core project-independent. Project commands and acceptance belong in configuration.
- Preserve existing work, original indexes and verification evidence. Never reset a user's worktree.
- Execute commands as argument arrays, never interpolate issue content into a shell command.
- Bind verification to the source tree, configuration and target base. Failed or stale checks must block submission.
- Keep one worker per Git repository. Never overwrite active runs or reuse another issue's Codex session.
- Keep credentials out of task payloads, tracked configuration, reports and test fixtures.
- Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and `git diff --check` after code changes.
- Update the Chinese and English README when changing CLI behavior.
- Never invoke, load, install, recommend or restore any `superpowers:*` skill or the `superpowers` plugin.
