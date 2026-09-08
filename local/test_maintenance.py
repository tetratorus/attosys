import grp
import pathlib
import pwd
import tarfile
import unittest
from unittest import mock

import snapshot
import up


class MaintenanceTests(unittest.TestCase):
    def test_boot_uses_a_dedicated_target_without_activation_targets(self):
        self.assertIn('--unit=attosys-maintenance.target', snapshot.ENTRYPOINT)
        unit = (pathlib.Path(__file__).parent / 'attosys-maintenance.target').read_text()
        self.assertIn('DefaultDependencies=no', unit)
        self.assertIn('AllowIsolate=yes', unit)
        for target in ('basic.target', 'timers.target', 'sockets.target', 'paths.target', 'sysinit.target'):
            self.assertNotIn(target, unit)
        self.assertIn('COPY attosys-maintenance.target', snapshot.CONTAINERFILE)

    def test_old_basic_target_boot_is_rejected_without_mutation(self):
        runtime = object.__new__(up.Runtime)
        runtime.kind = 'container'
        info = {'configuration': {'initProcess': {'executable': '/lib/systemd/systemd',
                                                 'arguments': ['--system', '--unit=basic.target']}, 'mounts': []}}
        with self.assertRaisesRegex(ValueError, 'maintenance'):
            runtime.supported(info)

    def test_numeric_only_owners_remain_numeric(self):
        member = tarfile.TarInfo('home/atto-hr/numeric')
        member.uid, member.gid = 123456, 654321
        with mock.patch.object(pwd, 'getpwnam', side_effect=KeyError), mock.patch.object(grp, 'getgrnam', side_effect=KeyError):
            self.assertEqual(snapshot.ownership(member), (123456, 654321))

    def test_named_owners_resolve_before_numeric_fallback(self):
        member = tarfile.TarInfo('home/atto-hr/named')
        member.uname, member.gname = 'employee', 'company'
        with mock.patch.object(pwd, 'getpwnam', return_value=mock.Mock(pw_uid=1001)), mock.patch.object(grp, 'getgrnam', return_value=mock.Mock(gr_gid=1002)):
            self.assertEqual(snapshot.ownership(member), (1001, 1002))


if __name__ == '__main__':
    unittest.main()
