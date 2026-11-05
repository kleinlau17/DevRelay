"""Loopback-only configuration dashboard; no task execution endpoints."""

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tomllib
from urllib.parse import urlsplit
import webbrowser

from .errors import RelayError


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
    catalog = catalog.resolve()
    try:
        data = tomllib.loads(catalog.read_text())
    except (OSError, ValueError) as error:
        raise RelayError(f"Cannot read project catalog: {error}") from error
    if set(data) != {"projects"} or not isinstance(data["projects"], list) or not data["projects"]:
        raise RelayError("Catalog must contain one or more [[projects]] entries")
    result, ids = [], set()
    for entry in data["projects"]:
        if not isinstance(entry, dict) or set(entry) - {"id", "name", "path", "runtime"}:
            raise RelayError("Unknown project catalog fields")
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", identifier) or identifier in ids:
            raise RelayError("Project IDs must be unique simple names")
        if not isinstance(entry.get("path"), str) or not entry["path"]:
            raise RelayError("Every project needs a path")
        if any(not isinstance(entry[key], str) or not entry[key] for key in ("name", "runtime") if key in entry):
            raise RelayError("Project name/runtime must be nonempty strings")
        ids.add(identifier)
        project_root = (catalog.parent / entry["path"]).resolve()
        project_runtime = (catalog.parent / entry["runtime"]).resolve() if "runtime" in entry else runtime
        if not project_root.is_dir() or not (project_runtime / "devrelay/config.py").is_file():
            raise RelayError(f"Project {identifier}: path or runtime package directory does not exist")
        result.append(Project(identifier, entry.get("name", identifier), project_root, project_runtime))
    return result


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, entries, port=0):
        self.projects = {project.id: project for project in entries}
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


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
        if path == "/favicon.ico":
            return self.respond(204, b"")
        if path == "/api/session":
            return self.respond(200, {"token": self.server.token,
                                      "projects": [{"id": p.id, "name": p.name} for p in self.server.projects.values()]})
        assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                  "/style.css": ("style.css", "text/css")}
        if path in assets:
            name, kind = assets[path]
            return self.respond(200, Path(__file__).with_name("web").joinpath(name).read_bytes(), kind + "; charset=utf-8")
        return self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if not self.trusted(write=True):
            return self.respond(403, {"error": "Reload the local dashboard before making changes"})
        match = re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]+)/(inspect|preview|save|queue|doctor)", self.path)
        if not match or match[1] not in self.server.projects:
            return self.respond(404, {"error": "Unknown project or operation"})
        if self.headers.get("Content-Type") != "application/json":
            return self.respond(415, {"error": "Expected application/json"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_048_576:
                return self.respond(413, {"error": "Request body must be between 1 byte and 1 MiB"})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) - {"revision", "source", "changes", "preview_sha256"}:
                raise ValueError("Invalid request fields")
            result = self.server.projects[match[1]].call(match[2], **payload)
        except (RelayError, ValueError, UnicodeError) as error:
            return self.respond(400, {"error": str(error)})
        return self.respond(200, result)


def serve(root, *, catalog=None, port=8765, open_browser=False):
    if not 0 <= port <= 65535:
        raise RelayError("UI port must be between 0 and 65535")
    try:
        server = DashboardServer(projects(root, catalog), port)
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
