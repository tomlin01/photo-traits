"""Offline contract and boundary tests for the isolated Gate 1 endpoint."""
import builtins
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / 'plugins/photo-traits-local/capabilities_server.py'
spec = importlib.util.spec_from_file_location('gate1_capabilities', ENTRYPOINT)
cap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cap)


def rpc(method, params=None, request_id=1):
    return {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params or {}}


def initialize():
    return rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                              'clientInfo': {'name': 'offline-test', 'version': '1'}})


def messages(*requests):
    return b''.join((json.dumps(request) + '\n').encode() for request in requests)


class CapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.audit = io.StringIO()
        self.server = cap.Server(self.audit)
        self.server.dispatch(initialize())
        self.server.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def test_only_capabilities_tool_is_advertised(self):
        tools = self.server.dispatch(rpc('tools/list'))['result']['tools']
        self.assertEqual([tool['name'] for tool in tools], ['photo_traits_capabilities'])
        self.assertEqual(tools[0]['inputSchema'], {
            'type': 'object', 'properties': {}, 'additionalProperties': False})
        self.assertTrue(tools[0]['annotations']['readOnlyHint'])
        self.assertFalse(tools[0]['annotations']['openWorldHint'])

    def test_no_file_network_environment_or_subprocess_access(self):
        blocked = AssertionError('Forbidden access')
        with patch.object(builtins, 'open', side_effect=blocked), \
             patch.object(io, 'open', side_effect=blocked), \
             patch.object(socket, 'socket', side_effect=blocked), \
             patch.object(os, 'getenv', side_effect=blocked), \
             patch.object(os, 'environ', {}), \
             patch.object(subprocess, 'Popen', side_effect=blocked):
            result = self.server.dispatch(rpc('tools/call', {'name': cap.TOOL_NAME, 'arguments': {}}))['result']
        data = result['structuredContent']
        self.assertEqual(json.loads(result['content'][0]['text']), data)
        self.assertFalse(result['isError'])
        self.assertEqual(set(data), set(cap.CAPABILITIES) | {'receipt_id'})
        for key in cap.CAPABILITIES:
            self.assertEqual(data[key], cap.CAPABILITIES[key])
        self.assertEqual(json.loads(self.audit.getvalue())['receipt_id'], data['receipt_id'])

    def test_tools_files_urls_credentials_and_live_flags_are_rejected(self):
        for name in ['status', 'new_session', 'plan_fields', 'session_status',
                     'execute_confirmed', 'cancel_session', 'read_file', 'run']:
            with self.subTest(name=name):
                self.assertEqual(self.server.dispatch(rpc('tools/call', {'name': name}))['error']['code'], -32602)
        for args in [{'path': '/secret'}, {'url': 'https://example.com/photo.jpg'},
                     {'photo': 'data:image/jpeg;base64,AAAA'}, {'key': 'synthetic-secret'},
                     {'allow_live': True}, None, [], 'synthetic-secret']:
            result = self.server.dispatch(rpc('tools/call', {'name': cap.TOOL_NAME, 'arguments': args}))
            self.assertEqual(result['error']['code'], -32602)
            self.assertNotIn('synthetic-secret', json.dumps(result))
        for method in ['resources/list', 'resources/read', 'prompts/list', 'completion/complete']:
            self.assertEqual(self.server.dispatch(rpc(method))['error']['code'], -32601)
        self.assertEqual(self.audit.getvalue(), '')

    def test_initialization_and_notifications_cannot_bypass_dispatch(self):
        fresh = cap.Server(self.audit)
        self.assertEqual(fresh.dispatch(rpc('tools/list'))['error']['code'], -32000)
        self.assertEqual(fresh.dispatch(initialize())['result']['protocolVersion'], cap.PROTOCOLS[0])
        self.assertEqual(fresh.dispatch(rpc('tools/list'))['error']['code'], -32000)
        self.assertIsNone(fresh.dispatch({'jsonrpc': '2.0', 'method': 'tools/call',
                                         'params': {'name': cap.TOOL_NAME}}))
        self.assertEqual(self.audit.getvalue(), '')
        self.assertEqual(fresh.dispatch(rpc('ping'))['result'], {})
        for version in cap.PROTOCOLS + ('future-unknown',):
            request = initialize()
            request['params']['protocolVersion'] = version
            response = cap.Server(io.StringIO()).dispatch(request)['result']
            self.assertEqual(response['protocolVersion'], version if version in cap.PROTOCOLS else cap.PROTOCOLS[0])

    def test_malformed_json_and_oversized_frames_fail_without_payload_echo(self):
        out, audit = io.StringIO(), io.StringIO()
        raw = b'{"secret":"synthetic-secret",}\n' + b'[]\n' + b'{"id":1,"id":2}\n' + b'{"id":NaN}\n' + b'\xff\n'
        self.assertEqual(cap.serve(io.BytesIO(raw), out, audit), 0)
        self.assertEqual(len(out.getvalue().splitlines()), 5)
        self.assertNotIn('synthetic-secret', out.getvalue())
        self.assertEqual(audit.getvalue(), '')
        out = io.StringIO()
        self.assertEqual(cap.serve(io.BytesIO(b'x' * (cap.MAX_MESSAGE_BYTES + 1)), out, audit), 1)
        self.assertEqual(json.loads(out.getvalue())['error']['code'], -32600)

    def test_actual_isolated_process_has_matching_receipts_and_no_file_writes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, '-I', '-S', '-B', str(ENTRYPOINT)], cwd=directory, env={},
                input=messages(initialize(), {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                               rpc('tools/list', request_id=2),
                               rpc('tools/call', {'name': cap.TOOL_NAME, 'arguments': {}}, 3),
                               rpc('tools/call', {'name': cap.TOOL_NAME, 'arguments': {}}, 4)),
                capture_output=True, timeout=5, check=True)
            self.assertEqual(list(Path(directory).iterdir()), [])
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        receipts = [json.loads(line) for line in result.stderr.splitlines()]
        self.assertEqual(len(responses), 4)
        self.assertEqual(len(receipts), 2)
        self.assertEqual([response['id'] for response in responses], [1, 2, 3, 4])
        for response, receipt in zip(responses[2:], receipts):
            self.assertEqual(response['result']['structuredContent']['receipt_id'], receipt['receipt_id'])
        self.assertNotEqual(receipts[0]['receipt_id'], receipts[1]['receipt_id'])

    def test_no_workspace_or_live_startup_options(self):
        result = subprocess.run([sys.executable, '-I', '-S', '-B', str(ENTRYPOINT), '--allow-live'],
                                env={}, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b'')


if __name__ == '__main__':
    unittest.main()
