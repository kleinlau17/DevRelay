"""Bounded foreground commands with retained logs and crash markers."""

import json
import locale
import os
from pathlib import Path
import signal
import subprocess
import time

from .errors import RelayError
from .state import group_alive, now


def _read_output(reader, pending, on_output_line, *, final=False):
    pending += reader.read()
    lines = pending.splitlines(keepends=True)
    pending = ""
    for line in lines:
        if line.endswith(("\n", "\r")) or final:
            on_output_line(line.rstrip("\r\n"))
        else:
            pending = line
    return pending


def _close_stdin(process):
    stream = process.stdin
    process.stdin = None
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass


def _stream_output(process, log, timeout, started, input, on_output_line):
    payload = input.encode(locale.getpreferredencoding(False)) if input is not None else b""
    offset = 0
    deadline = started + timeout
    pending = ""
    if process.stdin is not None:
        os.set_blocking(process.stdin.fileno(), False)
    with log.open(errors="replace") as reader:
        while process.poll() is None:
            pending = _read_output(reader, pending, on_output_line)
            writes = 0
            while process.stdin is not None and offset < len(payload) and writes < 16:
                try:
                    offset += os.write(process.stdin.fileno(), payload[offset:offset + 65536])
                    writes += 1
                except BlockingIOError:
                    break
                except OSError:
                    _close_stdin(process)
            if process.stdin is not None and offset == len(payload):
                _close_stdin(process)
            if time.monotonic() >= deadline:
                return True
            time.sleep(0.05)
        if process.stdin is not None:
            _close_stdin(process)
        _read_output(reader, pending, on_output_line, final=True)
    return False


def execute(state, argv, cwd, log, timeout, *, input=None, env=None, on_output_line=None):
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = None
    timed_out = False
    stderr_log = log.with_suffix(log.suffix + ".stderr.log") if on_output_line is not None else None
    with log.open("x") as stream:
        diagnostics = stderr_log.open("x") if stderr_log is not None else None
        try:
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                       stdout=stream, stderr=diagnostics or subprocess.STDOUT,
                                       text=on_output_line is None, env=env,
                                       start_new_session=True)
            state.set_metadata("active_process", {"group": process.pid, "argv": argv, "started_at": now()})
            try:
                if on_output_line is None:
                    process.communicate(input, timeout=timeout)
                else:
                    timed_out = _stream_output(process, log, timeout, started, input, on_output_line)
                    if timed_out:
                        terminate(process)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate(process)
            except BaseException:
                terminate(process)
                raise
        except OSError as error:
            raise RelayError(f"Cannot start {argv[0]}: {error}") from error
        finally:
            if diagnostics is not None:
                diagnostics.close()
            if process is not None and process.stdin is not None:
                _close_stdin(process)
            if process is not None and process.poll() is not None:
                if group_alive(process.pid):
                    # Keep evidence of detached foreground descendants; don't claim a clean finish.
                    raise RelayError(f"Command left live descendants in process group {process.pid}")
                state.set_metadata("active_process", None)
    receipt = {"argv": argv, "cwd": str(cwd), "log": str(log), "returncode": process.returncode,
               "timed_out": timed_out, "wall_seconds": round(time.monotonic() - started, 3), "finished_at": now()}
    if stderr_log is not None:
        receipt["stderr_log"] = str(stderr_log)
    log.with_suffix(log.suffix + ".receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if timed_out or process.returncode:
        raise RelayError(f"{argv[0]} {'timed out' if timed_out else 'failed (' + str(process.returncode) + ')'}; log: {log}")
    return receipt


def terminate(process):
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 3)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue
    raise RelayError(f"Cannot stop process group {process.pid}; recovery will remain blocked")
