import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from devrelay.config import load
from devrelay.state import State


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="devrelay-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "DevRelay Test")
        self.git("config", "user.email", "devrelay@example.invalid")
        (self.root / ".gitignore").write_text(".devrelay/worktrees/\n.devrelay/runtime/\n__pycache__/\n")
        (self.root / "value.txt").write_text("broken\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.root, text=True,
                                       stderr=subprocess.PIPE).strip()

    def configure(self, *, provider="local", checks=None, setup=None, executable="codex", attempts=2):
        directory = self.root / ".devrelay"
        directory.mkdir(exist_ok=True)
        lines = ["[project]", f"provider = {json.dumps(provider)}", 'base_branch = "main"',
                 'repository = "example/project"', "[codex]", f"executable = {json.dumps(str(executable))}",
                 'sandbox = "workspace-write"', "timeout_seconds = 10", f"max_attempts = {attempts}"]
        if checks is None:
            checks = [[sys.executable, "-c", "from pathlib import Path; assert Path('value.txt').read_text() == 'fixed\\n'"]]
        for phase, commands in (("checks", checks), ("setup", setup or [])):
            for index, command in enumerate(commands):
                lines += [f"[[{phase}]]", f'id = "{phase}-{index}"', f"argv = {json.dumps(command)}", "timeout_seconds = 10"]
        (directory / "config.toml").write_text("\n".join(lines) + "\n")
        self.git("add", ".")
        self.git("commit", "-qm", "project config")
        self.config = load(self.root)
        self.state = State(self.root)
        self.addCleanup(self.state.close)
        return self.config

    def fake_codex(self):
        executable = Path(self.temporary.name) / "fake-codex"
        executable.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args == ['login', 'status']:
    raise SystemExit(0)
prompt = sys.stdin.read()
mode = os.environ.get('DEVRELAY_TEST_MODE', 'pass')
trace = os.environ.get('DEVRELAY_TEST_TRACE')
if trace:
    with open(trace, 'a') as f:
        f.write(json.dumps({'argv': args, 'cwd': str(Path.cwd()), 'prompt': prompt}) + '\\n')
destination = Path(args[args.index('--output-last-message') + 1])
schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())
if 'verdict' in schema['properties']:
    review_mode = os.environ.get('DEVRELAY_TEST_REVIEW', 'approve')
    print(json.dumps({'type': 'thread.started', 'thread_id':
        'exact-test-session' if review_mode == 'same-session' else 'fresh-review-session'}), flush=True)
    if review_mode == 'failure':
        print(json.dumps({'type': 'turn.failed'}))
        raise SystemExit(9)
    if review_mode == 'modify':
        Path('value.txt').write_text('changed by reviewer\\n')
    result = {'verdict': review_mode if review_mode in ('changes_requested', 'blocked') else 'approve',
              'summary': 'Inspected the diff',
              'findings': ['Fix the reported regression'] if review_mode in ('changes_requested', 'blocked') else []}
    if review_mode == 'inconsistent':
        result['findings'] = ['Blocking issue']
    destination.write_text('invalid json' if review_mode == 'invalid' else json.dumps(result))
    if review_mode != 'incomplete':
        print(json.dumps({'type': 'turn.completed'}), flush=True)
    raise SystemExit(0)
session = args[args.index('resume') + 1] if 'resume' in args else 'exact-test-session'
print(json.dumps({'type': 'thread.started', 'thread_id': session}), flush=True)
if mode == 'command-failure':
    print(json.dumps({'type': 'turn.failed'}))
    raise SystemExit(9)
if mode != 'blocked':
    marker = Path('.devrelay/runtime/fake-attempt')
    marker.parent.mkdir(parents=True, exist_ok=True)
    first = not marker.exists()
    marker.write_text('attempted')
    Path('value.txt').write_text('broken\\n' if mode == 'retry' and first else 'fixed\\n')
result = {'outcome': 'blocked' if mode == 'blocked' else 'ready',
          'summary': 'Missing dataset' if mode == 'blocked' else 'Fixed the bounded behavior',
          'blockers': ['Provide dataset'] if mode == 'blocked' else []}
destination.write_text('invalid json' if mode == 'bad-result' else json.dumps(result))
if mode != 'no-completed-turn':
    print(json.dumps({'type': 'turn.completed'}), flush=True)
''')
        executable.chmod(0o755)
        return executable
