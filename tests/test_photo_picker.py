"""Synthetic-only picker, loopback OAuth and simulated MCP Apps host acceptance."""
import base64
import copy
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tests import test_workflow as fixture
from tests import test_workflow_http as http_fixture

w, t, h = fixture.w, fixture.t, http_fixture.h


def encoded(data):
    return base64.b64encode(data).decode('ascii')


def padded_jpeg(size):
    """Pad a generated 8x8 JPEG with legal synthetic COM segments, no real data."""
    original = fixture.mock_files.fixture_jpeg()
    remaining = size - len(original)
    segments = []
    while remaining:
        amount = min(65537, remaining)
        if 0 < remaining - amount < 4:
            amount -= 4
        assert amount >= 4
        segments.append(b'\xff\xfe' + (amount - 2).to_bytes(2, 'big') + b'x' * (amount - 4))
        remaining -= amount
    return original[:2] + b''.join(segments) + original[2:]


class PhotoPickerTests(unittest.TestCase):
    call = fixture.WorkflowTests.call
    analyze = fixture.WorkflowTests.analyze
    finish = fixture.WorkflowTests.finish

    def setUp(self):
        fixture.WorkflowTests.setUp(self)
        self.addCleanup(self.engine.close)
        self.s.edit('Synthetic picker acceptance.', 'Single synthetic species', fixture.fields()[:1], 0)
        self.jpeg = fixture.mock_files.fixture_jpeg()

    def attach(self, data=None, revision=None):
        return self.call('attach_photo_bytes', {'revision': self.s.revision if revision is None else revision,
            'jpeg_base64': encoded(self.jpeg if data is None else data)})

    def snapshot(self):
        return copy.deepcopy((self.s.state(), self.s.offer, self.s.last_attachment)), {
            path.name: path.read_bytes() for path in self.s.path.iterdir() if path.is_file()}

    def test_bytes_strip_metadata_without_dns_download_key_or_model(self):
        with patch('socket.getaddrinfo', side_effect=AssertionError('No DNS')) as dns, \
             patch('socket.socket', side_effect=AssertionError('No sockets')) as socket, \
             patch.object(t, 'load_key', side_effect=AssertionError('No key')) as key, \
             patch.object(t, 'post_decisions', side_effect=AssertionError('No model')) as model:
            state = self.call('open_photo_picker', {'revision': self.s.revision})['structuredContent']
            self.assertFalse(state['has_photo'])
            result = self.attach()
        for guard in (dns, socket, key, model, self.reader.check_descriptor, self.reader.download):
            guard.assert_not_called()
        self.assertEqual(set(result['structuredContent']), set(w.PICKER_SCHEMA['required']))
        self.assertLess(len(json.dumps(result)), 1000)
        photo = self.s.photos[0]
        clean = self.s.image(photo['handle'])
        self.assertEqual(photo['original_sha256'], t.sha(self.jpeg))
        self.assertEqual(photo['sha256'], t.sha(clean))
        self.assertEqual(photo['dimensions'], [8, 8])
        self.assertEqual(photo['side'], 'unknown')
        self.assertEqual(photo['individual_label'], '')
        self.assertEqual(photo['observation_label'], '')
        for private in (b'Exif', b'SYNTHETIC_GPS', b'SYNTHETIC_COMMENT', encoded(self.jpeg).encode()):
            for data in self.snapshot()[1].values():
                self.assertNotIn(private, data)
        self.assertIsNone(self.s.confirmed)
        self.assertIsNone(self.s.thread)
        self.assertEqual(self.engine.reserved, 0)

    def test_malformed_noncanonical_or_oversize_base64_preserves_existing_photo(self):
        self.attach()
        self.call('preview_plan')
        before = self.snapshot()
        values = [None, 1, {}, [], '', 'abc', '!!!!', 'éééé', 'AA A', 'AA==\n',
                  'AA===', 'AAAA====', '/x==', 'data:image/jpeg;base64,AA==',
                  'https://fixture.invalid/a', 'A' * (w.MAX_BASE64_CHARS + 4),
                  encoded(b'x' * (w.attachments.MAX_BYTES + 1))]
        for value in values:
            with self.subTest(kind=type(value).__name__, length=len(value) if isinstance(value, str) else None), \
                 self.assertRaises(w.p.Invalid):
                self.call('attach_photo_bytes', {'revision': self.s.revision, 'jpeg_base64': value})
            self.assertEqual(self.snapshot(), before)

    def test_jpeg_validation_and_exact_one_megabyte_limit(self):
        oversized_dimensions = bytearray(self.jpeg)
        frame = oversized_dimensions.index(b'\xff\xc0')
        oversized_dimensions[frame + 5:frame + 7] = (2049).to_bytes(2, 'big')
        for data in (b'\x89PNG\r\n', self.jpeg[:-2], self.jpeg + b'trailing',
                     b'\xff\xd8\xff\xd9', bytes(oversized_dimensions)):
            with self.subTest(size=len(data)), self.assertRaises((w.p.Invalid, t.SafeError)):
                self.attach(data)
            self.assertFalse(self.s.staging)
            self.assertFalse(list(self.s.path.glob('*.jpg')))
        maximum = padded_jpeg(w.attachments.MAX_BYTES)
        self.assertEqual(len(maximum), 1_000_000)
        self.assertEqual(len(encoded(maximum)), w.MAX_BASE64_CHARS)
        self.attach(maximum)
        self.assertEqual(self.s.image(self.s.photos[0]['handle']), w.p.strip_metadata(self.jpeg)[0])
        with self.assertRaises(w.p.Invalid):
            self.attach(padded_jpeg(1_000_001))

    def test_owner_scope_mode_plan_and_revision_are_checked_before_decode(self):
        args = {'session_id': self.s.id, 'revision': self.s.revision, 'jpeg_base64': encoded(self.jpeg)}
        with patch.object(w, 'decode_photo_bytes', side_effect=AssertionError('Must not decode')) as decoder:
            for principal in (w.Principal('stranger', frozenset(w.SCOPES)),
                              w.Principal(self.principal.subject, frozenset({'results:read'})),
                              w.Principal(self.principal.subject, frozenset({'photo:analyze'}))):
                for tool in ('attach_photo_bytes', 'open_photo_picker'):
                    tool_args = args if tool == 'attach_photo_bytes' else {k: v for k, v in args.items() if k != 'jpeg_base64'}
                    with self.assertRaises(w.p.Invalid):
                        w.call_tool(self.engine, principal, tool, tool_args)
            for revision in (0, True, '1', 1.0, None):
                with self.assertRaises(w.p.Invalid):
                    self.attach(revision=revision if revision is not None else -1)
            with patch.object(self.engine, 'mode', 'live'), self.assertRaises(w.p.Invalid):
                self.attach()
            with patch.object(self.s, 'fields', []), self.assertRaises(w.p.Invalid):
                self.attach()
            for extra in ({'subject': self.principal.subject}, {'file_name': 'private.jpg'},
                          {'download_url': 'https://fixture.invalid/a'}, {'path': '/tmp/a'}):
                with self.assertRaises(w.p.Invalid):
                    w.call_tool(self.engine, self.principal, 'attach_photo_bytes', {**args, **extra})
            decoder.assert_not_called()

    def test_duplicate_and_replacement_keep_retention_and_invalidate_confirmation(self):
        self.attach()
        original = copy.deepcopy(self.s.photos[0])
        revision, expires = self.s.revision, self.s.expires
        self.attach()
        self.assertEqual(self.s.revision, revision)
        self.assertEqual(self.s.photos[0], original)
        offer = self.call('preview_plan')['structuredContent']
        self.attach(fixture.mock_files.fixture_jpeg(True))
        self.assertEqual(self.s.revision, revision + 1)
        self.assertEqual(self.s.expires, expires)
        self.assertIsNone(self.s.offer)
        self.assertFalse((self.s.path / (original['handle'] + '.jpg')).exists())
        self.assertEqual(len(list(self.s.path.glob('*.jpg'))), 1)
        with self.assertRaises(w.p.Invalid): self.analyze(offer)
        with self.assertRaises(w.p.Invalid): self.attach(revision=revision)

    def test_same_photo_across_existing_attachment_and_bytes_ingress_is_idempotent(self):
        self.call('attach_photo', {'revision': self.s.revision, 'file': self.file})
        original, revision = copy.deepcopy(self.s.photos), self.s.revision
        self.attach()
        self.assertEqual(self.s.photos, original)
        self.assertEqual(self.s.revision, revision)
        self.reader.download.assert_called_once()

    def test_concurrent_replacement_cannot_supply_another_uploads_reply_revision(self):
        for prior_ingress in ('none', 'bytes', 'descriptor'):
            with self.subTest(prior_ingress=prior_ingress):
                self.engine.clear(self.s)
                self.s = self.engine.new(self.principal)
                self.s.edit('Synthetic concurrent uploads.', 'Synthetic species', fixture.fields()[:1], 0)
                if prior_ingress == 'bytes':
                    self.attach()
                elif prior_ingress == 'descriptor':
                    self.call('attach_photo', {'revision': self.s.revision, 'file': self.file})
                # Exercise all success paths: new write, same bytes fingerprint,
                # and the same photo previously staged via a URL descriptor.
                staged, release_reply = threading.Event(), threading.Event()
                original_attach = self.s.attach_bytes
                outcome = {}

                def pause_after_stage(value, revision):
                    committed_revision = original_attach(value, revision)
                    if threading.current_thread() is first:
                        staged.set()
                        if not release_reply.wait(5):
                            raise AssertionError('Concurrent test did not release the first reply.')
                    return committed_revision

                def upload_first():
                    try:
                        outcome['result'] = self.attach()
                    except Exception as error:
                        outcome['error'] = error

                first = threading.Thread(target=upload_first, daemon=True)
                with patch.object(self.s, 'attach_bytes', side_effect=pause_after_stage):
                    first.start()
                    try:
                        self.assertTrue(staged.wait(5), 'The first upload did not finish staging.')
                        first_revision = self.s.revision
                        second = self.attach(fixture.mock_files.fixture_jpeg(True))['structuredContent']
                        self.assertEqual(second['revision'], first_revision + 1)
                    finally:
                        release_reply.set()
                        first.join(5)
                self.assertFalse(first.is_alive())
                self.assertNotIn('result', outcome, 'The first upload must not acknowledge the second revision.')
                self.assertIsInstance(outcome.get('error'), w.p.Invalid)
                self.assertEqual(str(outcome['error']), 'Plan changed; reload before editing or confirming.')
                self.assertEqual(self.s.revision, second['revision'])
                self.assertEqual(self.s.photos[0]['original_sha256'], t.sha(fixture.mock_files.fixture_jpeg(True)))
                self.assertEqual(len(list(self.s.path.glob('*.jpg'))), 1)
                self.assertFalse(self.s.staging)
                self.assertIsNone(self.s.thread)
                self.assertEqual(self.engine.reserved, 0)

    def test_cancel_expiry_clear_or_plan_change_during_validation_prevents_writes(self):
        original_strip = w.p.strip_metadata
        for change in ('cancel', 'expire', 'clear', 'revision'):
            self.s = self.engine.new(self.principal)
            self.s.edit('Synthetic race.', 'Synthetic species', fixture.fields()[:1], 0)
            def interrupted(data):
                if change == 'cancel': self.s.cancel()
                elif change == 'expire': self.now = self.s.expires
                elif change == 'clear': self.engine.clear(self.s)
                else: self.s.revision += 1
                return original_strip(data)
            with self.subTest(change=change), patch.object(w.p, 'strip_metadata', side_effect=interrupted), \
                 self.assertRaises(w.p.Invalid):
                self.attach()
            self.assertFalse(list(self.s.path.glob('*.jpg')))
            self.assertFalse(self.s.staging)
            self.assertEqual(self.engine.reserved, 0)
            if change in ('expire', 'clear'):
                self.assertFalse(self.s.path.exists())
            self.engine.clear(self.s)

    def test_new_ingress_retains_preview_human_confirmation_and_post_attempt_cleanup(self):
        self.attach()
        with self.assertRaises(w.p.Invalid): self.s.execute(self.s.digest())
        offer = self.call('preview_plan')['structuredContent']
        with self.assertRaises(w.p.Invalid):
            self.s.analyze(self.s.digest(), offer['confirmation_id'], False)
        with patch.object(t, 'load_key', side_effect=AssertionError('No key')), \
             patch.object(t, 'post_decisions', side_effect=AssertionError('No model')):
            self.analyze(offer); self.finish()
        self.assertEqual(self.s.status, 'completed')
        self.assertFalse(list(self.s.path.glob('*.jpg')))
        self.call('review_values', {'plan_sha256': self.s.digest(), 'values': {'surface_mark': 'absent'}})
        self.call('export_csv')
        self.assertEqual(self.s.expires, self.now + w.EXPORT_TTL)
        self.now = self.s.expires
        self.engine.cleanup()
        self.assertFalse(self.s.path.exists())

    def test_four_exact_download_hosts_unchanged_and_new_region_fails_before_dns(self):
        hosts = {'sdmntprjapaneast.oaiusercontent.com', 'sdmntprcentralus.oaiusercontent.com',
                 'sdmntprukwest.oaiusercontent.com', 'sdmntprwestus3.oaiusercontent.com'}
        resolver, connection = Mock(), Mock()
        reader = w.attachments.SafeHTTPS(hosts, resolver=resolver, connection=connection)
        for host in hosts:
            reader.check_descriptor({**self.file, 'download_url': 'https://' + host + '/synthetic'})
        with self.assertRaises(w.attachments.HostNotAllowed):
            reader.check_descriptor({**self.file, 'download_url': 'https://sdmntprwestus2.oaiusercontent.com/synthetic'})
        self.engine.reader = reader
        self.attach()
        self.assertEqual(reader.hosts, frozenset(hosts))
        resolver.assert_not_called(); connection.assert_not_called()


@unittest.skipUnless(http_fixture.jwt is not None, 'Requires the existing HTTP OAuth test extras.')
class PhotoPickerHTTPTests(http_fixture.OAuthFixture, unittest.TestCase):
    request = http_fixture.HTTPAcceptanceTests.request
    tool = http_fixture.HTTPAcceptanceTests.tool
    stop_server = http_fixture.HTTPAcceptanceTests.stop_server

    def setUp(self):
        http_fixture.OAuthFixture.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        (root / 'data/derived').mkdir(parents=True)
        for patcher in (patch.object(t, 'ROOT', root), patch.object(t, 'ensure_ignored')):
            patcher.start(); self.addCleanup(patcher.stop)
        self.reader = Mock()
        self.reader.download.side_effect = AssertionError('No remote download')
        self.reader.check_descriptor.side_effect = AssertionError('No attachment descriptor')
        self.engine = h.w.WorkflowEngine(root / 'data/derived/picker', reader=self.reader)
        self.addCleanup(self.engine.close)
        self.server = h.make_server(self.engine, self.verifier)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start(); self.addCleanup(self.stop_server)
        self.counter = 0
        self.sid = self.tool('new_session', {})['structuredContent']['session_id']
        self.tool('plan_fields', {'session_id': self.sid, 'revision': 0,
            'purpose': 'Synthetic picker transport.', 'species_scope': 'Synthetic single species', 'fields': fixture.fields()[:1]})

    def rpc(self, method, params, **kwargs):
        return self.request('POST', '/mcp', {'jsonrpc': '2.0', 'id': 99, 'method': method, 'params': params}, **kwargs)

    def test_static_resource_and_app_visibility_require_oauth_scopes(self):
        status, _, reply = self.rpc('initialize', {'protocolVersion': h.PROTOCOLS[0]})
        self.assertEqual(status, 200)
        self.assertIn('io.modelcontextprotocol/ui', reply['result']['capabilities']['extensions'])
        status, _, reply = self.rpc('tools/list', {})
        tools = {item['name']: item for item in reply['result']['tools']}
        picker, upload = tools['open_photo_picker'], tools['attach_photo_bytes']
        self.assertEqual(picker['_meta']['ui'], {'resourceUri': h.w.PICKER_URI})
        self.assertEqual(upload['_meta']['ui'], {'visibility': ['app']})
        self.assertFalse(upload['annotations']['readOnlyHint'])
        self.assertFalse(upload['annotations']['openWorldHint'])
        self.assertTrue(upload['annotations']['destructiveHint'])
        self.assertNotIn('resourceUri', upload['_meta']['ui'])
        for method, params in (('resources/list', {}), ('resources/read', {'uri': h.w.PICKER_URI})):
            self.assertEqual(self.rpc(method, params, token=False)[0], 401)
            self.assertEqual(self.rpc(method, params, changes={'scope': 'results:read'})[0], 403)
            self.assertEqual(self.rpc(method, params, changes={'sub': 'other'})[0], 401)
            self.assertEqual(self.rpc(method, params)[0], 200)
        _, _, reply = self.rpc('resources/read', {'uri': h.w.PICKER_URI})
        resource = reply['result']['contents'][0]
        self.assertEqual(resource['mimeType'], h.w.PICKER_MIME)
        self.assertEqual(resource['_meta']['ui']['csp'], {'connectDomains': [], 'resourceDomains': [], 'frameDomains': []})
        self.assertIn('type="file"', resource['text'])
        self.assertNotIn(self.sid, resource['text'])
        for uri in ('file:///etc/passwd', '../workflow.py', h.w.PICKER_URI + '?x=1'):
            self.assertEqual(self.rpc('resources/read', {'uri': uri})[0], 400)

    def test_resource_request_metadata_is_optional_and_never_reflected(self):
        metadata = {'progressToken': 'PRIVATE_RESOURCE_META', 'openai/locale': 'fixture',
                    'opaque': {'session_id': self.sid, 'photo': 'PRIVATE_RESOURCE_META'}}
        for method, params in (('resources/list', {}), ('resources/read', {'uri': h.w.PICKER_URI})):
            baseline = self.rpc(method, params)[2]
            for meta in ({}, metadata):
                with self.subTest(method=method, empty=not meta):
                    status, _, body = self.rpc(method, {**params, '_meta': meta})
                    self.assertEqual(status, 200)
                    self.assertEqual(body, baseline)
                    self.assertNotIn('PRIVATE_RESOURCE_META', json.dumps(body))
        self.reader.download.assert_not_called()
        self.reader.check_descriptor.assert_not_called()

    def test_resource_metadata_preserves_schema_uri_identity_and_scope_guards(self):
        for method, base in (('resources/list', {}), ('resources/read', {'uri': h.w.PICKER_URI})):
            for meta in (None, [], 'PRIVATE_RESOURCE_META', 1, True):
                status, _, body = self.rpc(method, {**base, '_meta': meta})
                self.assertEqual(status, 400)
                self.assertEqual(body['error']['code'], -32602)
                self.assertNotIn('PRIVATE_RESOURCE_META', json.dumps(body))
            self.assertEqual(self.rpc(method, {**base, '_meta': {}, 'extra': 'PRIVATE_EXTRA'})[0], 400)
            params = {**base, '_meta': {'sub': self.claims['sub'], 'scope': self.claims['scope']}}
            for options, expected in (({'token': False}, 401), ({'changes': {'sub': 'other'}}, 401),
                                       ({'changes': {'scope': 'results:read'}}, 403)):
                self.assertEqual(self.rpc(method, params, **options)[0], expected)
        for params in ({'_meta': {'uri': h.w.PICKER_URI}}, {'uri': None, '_meta': {}},
                       {'uri': 'https://private.invalid/PRIVATE_SIGNED_URL', '_meta': {}},
                       {'uri': h.w.PICKER_URI + '?PRIVATE_QUERY', '_meta': {}}):
            status, _, body = self.rpc('resources/read', params)
            self.assertEqual(status, 400)
            self.assertNotIn('PRIVATE_', json.dumps(body))
        self.assertEqual(self.rpc('resources/list', {'uri': h.w.PICKER_URI, '_meta': {}})[0], 400)

    def test_resource_metadata_keeps_original_body_limit(self):
        for method, base in (('resources/list', {}), ('resources/read', {'uri': h.w.PICKER_URI})):
            status, _, body = self.rpc(method, {**base, '_meta': {'opaque': 'x' * h.MAX_BODY}})
            self.assertEqual(status, 400)
            self.assertEqual(body['error']['message'], 'Request too large.')

    def test_maximum_file_roundtrip_and_all_other_requests_keep_old_body_limit(self):
        data = padded_jpeg(1_000_000)
        args = {'session_id': self.sid, 'revision': 1, 'jpeg_base64': encoded(data)}
        params = {'name': 'attach_photo_bytes', 'arguments': args}
        self.assertGreater(len(json.dumps(params)), h.MAX_BODY)
        self.assertLess(len(json.dumps(params)) + 100, h.MAX_UPLOAD_BODY)
        for options, expected in (({'token': False}, 401),
                                   ({'changes': {'sub': 'other'}}, 401),
                                   ({'changes': {'scope': 'results:read'}}, 403),
                                   ({'changes': {'exp': int(time.time()) - 30}}, 401)):
            if expected == 403:
                self.assertEqual(self.rpc('tools/call', params, **options)[0], expected)
                continue
            # Invalid authorization must be rejected before reading the announced
            # large body. Sending it anyway can race the server's early close.
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
            try:
                conn.putrequest('POST', '/mcp', skip_host=True)
                conn.putheader('Host', 'phototraits.example')
                conn.putheader('Content-Type', 'application/json')
                conn.putheader('Content-Length', str(h.MAX_UPLOAD_BODY))
                if options.get('token', True):
                    conn.putheader('Authorization', 'Bearer ' + self.token(options.get('changes')))
                conn.endheaders()
                response = conn.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
            finally:
                conn.close()
        result = self.tool('attach_photo_bytes', args)
        self.assertTrue(result['structuredContent']['has_photo'])
        self.assertLess(len(json.dumps(result)), 1000)
        session = self.engine.sessions[self.sid]
        self.assertEqual(session.photos[0]['original_sha256'], t.sha(data))
        self.reader.download.assert_not_called(); self.reader.check_descriptor.assert_not_called()
        for method, params in (('ping', {'padding': 'x' * h.MAX_BODY}),
                               ('resources/read', {'uri': h.w.PICKER_URI, 'padding': 'x' * h.MAX_BODY}),
                               ('tools/call', {'name': 'attach_photo', '_meta': {'padding': 'x' * h.MAX_BODY}}),
                               ('tools/call', {'name': 'new_session', '_meta': {'padding': 'x' * h.MAX_BODY}})):
            self.assertEqual(self.rpc(method, params)[0], 400)
        principal = self.verifier.verify(self.token())
        self.assertEqual(h.dispatch(self.engine, principal, b' ' * (h.MAX_UPLOAD_BODY + 1))[0], 400)

    def test_upload_http_framing_and_invalid_bytes_fail_closed(self):
        params = {'name': 'attach_photo_bytes', 'arguments': {'session_id': self.sid, 'revision': 1, 'jpeg_base64': '!!!!'}}
        status, _, reply = self.rpc('tools/call', params)
        self.assertEqual(status, 200)
        self.assertTrue(reply['result']['isError'])
        for name, value in (('Content-Length', str(h.MAX_UPLOAD_BODY + 1)),
                            ('Content-Encoding', 'gzip'), ('Transfer-Encoding', 'chunked'),
                            ('Content-Type', 'multipart/form-data')):
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
            conn.putrequest('POST', '/mcp', skip_host=True)
            conn.putheader('Host', 'phototraits.example')
            conn.putheader('Authorization', 'Bearer ' + self.token())
            conn.putheader('Content-Type', value if name == 'Content-Type' else 'application/json')
            conn.putheader('Content-Length', value if name == 'Content-Length' else '1')
            if name not in ('Content-Type', 'Content-Length'): conn.putheader(name, value)
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read(); conn.close()
        self.assertFalse(self.engine.sessions[self.sid].photos)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed only for the simulated UI bridge test.')
    def test_actual_ui_script_through_simulated_host_and_real_loopback_mcp(self):
        picker = self.tool('open_photo_picker', {'session_id': self.sid, 'revision': 1})
        _, _, resource = self.rpc('resources/read', {'uri': h.w.PICKER_URI})
        # Ephemeral synthetic token exists only in child stdin/memory, never argv,
        # files, browser state, logs or the production HTML/script sandbox.
        config = {'port': self.server.server_port, 'token': self.token(), 'picker': picker,
                  'html': resource['result']['contents'][0]['text'], 'jpeg': encoded(padded_jpeg(1_000_000))}
        with patch.object(t, 'load_key', side_effect=AssertionError('No model key')), \
             patch.object(t, 'post_decisions', side_effect=AssertionError('No model API')):
            result = subprocess.run([shutil.which('node'), str(Path(__file__).with_name('photo_picker_bridge.mjs'))],
                                    input=json.dumps(config), text=True, capture_output=True, timeout=30)
        # Never reflect the token or fixture bytes even when a harness fails.
        self.assertNotIn(config['token'], result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        report = json.loads(result.stdout)
        self.assertEqual(report['evidence_kind'], 'SIMULATED_MCP_APPS_BRIDGE')
        self.assertEqual(report['jpeg_bytes'], 1_000_000)
        self.assertEqual(report['mcp_uploads'], 1)
        self.assertGreater(report['rpc_bytes'], h.MAX_BODY)
        self.assertLessEqual(report['rpc_bytes'], h.MAX_UPLOAD_BODY)
        print('Picker bridge: ' + json.dumps(report, sort_keys=True))
        session = self.engine.sessions[self.sid]
        self.assertEqual(session.photos[0]['original_sha256'], t.sha(base64.b64decode(config['jpeg'])))
        self.assertIsNone(session.thread)
        self.reader.download.assert_not_called(); self.reader.check_descriptor.assert_not_called()
