import json
import os
import pathlib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_for(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (requests.RequestException, sqlite3.OperationalError):
            pass
        time.sleep(.05)
    raise AssertionError('condition not reached')


class MuxPersistence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.chat_port, self.mux_port = port(), port()
        self.chat = f'http://127.0.0.1:{self.chat_port}'
        self.mux = f'http://127.0.0.1:{self.mux_port}/botatto-worker/getUpdates'
        company = {'org': 'atto', 'name': 'Test company', 'telegram_chat_id': '-1001',
                   'telegram_api_base': self.chat, 'agents': {'worker': {'topic_id': 1}, 'other': {'topic_id': 2}}}
        (self.root / 'company.yaml').write_text(yaml.safe_dump(company))
        (self.root / 'secrets.yaml').write_text(yaml.safe_dump({'telegram_bot_token': 'local-test-token'}))
        self.env = {**os.environ, 'ATTOSYS_ROOT': str(self.root), 'CHAT_STATE': str(self.root / 'chat'),
                    'CHAT_PORT': str(self.chat_port), 'MUX_PORT': str(self.mux_port), 'MUX_DB': str(self.root / 'mux.sqlite3')}
        self.chat_process = self.start(ROOT / 'local/chat.py')
        self.addCleanup(self.stop, self.chat_process)
        wait_for(lambda: requests.get(self.chat, timeout=1).ok)
        self.mux_process = self.start(ROOT / 'mux/mux.py')
        self.addCleanup(lambda: self.stop(self.mux_process))
        wait_for(lambda: requests.post(self.mux, data={'offset': 0}, timeout=1).ok)

    def start(self, path):
        return subprocess.Popen([sys.executable, str(path)], env=self.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def test_local_chat_replies_and_attachments_use_telegram_protocol(self):
        headers = {'X-Attobot-Lab': '1'}
        payload = b'local attachment round trip\n'
        response = requests.post(self.chat + '/api', headers=headers,
                                 data={'text': 'read this attachment', 'message_thread_id': 1},
                                 files={'file': ('note.txt', payload, 'text/plain')}, timeout=2)
        response.raise_for_status()
        incoming = response.json()['result']
        updates = wait_for(lambda: requests.post(self.mux, data={'offset': 0}, timeout=2).json()['result'])
        self.assertEqual(updates[0]['message']['message_id'], incoming['message_id'])
        bot = self.mux.removesuffix('/getUpdates')
        result = requests.post(bot + '/getFile', data={'file_id': incoming['document']['file_id']}, timeout=2).json()
        download = f"http://127.0.0.1:{self.mux_port}/file/botatto-worker/{result['result']['file_path']}"
        self.assertEqual(requests.get(download, timeout=2).content, payload)
        reply = requests.post(bot + '/sendDocument', data={'chat_id': '-1001', 'message_thread_id': 1, 'caption': 'attachment received'},
                              files={'document': ('reply.txt', payload, 'text/plain')}, timeout=2)
        reply.raise_for_status()
        self.assertTrue(reply.json()['ok'])
        state = requests.get(self.chat + '/api', headers=headers, timeout=2).json()
        self.assertEqual(state['company'], 'Test company')
        self.assertIn({'name': 'atto-worker', 'topic': 1}, state['agents'])
        outgoing = state['messages'][-1]
        self.assertEqual(outgoing['direction'], 'out')
        self.assertEqual(outgoing['message_thread_id'], 1)
        self.assertEqual(outgoing['caption'], 'attachment received')
        self.assertEqual(requests.get(self.chat + '/files/' + outgoing['document']['file_id'], timeout=2).content, payload)

    def test_chat_renders_markdown_without_raw_html_or_unsafe_links(self):
        headers = {'X-Attobot-Lab': '1'}
        text = '# Heading\n\n**bold** and *italic*\n\n- item\n\n```python\nprint("hello")\n```\n\n[link](https://example.invalid)\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n![tracker](https://example.invalid/image.png)'
        requests.post(self.chat + '/api', headers=headers, json={'text': text, 'message_thread_id': 1}, timeout=2).raise_for_status()
        item = requests.get(self.chat + '/api', headers=headers, timeout=2).json()['messages'][-1]
        self.assertEqual(item['text'], text)
        html = item['html']
        for expected in ('<h1>Heading</h1>', '<strong>bold</strong>', '<em>italic</em>', '<ul>', '<pre><code class="language-python">', 'href="https://example.invalid"'):
            self.assertIn(expected, html)
        for forbidden in ('<script', 'href="javascript:', '<img'):
            self.assertNotIn(forbidden, html)

    def test_chat_renders_attachment_captions_as_markdown(self):
        headers = {'X-Attobot-Lab': '1'}
        requests.post(self.chat + '/api', headers=headers, data={'text': '**caption**', 'message_thread_id': 1},
                      files={'file': ('note.txt', b'attachment', 'text/plain')}, timeout=2).raise_for_status()
        item = requests.get(self.chat + '/api', headers=headers, timeout=2).json()['messages'][-1]
        self.assertIn('<strong>caption</strong>', item['html'])
        self.assertEqual(item['document']['file_name'], 'note.txt')

    def test_local_operator_rejects_cross_origin_and_missing_header(self):
        self.assertEqual(requests.get(self.chat + '/api', timeout=2).status_code, 403)
        self.assertEqual(requests.post(self.chat + '/api', headers={'X-Attobot-Lab': '1', 'Origin': 'https://example.invalid'},
                                      json={'text': 'not allowed', 'message_thread_id': 1}, timeout=2).status_code, 403)

    def test_upstream_acknowledged_message_survives_mux_restart(self):
        response = requests.post(self.chat + '/api', headers={'X-Attobot-Lab': '1'},
                                 json={'text': 'persist this delivery', 'message_thread_id': 1}, timeout=2)
        response.raise_for_status()
        message_id = response.json()['result']['message_id']
        def acknowledged():
            with sqlite3.connect(self.root / 'chat/chat.sqlite3') as db:
                return db.execute('SELECT offset FROM cursor').fetchone()[0] > message_id
        wait_for(acknowledged)
        self.stop(self.mux_process)
        self.mux_process = self.start(ROOT / 'mux/mux.py')
        wait_for(lambda: requests.post(self.mux, data={'offset': 0}, timeout=1).ok)
        updates = requests.post(self.mux, data={'offset': 0}, timeout=2).json()['result']
        self.assertEqual([item['update_id'] for item in updates], [message_id])
        self.assertEqual(requests.post(self.mux, data={'offset': 0}, timeout=2).json()['result'], updates)
        other = self.mux.replace('atto-worker', 'atto-other')
        self.assertEqual(requests.post(other, data={'offset': message_id + 1}, timeout=2).json()['result'], [])
        self.assertEqual(requests.post(self.mux, data={'offset': 0}, timeout=2).json()['result'], updates)
        self.assertEqual(requests.post(self.mux, data={'offset': message_id + 1}, timeout=2).json()['result'], [])
        self.stop(self.mux_process)
        self.mux_process = self.start(ROOT / 'mux/mux.py')
        wait_for(lambda: requests.post(self.mux, data={'offset': 0}, timeout=1).ok)
        self.assertEqual(requests.post(self.mux, data={'offset': 0}, timeout=2).json()['result'], [])


if __name__ == '__main__':
    unittest.main()
