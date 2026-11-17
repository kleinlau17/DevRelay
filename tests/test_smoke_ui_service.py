import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from scripts.smoke_ui_service import run, uninstall_after


class SmokeServiceTests(unittest.TestCase):
    def test_failed_command_reports_both_output_streams(self):
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(subprocess.CalledProcessError):
            run([sys.executable, '-c',
                 'import sys; print("partial result"); print("installer cause", file=sys.stderr); sys.exit(7)'])
        self.assertIn('Command failed (7)', errors.getvalue())
        self.assertIn('partial result', errors.getvalue())
        self.assertIn('installer cause', errors.getvalue())

    def test_cleanup_failure_preserves_original_failure(self):
        original = RuntimeError('installation failed')
        with patch('scripts.smoke_ui_service.run', side_effect=RuntimeError('cleanup failed')) as cleanup, \
                redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError) as caught:
            with uninstall_after('/tool/devrelay', {'PATH': '/tool'}):
                raise original
        self.assertIs(caught.exception, original)
        cleanup.assert_called_once_with(['/tool/devrelay', 'ui', 'uninstall'], env={'PATH': '/tool'})

    def test_cleanup_failure_after_success_still_fails(self):
        with patch('scripts.smoke_ui_service.run', side_effect=RuntimeError('cleanup failed')), \
                self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
            with uninstall_after('/tool/devrelay', {}):
                pass
