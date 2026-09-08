import hashlib
import json
import os
import pathlib
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def fixture(action):
    import pwd
    import sqlite3
    import requests
    import yaml

    record_path = ROOT / 'persistence-test.json'
    def database(path, query, values=()):
        with sqlite3.connect(path) as db:
            return db.execute(query, values).fetchall()
    def fingerprint(path):
        info = path.stat()
        return [hashlib.sha256(path.read_bytes()).hexdigest(), info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]
    if action == 'prepare':
        subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends', 'jq'], check=True)
        company = yaml.safe_load((ROOT / 'company.yaml').read_text())
        company['agents']['apprentice'] = {'soul': 'labs', 'description': 'Persistence verification worker'}
        (ROOT / 'company.yaml').write_text(yaml.safe_dump(company, sort_keys=False))
        subprocess.run(['python3', str(ROOT / 'seed.py')], check=True)
        subprocess.run(['python3', str(ROOT / 'hire.py'), '--no-start', 'apprentice'], check=True)
        service = pathlib.Path('/etc/systemd/system/custom-snapshot-test.service')
        service.write_text('[Unit]\nDescription=Snapshot service fixture\n[Service]\nExecStart=/bin/sleep infinity\n[Install]\nWantedBy=multi-user.target\n')
        subprocess.run(['systemctl', 'enable', 'custom-snapshot-test.service'], check=True)
        paths = {str(service): fingerprint(service)}
        codeword = 'cedar-' + uuid.uuid4().hex
        for user in ('atto-hr', 'atto-apprentice'):
            home = pathlib.Path('/home') / user
            for relative in ('agent/config.json', 'subconscious/config.json'):
                path = home / relative
                data = json.loads(path.read_text())
                data['persistence_marker'] = 'keep employee customization'
                path.write_text(json.dumps(data))
                paths[str(path)] = fingerprint(path)
            files = {'agent/MEMORY.md': 'Read memory/transfer.md for the transfer check codeword.\n',
                     'agent/memory/transfer.md': 'The transfer check codeword is ' + codeword + '.\n',
                     'TODO.md': 'Unfinished: write the transfer check codeword to transfer-result.txt.\n',
                     'agent/messages.jsonl': json.dumps({'role': 'assistant', 'content': 'Waiting for the saved transfer check.'}) + '\n'}
            if user == 'atto-apprentice':
                files['agent/trigger-queue/00000000000000000001.json'] = json.dumps({'role': 'user', 'content':
                    '<system-message>[trigger transfer-check] Read your saved memory/transfer.md and write ONLY its codeword to /home/atto-apprentice/transfer-result.txt. This completes the paused transfer verification task. Do not do other work.</system-message>'})
            account = pwd.getpwnam(user)
            for relative, body in files.items():
                path = home / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                os.chown(path, account.pw_uid, account.pw_gid)
                path.chmod(0o600)
                os.chown(path.parent, account.pw_uid, account.pw_gid)
                paths[str(path)] = fingerprint(path)
        tool = pathlib.Path('/home/atto-apprentice/.venv/bin/jq')
        tool.parent.mkdir(parents=True)
        tool.symlink_to('/usr/bin/jq')
        paths[str(tool)] = fingerprint(tool)
        shared = ROOT / 'shared/transfer-evidence.json'
        shared.write_text(json.dumps({'test_fixture': True, 'unfinished_trial': 'retained'}))
        paths[str(shared)] = fingerprint(shared)
        private = pathlib.Path('/home/atto-hr/browser/transfer-private.marker')
        private.write_text('managed browser profile must not be shared')
        pathlib.Path('/run/attosys/llm.env').write_text('ATTOBOT_API_KEY="private-test-sentinel"\n')
        database('/var/lib/atto-proxy/requests.db', 'INSERT INTO requests(id,body,agent) VALUES(?,?,?)',
                 ('snapshot-fixture', 'retained capture fixture', 'atto-hr'))
        message = requests.post('http://127.0.0.1:8090/api', headers={'X-Attobot-Lab': '1'},
                                json={'text': 'saved pending chat delivery', 'message_thread_id': company['agents']['hr']['topic_id']}, timeout=5).json()['result']
        for _ in range(100):
            if database('/var/lib/atto-mux/updates.db', 'SELECT id FROM updates WHERE id=?', (message['message_id'],)):
                break
            time.sleep(.1)
        else:
            raise AssertionError('mux did not persist the chat delivery')
        record_path.write_text(json.dumps({'files': paths, 'codeword': codeword, 'message_id': message['message_id'],
                                          'secrets_hash': hashlib.sha256((ROOT / 'secrets.yaml').read_bytes()).hexdigest()}))
    elif action in ('original', 'restored', 'offline'):
        record = json.loads(record_path.read_text())
        for filename, expected in record['files'].items():
            assert fingerprint(pathlib.Path(filename)) == expected, f'changed saved state: {filename}'
        assert database('/var/lib/atto-proxy/requests.db', 'SELECT body FROM requests WHERE id=?', ('snapshot-fixture',)) == [('retained capture fixture',)]
        assert database('/var/lib/atto-mux/updates.db', 'SELECT id FROM updates WHERE id=?', (record['message_id'],))
        assert database('/var/lib/atto-chat/chat.sqlite3', 'SELECT id FROM messages WHERE id=?', (record['message_id'],))
        assert not pathlib.Path('/run/attosys/llm.env').exists()
        if action != 'original':
            assert not pathlib.Path('/home/atto-hr/browser/transfer-private.marker').exists()
        if action == 'offline':
            assert not (ROOT / 'secrets.yaml').exists()
            for name in ('atto-chat', 'atto-mux', 'atto-proxy', 'atto-hr', 'atto-apprentice'):
                assert subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode != 0
        elif action == 'restored':
            assert hashlib.sha256((ROOT / 'secrets.yaml').read_bytes()).hexdigest() != record['secrets_hash']
        result = subprocess.check_output(['runuser', '-u', 'atto-apprentice', '--', '/home/atto-apprentice/.venv/bin/jq', '-n', '6*7'], text=True)
        assert result.strip() == '42'
        print('Saved files, ownership, roster, installed native tool and all three databases verified.')
    elif action == 'live':
        company = yaml.safe_load((ROOT / 'company.yaml').read_text())
        subprocess.run(['systemctl', 'disable', *[f'atto-{role}.service' for role in company['agents'] if role != 'apprentice']], check=True)
        company['agents'] = {'apprentice': company['agents']['apprentice']}
        (ROOT / 'company.yaml').write_text(yaml.safe_dump(company, sort_keys=False))
    elif action == 'live-result':
        result = pathlib.Path('/home/atto-apprentice/transfer-result.txt')
        assert result.read_text().strip() == json.loads(record_path.read_text())['codeword'], 'incorrect transfer result'
        captures = database('/var/lib/atto-proxy/requests.db', 'SELECT COUNT(*) FROM requests WHERE agent=? AND status_code=200', ('atto-apprentice',))[0][0]
        assert captures > 0, 'no successful model capture'
        subprocess.run(['systemctl', 'is-active', '--quiet', 'custom-snapshot-test.service'], check=True)
        print(f'Live employee continued from saved memory and trigger; {captures} successful model captures.')


@unittest.skipUnless(platform.system() in ('Darwin', 'Linux') and os.environ.get('ATTOSYS_CONTAINER_TESTS'), 'set ATTOSYS_CONTAINER_TESTS=1 to use real containers')
class LifecycleTests(unittest.TestCase):
    def test_stop_restart_and_transfer(self):
        import up
        runtime = up.Runtime()
        suffix = uuid.uuid4().hex[:10]
        source, restored = 'atto-test-' + suffix, 'atto-restored-' + suffix
        names = []
        def cli(command, name, *extra):
            return subprocess.run([sys.executable, str(ROOT / 'local/up.py'), command, *map(str, extra),
                                   '--name', name, '--no-publish', '--dns', '1.1.1.1'], check=True, timeout=600)
        def inside(name, action):
            runtime.run('exec', name, 'python3', '/opt/attosys/local/test_lifecycle.py', '--fixture', action)
        try:
            names.append(source)
            cli('start', source, '--idle', '--no-build')
            expected = {}
            for repo, patterns in up.SOURCES.items():
                for pattern in patterns:
                    for path in (ROOT.parent / repo).glob(pattern):
                        for file in path.rglob('*') if path.is_dir() else [path]:
                            if file.is_file() and '__pycache__' not in file.parts and file.suffix != '.pyc':
                                expected['/opt/' + str(file.relative_to(ROOT.parent))] = hashlib.sha256(file.read_bytes()).hexdigest()
            runtime.run('exec', '--interactive', source, 'python3', '-c',
                        "import hashlib,json,pathlib,sys; expected=json.load(sys.stdin); changed=[name for name,digest in expected.items() if not pathlib.Path(name).is_file() or hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()!=digest]; assert not changed, 'Stale image; run local/up.py start --build-only: '+str(changed)",
                        input=json.dumps(expected), text=True)
            print(f'Verified {len(expected)} packaged source files against the current checkouts.', flush=True)
            runtime.run('cp', __file__, source + ':/opt/attosys/local/test_lifecycle.py')
            runtime.run('cp', ROOT / 'local/test_persistence.py', source + ':/opt/attosys/local/test_persistence.py')
            runtime.run('exec', source, 'python3', '-m', 'unittest', 'discover', '-s', '/opt/attosys/local', '-p', 'test_persistence.py', '-v')
            inside(source, 'prepare')
            cli('start', source, '--idle')
            cli('stop', source)
            cli('start', source, '--idle', '--source-root', '/nonexistent/sibling-checkouts')
            inside(source, 'original')
            with tempfile.TemporaryDirectory(prefix='attosys-transfer-test-') as directory:
                archive = pathlib.Path(directory) / 'company-snapshot'
                cli('save', source, archive)
                self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o700)
                self.assertEqual({path.name for path in archive.iterdir()}, {'os.tar', 'data.tar', 'manifest.json'})
                self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in archive.iterdir()))
                failed = 'atto-incomplete-' + suffix
                names.append(failed)
                args = SimpleNamespace(name=failed, archive=archive, dns='1.1.1.1', no_publish=True)
                with mock.patch.object(runtime, 'bootstrap', side_effect=RuntimeError('injected validation failure')):
                    with self.assertRaisesRegex(RuntimeError, 'injected validation failure'):
                        up.restore(runtime, args)
                self.assertEqual(runtime.info(failed)['status']['state'], 'stopped')
                for command in (['start', '--idle'], ['save', str(pathlib.Path(directory) / 'incomplete-snapshot')]):
                    rejected = subprocess.run([sys.executable, str(ROOT / 'local/up.py'), *command, '--name', failed], capture_output=True, text=True, timeout=240)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn('restore is incomplete', rejected.stderr)
                    self.assertEqual(runtime.info(failed)['status']['state'], 'stopped')
                self.assertFalse((pathlib.Path(directory) / 'incomplete-snapshot').exists())
                names.append(restored)
                cli('restore', restored, archive, '--source-root', '/nonexistent/sibling-checkouts')
                self.assertEqual(runtime.info(restored)['status']['state'], 'stopped')
                runtime.run('start', restored)
                runtime.wait(restored)
                inside(restored, 'offline')
                cli('start', restored, '--idle', '--source-root', '/nonexistent/sibling-checkouts')
                inside(restored, 'restored')
                rejected = subprocess.run([sys.executable, str(ROOT / 'local/up.py'), 'restore', str(archive), '--name', restored], capture_output=True, text=True)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn('will not overwrite', rejected.stderr)
                key = os.environ.get('ATTOBOT_API_KEY', '')
                if key:
                    inside(restored, 'live')
                    runtime.bootstrap(restored, 'start', {'api_key': key, 'duration': 120})
                    deadline = time.monotonic() + 110
                    while time.monotonic() < deadline and not runtime.present(restored, '/home/atto-apprentice/transfer-result.txt'):
                        time.sleep(1)
                    inside(restored, 'live-result')
                    runtime.bootstrap(restored, 'stop')
                    runtime.bootstrap(restored, 'start', {'duration': 3})
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline and runtime.present(restored, '/run/attosys/ready'):
                        time.sleep(.5)
                    self.assertFalse(runtime.present(restored, '/run/attosys/ready'), 'new run deadline did not fire')
                else:
                    print('Live continuation not run: supply ATTOBOT_API_KEY to include paid model verification.', flush=True)
        finally:
            for name in names:
                if runtime.info(name) and runtime.info(name)['status']['state'] == 'running':
                    cli('stop', name)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--fixture']:
        fixture(sys.argv[2])
    else:
        unittest.main()
