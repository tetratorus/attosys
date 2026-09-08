import json
import pathlib
import tarfile
import tempfile
import unittest

import snapshot


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name) / 'root'
        files = {'opt/attosys/company.yaml': 'org: atto\nagents:\n  hr: {}\n',
                 'opt/attosys/local-state.json': '{"version":1,"browser_ports":{"atto-hr":9229}}',
                 'opt/attosys/secrets.yaml': 'private test sentinel',
                 'home/atto-hr/agent/memory/lesson.md': 'keep this knowledge',
                 'home/atto-hr/browser/private': 'do not share managed browser profiles',
                 'home/atto-hr/.venv/bin/tool': 'keep installed workspace tools',
                 'home/atto-hr/project/browser/code.js': 'keep browser project code',
                 'var/lib/atto-chat/chat.sqlite3-wal': 'retain committed WAL',
                 'etc/installed-tool.conf': 'belongs in the separate OS snapshot',
                 'run/attosys/llm.env': 'private test sentinel'}
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o600)
        self.archive = pathlib.Path(self.temp.name) / 'data.tar'
        snapshot.pack(self.archive, 'atto', ['atto-hr'], root=self.root)

    def test_separates_data_from_os_and_keeps_workspace_tools(self):
        manifest = snapshot.inspect_archive(self.archive)
        names = set(manifest['files'])
        for name in ('opt/attosys/secrets.yaml', 'home/atto-hr/browser/private', 'run/attosys/llm.env', 'etc/installed-tool.conf'):
            self.assertNotIn(name, names)
        for name in ('home/atto-hr/.venv/bin/tool', 'home/atto-hr/project/browser/code.js', 'var/lib/atto-chat/chat.sqlite3-wal'):
            self.assertIn(name, names)
        self.assertEqual(manifest['files']['home/atto-hr/agent/memory/lesson.md']['mode'], 0o600)

    def test_detects_corrupted_file_contents(self):
        with tarfile.open(self.archive) as archive:
            offset = archive.getmember('home/atto-hr/agent/memory/lesson.md').offset_data
        with self.archive.open('r+b') as file:
            file.seek(offset)
            file.write(b'X')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            snapshot.inspect_archive(self.archive)

    def test_rejects_duplicate_paths(self):
        with tarfile.open(self.archive, 'a') as archive:
            archive.add(self.root / 'home/atto-hr/agent/memory/lesson.md', arcname='home/atto-hr/agent/memory/lesson.md')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            snapshot.inspect_archive(self.archive)

    def test_pair_checksums_bind_os_and_company_data(self):
        directory = pathlib.Path(self.temp.name)
        (directory / 'os.tar').write_bytes(b'OS snapshot fixture')
        manifest = {'format': 'attosys-pair', 'version': 1, 'architecture': 'arm64',
                    'files': {name: snapshot.checksum(directory / name) for name in ('os.tar', 'data.tar')}}
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        self.assertEqual(snapshot.inspect_pair(directory)['architecture'], 'arm64')
        (directory / 'os.tar').write_bytes(b'different OS snapshot')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            snapshot.inspect_pair(directory)

    def test_rejects_paths_outside_the_snapshot(self):
        for path in ('/etc/passwd', '../escape', 'home/../../escape'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                snapshot.normalized(path)


if __name__ == '__main__':
    unittest.main()
