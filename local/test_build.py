import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import up


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = pathlib.Path(self.temp.name)
        for repo in up.SOURCES:
            (self.source / repo).mkdir()
        self.root = self.source / 'attosys'
        (self.root / 'local').mkdir()
        for name in ('Containerfile', 'constraints.txt', 'requirements.txt'):
            (self.root / 'local' / name).write_bytes((up.ROOT / 'local' / name).read_bytes())
        (self.source / 'attobot/agent.py').write_text('')
        (self.source / 'attobot/requirements.txt').write_text('requests\n')
        self.args = SimpleNamespace(source_root=self.source, dns=None)
        self.runtime = mock.Mock(kind='container')

    def test_build_packages_dependency_pins_with_company_sources(self):
        def build(*args, **kwargs):
            context = pathlib.Path(kwargs['cwd'])
            constraints = context / 'attosys/local/constraints.txt'
            self.assertEqual(constraints.read_bytes(), (self.root / 'local/constraints.txt').read_bytes())
            self.assertTrue((context / 'attobot/agent.py').is_file())
            recipe = (context / 'Containerfile').read_text()
            self.assertIn('-c /opt/attosys/local/constraints.txt', recipe)
            self.assertIn('-r /opt/attosys/local/requirements.txt', recipe)
        self.runtime.run.side_effect = build
        with mock.patch.object(up, 'ROOT', self.root):
            up.build(self.runtime, self.args)
        self.runtime.run.assert_called_once()

    def test_build_refuses_missing_dependency_pins(self):
        (self.root / 'local/constraints.txt').unlink()
        with mock.patch.object(up, 'ROOT', self.root), self.assertRaisesRegex(ValueError, 'dependency constraints'):
            up.build(self.runtime, self.args)
        self.runtime.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
