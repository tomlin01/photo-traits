"""Temporary synthetic tests; remove with the approved resource diagnostics."""
import concurrent.futures
import json
import unittest
from unittest.mock import Mock, patch

import test_workflow_http as fixture

h = fixture.h


class TemporaryResourceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.lines = []
        self.diag = h.TemporaryResourceDiagnostics(clock=lambda: self.now, sink=self.lines.append)

    def raw(self, method='resources/read', params=None):
        return json.dumps({'jsonrpc': '2.0', 'id': 'PRIVATE_REQUEST_ID', 'method': method,
                           'params': {'uri': h.w.PICKER_URI} if params is None else params}).encode()

    def test_allowlist_redacts_all_arbitrary_input_and_bounds_values(self):
        private = {'Authorization': 'PRIVATE_TOKEN', 'filename': 'PRIVATE_FILE.jpg',
                   'session_id': 'PRIVATE_SESSION', 'jpeg_base64': 'PRIVATE_PHOTO',
                   'fields': 'PRIVATE_FIELDS', 'url': 'https://private.invalid/?signed=PRIVATE_SIGNED_URL'}
        for uri in (h.w.PICKER_URI, private['url'], 'PRIVATE_URI\nFORGED_LOG'):
            ticket = self.diag.begin(self.raw(params={'uri': uri, '_meta': private}), -1e9)
            self.diag.emit(ticket, 200, 'ok')
        for line in self.lines:
            self.assertLessEqual(len(line.encode()), self.diag.MAX_LINE_BYTES)
            self.assertNotIn('\n', line)
            self.assertNotIn('PRIVATE', line)
            self.assertNotIn('FORGED', line)
            row = json.loads(line.removeprefix('PT_RESOURCE_DIAG '))
            self.assertEqual(set(row), {'method', 'uri', 'meta_present', 'elapsed_ms', 'http_status', 'rpc_status'})
            self.assertIn(row['uri'], (h.w.PICKER_URI, '[redacted]'))
            self.assertTrue(row['meta_present'])
            self.assertLessEqual(row['elapsed_ms'], 3_600_000)
        self.assertEqual(len(self.lines), 6)

    def test_request_count_and_lifetime_bounds_under_concurrency(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            tickets = list(pool.map(lambda _: self.diag.begin(self.raw(), self.now), range(40)))
        for ticket in tickets:
            self.diag.emit(ticket, 400, -32602)
        self.assertEqual(sum(t is not None for t in tickets), self.diag.MAX_REQUESTS)
        self.assertEqual(len(self.lines), 2 * self.diag.MAX_REQUESTS)
        other = h.TemporaryResourceDiagnostics(clock=lambda: self.now, sink=self.lines.append)
        self.now += other.MAX_SECONDS
        self.assertIsNone(other.begin(self.raw(), self.now))
        self.assertEqual(len(self.lines), 2 * self.diag.MAX_REQUESTS)

    def test_nonresource_malformed_and_oversized_input_produce_no_logs(self):
        for raw in (self.raw('tools/call', {'name': 'PRIVATE_TOOL'}), b'[]', b'{PRIVATE',
                    self.raw(params={'uri': h.w.PICKER_URI, '_meta': {'private': 'x' * h.MAX_BODY}})):
            self.assertIsNone(self.diag.begin(raw, self.now))
        self.assertFalse(self.lines)

    def test_sink_failure_never_changes_dispatch_and_status_values_are_bounded(self):
        ticket = self.diag.begin(self.raw(), self.now)
        self.diag.emit(ticket, 'PRIVATE_HTTP', 'PRIVATE_RPC')
        row = json.loads(self.lines[-1].removeprefix('PT_RESOURCE_DIAG '))
        self.assertIsNone(row['http_status'])
        self.assertEqual(row['rpc_status'], 'unknown')
        self.diag.sink = Mock(side_effect=RuntimeError('PRIVATE_EXCEPTION'))
        self.diag.emit(ticket, 200, 'ok')


@unittest.skipUnless(fixture.jwt is not None, 'Requires existing HTTP OAuth test extras.')
class TemporaryResourceHTTPDiagnosticsTests(fixture.OAuthFixture, unittest.TestCase):
    request = fixture.HTTPAcceptanceTests.request

    def test_authentication_precedes_logging_and_only_resources_are_recorded(self):
        import threading
        lines = []
        engine = Mock(mode='mock')
        diagnostics = h.TemporaryResourceDiagnostics(sink=lines.append)
        with patch.object(h, 'TemporaryResourceDiagnostics', return_value=diagnostics):
            self.server = h.make_server(engine, self.verifier)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        try:
            body = {'jsonrpc': '2.0', 'id': 1, 'method': 'resources/read',
                    'params': {'uri': h.w.PICKER_URI, '_meta': {'private': 'PRIVATE_META'}}}
            self.assertEqual(self.request('POST', '/mcp', body, token=False)[0], 401)
            self.assertFalse(lines)
            self.assertEqual(self.request('POST', '/mcp', body, changes={'sub': 'other'})[0], 401)
            self.assertFalse(lines)
            self.assertEqual(self.request('POST', '/mcp', body)[0], 200)
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[-1].removeprefix('PT_RESOURCE_DIAG '))['rpc_status'], 'ok')
            self.assertEqual(self.request('POST', '/mcp', body, changes={'scope': 'results:read'})[0], 403)
            self.assertEqual(json.loads(lines[-1].removeprefix('PT_RESOURCE_DIAG '))['rpc_status'], -32001)
            self.assertNotIn('PRIVATE', ''.join(lines))
        finally:
            self.server.shutdown(); self.server.server_close(); thread.join(2)
