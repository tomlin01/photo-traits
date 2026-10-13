"""Loopback-only HTTP tests; no external services, photos or real credentials."""
import builtins
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / 'plugins/photo-traits-local/capabilities_http.py'
spec = importlib.util.spec_from_file_location('gate1_http', ENTRYPOINT)
http_cap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(http_cap)
ACCEPT = 'application/json, text/event-stream'


def rpc(method, params=None, request_id=1):
    return {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params or {}}


def initialize(version='2025-11-25'):
    return rpc('initialize', {'protocolVersion': version, 'capabilities': {},
                              'clientInfo': {'name': 'offline-http-test', 'version': '1'}})


def exchange(port, request=None, *, method='POST', path='/mcp', raw=None,
             replace=None, extra=(), omit=()):
    body = raw if raw is not None else json.dumps(request).encode() if request is not None else b''
    headers = {'Host': f'127.0.0.1:{port}', 'Accept': ACCEPT,
               'Content-Type': 'application/json', 'MCP-Protocol-Version': '2025-11-25',
               'Content-Length': str(len(body))}
    headers.update(replace or {})
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, value in [*headers.items(), *extra]:
            if key not in omit:
                conn.putheader(key, value)
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


class CapabilityHTTPTests(unittest.TestCase):
    def setUp(self):
        self.audit = io.StringIO()
        self.server = http_cap.make_server(0, self.audit)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def request(self, *args, **kwargs):
        return exchange(self.port, *args, **kwargs)

    def test_initialize_list_call_are_json_with_matching_receipt(self):
        status, headers, raw = self.request(initialize())
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertNotIn('Mcp-Session-Id', headers)
        self.assertEqual(json.loads(raw)['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(json.loads(raw)['result']['capabilities'], {'tools': {'listChanged': False}})
        status, headers, raw = self.request({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertEqual((status, raw), (202, b''))
        self.assertEqual(headers['Content-Length'], '0')
        status, _, raw = self.request(rpc('tools/list', request_id=2))
        self.assertEqual(status, 200)
        tools = json.loads(raw)['result']['tools']
        self.assertEqual([tool['name'] for tool in tools], ['photo_traits_capabilities'])
        self.assertEqual(tools[0]['inputSchema'], {
            'type': 'object', 'properties': {}, 'additionalProperties': False})
        status, headers, raw = self.request(rpc('tools/call', {
            'name': 'photo_traits_capabilities', 'arguments': {}}, 3))
        self.assertEqual(status, 200)
        result = json.loads(raw)['result']
        self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
        payload = result['structuredContent']
        for name, value in http_cap.cap.CAPABILITIES.items():
            self.assertEqual(payload[name], value)
        self.assertEqual(json.loads(self.audit.getvalue())['receipt_id'], payload['receipt_id'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Connection'], 'close')
        self.assertFalse(any(name.lower().startswith('access-control-') for name in headers))

    def test_stateless_requests_and_protocol_negotiation(self):
        # Independent HTTP connections need no bearer/session credential.
        self.assertEqual(self.request(rpc('tools/list'))[0], 200)
        self.assertEqual(self.request(rpc('ping'), omit=('MCP-Protocol-Version',))[0], 200)
        for version in http_cap.HTTP_PROTOCOLS + ('2024-11-05', 'future-version'):
            with self.subTest(version=version):
                status, _, raw = self.request(initialize(version), omit=('MCP-Protocol-Version',))
                self.assertEqual(status, 200)
                selected = json.loads(raw)['result']['protocolVersion']
                self.assertEqual(selected, version if version in http_cap.HTTP_PROTOCOLS
                                 else http_cap.HTTP_PROTOCOLS[0])
        self.assertEqual(self.request(rpc('ping'), replace={'MCP-Protocol-Version': 'future-version'})[0], 400)
        self.assertEqual(self.request(rpc('ping'), extra=[('MCP-Protocol-Version', '2025-11-25')])[0], 400)

    def test_loopback_host_origin_and_forwarding_headers(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        for host in (f'127.0.0.1:{self.port}', f'localhost:{self.port}'):
            status, _, _ = self.request(rpc('ping'), replace={'Host': host, 'Origin': f'http://{host}'})
            self.assertEqual(status, 200)
        bad_cases = [
            {'replace': {'Host': 'evil.example'}},
            {'replace': {'Host': f'127.0.0.1:{self.port}.evil.example'}},
            {'replace': {'Origin': 'https://evil.example'}},
            {'replace': {'Origin': 'null'}},
            {'replace': {'Origin': 'http://127.0.0.1:1'}},
            {'replace': {'Origin': f'http://user@127.0.0.1:{self.port}'}},
            {'replace': {'Host': 'evil.example', 'X-Forwarded-Host': f'127.0.0.1:{self.port}'}},
            {'omit': ('Host',)},
            {'extra': [('Host', f'127.0.0.1:{self.port}')]},
            {'extra': [('Origin', f'http://localhost:{self.port}'),
                       ('Origin', f'http://localhost:{self.port}')]},
        ]
        for options in bad_cases:
            with self.subTest(options=options):
                self.assertEqual(self.request(rpc('ping'), **options)[0], 403)
        self.assertEqual(self.audit.getvalue(), '')

    def test_no_sse_sessions_static_files_or_other_routes(self):
        for method in ['GET', 'HEAD', 'DELETE', 'OPTIONS', 'PUT', 'PATCH']:
            with self.subTest(method=method):
                status, headers, _ = self.request(method=method)
                self.assertEqual(status, 405)
                self.assertEqual(headers['Allow'], 'POST')
        for path in ['/', '/mcp/', '/mcp?file=secret', '/ui.html', '/capabilities_server.py',
                     '/.env.local', '/../README.md', '/%2e%2e/.env.local']:
            for method in ['GET', 'POST']:
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.request(rpc('ping'), path=path, method=method)[0], 404)
        self.assertEqual(self.audit.getvalue(), '')

    def test_framing_media_size_and_protocol_headers(self):
        cases = [
            ({'omit': ('Content-Length',)}, 411),
            ({'replace': {'Content-Length': '-1'}}, 400),
            ({'extra': [('Content-Length', '2')]}, 400),
            ({'replace': {'Content-Length': str(http_cap.cap.MAX_MESSAGE_BYTES + 1)}}, 413),
            ({'replace': {'Content-Length': '9' * 5000}}, 413),
            ({'replace': {'Transfer-Encoding': 'chunked'}}, 400),
            ({'replace': {'Content-Encoding': 'gzip'}}, 415),
            ({'replace': {'Content-Type': 'text/plain'}}, 415),
            ({'replace': {'Content-Type': 'multipart/form-data'}}, 415),
            ({'extra': [('Content-Type', 'application/json')]}, 415),
            ({'omit': ('Accept',)}, 406),
            ({'replace': {'Accept': 'text/event-stream'}}, 406),
            ({'replace': {'Accept': 'application/json;q=0, text/event-stream'}}, 406),
            ({'replace': {'Expect': '100-continue'}}, 417),
        ]
        for options, expected in cases:
            with self.subTest(options=options):
                status, _, body = self.request(raw=b'{}', **options)
                self.assertEqual(status, expected)
                self.assertEqual(json.loads(body), {'error': 'HTTP request rejected.'})
        self.assertEqual(self.audit.getvalue(), '')

    def test_unapproved_inputs_and_methods_cannot_reach_other_tools(self):
        for tool in ['execute_confirmed', 'read_file', 'run', 'status', 'session_status']:
            status, _, raw = self.request(rpc('tools/call', {'name': tool, 'arguments': {}}))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)['error']['code'], -32602)
        for arguments in [{'path': '/secret'}, {'url': 'https://example.com/private'},
                          {'photo': 'data:image/jpeg;base64,AAAA'}, {'allow_live': True},
                          {'key': 'synthetic-secret'}, [], None]:
            status, _, raw = self.request(rpc('tools/call', {
                'name': 'photo_traits_capabilities', 'arguments': arguments}))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)['error']['code'], -32602)
            self.assertNotIn(b'synthetic-secret', raw)
        for method in ['resources/read', 'resources/list', 'prompts/list', 'completion/complete']:
            self.assertEqual(json.loads(self.request(rpc(method))[2])['error']['code'], -32601)
        status, _, raw = self.request({'jsonrpc': '2.0', 'method': 'tools/call',
                                      'params': {'name': 'photo_traits_capabilities', 'arguments': {}}})
        self.assertEqual((status, raw), (202, b''))
        self.assertEqual(self.audit.getvalue(), '')

    def test_bad_json_is_rejected_without_echo_or_receipt(self):
        for raw in [b'{"secret":"synthetic-secret",}', b'[]', b'{"id":1,"id":2}',
                    b'{"id":NaN}', b'\xff', b'null', b'{}', b'{"jsonrpc":"1.0"}']:
            with self.subTest(raw=raw):
                status, _, response = self.request(raw=raw)
                self.assertEqual(status, 400)
                self.assertIn(json.loads(response)['error']['code'], [-32700, -32600])
                self.assertNotIn(b'synthetic-secret', response)
        self.assertEqual(self.audit.getvalue(), '')

    def test_dispatch_cannot_read_files_environment_network_or_run_models(self):
        blocked = AssertionError('Forbidden access')
        raw = json.dumps(rpc('tools/call', {'name': 'photo_traits_capabilities', 'arguments': {}})).encode()
        with patch.object(builtins, 'open', side_effect=blocked), \
             patch.object(io, 'open', side_effect=blocked), \
             patch.object(os, 'getenv', side_effect=blocked), \
             patch.object(os, 'environ', {}), \
             patch.object(socket, 'socket', side_effect=blocked), \
             patch.object(socket, 'create_connection', side_effect=blocked), \
             patch.object(subprocess, 'Popen', side_effect=blocked):
            status, result = http_cap.dispatch_json(raw, self.audit)
        self.assertEqual(status, 200)
        self.assertFalse(result['result']['structuredContent']['model_api_calls_enabled'])

    def test_isolated_cli_has_no_file_writes_or_public_binding_option(self):
        for args in [['--host', '0.0.0.0'], ['--workspace', str(ROOT)], ['--allow-live']]:
            result = subprocess.run([sys.executable, '-I', '-S', '-B', str(ENTRYPOINT), *args],
                                    env={}, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2)
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                [sys.executable, '-I', '-S', '-B', str(ENTRYPOINT), '--port', '0'],
                env={}, cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            try:
                # The process emits only one nonsecret startup line; requests are bounded below.
                import selectors
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(timeout=5), 'HTTP process did not start')
                startup = json.loads(process.stdout.readline())
                self.assertEqual(startup['transport'], 'streamable-http')
                self.assertEqual(startup['response_mode'], 'json')
                self.assertTrue(startup['stateless'])
                self.assertRegex(startup['url'], r'^http://127\.0\.0\.1:\d+/mcp$')
                port = int(startup['url'].split(':')[2].split('/')[0])
                self.assertEqual(exchange(port, initialize())[0], 200)
                status, _, raw = exchange(port, rpc('tools/call', {
                    'name': 'photo_traits_capabilities', 'arguments': {}}))
                self.assertEqual(status, 200)
                receipt_id = json.loads(raw)['result']['structuredContent']['receipt_id']
            finally:
                process.terminate()
                _, stderr = process.communicate(timeout=5)
            self.assertEqual(json.loads(stderr)['receipt_id'], receipt_id)
            self.assertEqual(list(Path(directory).iterdir()), [])
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.1', port), timeout=0.2)


if __name__ == '__main__':
    unittest.main()
