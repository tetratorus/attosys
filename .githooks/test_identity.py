import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


POLICY = pathlib.Path(__file__).with_name('identity.py')
NAME = 'Leonard Tan'
EMAIL = 'lentan1029@gmail.com'


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='attosys-identity-')
        self.addCleanup(self.temp.cleanup)
        self.directory = pathlib.Path(self.temp.name)
        self.repo = self.directory / 'repo'
        self.remote = self.directory / 'remote.git'
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        self.env.update(HOME=str(self.directory), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        self.ok(self.run_command(['git', 'init', '-q', '-b', 'main', str(self.repo)]))
        self.ok(self.run_command(['git', 'init', '-q', '--bare', str(self.remote)]))
        self.ok(self.policy('install', str(self.repo)))
        self.hooks = self.repo / '.git/hooks'

    def run_command(self, command, *, cwd=None, identity=None, input=None):
        return subprocess.run(command, cwd=cwd, env={**self.env, **(identity or {})}, input=input,
                              text=True, capture_output=True)

    def git(self, *args, **kwargs):
        return self.run_command(['git', '-C', str(self.repo), *args], **kwargs)

    def policy(self, *args, **kwargs):
        return self.run_command([sys.executable, str(POLICY), *args], cwd=self.repo, **kwargs)

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def identity(self, author=EMAIL, committer=EMAIL, name=NAME):
        return {'GIT_AUTHOR_NAME': name, 'GIT_AUTHOR_EMAIL': author,
                'GIT_COMMITTER_NAME': name, 'GIT_COMMITTER_EMAIL': committer}

    def commit(self, message='safe commit', **identity):
        return self.git('commit', '--allow-empty', '-m', message, identity=self.identity(**identity))

    def fixture_commit(self, author=EMAIL, committer=EMAIL, message='fixture'):
        tree = self.ok(self.git('mktree', input=''))
        parent = self.git('rev-parse', '--verify', 'HEAD')
        parents = ['-p', parent.stdout.strip()] if parent.returncode == 0 else []
        sha = self.ok(self.git('commit-tree', tree, *parents, input=message + '\n',
                               identity=self.identity(author=author, committer=committer)))
        self.ok(self.git('update-ref', 'HEAD', sha))
        return sha

    def test_expected_identity_can_commit_and_push(self):
        self.ok(self.commit())
        self.ok(self.git('push', str(self.remote), 'HEAD:refs/heads/main'))
        self.ok(self.policy('check', 'HEAD'))

    def test_wrong_author_email_is_rejected(self):
        result = self.commit(author='someone@work.invalid')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('author', result.stderr)

    def test_wrong_committer_email_is_rejected(self):
        result = self.commit(committer='someone@work.invalid')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('committer', result.stderr)

    def test_wrong_name_with_correct_email_is_rejected(self):
        self.assertNotEqual(self.commit(name='Other Name').returncode, 0)

    def test_explicit_author_override_is_rejected(self):
        result = self.git('commit', '--allow-empty', '--author=Other <other@example.invalid>', '-m', 'unsafe',
                          identity=self.identity())
        self.assertNotEqual(result.returncode, 0)

    def test_commit_helper_overrides_inherited_wrong_identity(self):
        self.ok(self.policy('commit', '--allow-empty', '-m', 'safe helper', identity=self.identity(author='work@example.invalid')))
        self.assertEqual(self.ok(self.git('show', '-s', '--format=%an <%ae>|%cn <%ce>', 'HEAD')),
                         f'{NAME} <{EMAIL}>|{NAME} <{EMAIL}>')

    def test_commit_helper_refuses_hook_bypass_flags(self):
        for flag in ('-n', '--no-verify'):
            result = self.policy('commit', flag, '--allow-empty', '-m', 'unsafe')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('bypass', result.stderr)

    def test_commit_message_rejects_unapproved_attribution(self):
        result = self.commit('change\n\nCo-Authored-By: Other Person <other@example.invalid>')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('trailer', result.stderr)

    def test_push_checks_all_ancestors_on_a_new_branch(self):
        self.fixture_commit(author='old@work.invalid')
        self.ok(self.commit())
        result = self.git('push', str(self.remote), 'HEAD:refs/heads/new-branch')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('author', result.stderr)

    def test_push_rejects_wrong_committer_from_imported_commit(self):
        self.fixture_commit(committer='imported@work.invalid')
        result = self.git('push', str(self.remote), 'HEAD:refs/heads/main')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('committer', result.stderr)

    def test_push_rejects_attribution_in_imported_commit(self):
        self.fixture_commit(message='change\n\nSigned-off-by: Other <other@example.invalid>')
        result = self.git('push', str(self.remote), 'HEAD:refs/heads/main')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('trailer', result.stderr)

    def test_push_rejects_wrong_annotated_tagger(self):
        self.ok(self.commit())
        self.ok(self.git('tag', '-a', 'bad-tag', '-m', 'tag', identity=self.identity(committer='tagger@work.invalid')))
        result = self.git('push', str(self.remote), 'refs/tags/bad-tag')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('tagger', result.stderr)

    def test_push_accepts_safe_annotated_tag(self):
        self.ok(self.commit())
        self.ok(self.git('tag', '-a', 'good-tag', '-m', 'tag', identity=self.identity()))
        self.ok(self.git('push', str(self.remote), 'refs/tags/good-tag'))

    def test_push_rejects_shallow_history(self):
        self.ok(self.commit())
        sha = self.ok(self.git('rev-parse', 'HEAD'))
        (self.repo / '.git/shallow').write_text(sha + '\n')
        result = self.policy('pre-push', input=f'refs/heads/main {sha} refs/heads/main {"0" * 40}\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('shallow', result.stderr)

    def test_branch_deletion_does_not_publish_identity(self):
        self.ok(self.policy('pre-push', input=f'(delete) {"0" * 40} refs/heads/main {"1" * 40}\n'))

    def test_malformed_hook_input_and_unknown_ref_fail_closed(self):
        self.assertNotEqual(self.policy('pre-push', input='invalid input\n').returncode, 0)
        self.assertNotEqual(self.policy('check', 'no-such-ref').returncode, 0)

    def test_install_is_idempotent_and_preserves_unmanaged_hooks(self):
        self.ok(self.policy('install', str(self.repo)))
        before = (self.hooks / 'pre-commit').read_bytes()
        (self.hooks / 'pre-push').write_text('#!/bin/sh\nexit 0\n')
        result = self.policy('install', str(self.repo))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.hooks / 'pre-commit').read_bytes(), before)
        self.assertEqual((self.hooks / 'pre-push').read_text(), '#!/bin/sh\nexit 0\n')

    def test_install_refuses_custom_hooks_path(self):
        custom = self.directory / 'custom-hooks'
        result = self.policy('install', str(self.repo), identity={'GIT_CONFIG_COUNT': '1',
                             'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': str(custom)})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(custom.exists())

    def test_missing_installed_policy_blocks_commit(self):
        (self.hooks / 'attosys-identity.py').unlink()
        self.assertNotEqual(self.commit().returncode, 0)

    def test_installed_hooks_do_not_depend_on_checkout_location(self):
        moved = self.directory / 'moved'
        self.repo.rename(moved)
        self.repo = moved
        self.ok(self.commit())


if __name__ == '__main__':
    unittest.main()
