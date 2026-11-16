"""Install the loopback dashboard using OS socket activation, never a root worker.

This file is also the isolated sudo entry point. Only stdlib imports are used there.
"""

from contextlib import contextmanager
import fcntl
import http.client
import json
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

if __package__:
    from .errors import RelayError
else:
    class RelayError(Exception):
        pass

LABEL = "org.devrelay.dashboard"
UNIT = "devrelay-dashboard"
BLOCK = "# BEGIN DevRelay dashboard\n127.0.0.1 devrelay\n# END DevRelay dashboard\n"
FALLBACK = "Use devrelay ui --port 0 --open for a temporary dashboard, or ui install --port 8765."


def command(argv, *, check=True):
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RelayError(f"Cannot run {argv[0]}: {error}") from error
    if check and result.returncode:
        raise RelayError(f"{argv[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def hosts_content(source, *, install):
    if "# BEGIN DevRelay dashboard" in source or "# END DevRelay dashboard" in source:
        if source.count(BLOCK) != 1 or source.count("# BEGIN DevRelay dashboard") != 1 or source.count("# END DevRelay dashboard") != 1:
            raise RelayError("DevRelay hosts entry was edited; preserve it and resolve the conflict first")
        source = source.replace(BLOCK, "")
    if install:
        for line in source.splitlines():
            if "devrelay" in [part.lower().rstrip(".") for part in line.split("#", 1)[0].split()[1:]]:
                raise RelayError("An unmanaged devrelay hosts entry already exists; no settings changed")
        return source + ("" if not source or source.endswith("\n") else "\n") + BLOCK
    return source


def atomic_write(path, data, *, expected):
    """Compare before replacement; preserve metadata and reject symlink targets."""
    if path.is_symlink() or (path.read_bytes() if path.exists() else None) != expected:
        raise RelayError(f"File changed or is a symlink; preserved: {path}")
    metadata = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".devrelay-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), metadata.st_mode & 0o777 if metadata else 0o644)
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), metadata.st_uid if metadata else 0, metadata.st_gid if metadata else 0)
        if (path.read_bytes() if path.exists() else None) != expected or path.is_symlink():
            raise RelayError(f"File changed; preserved: {path}")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def unit_quote(value, *, expand_environment=False):
    # systemd performs specifier and environment expansion even without a shell.
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RelayError("Service settings cannot contain control characters")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (value.replace("$", "$$") if expand_environment else value) + '"'


def health(settings):
    connection = http.client.HTTPConnection("127.0.0.1", settings["port"], timeout=1)
    try:
        authority = "devrelay" + (f":{settings['port']}" if settings["port"] != 80 else "")
        connection.request("GET", "/api/health", headers={"Host": authority})
        response = connection.getresponse()
        body = response.read(4096)
        return response.status == 200 and json.loads(body) == {
            "service": "devrelay", "installation_id": settings["installation_id"]}
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def wait_healthy(settings):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if health(settings):
            return
        time.sleep(0.2)
    raise RelayError("Installed dashboard did not become healthy; inspect the OS service logs")


class Installation:
    def __init__(self, platform=None, root=Path("/")):
        self.platform = platform or sys.platform
        if self.platform not in ("darwin", "linux"):
            raise RelayError("Dashboard installation supports macOS and Linux with systemd. " + FALLBACK)
        root = root.resolve()
        self.root = root
        self.directory = root / ("Library/Application Support/DevRelay" if self.platform == "darwin" else "var/lib/devrelay")
        self.state = self.directory / "dashboard.json"
        self.hosts = root / "etc/hosts"
        self.services = ([root / f"Library/LaunchDaemons/{LABEL}.plist"] if self.platform == "darwin" else
                         [root / f"etc/systemd/system/{UNIT}.socket", root / f"etc/systemd/system/{UNIT}.service"])

    def read(self):
        if self.state.is_symlink():
            raise RelayError("Installation record is a symlink; preserved")
        try:
            try:
                content = self.state.read_text()
            except FileNotFoundError:
                return None
            saved = json.loads(content)
            if (saved["phase"] not in ("ready", "installing", "removing")
                    or len(saved["files"]) != len(self.services)
                    or not all(isinstance(item, str) for item in saved["files"])
                    or type(saved["settings"]["uid"]) is not int
                    or type(saved["settings"]["port"]) is not int
                    or not 1 <= saved["settings"]["port"] <= 65535):
                raise ValueError("Invalid installation record")
            if saved["phase"] == "installing":
                for key in ("previous", "hosts_before", "hosts_after"):
                    saved[key]
            return saved
        except (ValueError, KeyError, TypeError) as error:
            raise RelayError("Invalid dashboard installation record; preserved") from error

    def inspect(self):
        """Distinguish absence from a directory or record we cannot read."""
        metadata = None
        try:
            metadata = self.directory.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise RelayError("Installation directory is a symlink; preserved")
            # Also deterministic for privileged callers inspecting missing mode bits.
            groups = {*os.getgroups(), os.getegid()}
            required = (0o500 if metadata.st_uid == os.geteuid() else
                        0o050 if metadata.st_gid in groups else 0o005)
            if metadata.st_mode & required != required:
                raise PermissionError("Installation directory is inaccessible")
            try:
                record = self.state.lstat()
            except FileNotFoundError:
                return None, None
            if not record.st_mode & 0o444:
                raise PermissionError("Installation record is unreadable")
            return self.read(), None
        except FileNotFoundError:
            return None, None
        except OSError as error:
            owner = metadata.st_uid if metadata else None
            return None, {"installed": True, "readable": False, "healthy": False,
                          "owner_uid": owner,
                          "message": f"Dashboard installation is unreadable (directory owner uid {owner}); "
                          "it may be installed by another user. Ask the administrator to run "
                          "sudo devrelay ui status; ask the installing user to run devrelay ui uninstall. "
                          f"Details: {error}"}

    def untrusted_record_error(self):
        stop = (f"sudo launchctl bootout system/{LABEL}" if self.platform == "darwin" else
                f"sudo systemctl disable --now {UNIT}.socket {UNIT}.service")
        return RelayError(
            f"Untrusted dashboard record: {self.state}; directory required permission tightening. "
            "Record preserved; automatic install/uninstall and recovery refused. "
            f"Administrator recovery: stop the service with `{stop}` (if loaded); "
            "independently inspect and remove only confirmed DevRelay service files: "
            + ", ".join(str(path) for path in self.services)
            + f"; remove only the exact DevRelay dashboard block from {self.hosts}; "
            f"move {self.state} to an administrator-only quarantine outside {self.directory} "
            "without executing or restoring its contents; then run devrelay ui uninstall "
            "to re-initialise the empty directory. Do not merely chmod the directory and retry.")

    @contextmanager
    def lock(self, *, gid=None):
        # Walk from the filesystem root with pinned descriptors. Trusted owners
        # (root and the installer) can still rename their own directories; they
        # are inside this trust boundary. Sticky shared parents are safe here.
        # The privileged entry point validates settings against SUDO_UID's
        # passwd account (including its primary gid), rather than trusting a
        # payload or SUDO_GID alone. Root retains write ownership; 0750 grants
        # that installer's group read/traverse only, so status needs no sudo.
        gid = os.getegid() if gid is None else gid
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open("/", flags)
        parent_fd = None
        try:
            for index, component in enumerate(self.directory.parts[1:], 1):
                parent = os.fstat(fd)
                if (parent.st_uid not in (0, os.geteuid())
                        or parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX):
                    raise RelayError("Unsafe dashboard parent directory: untrusted owner or writable without sticky bit")
                created = False
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
                # A newly created mode 000 directory needs owner permissions
                # before O_RDONLY can open it. Its verified parent prevents swaps
                # by untrusted users; chmod never follows a symlink.
                if created:
                    os.chmod(component, 0o700 if index == len(self.directory.parts) - 1 else 0o755,
                             dir_fd=fd, follow_symlinks=False)
                try:
                    child = os.open(component, flags, dir_fd=fd)
                except OSError as error:
                    raise RelayError(f"Cannot safely open dashboard directory (symlink or inaccessible): {component}") from error
                if index == len(self.directory.parts) - 1:
                    parent_fd = os.dup(fd)
                os.close(fd)
                fd = child
            metadata = os.fstat(fd)
            if metadata.st_uid != os.geteuid():
                raise RelayError("Dashboard system directory must be owned by the installer")
            # Keep the marker in the verified parent: users who could write
            # the loose installation directory must not be able to unlink it.
            marker = f".{self.directory.name}.directory-untrusted"
            try:
                os.stat(marker, dir_fd=parent_fd, follow_symlinks=False)
                marked = True
            except FileNotFoundError:
                marked = False
            # Both legacy 0700 and our read-only-group 0750 protect records
            # from planting. Accept 0750 on repeat installs without treating
            # its existing record as untrusted; all other modes fail closed.
            tightening = stat.S_IMODE(metadata.st_mode) not in (0o700, 0o750)
            if tightening or marked:
                try:
                    os.stat("dashboard.json", dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    # Refuse BEFORE chmod: retries must not turn a planted record
                    # into a trusted record just because we tightened its directory.
                    raise self.untrusted_record_error()
            if tightening and not marked:
                marker_fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
                os.close(marker_fd)
                os.fsync(parent_fd)
            # Set group before the final mode, independently of the caller umask.
            if metadata.st_gid != gid:
                os.fchown(fd, -1, gid)
            os.fchmod(fd, 0o750)
            metadata = os.fstat(fd)
            if (metadata.st_uid != os.geteuid() or metadata.st_gid != gid
                    or stat.S_IMODE(metadata.st_mode) != 0o750):
                raise RelayError("Cannot secure dashboard system directory")
            if tightening or marked:
                # Catch records planted between the first check and fchmod.
                # The durable marker keeps a crash/retry fail-closed as well.
                try:
                    os.stat("dashboard.json", dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    os.unlink(marker, dir_fd=parent_fd)
                else:
                    raise self.untrusted_record_error()
            lock_fd = os.open("install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
            with os.fdopen(lock_fd, "a") as stream:
                lock_metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(lock_metadata.st_mode) or lock_metadata.st_uid != os.geteuid() or lock_metadata.st_nlink != 1:
                    raise RelayError("Unsafe dashboard installation lock; preserved")
                os.fchmod(stream.fileno(), 0o600)
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RelayError("Another dashboard installation is in progress") from error
                yield
        finally:
            os.close(fd)
            if parent_fd is not None:
                os.close(parent_fd)

    def definitions(self, settings):
        manager = "launchd" if self.platform == "darwin" else "systemd"
        argv = [settings["python"], "-I", "-m", "devrelay", "ui", "--hostname", "devrelay",
                "--port", str(settings["port"]), "--catalog", settings["catalog"],
                "--socket-activation", manager, "--installation-id", settings["installation_id"]]
        environment = {"HOME": settings["home"], "PATH": settings["path"], "PYTHONUNBUFFERED": "1"}
        if self.platform == "darwin":
            return [plistlib.dumps({"Label": LABEL, "ProgramArguments": argv, "UserName": settings["user"],
                                   "WorkingDirectory": settings["home"], "EnvironmentVariables": environment,
                                   "Sockets": {"Dashboard": {"SockNodeName": "127.0.0.1", "SockServiceName": str(settings["port"]),
                                                              "SockFamily": "IPv4", "SockType": "stream"}},
                                   "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "Umask": 0o077}).decode()]
        socket_unit = (f"[Unit]\nDescription=DevRelay local dashboard socket\n\n[Socket]\n"
                       f"ListenStream=127.0.0.1:{settings['port']}\n\n[Install]\nWantedBy=sockets.target\n")
        service = (f"[Unit]\nDescription=DevRelay local dashboard\nRequires={UNIT}.socket\nAfter={UNIT}.socket\n\n"
                   f"[Service]\nUser={settings['uid']}\nGroup={settings['gid']}\n"
                   f"WorkingDirectory={unit_quote(settings['home'])}\n"
                   + "".join(f"Environment={unit_quote(key + '=' + value)}\n" for key, value in environment.items())
                   + "ExecStart=" + " ".join(unit_quote(arg, expand_environment=True) for arg in argv)
                   + "\nRestart=on-failure\nNoNewPrivileges=yes\nUMask=0077\n")
        return [socket_unit, service]

    def running(self):
        argv = (["/bin/launchctl", "print", f"system/{LABEL}"] if self.platform == "darwin" else
                ["systemctl", "is-active", "--quiet", f"{UNIT}.socket", f"{UNIT}.service"])
        return command(argv, check=False).returncode == 0

    def stop(self):
        if self.platform == "darwin":
            if self.running():
                command(["/bin/launchctl", "bootout", f"system/{LABEL}"])
        else:
            # A failed first installation may not have written both units yet.
            for suffix in ("socket", "service"):
                result = command(["systemctl", "stop", f"{UNIT}.{suffix}"], check=False)
                if result.returncode and result.returncode != 5:
                    raise RelayError(f"Cannot stop dashboard {suffix}: {result.stderr.strip()}")
            result = command(["systemctl", "disable", f"{UNIT}.socket"], check=False)
            if result.returncode and self.services[0].exists():
                raise RelayError(f"Cannot disable dashboard socket: {result.stderr.strip()}")

    def start(self):
        if self.platform == "darwin":
            command(["/bin/launchctl", "enable", f"system/{LABEL}"])
            command(["/bin/launchctl", "bootstrap", "system", str(self.services[0])])
        else:
            command(["systemctl", "daemon-reload"])
            command(["systemctl", "enable", "--now", f"{UNIT}.socket"])

    def check_files(self, saved):
        for path, expected in zip(self.services, saved["files"] if saved else [None] * len(self.services)):
            actual = path.read_text() if path.exists() else None
            if path.is_symlink() or actual != expected:
                raise RelayError(f"Unmanaged or edited service file; preserved: {path}")

    def save(self, value):
        atomic_write(self.state, json.dumps(value, indent=2).encode(),
                     expected=self.state.read_bytes() if self.state.exists() else None)

    def apply(self, action, settings):
        with self.lock(gid=settings["gid"] if os.geteuid() == 0 else os.getegid()):
            saved = self.read()
            if saved and saved["settings"]["uid"] != settings["uid"]:
                raise RelayError("The machine dashboard belongs to another user; ask that user to uninstall it first")
            if saved and saved.get("phase") == "installing" and action == "uninstall":
                self.recover(saved)
                saved = self.read()
            if saved and saved.get("phase") == "removing" and action == "uninstall":
                return self.remove(saved)
            if saved and saved.get("phase") != "ready":
                raise RelayError("An interrupted installation needs recovery; run devrelay ui uninstall first")
            self.check_files(saved)
            if action == "uninstall":
                if not saved:
                    return {"installed": False, "message": "No managed dashboard is installed"}
                return self.remove(saved)
            source = self.hosts.read_bytes()
            updated_hosts = hosts_content(source.decode(), install=True).encode()
            if not saved and self.running():
                raise RelayError("An unmanaged dashboard service is already loaded; preserved")
            if saved:
                settings["installation_id"] = saved["settings"]["installation_id"]
            new = {"settings": settings, "files": self.definitions(settings), "phase": "installing",
                   "hosts_prefix": saved.get("hosts_prefix", False) if saved else bool(source and not source.endswith(b"\n"))}
            was_running = self.running() if saved else False
            if saved:
                self.stop()
            try:
                with socket.socket() as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        probe.bind(("127.0.0.1", settings["port"]))
                    except OSError as error:
                        raise RelayError(f"Port {settings['port']} is unavailable; no other service was stopped. {FALLBACK}") from error
                # Persist recovery information before the first configuration mutation.
                new["previous"] = saved
                new["hosts_before"] = source.decode()
                new["hosts_after"] = updated_hosts.decode()
                self.save(new)
                for index, path in enumerate(self.services):
                    atomic_write(path, new["files"][index].encode(), expected=saved["files"][index].encode() if saved else None)
                atomic_write(self.hosts, updated_hosts, expected=source)
                self.start()
                wait_healthy(settings)
                new = {key: new[key] for key in ("settings", "files", "hosts_prefix")}
                new["phase"] = "ready"
                self.save(new)
            except (Exception, KeyboardInterrupt):
                if self.state.exists() and self.read().get("phase") == "installing":
                    self.recover(self.read())
                elif was_running:
                    self.start()
                raise
            return {"installed": True, "healthy": True, "url": url(settings), "user": settings["user"]}

    def recover(self, journal):
        """Undo only bytes we wrote; leave external changes and the journal intact."""
        self.stop()
        before = journal["previous"]
        for index, path in enumerate(self.services):
            current = path.read_bytes() if path.exists() else None
            old = before["files"][index].encode() if before else None
            new = journal["files"][index].encode()
            if path.is_symlink() or current not in (old, new):
                raise RelayError(f"Recovery found an edited service file; preserved: {path}")
            if current != old:
                if old is None:
                    path.unlink()
                else:
                    atomic_write(path, old, expected=current)
        current = self.hosts.read_bytes()
        original, applied = journal["hosts_before"].encode(), journal["hosts_after"].encode()
        if current == applied:
            atomic_write(self.hosts, original, expected=current)
        elif current != original:
            raise RelayError("Hosts changed during installation; preserved with the recovery record")
        if before:
            self.save(before)
            self.start()
        else:
            self.state.unlink()
            if self.platform == "linux":
                command(["systemctl", "daemon-reload"])

    def remove(self, saved):
        for path, expected in zip(self.services, saved["files"]):
            if path.is_symlink() or (path.exists() and path.read_text() != expected):
                raise RelayError(f"Edited service file; preserved: {path}")
        source = self.hosts.read_bytes()
        replacement = hosts_content(source.decode(), install=False).encode()
        if saved.get("hosts_prefix") and ("\n" + BLOCK).encode() in source:
            replacement = source.replace(("\n" + BLOCK).encode(), b"")
        saved = {**saved, "phase": "removing"}
        self.save(saved)
        self.stop()
        atomic_write(self.hosts, replacement, expected=source)
        for path in self.services:
            path.unlink(missing_ok=True)
        self.state.unlink()
        if self.platform == "linux":
            command(["systemctl", "daemon-reload"])
        return {"installed": False, "message": "Dashboard removed; projects, catalogs and task evidence preserved"}


def url(settings):
    return "http://devrelay" + (f":{settings['port']}" if settings["port"] != 80 else "")


def manage(action, *, catalog=None, port=None, dry_run=False, open_browser=False):
    installation = Installation()
    if action == "status":
        saved, inaccessible = installation.inspect()
        if inaccessible:
            return inaccessible
        return ({"installed": True, "phase": saved["phase"], "url": url(saved["settings"]),
                 "user": saved["settings"]["user"], "healthy": saved["phase"] == "ready" and health(saved["settings"])}
                if saved else {"installed": False})
    if os.getuid() == 0:
        raise RelayError("Run devrelay ui install/uninstall as your normal user; it requests sudo only for system setup")
    account = pwd.getpwuid(os.getuid())
    from .project_registry import default_catalog
    settings = {"uid": account.pw_uid, "gid": account.pw_gid, "user": account.pw_name, "home": account.pw_dir,
                "python": sys.executable, "path": os.environ.get("PATH", os.defpath),
                "catalog": str((catalog or default_catalog()).expanduser().absolute()),
                "port": 80 if port is None else port, "installation_id": str(uuid.uuid4())}
    if not 1 <= settings["port"] <= 65535:
        raise RelayError("Installed dashboard port must be between 1 and 65535")
    if dry_run:
        saved, inaccessible = installation.inspect() if action == "uninstall" else (None, None)
        if inaccessible:
            raise RelayError(inaccessible["message"])
        if saved:
            settings = saved["settings"]
        return {"action": action, "url": url(settings), "user": account.pw_name, "requires_admin": True,
                "hosts_file": str(installation.hosts), "service_files": [str(path) for path in installation.services],
                "catalog": settings["catalog"]}
    if action == "install":
        # A PYTHONPATH-only development checkout cannot survive an OS restart.
        result = command([sys.executable, "-I", "-c", "import devrelay; print(devrelay.__file__)"], check=False)
        if result.returncode or Path(result.stdout.strip()).resolve().parent != Path(__file__).resolve().parent:
            raise RelayError("Install this version as a package first (python3 scripts/install.py), then retry ui install")
    if not shutil.which("sudo"):
        raise RelayError("sudo is required for dashboard system setup. " + FALLBACK)
    print("Setting up the local dashboard requires administrator access; sudo may ask for your password.", file=sys.stderr)
    result = subprocess.run(["sudo", "--", sys.executable, "-I", str(Path(__file__).resolve()), action],
                            input=json.dumps(settings), text=True, stdout=subprocess.PIPE)
    if result.returncode:
        raise RelayError("Dashboard system setup did not complete. " + FALLBACK)
    response = json.loads(result.stdout)
    if open_browser and action == "install":
        import webbrowser
        webbrowser.open(response["url"])
    return response


def privileged_main():
    if os.geteuid() != 0 or len(sys.argv) != 2 or sys.argv[1] not in ("install", "uninstall"):
        raise RelayError("Use devrelay ui install/uninstall from your normal user account")
    settings = json.load(sys.stdin)
    uid = int(os.environ.get("SUDO_UID", "0"))
    account = pwd.getpwuid(uid)
    if uid == 0 or settings["uid"] != uid or settings["gid"] != account.pw_gid or settings["user"] != account.pw_name or settings["home"] != account.pw_dir:
        raise RelayError("Dashboard owner does not match the user invoking sudo")
    if type(settings["port"]) is not int or not 1 <= settings["port"] <= 65535:
        raise RelayError("Invalid service port")
    for key in ("home", "python", "catalog", "path", "installation_id", "user"):
        unit_quote(settings[key])
    if not all(Path(settings[key]).is_absolute() for key in ("home", "python", "catalog")):
        raise RelayError("Dashboard paths must be absolute")
    installation = Installation()
    if installation.platform == "linux" and not Path("/run/systemd/system").is_dir():
        raise RelayError("This Linux installation needs systemd. " + FALLBACK)
    print(json.dumps(installation.apply(sys.argv[1], settings)))


if __name__ == "__main__":
    try:
        privileged_main()
    except (RelayError, OSError, ValueError, KeyError) as error:
        print(f"DevRelay dashboard: {error}", file=sys.stderr)
        sys.exit(1)
