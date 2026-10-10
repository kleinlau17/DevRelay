"""Bounded foreground commands with retained logs and crash markers."""

import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .errors import RelayError
from .state import group_alive, now


_TERMINATE_GRACE_SECONDS = 3
_KILL_GRACE_SECONDS = 3
_GROUP_POLL_SECONDS = 0.05


def execute(state, argv, cwd, log, timeout, *, input=None, env=None):
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = None
    timed_out = False
    termination_failed = False
    with log.open("x") as stream:
        try:
            try:
                process = subprocess.Popen(argv, cwd=cwd,
                                           stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                           stdout=stream, stderr=subprocess.STDOUT, text=True, env=env,
                                           start_new_session=True)
            except OSError as error:
                raise RelayError(f"Cannot start {argv[0]}: {error}") from error
            state.set_metadata("active_process", {"group": process.pid, "argv": argv, "started_at": now()})
            try:
                process.communicate(input, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    terminate(process)
                except BaseException:
                    termination_failed = True
                    raise
            except BaseException:
                try:
                    terminate(process)
                except BaseException:
                    termination_failed = True
                    raise
                raise
        finally:
            if not termination_failed and process is not None and process.poll() is not None:
                if group_alive(process.pid):
                    # Keep evidence of detached foreground descendants; don't claim a clean finish.
                    raise RelayError(f"Command left live descendants in process group {process.pid}")
                state.set_metadata("active_process", None)
    receipt = {"argv": argv, "cwd": str(cwd), "log": str(log), "returncode": process.returncode,
               "timed_out": timed_out, "wall_seconds": round(time.monotonic() - started, 3), "finished_at": now()}
    log.with_suffix(log.suffix + ".receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if timed_out or process.returncode:
        raise RelayError(f"{argv[0]} {'timed out' if timed_out else 'failed (' + str(process.returncode) + ')'}; log: {log}")
    return receipt


def terminate(process):
    inspection_error = None
    for sig, wait in ((signal.SIGTERM, _TERMINATE_GRACE_SECONDS),
                      (signal.SIGKILL, _KILL_GRACE_SECONDS)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise RelayError(f"Cannot signal process group {process.pid} with {sig.name}: {error}; "
                             "recovery will remain blocked") from error
        try:
            if _wait_for_group(process, wait):
                return
            inspection_error = None
        except RelayError as error:
            # An unknown state is treated as alive so SIGTERM inspection
            # failure still escalates to SIGKILL.
            inspection_error = error
    if inspection_error is not None:
        raise RelayError(f"Cannot confirm process group {process.pid} stopped: {inspection_error}; "
                         "recovery will remain blocked") from inspection_error
    raise RelayError(f"Cannot stop process group {process.pid}; recovery will remain blocked")


def _wait_for_group(process, timeout):
    deadline = time.monotonic() + timeout
    while True:
        # Reap the direct child when possible, but completion is determined by
        # every non-zombie member of the controlled process group.
        process.poll()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not group_alive(process.pid, timeout=remaining):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_GROUP_POLL_SECONDS, remaining))
