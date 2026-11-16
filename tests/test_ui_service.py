import http.client
import io
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from devrelay.cli import main
from devrelay.errors import RelayError
from devrelay.ui import DashboardServer, activated_socket
from devrelay.ui_service import BLOCK, Installation, hosts_content, health, unit_quote


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.installation = Installation('darwin', self.root)
        self.installation.hosts.parent.mkdir(parents=True)
        self.original = b'127.0.0.1 localhost\n# preserve aliases\n10.0.0.2 internal\n'
        self.installation.hosts.write_bytes(self.original)
        self.installation.services[0].parent.mkdir(parents=True)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.settings = dict(uid=501, gid=20, user='owner', home='/home/owner', python='/home/owner/tool/bin/python',
                             path='/usr/bin:/home/owner/bin', catalog='/home/owner/catalog.toml',
                             port=port, installation_id='test-instance')
        self.running = self.patch('devrelay.ui_service.Installation.running', return_value=False)
        self.start = self.patch('devrelay.ui_service.Installation.start')
        self.stop = self.patch('devrelay.ui_service.Installation.stop')
        self.wait = self.patch('devrelay.ui_service.wait_healthy')

    def patch(self, *args, **kwargs):
        item = patch(*args, **kwargs)
        self.addCleanup(item.stop)
        return item.start()

    def install(self):
        return self.installation.apply('install', dict(self.settings))

    def test_install_repeat_upgrade_and_uninstall_preserve_other_hosts_and_projects(self):
        self.assertTrue(self.install()['healthy'])
        first = self.installation.read()
        self.assertEqual(first['phase'], 'ready')
        self.assertEqual(self.installation.hosts.read_text().count(BLOCK), 1)
        self.settings['python'] = '/home/owner/new tool/bin/python'
        self.install()
        self.assertEqual(self.installation.hosts.read_text().count(BLOCK), 1)
        self.assertEqual(self.installation.read()['settings']['python'], self.settings['python'])
        self.assertEqual(self.installation.read()['settings']['installation_id'], first['settings']['installation_id'])
        with self.installation.hosts.open('ab') as stream:
            stream.write(b'10.0.0.3 later-change\n')
        result = self.installation.apply('uninstall', self.settings)
        self.assertFalse(result['installed'])
        self.assertEqual(self.installation.hosts.read_bytes(), self.original + b'10.0.0.3 later-change\n')
        self.assertFalse(self.installation.services[0].exists())
        self.assertFalse(self.installation.state.exists())
        self.assertFalse(self.installation.apply('uninstall', self.settings)['installed'])

    def test_hosts_without_final_newline_round_trip(self):
        self.installation.hosts.write_bytes(b'127.0.0.1 localhost')
        self.install()
        self.install()
        self.installation.apply('uninstall', self.settings)
        self.assertEqual(self.installation.hosts.read_bytes(), b'127.0.0.1 localhost')

    def test_busy_port_does_not_modify_configuration(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            self.settings['port'] = listener.getsockname()[1]
            with self.assertRaisesRegex(RelayError, 'unavailable'):
                self.install()
        self.assertEqual(self.installation.hosts.read_bytes(), self.original)
        self.assertFalse(self.installation.state.exists())
        self.start.assert_not_called()
        self.stop.assert_not_called()

    def test_failed_start_restores_fresh_install_and_prior_install(self):
        self.wait.side_effect = RelayError('health failure')
        with self.assertRaisesRegex(RelayError, 'health failure'):
            self.install()
        self.assertEqual(self.installation.hosts.read_bytes(), self.original)
        self.assertFalse(self.installation.services[0].exists())
        self.assertFalse(self.installation.state.exists())
        self.wait.side_effect = None
        self.install()
        old = self.installation.read()
        self.settings['python'] = '/other/python'
        self.wait.side_effect = RelayError('health failure')
        with self.assertRaisesRegex(RelayError, 'health failure'):
            self.install()
        self.assertEqual(self.installation.read(), old)
        self.assertEqual(self.installation.services[0].read_text(), old['files'][0])

    def test_unmanaged_and_modified_settings_are_never_overwritten(self):
        path = self.installation.services[0]
        path.write_text('user-owned config')
        with self.assertRaisesRegex(RelayError, 'preserved'):
            self.install()
        self.assertEqual(path.read_text(), 'user-owned config')
        path.unlink()
        self.install()
        path.write_text(path.read_text() + '\n<!-- manual edit -->')
        with self.assertRaisesRegex(RelayError, 'preserved'):
            self.installation.apply('uninstall', self.settings)
        self.assertTrue(self.installation.state.exists())

    def test_partial_linux_file_install_rolls_back_and_corrupt_records_fail_closed(self):
        from devrelay.ui_service import atomic_write
        linux = Installation('linux', self.root)
        linux.services[0].parent.mkdir(parents=True)
        self.patch('devrelay.ui_service.command')
        def fail_second_file(path, data, *, expected):
            if path == linux.services[1]:
                raise OSError('disk failure')
            return atomic_write(path, data, expected=expected)
        with patch('devrelay.ui_service.atomic_write', side_effect=fail_second_file):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                linux.apply('install', self.settings)
        self.assertFalse(linux.state.exists())
        self.assertTrue(all(not path.exists() for path in linux.services))
        self.assertEqual(linux.hosts.read_bytes(), self.original)
        linux.state.write_text('{"files": []}')
        with self.assertRaisesRegex(RelayError, 'Invalid dashboard installation record'):
            linux.apply('install', self.settings)

    def test_second_user_cannot_take_over_installation(self):
        self.install()
        with self.assertRaisesRegex(RelayError, 'another user'):
            self.installation.apply('install', {**self.settings, 'uid': 502})

    def test_hosts_conflicts_and_symlinks_are_preserved(self):
        for source in ('127.0.0.1 devrelay\n', '10.0.0.2 DEVRELAY.\n', BLOCK.replace('127.0.0.1', '10.0.0.2')):
            with self.subTest(source=source), self.assertRaises(RelayError):
                hosts_content(source, install=True)
        target = self.root / 'hosts-original'
        self.installation.hosts.rename(target)
        self.installation.hosts.symlink_to(target)
        with self.assertRaisesRegex(RelayError, 'symlink'):
            self.install()
        self.assertEqual(target.read_bytes(), self.original)

    def test_interrupt_journal_can_be_recovered_by_uninstall(self):
        self.wait.side_effect = SystemExit('simulate hard interruption')
        with self.assertRaises(SystemExit):
            self.install()
        self.assertEqual(self.installation.read()['phase'], 'installing')
        self.wait.side_effect = None
        self.assertFalse(self.installation.apply('uninstall', self.settings)['installed'])
        self.assertEqual(self.installation.hosts.read_bytes(), self.original)
        self.assertFalse(self.installation.state.exists())

    def test_uninstall_can_resume_after_interruption(self):
        self.install()
        self.stop.side_effect = RelayError('stop failed')
        with self.assertRaisesRegex(RelayError, 'stop failed'):
            self.installation.apply('uninstall', self.settings)
        self.assertEqual(self.installation.read()['phase'], 'removing')
        self.stop.side_effect = None
        self.installation.services[0].unlink()
        self.assertFalse(self.installation.apply('uninstall', self.settings)['installed'])
        self.assertEqual(self.installation.hosts.read_bytes(), self.original)

    def test_service_definitions_use_unprivileged_owner_loopback_and_literal_arguments(self):
        self.settings['python'] = '/home/space $HOME %x "name"/python'
        plist = plistlib.loads(self.installation.definitions(self.settings)[0].encode())
        self.assertEqual(plist['UserName'], 'owner')
        self.assertEqual(plist['ProgramArguments'][0], self.settings['python'])
        self.assertEqual(plist['Sockets']['Dashboard']['SockNodeName'], '127.0.0.1')
        self.assertEqual(set(plist['EnvironmentVariables']), {'HOME', 'PATH', 'PYTHONUNBUFFERED'})
        linux = Installation('linux', self.root)
        socket_unit, service_unit = linux.definitions(self.settings)
        self.assertIn(f'ListenStream=127.0.0.1:{self.settings["port"]}', socket_unit)
        self.assertIn('User=501\nGroup=20\n', service_unit)
        self.assertIn('NoNewPrivileges=yes', service_unit)
        self.assertIn('$$HOME %%x \\"name\\"', service_unit)
        with self.assertRaises(RelayError):
            unit_quote('/path\nExecStart=/bad')

    def test_cli_preview_and_status_do_not_initialize_projects_or_elevate(self):
        with patch('devrelay.cli.State') as state, patch('devrelay.ui_service.subprocess.run') as run:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['ui', 'install', '--dry-run']), 0)
            self.assertTrue(json.loads(output.getvalue())['requires_admin'])
            self.assertEqual(json.loads(output.getvalue())['url'], 'http://devrelay')
            state.assert_not_called()
            run.assert_not_called()
        for args in (['--project', '/tmp', 'ui', 'install'], ['ui', 'install', '--port', '0'],
                     ['ui', 'uninstall', '--port', '80'], ['ui', '--dry-run']):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 1)


class ActivatedDashboardTests(unittest.TestCase):
    def test_real_subprocess_adopts_systemd_descriptor_without_leaking_it(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            code = '''import os
from devrelay.ui import DashboardServer, activated_socket
os.dup2(0, 3)
os.environ.update(LISTEN_PID=str(os.getpid()), LISTEN_FDS='1')
listener = activated_socket('systemd')
assert not os.get_inheritable(listener.fileno())
server = DashboardServer([], listener.getsockname()[1], listener=listener, hostname='devrelay', installation_id='child')
server.daemon_threads = False
server.handle_request()
server.server_close()
'''
            environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
            child = subprocess.Popen([sys.executable, '-c', code], stdin=listener, env=environment,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                healthy = health({'port': port, 'installation_id': 'child'})
                if not healthy and child.poll() is None:
                    child.kill()
                output, error = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, error)
                self.assertTrue(healthy, error)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_adopted_socket_accepts_only_named_origin_and_valid_token(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = DashboardServer([], port, hostname='devrelay', listener=listener, installation_id='expected')
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.assertEqual(server.origin, f'http://devrelay:{port}')
        self.assertTrue(health({'port': port, 'installation_id': 'expected'}))
        self.assertFalse(health({'port': port, 'installation_id': 'another-install'}))
        def request(path, headers, method='GET'):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                connection.request(method, path, body='{}' if method == 'POST' else None, headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        authority = f'devrelay:{port}'
        status, session = request('/api/session', {'Host': authority})
        self.assertEqual(status, 200)
        for host in (f'127.0.0.1:{port}', f'evil.example:{port}', 'devrelay'):
            self.assertEqual(request('/api/session', {'Host': host})[0], 403)
        headers = {'Host': authority, 'Origin': server.origin, 'X-DevRelay-Token': session['token'],
                   'Content-Type': 'application/json'}
        # Valid origin passes authorization, then fails because no registry was configured.
        self.assertEqual(request('/api/projects/probe', headers, 'POST')[0], 400)
        self.assertEqual(request('/api/projects/probe', {**headers, 'Origin': 'http://evil.example'}, 'POST')[0], 403)
        self.assertEqual(request('/api/projects/probe', {**headers, 'X-DevRelay-Token': 'wrong'}, 'POST')[0], 403)

    def test_activation_rejects_wrong_address_and_missing_environment(self):
        listener = socket.socket()
        listener.bind(('0.0.0.0', 0))
        listener.listen()
        with self.assertRaisesRegex(RelayError, 'loopback'):
            DashboardServer([], listener.getsockname()[1], listener=listener)
        self.assertEqual(listener.fileno(), -1)
        with patch.dict(os.environ, {'LISTEN_PID': str(os.getpid() + 1), 'LISTEN_FDS': '1'}):
            with self.assertRaisesRegex(RelayError, 'systemd'):
                activated_socket('systemd')


if __name__ == '__main__':
    unittest.main()
