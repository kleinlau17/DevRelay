"""Exercise real system installation on disposable macOS/systemd Linux CI machines.

This deliberately changes hosts and installs an OS service. Never run on a shared
workstation: require --disposable-machine and passwordless sudo. Always uninstall.
"""

import argparse
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import venv


def run(argv, *, env=None):
    try:
        return subprocess.check_output(argv, text=True, stderr=subprocess.PIPE, env=env).strip()
    except subprocess.CalledProcessError as error:
        print(f'Command failed ({error.returncode}): {argv!r}', file=sys.stderr)
        print(f'stdout:\n{error.stdout or ""}\nstderr:\n{error.stderr or ""}', file=sys.stderr)
        raise


@contextmanager
def uninstall_after(entry, environment):
    try:
        yield
    finally:
        failed = sys.exc_info()[0] is not None
        try:
            run([str(entry), 'ui', 'uninstall'], env=environment)
        except Exception:
            if not failed:
                raise
            print('Cleanup also failed; preserving the original smoke-test error.', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    parser.add_argument('--disposable-machine', action='store_true', required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    run(['sudo', '-n', 'true'])
    managed = (Path('/Library/LaunchDaemons/org.devrelay.dashboard.plist') if sys.platform == 'darwin'
               else Path('/etc/systemd/system/devrelay-dashboard.socket'))
    if managed.exists():
        raise RuntimeError('An existing installation must not be replaced by this smoke test')
    hosts = Path('/etc/hosts').read_bytes()
    with tempfile.TemporaryDirectory(prefix='devrelay-service-') as temporary:
        root = Path(temporary)
        venv.EnvBuilder(with_pip=True).create(root / 'tool')
        python = root / 'tool/bin/python'
        entry = root / 'tool/bin/devrelay'
        environment = dict(os.environ, XDG_CONFIG_HOME=str(root / 'config'), PATH=str(root / 'tool/bin') + ':' + os.environ['PATH'])
        environment.pop('PYTHONPATH', None)
        environment.pop('PYTHONHOME', None)
        run([str(python), '-m', 'pip', 'install', '--no-deps', str(wheel)], env=environment)
        with uninstall_after(entry, environment):
            for attempt in range(2):
                installed = json.loads(run([str(entry), 'ui', 'install'], env=environment))
                assert installed['url'] == 'http://devrelay' and installed['healthy'], installed
                status = json.loads(run([str(entry), 'ui', 'status'], env=environment))
                assert status['healthy'], status
                connection = http.client.HTTPConnection('devrelay', timeout=10)
                connection.request('GET', '/')
                response = connection.getresponse()
                assert response.status == 200 and b'DevRelay' in response.read()
                connection.close()
            if sys.platform == 'darwin':
                details = run(['sudo', '-n', '/bin/launchctl', 'print', 'system/org.devrelay.dashboard'])
                pid = re.search(r'\bpid = (\d+)', details)[1]
            else:
                pid = run(['systemctl', 'show', '--property=MainPID', '--value', 'devrelay-dashboard.service'])
            assert int(run(['ps', '-o', 'uid=', '-p', pid])) == os.getuid() != 0
            # Reopen the service after terminating its process: OS owns the listening socket.
            os.kill(int(pid), 9)
            for _ in range(30):
                time.sleep(0.5)
                result = subprocess.run([str(entry), 'ui', 'status'], env=environment, capture_output=True, text=True)
                if result.returncode == 0:
                    break
            else:
                raise RuntimeError('Service did not recover after process termination')
        assert Path('/etc/hosts').read_bytes() == hosts, 'Hosts bytes were not restored'
        assert not managed.exists(), 'Service definition remains after uninstall'
    print('System dashboard installation, reinstall, HTTP access, unprivileged execution, restart and uninstall passed.')


if __name__ == '__main__':
    main()
