import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import snapshot
import up


class RestoreRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = pathlib.Path(self.temp.name)
        self.args = types.SimpleNamespace(name='atto-recovery-test', idle=True, no_build=True, model='test', duration=3,
                                          archive=self.directory / 'saved', dns=None, no_publish=True)
        self.runtime = mock.Mock(spec=up.Runtime)
        self.runtime.info.return_value = {'status': {'state': 'stopped'}, 'configuration': {'publishedPorts': [], 'dns': {}}}
        self.runtime.present.side_effect = lambda name, path: path == '/var/lib/attosys-restore-pending'
        def run(*args, **kwargs):
            if args[0] in ('start', 'stop'):
                self.runtime.info.return_value['status']['state'] = 'running' if args[0] == 'start' else 'stopped'
            return subprocess.CompletedProcess([], 0, stdout='aarch64\n')
        self.runtime.run.side_effect = run

    def test_restored_image_starts_with_an_incomplete_marker(self):
        self.assertIn('touch /var/lib/attosys-restore-pending', snapshot.CONTAINERFILE)

    def test_start_refuses_incomplete_restore(self):
        with self.assertRaisesRegex(ValueError, 'incomplete.*new --name'):
            up.start(self.runtime, self.args)
        self.runtime.bootstrap.assert_not_called()
        self.runtime.run.assert_called_with('stop', self.args.name)

    def test_incomplete_restore_is_rejected_before_credentials_or_ready_shortcut(self):
        self.args.idle = False
        self.runtime.info.return_value['status']['state'] = 'running'
        self.runtime.present.side_effect = lambda name, path: True
        with mock.patch.object(up.getpass, 'getpass') as prompt:
            with self.assertRaisesRegex(ValueError, 'restore is incomplete'):
                up.start(self.runtime, self.args)
        prompt.assert_not_called()
        self.runtime.bootstrap.assert_not_called()

    def test_save_refuses_incomplete_restore(self):
        with mock.patch.object(snapshot, 'inspect_pair'), self.assertRaisesRegex(ValueError, 'incomplete.*new --name'):
            up.save(self.runtime, self.args)
        self.assertFalse(self.args.archive.exists())
        self.assertFalse(any('pack' in call.args for call in self.runtime.run.call_args_list))
        self.runtime.run.assert_called_with('stop', self.args.name)

    def test_failed_validation_keeps_restore_incomplete(self):
        self.args.archive.mkdir()
        for name in ('os.tar', 'data.tar'):
            (self.args.archive / name).write_bytes(b'test fixture')
        self.runtime.info.side_effect = [None, {'status': {'state': 'stopped'}}]
        self.runtime.architecture.return_value = 'arm64'
        self.runtime.bootstrap.side_effect = RuntimeError('validation failed')
        with mock.patch.object(snapshot, 'inspect_pair', return_value={'architecture': 'arm64'}):
            with self.assertRaisesRegex(RuntimeError, 'validation failed'):
                up.restore(self.runtime, self.args)
        self.assertFalse(any('/var/lib/attosys-restore-pending' in call.args for call in self.runtime.run.call_args_list))
        self.runtime.run.assert_called_with('stop', self.args.name)

    def test_restore_uses_current_importer_without_overwriting_saved_application(self):
        self.args.archive.mkdir()
        for name in ('os.tar', 'data.tar'):
            (self.args.archive / name).write_bytes(b'test fixture')
        self.runtime.info.side_effect = [None, {'status': {'state': 'stopped'}}]
        self.runtime.architecture.return_value = 'arm64'
        with mock.patch.object(snapshot, 'inspect_pair', return_value={'architecture': 'arm64'}):
            up.restore(self.runtime, self.args)
        calls = self.runtime.run.call_args_list
        importer = next(call.args[3] for call in calls if 'unpack' in call.args)
        self.assertTrue(importer.startswith('/run/'))
        self.assertTrue(any(getattr(call.kwargs.get('stdin'), 'name', None) == snapshot.__file__ for call in calls))
        self.assertFalse(any('/opt/attosys/local/snapshot.py' in call.args for call in calls))

    def test_presence_probe_fails_closed_on_runtime_errors(self):
        runtime = object.__new__(up.Runtime)
        runtime.binary = 'container'
        for status in (1, 125):
            result = subprocess.CompletedProcess([], status)
            with self.subTest(status=status), mock.patch.object(subprocess, 'run', return_value=result):
                with self.assertRaises(subprocess.CalledProcessError):
                    runtime.present(self.args.name, '/var/lib/attosys-restore-pending')

    def test_presence_probe_distinguishes_absent_files_from_invalid_responses(self):
        runtime = object.__new__(up.Runtime)
        runtime.binary = 'container'
        for output, expected in (('0\n', False), ('1\n', True)):
            with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=output)):
                self.assertEqual(runtime.present(self.args.name, '/marker'), expected)
        with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='')):
            with self.assertRaises(RuntimeError):
                runtime.present(self.args.name, '/marker')


if __name__ == '__main__':
    unittest.main()
