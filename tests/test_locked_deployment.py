"""Public health / locked workflow transport checks, with loopback requests only."""
from argparse import Namespace
import http.client
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from tests.test_workflow_http import h


class LockedDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.args = Namespace(workspace=Path('unused'), bind='127.0.0.1', port=0,
                              attachment_host=[], locked=True)
        self.server, engine = h.startup(self.args)
        self.assertIsNone(engine)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        return response.status, dict(response.headers), response.read()

    def test_only_health_is_successful_and_no_files_or_oauth_metadata_are_exposed(self):
        code, headers, body = self.request('GET', '/healthz')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {'status': 'ok', 'mode': 'mock', 'authorization': 'locked'})
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.request('HEAD', '/healthz')[::2], (200, b''))
        for path in ('/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp'):
            code, _, body = self.request('GET', path)
            self.assertEqual(code, 503)
            self.assertNotIn(b'authorization_servers', body)
        for path in ('/', '/.env.local', '/data/derived/result.csv', '/../README.md', '/%2e%2e/.env.local'):
            self.assertEqual(self.request('GET', path)[0], 404)

    def test_initialize_list_and_every_tool_are_denied_before_dispatch(self):
        calls = [{'method': 'initialize'}, {'method': 'tools/list'}]
        calls += [{'method': 'tools/call', 'params': {'name': tool['name'], 'arguments': {}}}
                  for tool in h.w.tool_definitions()]
        with patch.object(h, 'dispatch', side_effect=AssertionError('No workflow dispatch')):
            for call in calls:
                for token in (None, 'Bearer synthetic-invalid-token'):
                    headers = {'Content-Type': 'application/json'}
                    if token:
                        headers['Authorization'] = token
                    code, response_headers, body = self.request('POST', '/mcp',
                        json.dumps({'jsonrpc': '2.0', 'id': 1, **call}), headers)
                    self.assertEqual(code, 401)
                    self.assertIn('Bearer', response_headers['WWW-Authenticate'])
                    self.assertNotIn(b'result', body)
        self.assertEqual(self.request('GET', '/mcp')[0], 401)
        self.assertEqual(self.request('HEAD', '/mcp')[0], 401)
        for method in ('PUT', 'DELETE', 'OPTIONS', 'PATCH'):
            self.assertGreaterEqual(self.request(method, '/mcp')[0], 400)

    def test_locked_startup_ignores_configuration_and_does_not_construct_workflow(self):
        forbidden = Mock(side_effect=AssertionError('Locked startup must not initialize this'))
        with patch.object(h, 'TokenVerifier', forbidden), patch.object(h.w, 'WorkflowEngine', forbidden), \
             patch.object(h.w.p, 'bind_workspace', forbidden), patch.object(h.w.attachments, 'SafeHTTPS', forbidden), \
             patch.object(h.os, 'environ', {}):
            server, engine = h.startup(self.args)
            server.server_close()
            self.assertIsNone(engine)
            forbidden.assert_not_called()
        self.args.attachment_host = ['fixture.example']
        with self.assertRaises(ValueError):
            h.startup(self.args)

    def test_transport_rejects_partial_authentication_construction(self):
        with self.assertRaises(ValueError):
            h.make_server(Mock(), None)
        with self.assertRaises(ValueError):
            h.make_server(None, Mock())
        self.args.locked = False
        with patch.object(h.w.p, 'bind_workspace'), patch.object(h.os, 'environ', {}), \
             patch.object(h.w, 'WorkflowEngine') as engine, patch.object(h, 'make_server') as server:
            with self.assertRaises(KeyError):
                h.startup(self.args)
            engine.assert_not_called()
            server.assert_not_called()


if __name__ == '__main__':
    unittest.main()
