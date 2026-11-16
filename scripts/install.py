#!/usr/bin/env python3
"""Install the user-level tool and its optional local dashboard in one flow."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install DevRelay and http://devrelay (macOS / systemd Linux)")
    checkout = Path(__file__).resolve().parents[1]
    parser.add_argument("--source", default=str(checkout) if (checkout / "pyproject.toml").exists() else
                        "git+https://github.com/kleinlau17/DevRelay.git", help="Package path or pinned Git URL")
    parser.add_argument("--without-ui", action="store_true", help="Install only the command; no administrator access")
    parser.add_argument("--port", type=int, default=80, help="Dashboard port; use 8765 if port 80 is occupied")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser after installation")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if hasattr(os, "getuid") and os.getuid() == 0:
        parser.error("Run as your normal user, without sudo; only system setup requests administrator access")
    uv = shutil.which("uv")
    if not uv:
        parser.error("Install uv first, then rerun this installer")
    try:
        subprocess.run([uv, "tool", "install", "--force", "--", args.source], check=True)
        directory = subprocess.run([uv, "tool", "dir", "--bin"], check=True, text=True, capture_output=True).stdout.strip()
        executable = str(Path(directory) / "devrelay")
        subprocess.run([executable, "--version"], check=True)
        if not args.without_ui:
            command = [executable, "ui", "install", "--port", str(args.port)]
            if not args.no_open:
                command.append("--open")
            subprocess.run(command, check=True)
        print("DevRelay installed." if args.without_ui else "DevRelay installed. Open http://devrelay" +
              (f":{args.port}" if args.port != 80 else ""))
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Installation did not complete: {error}\nIf the tool was installed, it remains available; "
              "retry devrelay ui install or use devrelay ui --port 0 --open.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
