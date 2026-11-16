"""Loopback-only configuration dashboard; no task execution endpoints."""

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
from socketserver import TCPServer
import subprocess
import sys
from urllib.parse import urlsplit
import webbrowser

from .errors import RelayError
from .project_registry import Registry, runtime_info, _common


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    root: Path
    runtime: Path

    def call(self, operation, **payload):
        bridge = Path(__file__).with_name("ui_bridge.py")
        try:
            result = subprocess.run([sys.executable, "-I", str(bridge), str(self.root), str(self.runtime)],
                                    input=json.dumps({**payload, "operation": operation}), text=True,
                                    capture_output=True, timeout=90, cwd=self.root)
            response = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise RelayError(f"Cannot read project runtime: {error}") from error
        if result.returncode or "error" in response:
            raise RelayError(response.get("error", "Project runtime failed"))
        return response["result"]


def projects(root, catalog=None):
    runtime = Path(__file__).resolve().parent.parent
    if catalog is None:
        return [Project("project", Path(root).resolve().name, Path(root).resolve(), runtime)]
    registry = Registry(catalog)
    _, entries = registry.read()
    return [Project(entry["id"], entry.get("name", entry["id"]), registry.path_for(entry),
                    registry.path_for(entry, "runtime") if "runtime" in entry else runtime)
            for entry in entries]


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, entries, port=0, *, registry=None, hostname="127.0.0.1", listener=None,
                 installation_id=None):
        if hostname not in ("127.0.0.1", "devrelay", "devrelay.localhost"):
            raise RelayError("Unsupported local dashboard hostname")
        self.registry = registry
        self.installation_id = installation_id
        self.initial_projects = entries
        self.projects = {project.id: project for project in entries}
        self.token = secrets.token_urlsafe(32)
        if listener is None:
            super().__init__(("127.0.0.1", port), DashboardHandler)
        else:
            if (listener.family != socket.AF_INET or listener.type != socket.SOCK_STREAM
                    or listener.getsockname() != ("127.0.0.1", port)):
                listener.close()
                raise RelayError("Dashboard activation requires the expected listening IPv4 loopback socket")
            try:
                # SO_ACCEPTCONN is unavailable on some macOS releases. listen
                # also rejects connected sockets without acquiring another port.
                listener.listen(self.request_queue_size)
            except OSError:
                listener.close()
                raise
            super().__init__(("127.0.0.1", port), DashboardHandler, bind_and_activate=False)
            self.socket.close()
            self.socket = listener
            self.server_address = listener.getsockname()
            self.server_name, self.server_port = self.server_address
        self.origin = f"http://{hostname}" + (f":{self.server_port}" if self.server_port != 80 else "")

    def server_bind(self):
        # HTTPServer resolves a hostname here. This loopback-only service must
        # start without reverse DNS, which can stall on an offline machine.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def refresh_projects(self):
        if self.registry is None:
            return
        entries = projects(None, self.registry.path)
        common = {_common(entry.root) for entry in entries} - {None}
        ids = {entry.id for entry in entries}
        for entry in self.initial_projects:
            if entry.id not in ids and _common(entry.root) not in common:
                entries.insert(0, entry)
        self.projects = {project.id: project for project in entries}

    def session(self):
        self.refresh_projects()
        registered = {entry["id"] for entry in self.registry.read()[1]} if self.registry else set()
        return {"token": self.token,
                "catalog": str(self.registry.path) if self.registry else None,
                "runtime": runtime_info(),
                "projects": [{"id": p.id, "name": p.name, "path": str(p.root),
                              "registered": p.id in registered}
                             for p in self.projects.values()]}


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # No configuration contents or query strings in access logs.
        pass

    def respond(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def trusted(self, *, write=False):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            return False
        if self.headers.get("Sec-Fetch-Site", "none") not in ("none", "same-origin"):
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            return False
        return not write or (origin == self.server.origin and
                             secrets.compare_digest(self.headers.get("X-DevRelay-Token", ""), self.server.token))

    def do_GET(self):
        if not self.trusted():
            return self.respond(403, {"error": "Only same-origin loopback requests are allowed"})
        path = urlsplit(self.path).path
        if path == "/api/health":
            return self.respond(200, {"service": "devrelay", "installation_id": self.server.installation_id})
        if path == "/favicon.ico":
            return self.respond(204, b"")
        if path == "/api/session":
            try:
                return self.respond(200, self.server.session())
            except (RelayError, OSError, ValueError) as error:
                return self.respond(400, {"error": str(error)})
        assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                  "/style.css": ("style.css", "text/css")}
        if path in assets:
            name, kind = assets[path]
            return self.respond(200, Path(__file__).with_name("web").joinpath(name).read_bytes(), kind + "; charset=utf-8")
        return self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if not self.trusted(write=True):
            return self.respond(403, {"error": "Reload the local dashboard before making changes"})
        registration = self.path in ("/api/projects/probe", "/api/projects/add")
        match = re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/(inspect|preview|save|queue|doctor|setup-github)", self.path)
        if not registration and (not match or match[1] not in self.server.projects):
            return self.respond(404, {"error": "Unknown project or operation"})
        if self.headers.get("Content-Type") != "application/json":
            return self.respond(415, {"error": "Expected application/json"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_048_576:
                return self.respond(413, {"error": "Request body must be between 1 byte and 1 MiB"})
            payload = json.loads(self.rfile.read(length))
            if registration:
                if self.server.registry is None:
                    raise ValueError("Project registration is unavailable in this server")
                allowed = {"path", "name", "replace_id", "revision"}
                if not isinstance(payload, dict) or set(payload) - allowed or "path" not in payload:
                    raise ValueError("Invalid registration fields")
                if self.path.endswith("/probe"):
                    if "revision" in payload:
                        raise ValueError("Check the project before adding it")
                    result = self.server.registry.plan(**payload)
                else:
                    if not isinstance(payload.get("revision"), str) or not payload["revision"]:
                        raise ValueError("Check the project before adding it / 请先检查项目")
                    result = self.server.registry.add(**payload)
                    self.server.refresh_projects()
            else:
                if not isinstance(payload, dict) or set(payload) - {"revision", "source", "changes", "preview_sha256"}:
                    raise ValueError("Invalid request fields")
                result = self.server.projects[match[1]].call(match[2], **payload)
        except (RelayError, ValueError, OSError) as error:
            return self.respond(400, {"error": str(error)})
        return self.respond(200, result)


def activated_socket(manager):
    """Adopt an OS-owned listener; never give the dashboard privileged execution."""
    if manager == "systemd":
        if os.environ.pop("LISTEN_PID", "") != str(os.getpid()) or os.environ.pop("LISTEN_FDS", "") != "1":
            raise RelayError("Expected exactly one systemd activation socket")
        os.environ.pop("LISTEN_FDNAMES", None)
        fd = 3
    elif manager == "launchd":
        import ctypes
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        descriptors = ctypes.POINTER(ctypes.c_int)()
        count = ctypes.c_size_t()
        library.launch_activate_socket.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
                                                   ctypes.POINTER(ctypes.c_size_t)]
        library.launch_activate_socket.restype = ctypes.c_int
        error = library.launch_activate_socket(b"Dashboard", ctypes.byref(descriptors), ctypes.byref(count))
        if error:
            raise RelayError(f"Cannot activate launchd dashboard socket: {os.strerror(error)}")
        try:
            if count.value != 1:
                for index in range(count.value):
                    os.close(descriptors[index])
                raise RelayError("Expected exactly one launchd activation socket")
            fd = descriptors[0]
        finally:
            library.free.argtypes = [ctypes.c_void_p]
            library.free(descriptors)
    else:
        raise RelayError("Unknown socket activation manager")
    os.set_inheritable(fd, False)
    listener = socket.socket(fileno=fd)
    listener.setblocking(True)
    return listener


def serve(root=None, *, catalog=None, port=8765, open_browser=False, hostname="127.0.0.1",
          socket_activation=None, installation_id=None):
    if not 0 <= port <= 65535:
        raise RelayError("UI port must be between 0 and 65535")
    try:
        registry = Registry(catalog)
        entries = projects(root) if root is not None else []
        listener = activated_socket(socket_activation) if socket_activation else None
        server = DashboardServer(entries, port, registry=registry, hostname=hostname, listener=listener,
                                 installation_id=installation_id)
        server.refresh_projects()
    except OSError as error:
        raise RelayError(f"Cannot start local dashboard: {error}") from error
    print(json.dumps({"url": server.origin, "message": "Local configuration dashboard; no worker started"}), flush=True)
    if open_browser:
        webbrowser.open(server.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
