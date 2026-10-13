"""Local fileParams contract rehearsal; only generated, non-sensitive JPEGs."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from scripts import traits as t
from tests.test_plugin import fields

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'plugins/photo-traits-local'
spec = importlib.util.spec_from_file_location('attachment_mock', PACKAGE / 'attachment_mock.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
a.p.t = t
DEFAULT_FILE = object()


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'data/derived').mkdir(parents=True)
        for patcher in (patch.object(t, 'ROOT', self.root), patch.object(t, 'ensure_ignored')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = 100.0
        self.engine = a.MockEngine(self.root / 'data/derived/sessions', clock=lambda: self.clock)
        self.engine.origin = 'http://127.0.0.1:1'
        self.created = a.call_tool(self.engine, 'new_session', {})
        self.session = self.engine.get(self.created['session_id'])
        self.file = self.created['fixture_files'][0]

    def stage(self, file=DEFAULT_FILE, revision=None):
        return a.call_tool(self.engine, 'stage_chatgpt_file', {
            'session_id': self.session.id,
            'revision': self.session.revision if revision is None else revision,
            'file': self.file if file is DEFAULT_FILE else file})

    def plan(self):
        a.call_tool(self.engine, 'plan_fields', {
            'session_id': self.session.id, 'revision': self.session.revision,
            'purpose': 'Artificial fixture QA only.', 'species_scope': 'Synthetic test scope',
            'fields': fields()[:1]})

    def test_descriptor_has_official_four_properties_and_two_required_values(self):
        tool = next(v for v in a.tool_definitions() if v['name'] == 'stage_chatgpt_file')
        self.assertEqual(tool['_meta']['openai/fileParams'], ['file'])
        schema = tool['inputSchema']['properties']['file']
        self.assertEqual(set(schema['properties']), {'download_url', 'file_id', 'mime_type', 'file_name'})
        self.assertEqual(schema['required'], ['download_url', 'file_id'])
        self.assertFalse(tool['annotations']['readOnlyHint'])  # Stages local bytes.
        self.assertFalse(tool['annotations']['openWorldHint'])
        self.assertEqual(len(a.p.tool_definitions()), 6)  # Original manifest unchanged.
        status = a.call_tool(self.engine, 'status', {})
        self.assertFalse(status['remote_downloads_enabled'])
        self.assertFalse(status['real_host_attachment_verified'])
        self.assertEqual(status['mode'], 'mock')

    def test_schema_rejects_missing_extra_typed_and_oversized_values_without_echo(self):
        invalid = [None, [], 'path', {}, {'file_id': self.file['file_id']},
                   {**self.file, 'path': '/private/secret'},
                   {**self.file, 'expires_at': '2099-01-01'},
                   {**self.file, 'file_id': 1}, {**self.file, 'download_url': ''},
                   {**self.file, 'download_url': 'x' * 8193},
                   {**self.file, 'file_name': 'a\x00secret'},
                   {**self.file, 'file_name': 'a' * 257},
                   {**self.file, 'mime_type': 'image/png'}]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(a.p.Invalid) as caught:
                self.stage(value)
            self.assertNotIn('private/secret', str(caught.exception))
        self.assertEqual(self.session.revision, 0)
        self.assertEqual(list(self.session.path.iterdir()), [])

    def test_all_remote_urls_paths_redirect_variants_and_dns_are_denied(self):
        urls = ['file:///etc/passwd', '/tmp/photo.jpg', 'data:image/jpeg;base64,AA',
                'http://127.0.0.1/a', 'http://[::1]/a', 'https://10.0.0.1/a',
                'http://169.254.169.254/latest/meta-data/', 'https://example.com/a.jpg',
                'https://files.oaiusercontent.com/unverified',
                'https://phototraits-fixture.invalid.evil.example/a',
                'https://phototraits-fixture.invalid@evil.example/a',
                self.file['download_url'] + '?redirect=https://example.com',
                self.file['download_url'] + '#fragment', self.file['download_url'] + '/%2e%2e',
                self.file['download_url'] + '圖']
        with patch('socket.getaddrinfo', side_effect=AssertionError('No DNS')) as dns, \
             patch('socket.socket', side_effect=AssertionError('No sockets')) as socket, \
             patch('urllib.request.urlopen', side_effect=AssertionError('No HTTP')) as http:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(a.p.Invalid):
                    self.stage({**self.file, 'download_url': url})
            dns.assert_not_called()
            socket.assert_not_called()
            http.assert_not_called()
        self.assertFalse(self.session.photos)

    def test_session_binding_expiry_single_use_and_cancel_revoke(self):
        other = a.call_tool(self.engine, 'new_session', {})
        with self.assertRaises(a.p.Invalid): self.stage(other['fixture_files'][0])
        self.clock += a.FIXTURE_TTL_SECONDS
        with self.assertRaisesRegex(a.p.Invalid, 'expired'): self.stage()
        self.assertFalse(self.session.photos)
        files = self.engine.files.issue(self.session)
        self.stage(files[0])
        with self.assertRaises(a.p.Invalid): self.stage(files[0])
        self.session.cancel()  # Same path as the existing browser cancel action.
        self.assertFalse(any(k[0] == self.session.id for k in self.engine.files.grants))
        self.plan()  # Editing does not resurrect a revoked reference.
        with self.assertRaises(a.p.Invalid): self.stage(files[1])

    def test_mime_size_signature_and_complete_jpeg_checks(self):
        key = (self.session.id, self.file['file_id'])
        grant = self.engine.files.grants[key]
        valid = copy.deepcopy(grant)
        for data, mime in [(b'x' * (a.p.MAX_BYTES + 1), 'image/jpeg'),
                           (b'\x89PNG', 'image/jpeg'), (a.fixture_jpeg()[:-2], 'image/jpeg'),
                           (a.fixture_jpeg() + b'trailer', 'image/jpeg'),
                           (a.fixture_jpeg(), 'application/octet-stream')]:
            grant.update(data=data, mime=mime)
            with self.assertRaises(a.p.Invalid): self.stage()
        self.engine.files.grants[key] = valid
        self.stage({k: self.file[k] for k in ('download_url', 'file_id')})
        self.assertEqual(self.session.photos[0]['dimensions'], [8, 8])

    def test_metadata_removed_and_host_references_never_persisted_or_echoed(self):
        file = {**self.file, 'file_name': '../../SYNTHETIC_PRIVATE_NAME.jpg'}
        result = self.stage(file)
        image = self.session.image(result['photos'][0]['handle'])
        self.assertNotIn(b'Exif', image)
        self.assertNotIn(b'SYNTHETIC_GPS', image)
        self.assertNotIn(b'ARTIFICIAL_NON_SENSITIVE', image)
        self.assertNotEqual(result['photos'][0]['original_sha256'], result['photos'][0]['sha256'])
        visible = json.dumps(result).encode() + b''.join(path.read_bytes() for path in self.session.path.iterdir())
        for text in (self.file['download_url'], self.file['file_id'], file['file_name']):
            self.assertNotIn(text.encode(), visible)
        self.assertNotIn(b'SYNTHETIC_GPS', visible)

    def test_fields_and_photo_staging_invalidate_confirmation_without_adding_fields(self):
        self.plan()
        self.stage()
        handles = [p['handle'] for p in self.session.photos]
        self.session.confirm(self.session.digest(), handles, False)
        old, revision = self.session.digest(), self.session.revision
        self.stage(self.created['fixture_files'][1])
        self.assertIsNone(self.session.confirmed)
        with self.assertRaises(a.p.Invalid): self.session.execute(old)
        with self.assertRaises(a.p.Invalid): self.stage(self.file, revision)
        self.session.confirm(self.session.digest(), [p['handle'] for p in self.session.photos], False)
        before = self.session.digest()
        changed = fields()[:1]
        changed[0]['definition'] = 'Classify only the central artificial gray square.'
        self.session.edit('Artificial fixture QA only.', 'Synthetic test scope', changed, self.session.revision)
        with self.assertRaises(a.p.Invalid): self.session.execute(before)
        self.assertEqual([q['name'] for q in self.session.state()['questions']], ['surface_mark'])
        self.assertEqual(len(self.session.photos), 2)

    def test_no_mcp_confirmation_budget_live_or_cancel_bypass(self):
        for name in ('confirm', 'enable_live', 'download_url', 'read_file'):
            with self.assertRaises(a.p.Invalid): a.call_tool(self.engine, name, {})
        with self.assertRaises(a.p.Invalid):
            a.call_tool(self.engine, 'stage_chatgpt_file', {'session_id': self.session.id,
                'revision': 0, 'file': self.file, 'confirmed': True})
        with self.assertRaises(TypeError): a.MockEngine(self.root / 'live', budget='5')
        self.plan()
        self.stage()
        with self.assertRaises(a.p.Invalid): self.session.execute(self.session.digest())
        self.session.confirm(self.session.digest(), [p['handle'] for p in self.session.photos], False)
        a.call_tool(self.engine, 'cancel_session', {'session_id': self.session.id})
        with self.assertRaises(a.p.Invalid): self.session.execute(self.session.digest())
        self.engine.mode = 'live'
        with self.assertRaises(a.p.Invalid): a.call_tool(self.engine, 'status', {})

    def test_stdio_advertises_file_params_and_stages_into_the_same_session(self):
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                        'name': 'stage_chatgpt_file', 'arguments': {'session_id': self.session.id,
                        'revision': 0, 'file': self.file}}}]
        inp = io.BytesIO(('\n'.join(json.dumps(r) for r in requests) + '\n').encode())
        out = io.StringIO()
        a.p.stdio(self.engine, inp, out, definitions=a.tool_definitions, dispatch=a.call_tool)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(replies[1]['result']['tools']), 7)
        value = replies[2]['result']['structuredContent']
        self.assertEqual(value['session_id'], self.session.id)
        self.assertFalse(value['confirmed'])
        self.assertEqual(value['mode'], 'mock')
        self.assertEqual(len(value['photos']), 1)

    def test_existing_local_ui_confirmation_mock_review_and_csv_flow(self):
        http = a.p.serve_http(self.engine)
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        self.plan()
        self.stage()
        session = self.session
        def request(action='', obj=None, auth=True, origin=None):
            url = self.engine.origin + '/s/' + session.id + ('/' + action if action else '')
            headers = {'Origin': origin or self.engine.origin}
            if auth: headers['Authorization'] = 'Bearer ' + session.token
            if obj is not None: headers['Content-Type'] = 'application/json'
            data = None if obj is None else json.dumps(obj).encode()
            with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=3) as response:
                return response.read()
        self.assertIn(b'<title>PhotoTraits', request())  # Same HTML; no parallel demo.
        photo = session.photos[0]
        self.assertEqual(request('photo/' + photo['handle']), session.image(photo['handle']))
        for auth, origin in [(False, None), (True, 'https://example.com')]:
            with self.assertRaises(urllib.error.HTTPError): request('state', auth=auth, origin=origin)
        with self.assertRaises(urllib.error.HTTPError):
            request('confirm', {'digest': session.digest(), 'decoded_handles': [], 'upload_approved': False})
        # Explicit synthetic QA acknowledgement; full browser decoding is checked separately.
        request('confirm', {'digest': session.digest(), 'decoded_handles': [photo['handle']], 'upload_approved': False})
        with patch.object(t, 'load_key', side_effect=AssertionError('No key')) as key, \
             patch.object(t, 'post_decisions', side_effect=AssertionError('No model')) as model:
            a.call_tool(self.engine, 'execute_confirmed', {'session_id': session.id, 'plan_sha256': session.digest()})
            session.thread.join(3)
            self.assertFalse(session.thread.is_alive())
            key.assert_not_called()
            model.assert_not_called()
        self.assertEqual(session.status, 'completed')
        request('review', {'handle': photo['handle'], 'values': {'surface_mark': 'unknown'}, 'reviewer': 'fixture-a', 'note': 'Synthetic review.'})
        request('review', {'handle': photo['handle'], 'values': {'surface_mark': 'absent'}, 'reviewer': 'fixture-b', 'note': 'Synthetic disagreement.'})
        self.assertEqual(session.reviews[-1]['status'], 'needs_adjudication')
        self.assertEqual(len(list(session.path.glob('*-human-review.json'))), 2)
        csv = request('export').decode('utf-8-sig')
        self.assertIn('MOCK_NOT_MODEL_OUTPUT', csv)
        self.assertIn('surface_mark', csv)
        self.assertIn('needs_adjudication', csv)
        for absent in ('reflection', 'visible_extent', self.file['download_url'], self.file['file_id'], 'SYNTHETIC_GPS'):
            self.assertNotIn(absent, csv)
        self.assertEqual(self.engine.reserved, 0)

    def test_cli_rejects_live_and_offline_isolated_process_starts(self):
        command = [sys.executable, '-I', '-S', '-B', str(PACKAGE / 'attachment_mock.py'), '--workspace', str(ROOT), '--stdio']
        denied = subprocess.run(command + ['--allow-live'], input='', capture_output=True, text=True, env={}, timeout=5)
        self.assertEqual(denied.returncode, 2)
        handshake = '\n'.join(json.dumps(r) for r in [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'status', 'arguments': {}}}]) + '\n'
        run = subprocess.run(command, input=handshake, capture_output=True, text=True, env={}, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stderr, '')
        status = json.loads(run.stdout.splitlines()[-1])['result']['structuredContent']
        self.assertFalse(status['remote_downloads_enabled'])
        self.assertFalse(status['live_enabled_by_operator'])
        self.assertEqual(status['model'], 'gpt-6-luna')
        self.assertEqual(status['mode'], 'mock')


if __name__ == '__main__':
    unittest.main()
