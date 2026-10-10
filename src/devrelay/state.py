"""Repository-scoped state and a nonblocking single-worker lock (POSIX)."""

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3
import subprocess
from datetime import datetime, timezone

from .errors import RelayError
from .git import common_dir


_GROUP_PROBE_TIMEOUT_SECONDS = 3


def now():
    return datetime.now(timezone.utc).isoformat()


def group_alive(group, timeout=None):
    # Ignore zombies and include descendants that outlived the original worker.
    if timeout is None:
        timeout = _GROUP_PROBE_TIMEOUT_SECONDS
    try:
        result = subprocess.run(["ps", "-axo", "pgid=,stat="], text=True,
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RelayError(f"Process group {group} inspection timed out after {timeout:g} seconds; "
                         "refusing recovery") from error
    except OSError as error:
        raise RelayError(f"Cannot inspect process group {group}: {error}; refusing recovery") from error
    if result.returncode:
        raise RelayError(f"Cannot inspect process group {group}: ps exited {result.returncode}; refusing recovery")
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == str(group) and not fields[1].startswith("Z"):
            return True
    return False


class State:
    def __init__(self, root):
        self.directory = common_dir(root) / "devrelay"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.directory / "state.sqlite3", timeout=10)
        self.connection.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.connection.commit()

    def close(self):
        self.connection.close()

    def tasks(self):
        return [json.loads(row[0]) for row in self.connection.execute("SELECT data FROM tasks ORDER BY id")]

    def get(self, identifier):
        row = self.connection.execute("SELECT data FROM tasks WHERE id = ?", (str(identifier),)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, task):
        task = {**task, "updated_at": now()}
        with self.connection:
            self.connection.execute("INSERT INTO tasks(id, data) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                                    (task["id"], json.dumps(task)))
        return task

    def metadata(self, key):
        row = self.connection.execute("SELECT data FROM metadata WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_metadata(self, key, value):
        with self.connection:
            self.connection.execute("INSERT INTO metadata(key,data) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                                    (key, json.dumps(value)))

    @contextmanager
    def lock(self):
        with (self.directory / "worker.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RelayError("Another DevRelay worker holds this repository's lock") from error
            try:
                active = self.metadata("active_process")
                if active and group_alive(active["group"]):
                    raise RelayError(f"Previous command still runs in process group {active['group']}; wait for it to finish")
                if active:
                    self.set_metadata("active_process", None)
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
