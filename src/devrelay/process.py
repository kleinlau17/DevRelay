"""Bounded foreground commands with retained logs and crash markers."""

import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .errors import RelayError
from .state import group_alive, now


def execute(state, argv, cwd, log, timeout, *, input=None, env=None):
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = None
    timed_out = False
    with log.open("x") as stream:
        try:
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                       stdout=stream, stderr=subprocess.STDOUT, text=True, env=env,
                                       start_new_session=True)
            state.set_metadata("active_process", {"group": process.pid, "argv": argv, "started_at": now()})
            try:
                process.communicate(input, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate(process)
            except BaseException:
                terminate(process)
                raise
        except OSError as error:
            raise RelayError(f"Cannot start {argv[0]}: {error}") from error
        finally:
            if process is not None and process.poll() is not None:
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
