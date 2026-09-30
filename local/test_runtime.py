import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

import bootstrap
import up


class RuntimeOptionsTests(unittest.TestCase):
    def test_cli_accepts_unlimited_deepseek_run(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(up.tempfile, 'gettempdir', return_value=directory), \
             mock.patch.object(up.sys, 'argv', ['up.py', 'start', '--duration', '0', '--provider', 'deepseek', '--model', 'deepseek-flash']), \
             mock.patch.object(up, 'Runtime'), mock.patch.object(up, 'start') as start:
            up.main()
        args = start.call_args.args[1]
        self.assertEqual(args.duration, 0)
        self.assertEqual(args.provider, 'deepseek')
        self.assertEqual(args.model, 'deepseek-flash')

    def test_cli_rejects_negative_duration(self):
        with mock.patch.object(up.sys, 'argv', ['up.py', 'start', '--duration', '-1']), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            up.main()
        self.assertEqual(error.exception.code, 2)

    def test_unlimited_workers_clear_old_deadline_without_creating_a_timer(self):
        with mock.patch.object(bootstrap, 'run') as run, mock.patch.object(bootstrap, 'write') as write, \
             mock.patch.object(bootstrap.subprocess, 'run') as process:
            bootstrap.start_workers({'hr': 'atto-hr'}, 0)
        self.assertFalse(any(call.args[0] == 'systemd-run' for call in run.call_args_list))
        process.assert_any_call(['systemctl', 'stop', 'atto-discovery-deadline.timer', 'atto-discovery-deadline.service'], capture_output=True)
        write.assert_called_once_with(bootstrap.READY, 'ready\n', 0o600)
        run.assert_any_call('systemctl', 'start', 'multi-user.target', 'atto-hr.service')

    def test_bounded_workers_keep_the_deadline(self):
        with mock.patch.object(bootstrap, 'run') as run, mock.patch.object(bootstrap, 'write'), \
             mock.patch.object(bootstrap.subprocess, 'run'):
            bootstrap.start_workers({'hr': 'atto-hr'}, 900)
        timers = [call for call in run.call_args_list if call.args[0] == 'systemd-run']
        self.assertEqual(len(timers), 1)
        self.assertIn('--on-active=900', timers[0].args)

    def test_bootstrap_accepts_zero_and_configures_deepseek_chat_completions(self):
        class Configured(Exception):
            pass
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with mock.patch.object(bootstrap, 'ROOT', root), \
                 mock.patch.object(bootstrap, 'READY', root / 'ready'), \
                 mock.patch.object(bootstrap, 'RESTORE_PENDING', root / 'restore-pending'), \
                 mock.patch.object(bootstrap, 'check_databases'), \
                 mock.patch.object(bootstrap.pwd, 'getpwnam'), \
                 mock.patch.object(bootstrap.telegram, 'configure', side_effect=Configured) as configure:
                with self.assertRaises(Configured):
                    bootstrap.bootstrap({'workers': False, 'duration': 0, 'provider': 'deepseek', 'model': 'deepseek-flash'})
            company = configure.call_args.args[1]
        self.assertEqual(company['provider'], 'deepseek')
        self.assertEqual(company['model'], 'deepseek-flash')
        self.assertEqual(company['agent_config']['provider'], '')
        self.assertTrue(company['agent_config']['multimodal_support'])

    def test_bootstrap_rejects_negative_duration(self):
        with mock.patch.object(bootstrap, 'RESTORE_PENDING', '/nonexistent-attosys-test-marker'):
            with self.assertRaisesRegex(ValueError, 'duration'):
                bootstrap.bootstrap({'duration': -1})


if __name__ == '__main__':
    unittest.main()
