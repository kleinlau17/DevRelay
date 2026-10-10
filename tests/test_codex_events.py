import io
import json

from devrelay.codex import _observe_line, _read_events, _recover_previous_run
from devrelay.errors import RelayError
from devrelay.process import _read_output

from helpers import RepositoryTest


class CodexEventTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure()
        self.task = {"id": "unicode-events", "status": "implementing"}
        self.directory = self.state.directory / "runs" / self.task["id"] / "first-codex"
        self.directory.mkdir(parents=True)
        self.log = self.directory / "events.jsonl"
        self.events = [
            {"type": "thread.started", "thread_id": "unicode-session"},
            {"type": "item.completed", "item": {"type": "agent_message",
                                                "text": "first\u0085second\u2028third\u2029last"}},
            {"type": "turn.completed"},
        ]

    def test_chunked_jsonl_preserves_unicode_separators_until_lf(self):
        for separator in ("\n", "\r\n"):
            with self.subTest(separator=repr(separator)):
                text = separator.join(json.dumps(event, ensure_ascii=False) for event in self.events)
                observed = []

                def observe(line):
                    observed.append(json.loads(line))
                    _observe_line(self.state, self.task, line, self.log)

                pending = ""
                # Every character is a separate read, including both halves of CRLF
                # and the Unicode separators inside a partially written message.
                for character in text:
                    pending = _read_output(io.StringIO(character), pending, observe)
                self.assertEqual(observed, self.events[:2])
                self.assertEqual(self.state.get(self.task["id"])["session_id"], "unicode-session")
                pending = _read_output(io.StringIO(""), pending, observe, final=True)
                self.assertEqual(pending, "")
                self.assertEqual(observed, self.events)

    def test_recovery_reads_unicode_jsonl_with_exact_session(self):
        self.log.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in self.events) + "\n")
        self.task.update(last_run=str(self.directory), event_log=str(self.log))
        self.state.save(self.task)
        _recover_previous_run(self.state, self.task)
        self.assertEqual(self.state.get(self.task["id"])["session_id"], "unicode-session")
        self.assertEqual(_read_events(self.state, self.task, self.log, recovering=True), (True, False, True))

    def test_incomplete_stdout_event_with_unicode_still_fails_closed(self):
        incomplete = json.dumps(self.events[1], ensure_ascii=False)[:-2]
        pending = _read_output(io.StringIO(incomplete), "",
                               lambda line: _observe_line(self.state, self.task, line, self.log))
        self.assertEqual(pending, incomplete)
        with self.assertRaisesRegex(RelayError, "incomplete or invalid"):
            _read_output(io.StringIO(""), pending,
                         lambda line: _observe_line(self.state, self.task, line, self.log), final=True)
        self.log.write_text(incomplete)
        with self.assertRaisesRegex(RelayError, "incomplete or invalid"):
            _read_events(self.state, self.task, self.log, recovering=True)
