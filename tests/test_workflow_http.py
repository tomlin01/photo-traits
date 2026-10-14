"""OAuth/MCP acceptance with ephemeral RSA fixtures and loopback HTTP only."""
import copy
import csv
import http.client
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from scripts import traits as t
from tests.test_attachments import a as mock_files
from tests.test_plugin import fields

spec = importlib.util.spec_from_file_location('workflow_http', Path(__file__).resolve().parents[1] / 'plugins/photo-traits-local/workflow_http.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
h.w.p.t = t
try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    jwt = None


class OAuthFixture:
    @classmethod
    def setUpClass(cls):
        cls.signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        # Synthetic test signing material only; never exported, uploaded or persisted.
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.signer.public_key()))
        cls.jwk.update(kid='fixture-key', use='sig', alg='RS256')

    def setUp(self):
        self.clock = 100.0
        self.fetch = Mock(return_value=json.dumps({'keys': [self.jwk]}).encode())
        self.verifier = h.TokenVerifier(issuer='https://fixture.auth.example/', resource='https://phototraits.example/mcp',
                                       allowed_subject='auth0|synthetic-owner', fetch_jwks=self.fetch, clock=lambda: self.clock)
        self.claims = {'iss': self.verifier.issuer, 'aud': self.verifier.resource, 'sub': self.verifier.allowed_subject,
                       'exp': int(time.time())+300, 'iat': int(time.time())-1, 'scope': 'photo:analyze results:read'}

    def token(self, changes=None, headers=None, signer=None):
        return jwt.encode({**self.claims, **(changes or {})}, signer or self.signer, algorithm='RS256',
                          headers={'kid': 'fixture-key', **(headers or {})})


@unittest.skipUnless(jwt is not None, 'HTTP OAuth extras absent; run the consolidated acceptance entry point with requirements-http.txt.')
class OAuthTests(OAuthFixture, unittest.TestCase):
    def test_signature_issuer_audience_expiry_subject_and_scope(self):
        principal = self.verifier.verify(self.token())
        self.assertEqual(principal.subject, self.claims['sub'])
        self.assertEqual(principal.scopes, frozenset(h.w.SCOPES))
        bad = [self.token(signer=self.other_signer), self.token({'iss': 'https://wrong.example/'}),
               self.token({'aud': 'https://wrong.example/mcp'}), self.token({'exp': int(time.time())-1}),
               self.token({'iat': int(time.time())+300}), self.token({'nbf': int(time.time())+300}),
               self.token({'sub': 'auth0|other-user'}), self.token({'scope': ['photo:analyze']}),
               self.token({'exp': True}), self.token(headers={'jku': 'https://private.example/SECRET'}),
               self.token(headers={'crit': ['unrecognized']}), self.token(headers={'kid': '../PRIVATE'})]
        unsigned = jwt.encode(self.claims, '', algorithm='none', headers={'kid': 'fixture-key'})
        symmetric = jwt.encode(self.claims, 'synthetic-not-an-RSA-key-at-least-32-bytes', algorithm='HS256', headers={'kid': 'fixture-key'})
        for token in bad + [unsigned, symmetric, 'PRIVATE_TOKEN', 'x'*16385]:
            with self.subTest(length=len(token)), self.assertRaises(h.AuthorizationError) as caught:
                self.verifier.verify(token)
            self.assertNotIn(token, str(caught.exception))
        self.assertEqual(self.fetch.call_count, 1)

    def test_jwks_cache_does_not_bypass_token_validation_or_refresh_forever(self):
        self.verifier.verify(self.token())
        with self.assertRaises(h.AuthorizationError): self.verifier.verify(self.token({'exp': int(time.time())-1}))
        for _ in range(3):
            with self.assertRaises(h.AuthorizationError): self.verifier.verify(self.token(headers={'kid': 'unknown'}))
        self.assertEqual(self.fetch.call_count, 1)
        self.clock += 301
        self.fetch.side_effect = RuntimeError('PRIVATE_SIGNED_URL')
        with self.assertRaises(h.AuthorizationError) as caught: self.verifier.verify(self.token())
        self.assertNotIn('PRIVATE_SIGNED_URL', str(caught.exception))

    def test_duplicate_json_and_secret_request_id_not_reflected(self):
        principal = self.verifier.verify(self.token())
        code, body = h.dispatch(None, principal, b'{"jsonrpc":"2.0","id":1,"id":2}')
        self.assertEqual(code, 400)
        code, body = h.dispatch(None, principal, json.dumps({'jsonrpc': '2.0', 'id': 'https://secret.example/token', 'method': 'ping'}).encode())
        self.assertEqual(code, 400)
        self.assertIsNone(body['id'])


@unittest.skipUnless(jwt is not None, 'HTTP OAuth extras absent; run consolidated acceptance with requirements-http.txt.')
class HTTPAcceptanceTests(OAuthFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        (root / 'data/derived').mkdir(parents=True)
        for patcher in (patch.object(t, 'ROOT', root), patch.object(t, 'ensure_ignored')):
            patcher.start(); self.addCleanup(patcher.stop)
        reader = Mock(); reader.download.return_value = mock_files.fixture_jpeg()
        self.engine = h.w.WorkflowEngine(root / 'data/derived/http-workflow', reader=reader)
        self.server = h.make_server(self.engine, self.verifier)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.counter = 0

    def stop_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def request(self, method, path, body=None, *, token=True, changes=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        request_headers = {'Host': 'phototraits.example', 'Content-Type': 'application/json'}
        if token:
            request_headers['Authorization'] = 'Bearer ' + self.token(changes)
        request_headers.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, data, headers=request_headers)
        response = conn.getresponse()
        status, response_headers, raw = response.status, dict(response.headers), response.read()
        conn.close()
        return status, response_headers, json.loads(raw) if raw else None

    def tool(self, name, args):
        self.counter += 1
        status, _, response = self.request('POST', '/mcp', {'jsonrpc': '2.0', 'id': self.counter,
            'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
        self.assertEqual(status, 200)
        self.assertFalse(response['result'].get('isError'), response['result'])
        return response['result']

    def test_unauthenticated_challenge_metadata_scope_and_origin(self):
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}
        status, headers, body = self.request('POST', '/mcp', request, token=False)
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata="https://phototraits.example/.well-known/oauth-protected-resource"', headers['WWW-Authenticate'])
        status, _, metadata = self.request('GET', '/.well-known/oauth-protected-resource', token=False)
        self.assertEqual(status, 200)
        self.assertEqual(metadata['resource'], self.verifier.resource)
        self.assertNotIn('registration_endpoint', metadata)
        status, _, _ = self.request('POST', '/mcp', request, changes={'sub': 'auth0|stranger'})
        self.assertEqual(status, 401)
        for headers in ({'Origin': 'https://evil.example'}, {'Host': 'evil.example'}):
            self.assertEqual(self.request('POST', '/mcp', request, headers=headers)[0], 403)
        request.update(method='tools/call', params={'name': 'new_session', 'arguments': {}})
        status, _, body = self.request('POST', '/mcp', request, changes={'scope': 'results:read'})
        self.assertEqual(status, 403)
        self.assertIn('insufficient_scope', body['result']['_meta']['mcp/www_authenticate'][0])
        self.assertFalse(self.engine.sessions)

    def test_complete_authenticated_single_photo_mcp_flow(self):
        with patch.object(t, 'load_key', side_effect=AssertionError('No actual key')), \
             patch.object(t, 'post_decisions', side_effect=AssertionError('No model API')):
            status, _, response = self.request('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'offline-fixture', 'version': '1'}}})
            self.assertEqual(status, 200)
            self.assertEqual(response['result']['serverInfo']['name'], 'PhotoTraits')
            status, _, response = self.request('POST', '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
            self.assertEqual(len(response['result']['tools']), 10)
            self.assertTrue(all(d['securitySchemes'][0]['type'] == 'oauth2' for d in response['result']['tools']))
            self.assertTrue(all(d['_meta']['securitySchemes'] == d['securitySchemes'] for d in response['result']['tools']))
            attach = next(d for d in response['result']['tools'] if d['name'] == 'attach_photo')
            self.assertEqual(attach['_meta']['openai/fileParams'], ['file'])
            state = self.tool('new_session', {})['structuredContent']
            sid = state['session_id']
            state = self.tool('plan_fields', {'session_id': sid, 'revision': state['revision'],
                'purpose': 'Synthetic acceptance only', 'species_scope': 'One user-selected species', 'fields': fields()[:2]})['structuredContent']
            state = self.tool('attach_photo', {'session_id': sid, 'revision': state['revision'], 'file': {
                'download_url': 'https://fixture.invalid/SIGNED_VALUE', 'file_id': 'FIXTURE_FILE', 'mime_type': 'image/jpeg'}})['structuredContent']
            preview = self.tool('preview_plan', {'session_id': sid})
            self.assertEqual(preview['content'][1]['type'], 'image')
            offer = preview['structuredContent']
            args = {'session_id': sid, 'plan_sha256': state['plan_sha256'],
                    'confirmation_id': offer['confirmation_id'], 'human_approved_photo_and_fields': True}
            self.tool('analyze_photo', args)
            session = self.engine.sessions[sid]
            session.thread.join(3)
            self.tool('analyze_photo', args)
            result = self.tool('session_status', {'session_id': sid})['structuredContent']
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['results'][0]['evidence_kind'], 'MOCK_NOT_MODEL_OUTPUT')
            self.tool('review_values', {'session_id': sid, 'plan_sha256': state['plan_sha256'],
                'values': {'surface_mark': 'absent', 'reflection': 'unknown'}})
            exported = self.tool('export_csv', {'session_id': sid})
            csv_text = exported['content'][1]['resource']['text']
            self.assertEqual(t.sha(csv_text.encode()), exported['structuredContent']['sha256'])
            self.assertEqual(len(list(csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff'))))), 2)
            self.assertEqual(self.engine.reserved, 0)
            self.assertFalse(list(session.path.glob('*.jpg')))

    def test_empty_tool_arguments_and_request_metadata_are_protocol_fields(self):
        metadata = {'progressToken': 'PRIVATE_TEST_META', 'openai/locale': 'fixture',
                    'openai/userAgent': 'PRIVATE_TEST_META'}
        for params in ({'name': 'workflow_status', 'arguments': {}},
                       {'name': 'workflow_status'},
                       {'name': 'workflow_status', 'arguments': {}, '_meta': metadata},
                       {'name': 'workflow_status', '_meta': metadata}):
            with self.subTest(fields=sorted(params)), patch.object(h.w, 'call_tool', wraps=h.w.call_tool) as call:
                status, _, body = self.request('POST', '/mcp',
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': params})
                self.assertEqual(status, 200)
                self.assertFalse(body['result'].get('isError'), body['result'])
                self.assertEqual(body['result']['structuredContent']['mode'], 'mock')
                self.assertEqual(body['result']['structuredContent']['cost_reserved_usd'], '0')
                self.assertEqual(call.call_args.args[-1], {})
                self.assertNotIn('PRIVATE_TEST_META', json.dumps(body))
        self.assertFalse(self.engine.sessions)
        self.engine.reader.download.assert_not_called()

    def test_request_metadata_does_not_relax_arguments_identity_or_scopes(self):
        for params in ({'name': 'workflow_status', '_meta': 'PRIVATE_TEST_META'},
                       {'name': 'workflow_status', '_meta': []},
                       {'name': 'workflow_status', '_meta': None},
                       {'name': 'workflow_status', 'arguments': None},
                       {'name': 'workflow_status', 'arguments': {'_meta': {'note': 'PRIVATE_TEST_META'}}},
                       {'name': 'workflow_status', 'arguments': {'allow_live': True}},
                       {'name': 'workflow_status', 'unexpected': 'PRIVATE_TEST_META'}):
            with self.subTest(fields=sorted(params)):
                status, _, body = self.request('POST', '/mcp',
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': params})
                self.assertEqual(status, 200)
                self.assertTrue(body['result']['isError'])
                self.assertNotIn('PRIVATE_TEST_META', json.dumps(body))
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'workflow_status', '_meta': {'sub': self.claims['sub'], 'scope': self.claims['scope']}}}
        self.assertEqual(self.request('POST', '/mcp', request, token=False)[0], 401)
        self.assertEqual(self.request('POST', '/mcp', request, changes={'sub': 'auth0|stranger'})[0], 401)
        self.assertEqual(self.request('POST', '/mcp', request, changes={'scope': 'photo:analyze'})[0], 403)
        self.assertFalse(self.engine.sessions)
        self.engine.reader.download.assert_not_called()

    def test_unlisted_attachment_hostname_only_reaches_authorized_session_owner(self):
        principal = self.verifier.verify(self.token())
        session = self.engine.new(principal)
        session.edit('Purpose', 'Synthetic fixture', fields()[:1], 0)
        resolver, connection = Mock(), Mock()
        self.engine.reader = h.w.attachments.SafeHTTPS(resolver=resolver, connection=connection)
        file = {'download_url': 'https://new.example/PRIVATE_PATH?PRIVATE_QUERY', 'file_id': 'PRIVATE_FILE_ID',
                'file_name': 'PRIVATE_FILE_NAME.jpg', 'mime_type': 'image/jpeg'}
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'attach_photo',
                   'arguments': {'session_id': session.id, 'revision': session.revision, 'file': file}}}
        with patch.object(self.engine.reader, 'check_descriptor', wraps=self.engine.reader.check_descriptor) as check:
            for options, expected in (({'token': False}, 401), ({'changes': {'sub': 'auth0|stranger'}}, 401),
                                      ({'changes': {'scope': 'results:read'}}, 403)):
                status, _, body = self.request('POST', '/mcp', request, **options)
                self.assertEqual(status, expected)
                self.assertNotIn('new.example', json.dumps(body))
                self.assertNotIn('candidate_host', json.dumps(body))
            other = self.engine.new(h.w.Principal('synthetic-other-owner', frozenset(h.w.SCOPES)))
            wrong_session = copy.deepcopy(request)
            wrong_session['params']['arguments']['session_id'] = other.id
            status, _, body = self.request('POST', '/mcp', wrong_session)
            self.assertEqual(status, 200)
            self.assertTrue(body['result']['isError'])
            self.assertNotIn('candidate_host', json.dumps(body))
            check.assert_not_called()
            before = copy.deepcopy(session.state())
            status, _, body = self.request('POST', '/mcp', request)
            self.assertEqual(status, 200)
            self.assertEqual(body['result'], {'isError': True,
                'content': [{'type': 'text', 'text': t.encoded({'candidate_host': 'new.example', 'download_performed': False}).decode()}],
                'structuredContent': {'candidate_host': 'new.example', 'download_performed': False}})
            check.assert_called_once_with(file)
            self.assertEqual(session.state(), before)
        self.assertEqual(self.engine.reader.hosts, frozenset())
        resolver.assert_not_called()
        connection.assert_not_called()

    def test_malformed_attachment_urls_never_reflect_hostname_or_private_values(self):
        session = self.engine.new(self.verifier.verify(self.token()))
        session.edit('Purpose', 'Synthetic fixture', fields()[:1], 0)
        resolver, connection = Mock(), Mock()
        self.engine.reader = h.w.attachments.SafeHTTPS(resolver=resolver, connection=connection)
        before = copy.deepcopy(session.state())
        for url in ('http://new.example/PRIVATE_PATH?PRIVATE_QUERY',
                    'https://user:PRIVATE_PASSWORD@new.example/PRIVATE_PATH',
                    'https://new.example:bad/PRIVATE_PATH', 'https://new.example:8443/PRIVATE_PATH',
                    'https://new.example/PRIVATE_PATH#PRIVATE_FRAGMENT', 'https://[new.example/PRIVATE_PATH'):
            status, _, body = self.request('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1,
                'method': 'tools/call', 'params': {'name': 'attach_photo', 'arguments': {
                    'session_id': session.id, 'revision': session.revision, 'file': {
                        'download_url': url, 'file_id': 'PRIVATE_FILE_ID', 'file_name': 'PRIVATE_FILE_NAME.jpg'}}}})
            self.assertEqual(status, 200)
            self.assertEqual(body['result'], {'isError': True, 'content': [{'type': 'text',
                'text': 'Attachment unavailable or unsafe. Reattach one JPEG; no automatic retry.'}]})
            self.assertEqual(session.state(), before)
        for path in session.path.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'PRIVATE_', path.read_bytes())
                self.assertNotIn(b'new.example', path.read_bytes())
        resolver.assert_not_called()
        connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
