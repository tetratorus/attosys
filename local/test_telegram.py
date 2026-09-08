import contextlib
import copy
import io
import json
import pathlib
import runpy
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

import bootstrap
import telegram
import up
import yaml


class TelegramSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.company = {'org': 'atto', 'ceo': {'name': 'CEO', 'telegram_user_id': 1},
                        'agents': {'hr': {'sudo': True}, 'labs': {}}}
        self.options = {'telegram': True, 'telegram_bot_token': '123456:test-token',
                        'telegram_chat_id': '-100123', 'telegram_user_id': 42}
        self.calls = []
        self.results = {'getMe': {'id': 123456, 'username': 'test_bot', 'can_join_groups': True,
                                  'can_read_all_group_messages': True},
                        'getWebhookInfo': {'url': ''},
                        'getChat': {'id': -100123, 'type': 'supergroup', 'is_forum': True},
                        'getChatMember': {'status': 'administrator', 'can_manage_topics': True}}
        self.api = mock.patch.object(telegram, 'call', side_effect=self.call)
        self.api.start()
        self.addCleanup(self.api.stop)

    def call(self, token, method, **data):
        self.calls.append((method, data))
        if method == 'createForumTopic':
            return {'message_thread_id': 100 + len(self.calls)}
        return self.results[method]

    def configure(self, options=None, existing=False):
        return telegram.configure(self.root, self.company, self.options if options is None else options,
                                  bootstrap.write, existing=existing)

    def test_token_only_discovers_group_and_uses_group_owner_as_ceo(self):
        self.results['getUpdates'] = [{'message': {'chat': {'id': -100123, 'type': 'supergroup'}}}]
        self.results['getChatAdministrators'] = [{'status': 'creator', 'user': {'id': 42}}]
        checked = telegram.preflight(self.options['telegram_bot_token'])
        self.assertEqual(checked['telegram_chat_id'], '-100123')
        self.assertEqual(checked['telegram_user_id'], 42)
        self.assertNotIn('createForumTopic', [method for method, _ in self.calls])
        self.assertFalse((self.root / 'company.yaml').exists())
        self.assertEqual(next(data for method, data in self.calls if method == 'getUpdates'), {'timeout': 0})

    def test_discovery_fails_fast_for_no_group_or_ambiguous_groups(self):
        for groups, message in (([], 'no group'), ([-100123, -100456], 'multiple groups')):
            self.results['getUpdates'] = [{'my_chat_member': {'chat': {'id': group, 'type': 'supergroup'}}} for group in groups]
            with self.subTest(groups=groups), self.assertRaisesRegex(ValueError, message):
                telegram.preflight(self.options['telegram_bot_token'])
        self.assertNotIn('createForumTopic', [method for method, _ in self.calls])

    def test_explicit_group_skips_discovery(self):
        telegram.preflight(self.options['telegram_bot_token'], '-100123', 42)
        self.assertNotIn('getUpdates', [method for method, _ in self.calls])

    def test_local_default_needs_no_telegram_and_preserves_local_credentials(self):
        self.configure({})
        self.assertEqual(self.company['telegram_api_base'], telegram.LOCAL_API)
        original = (self.root / 'secrets.yaml').read_bytes()
        self.configure({}, existing=True)
        self.assertEqual((self.root / 'secrets.yaml').read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_new_telegram_company_validates_and_persists_topics_and_private_token(self):
        self.configure()
        saved = yaml.safe_load((self.root / 'company.yaml').read_text())
        self.assertEqual(saved['telegram_chat_id'], '-100123')
        self.assertEqual(saved['ceo']['telegram_user_id'], 42)
        self.assertEqual(saved['telegram_bot_id'], 123456)
        self.assertEqual(saved['telegram_api_base'], telegram.API)
        self.assertTrue(all(spec.get('topic_id') for spec in saved['agents'].values()))
        self.assertTrue(saved['agents']['hr']['sudo'])
        self.assertNotIn('sudo', saved['agents']['labs'])
        self.assertNotIn(self.options['telegram_bot_token'], (self.root / 'company.yaml').read_text())
        secret = self.root / 'secrets.yaml'
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(yaml.safe_load(secret.read_text())['telegram_bot_token'], self.options['telegram_bot_token'])

    def test_restart_reuses_topics_credentials_and_employee_settings(self):
        self.configure()
        original = copy.deepcopy(self.company)
        self.calls.clear()
        self.configure({}, existing=True)
        self.assertEqual(self.company, original)
        self.assertNotIn('createForumTopic', [method for method, _ in self.calls])

    def test_restore_requires_token_then_reuses_saved_topics(self):
        self.configure()
        (self.root / 'secrets.yaml').unlink()
        with self.assertRaisesRegex(ValueError, 'token'):
            self.configure({}, existing=True)
        self.calls.clear()
        self.configure({'telegram_bot_token': self.options['telegram_bot_token']}, existing=True)
        self.assertNotIn('createForumTopic', [method for method, _ in self.calls])

    def test_local_company_cannot_be_silently_switched(self):
        self.configure({})
        original = (self.root / 'secrets.yaml').read_bytes()
        with self.assertRaisesRegex(ValueError, 'new --name'):
            self.configure(existing=True)
        self.assertEqual((self.root / 'secrets.yaml').read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_existing_group_and_ceo_cannot_be_silently_changed(self):
        self.configure()
        for option, value in (('telegram_chat_id', '-100999'), ('telegram_user_id', 99)):
            with self.subTest(option=option), self.assertRaisesRegex(ValueError, 'saved'):
                self.configure({option: value}, existing=True)

    def test_different_bot_is_rejected_before_saved_token_changes(self):
        self.configure()
        original = (self.root / 'secrets.yaml').read_bytes()
        self.results['getMe']['id'] = 999
        with self.assertRaisesRegex(ValueError, 'different bot'):
            self.configure({'telegram_bot_token': '999:replacement'}, existing=True)
        self.assertEqual((self.root / 'secrets.yaml').read_bytes(), original)

    def test_preflight_failure_creates_no_topics_or_saved_configuration(self):
        failures = [('getMe', 'can_read_all_group_messages', False, 'privacy'),
                    ('getMe', 'can_join_groups', False, 'groups'),
                    ('getWebhookInfo', 'url', 'https://example.invalid/hook', 'webhook'),
                    ('getChat', 'is_forum', False, 'Topics'),
                    ('getChatMember', 'can_manage_topics', False, 'Manage Topics')]
        for method, field, value, message in failures:
            with self.subTest(field=field):
                old = self.results[method][field]
                self.results[method][field] = value
                with self.assertRaisesRegex(ValueError, message):
                    self.configure()
                self.results[method][field] = old
                self.assertFalse((self.root / 'company.yaml').exists())
                self.assertFalse((self.root / 'secrets.yaml').exists())
                self.assertNotIn('createForumTopic', [name for name, _ in self.calls])

    def test_partial_topic_creation_is_saved_and_retry_only_creates_missing_topics(self):
        normal = self.call
        def fail_second(token, method, **data):
            if method == 'createForumTopic' and data['name'] == 'atto-labs':
                raise ValueError('injected failure')
            return normal(token, method, **data)
        with mock.patch.object(telegram, 'call', side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, 'injected'):
                self.configure()
        self.company = yaml.safe_load((self.root / 'company.yaml').read_text())
        self.assertIn('topic_id', self.company['agents']['hr'])
        self.assertNotIn('topic_id', self.company['agents']['labs'])
        self.calls.clear()
        self.configure({}, existing=True)
        self.assertEqual([data['name'] for method, data in self.calls if method == 'createForumTopic'], ['atto-labs'])


class TelegramRequestTests(unittest.TestCase):
    def test_transport_errors_do_not_disclose_token_even_in_tracebacks(self):
        token = '123456:private-test-token'
        for error in (urllib.error.URLError('failed /bot' + token),
                      urllib.error.HTTPError('https://example.invalid/bot' + token, 401, 'Unauthorized', {}, None)):
            with mock.patch.object(telegram.urllib.request, 'urlopen', side_effect=error):
                with self.assertRaisesRegex(ValueError, 'Telegram getMe') as caught:
                    telegram.call(token, 'getMe')
            import traceback
            self.assertNotIn(token, ''.join(traceback.format_exception(caught.exception)))


class TelegramLauncherTests(unittest.TestCase):
    def setUp(self):
        self.args = types.SimpleNamespace(name='atto-telegram-test', idle=True, no_build=True,
                                          model='test', duration=3, telegram=True,
                                          telegram_chat_id='-100123', telegram_user_id=42)
        self.runtime = mock.Mock(spec=up.Runtime)
        self.runtime.info.return_value = {'status': {'state': 'stopped'}, 'configuration': {'publishedPorts': [8090]}}
        self.runtime.present.side_effect = lambda name, path: path.endswith('/local/telegram.py')
        self.runtime.chat_config.return_value = {'configured': False, 'telegram': False, 'has_token': False}
        patcher = mock.patch.object(telegram, 'preflight', return_value={'telegram_chat_id': '-100123',
                                   'telegram_user_id': 42, 'telegram_bot_id': 123456, 'telegram_bot_username': 'test_bot'})
        self.preflight = patcher.start()
        self.addCleanup(patcher.stop)

    def test_preflight_failure_happens_before_build_or_container_creation(self):
        self.runtime.info.return_value = None
        self.preflight.side_effect = ValueError('Telegram pre-start check: Topics required')
        with mock.patch.dict(up.os.environ, {'ATTOSYS_TELEGRAM_BOT_TOKEN': '123456:test-token'}), \
             mock.patch.object(up, 'build') as build:
            with self.assertRaisesRegex(ValueError, 'Topics'):
                up.start(self.runtime, self.args)
        build.assert_not_called()
        self.runtime.create.assert_not_called()
        self.runtime.run.assert_not_called()
        self.runtime.bootstrap.assert_not_called()

    def test_launcher_passes_hidden_token_over_stdin_and_does_not_advertise_local_chat(self):
        with mock.patch.dict(up.os.environ, {'ATTOSYS_TELEGRAM_BOT_TOKEN': '123456:test-token'}), \
             mock.patch.object(up.getpass, 'getpass') as prompt, contextlib.redirect_stdout(io.StringIO()) as output:
            up.start(self.runtime, self.args)
        prompt.assert_not_called()
        options = self.runtime.bootstrap.call_args.args[2]
        self.assertEqual(options['telegram_bot_token'], '123456:test-token')
        self.assertEqual(options['telegram_chat_id'], '-100123')
        self.assertNotIn('test-token', output.getvalue())
        self.assertNotIn('Chat: http', output.getvalue())
        self.assertIn('Telegram', output.getvalue())

    def test_restored_telegram_company_prompts_without_needing_flag(self):
        self.args.telegram = False
        self.args.telegram_chat_id = self.args.telegram_user_id = None
        self.runtime.chat_config.return_value = {'configured': True, 'telegram': True, 'has_token': False,
                                                  'telegram_chat_id': '-100123', 'telegram_user_id': 42}
        with mock.patch.dict(up.os.environ, {}, clear=True), \
             mock.patch.object(up.getpass, 'getpass', return_value='123456:test-token') as prompt:
            up.start(self.runtime, self.args)
        prompt.assert_called_once()
        self.assertEqual(self.runtime.bootstrap.call_args.args[2]['telegram_bot_token'], '123456:test-token')

    def test_telegram_requires_current_image(self):
        self.runtime.present.side_effect = None
        self.runtime.present.return_value = False
        with self.assertRaisesRegex(ValueError, 'rebuild'):
            up.start(self.runtime, self.args)
        self.runtime.bootstrap.assert_not_called()

    def test_runtime_bootstrap_keeps_token_out_of_command_arguments(self):
        runtime = object.__new__(up.Runtime)
        runtime.run = mock.Mock()
        runtime.bootstrap('test', 'start', {'telegram_bot_token': '123456:test-token'})
        self.assertNotIn('test-token', repr(runtime.run.call_args.args))
        self.assertIn('test-token', runtime.run.call_args.kwargs['input'])


class MuxTokenSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        self.token = '123456:private-test-token'
        (root / 'company.yaml').write_text(yaml.safe_dump({'org': 'atto', 'telegram_chat_id': '-100123',
                                                          'agents': {'hr': {'topic_id': 1}}}))
        (root / 'secrets.yaml').write_text(yaml.safe_dump({'telegram_bot_token': self.token}))
        with mock.patch.dict(up.os.environ, {'ATTOSYS_ROOT': str(root), 'MUX_DB': str(root / 'mux.db')}):
            self.mux = runpy.run_path(str(up.ROOT / 'mux/mux.py'))
        self.addCleanup(self.mux['db'].close)
        self.mux['refresh_topics']()

    async def test_forward_errors_do_not_disclose_upstream_token(self):
        session = mock.Mock()
        session.post.side_effect = RuntimeError('upstream URL /bot' + self.token)
        request = types.SimpleNamespace(match_info={'agent': 'atto-hr', 'method': 'sendMessage'},
                                         headers={}, method='POST', app={'session': session}, read=mock.AsyncMock(return_value=b''))
        response = await self.mux['handle_forward'](request)
        self.assertEqual(response.status, 502)
        self.assertNotIn(self.token, response.text)

    async def test_getme_and_file_errors_are_sanitized_at_http_boundary(self):
        session = mock.Mock()
        session.get.side_effect = RuntimeError('upstream URL /bot' + self.token)
        request = types.SimpleNamespace(match_info={'agent': 'atto-hr', 'path': 'document'}, app={'session': session})
        for handler in ('handle_get_me', 'handle_file'):
            response = await self.mux['upstream_errors'](request, self.mux[handler])
            self.assertEqual(response.status, 502)
            self.assertNotIn(self.token, response.text)


if __name__ == '__main__':
    unittest.main()
