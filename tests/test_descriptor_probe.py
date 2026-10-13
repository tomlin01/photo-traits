"""Descriptor delivery boundary tests; synthetic strings only, no host URLs."""
from contextlib import ExitStack
import http.client
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'plugins/photo-traits-local/descriptor_probe.py'
spec = importlib.util.spec_from_file_location('descriptor_probe_test', SCRIPT)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
FILE = {'download_url': 'https://synthetic.invalid/file?signature=DO_NOT_ECHO_TEST',
        'file_id': 'SYNTHETIC_FILE_PRIVATE_ID', 'mime_type': 'image/jpeg',
        'file_name': 'SYNTHETIC_PRIVATE_FILENAME.jpg'}


def call(file=FILE, **extra):
    return {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
            'params': {'name': p.NAME, 'arguments': {'file': file, **extra}}}


class DescriptorProbeTests(unittest.TestCase):
    def setUp(self):
        self.audit = io.StringIO()
        self.probe = p.Probe(self.audit)

    def test_exact_one_tool_and_official_fileparams_contract(self):
        listed = self.probe.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        tools = listed['result']['tools']
        self.assertEqual([tool['name'] for tool in tools], ['probe_file_descriptor'])
        tool = tools[0]
        self.assertEqual(tool['_meta']['openai/fileParams'], ['file'])
        file = tool['inputSchema']['properties']['file']
        self.assertEqual(set(file['properties']), set(p.FILE_FIELDS))
        self.assertEqual(file['required'], ['download_url', 'file_id'])
        self.assertEqual(tool['securitySchemes'], [{'type': 'noauth'}])
        for method in ('resources/list', 'resources/read', 'prompts/list'):
            self.assertIn('error', self.probe.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': method}))
        for name in ('photo_traits_capabilities', 'read_file', 'new_session', 'execute_confirmed'):
            request = call()
            request['params']['name'] = name
            self.assertIn('error', self.probe.dispatch(request))
        self.assertFalse(self.probe.consumed)

    def test_receipt_matches_audit_no_values_retained_and_one_success_only(self):
        result = self.probe.dispatch(call())['result']
        value = result['structuredContent']
        self.assertTrue(value['schema_valid'])
        self.assertFalse(value['download_performed'])
        self.assertFalse(value['photo_transfer_verified'])
        self.assertFalse(value['source_authenticated'])
        self.assertEqual(value['model_api_calls'], 0)
        self.assertEqual(json.loads(result['content'][0]['text']), value)
        record = json.loads(self.audit.getvalue())
        self.assertEqual(value['receipt_id'], record['receipt_id'])
        self.assertRegex(value['receipt_id'], '^[a-f0-9]{32}$')
        text = json.dumps(result) + self.audit.getvalue()
        for secret in FILE.values(): self.assertNotIn(secret, text)
        self.assertEqual(set(vars(self.probe)), {'audit', 'consumed'})
        self.assertIn('error', self.probe.dispatch(call()))
        self.assertEqual(len(self.audit.getvalue().splitlines()), 1)

    def test_optional_fields_may_be_omitted_and_values_are_not_interpreted(self):
        result = self.probe.dispatch(call({k: FILE[k] for k in ('download_url', 'file_id')}))
        value = result['result']['structuredContent']
        self.assertTrue(value['schema_valid'])
        self.assertEqual(value['checks']['mime_type'], {'present': False, 'type_is_string': False})
        self.assertEqual(value['checks']['file_name'], {'present': False, 'type_is_string': False})
        # The contract probe checks shape, not URL safety, MIME truth or image bytes.
        probe = p.Probe(io.StringIO())
        self.assertTrue(probe.dispatch(call({'download_url': 'file:///do-not-open',
             'file_id': 'synthetic', 'mime_type': 'not-an-image'}))['result']['structuredContent']['schema_valid'])

    def test_rejected_shapes_never_echo_private_values_or_consume(self):
        for file in (None, [], 'PRIVATE_STRING', {}, {'file_id': FILE['file_id']},
                     {**FILE, 'download_url': 42}, {**FILE, 'mime_type': None},
                     {**FILE, 'unexpected_private_name': 'PRIVATE_EXTRA_VALUE'}):
            result = self.probe.dispatch(call(file))['result']
            self.assertTrue(result['isError'])
            self.assertFalse(result['structuredContent']['schema_valid'])
            self.assertIsNone(result['structuredContent']['receipt_id'])
            text = json.dumps(result)
            for secret in (*FILE.values(), 'PRIVATE_STRING', 'PRIVATE_EXTRA_VALUE', 'unexpected_private_name'):
                self.assertNotIn(secret, text)
        self.assertTrue(self.probe.dispatch(call(FILE, confirmed=True))['result']['isError'])
        self.assertEqual(self.audit.getvalue(), '')
        self.assertFalse(self.probe.consumed)

    def test_dispatch_cannot_read_files_env_dns_network_or_spawn(self):
        with ExitStack() as stack:
            calls = [stack.enter_context(patch(name, side_effect=AssertionError('Forbidden access')))
                     for name in ('builtins.open', 'pathlib.Path.read_bytes', 'pathlib.Path.read_text',
                                  'os.getenv', 'socket.getaddrinfo', 'socket.socket',
                                  'urllib.request.urlopen', 'subprocess.Popen')]
            result = self.probe.dispatch(call())
            self.assertTrue(result['result']['structuredContent']['schema_valid'])
            for mocked in calls: mocked.assert_not_called()

    def test_untrusted_protocol_input_json_and_copied_id_are_redacted(self):
        for raw in (b'{"private":', b'[]', b'\xff', b'{}\n{}',
                    b'{"jsonrpc":"2.0","jsonrpc":"2.0"}',
                    b'{"x":NaN}', b'X' * (p.h.cap.MAX_MESSAGE_BYTES + 1)):
            status, result = self.probe.dispatch_json(raw)
            self.assertIn('error', result)
            self.assertIn(status, (200, 400, 413))
        for value in FILE.values():
            request = call()
            request['id'] = value
            result = self.probe.dispatch(request)
            self.assertIsNone(result['id'])
            self.assertNotIn(value, json.dumps(result))
        request = call()
        request.pop('id')
        self.assertIsNone(self.probe.dispatch(request))
        self.assertFalse(self.probe.consumed)
        self.assertEqual(self.audit.getvalue(), '')

    def test_http_boundary_response_and_no_input_logging(self):
        server = p.make_server(audit=self.audit)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port
        def request(method='POST', path='/mcp', obj=None, **headers):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            body = json.dumps(obj or {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
            standard = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
            standard.update(headers)
            connection.request(method, path, body, standard)
            response = connection.getresponse()
            data = response.read()
            status, returned = response.status, dict(response.getheaders())
            connection.close()
            return status, data, returned
        self.assertEqual(server.server_address[0], '127.0.0.1')
        self.assertEqual(request(Host='example.com')[0], 403)
        self.assertEqual(request(Origin='https://example.com')[0], 403)
        self.assertEqual(request(method='GET')[0], 405)
        self.assertEqual(request(path='/files/private?signature=PRIVATE_QUERY')[0], 404)
        self.assertEqual(request(**{'Content-Type': 'image/jpeg'})[0], 415)
        status, data, headers = request(obj=call())
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertTrue(json.loads(data)['result']['structuredContent']['schema_valid'])
        for secret in (*FILE.values(), 'PRIVATE_QUERY'):
            self.assertNotIn(secret, data.decode() + self.audit.getvalue())
        self.assertEqual(len(self.audit.getvalue().splitlines()), 1)

    def test_isolated_process_and_no_live_workspace_or_public_bind_flags(self):
        command = [sys.executable, '-I', '-S', '-B', str(SCRIPT)]
        for flags in (['--host', '0.0.0.0'], ['--workspace', str(ROOT)], ['--allow-live']):
            run = subprocess.run(command + flags, capture_output=True, env={}, timeout=5)
            self.assertEqual(run.returncode, 2)
        with tempfile.TemporaryDirectory() as cwd:
            process = subprocess.Popen(command, cwd=cwd, env={}, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                startup = json.loads(process.stdout.readline())
                self.assertTrue(startup['one_valid_probe_only'])
                self.assertTrue(startup['url'].startswith('http://127.0.0.1:'))
                self.assertEqual(list(Path(cwd).iterdir()), [])
            finally:
                process.terminate()
                _, stderr = process.communicate(timeout=5)
            self.assertEqual(stderr, '')


if __name__ == '__main__':
    unittest.main()
