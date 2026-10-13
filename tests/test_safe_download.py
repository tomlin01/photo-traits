"""No real URLs, DNS queries, private data, credentials or sockets in these tests."""
from email.message import Message
import importlib.util
from pathlib import Path
import socket
import ssl
import threading
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('safe_attachments', Path(__file__).resolve().parents[1] / 'plugins/photo-traits-local/attachments.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class Response:
    def __init__(self, data=b'JPEG_FIXTURE', status=200, headers=None):
        self.data, self.status = data, status
        self.headers = Message()
        for name, value in headers or [('Content-Type', 'image/jpeg'), ('Content-Length', str(len(data)))]:
            self.headers[name] = value

    def read1(self, count):
        result, self.data = self.data[:count], self.data[count:]
        return result


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.conn = Mock()
        self.conn.getresponse.return_value = Response()
        self.resolve = Mock(return_value=['1.1.1.1'])
        self.connect = Mock(return_value=self.conn)
        self.reader = a.SafeHTTPS(['attachments.example'], resolver=self.resolve,
                                 connection=self.connect, clock=lambda: self.now)
        self.file = {'download_url': 'https://attachments.example/photo?signature=PRIVATE_QUERY',
                     'file_id': 'PRIVATE_ID', 'mime_type': 'image/jpeg', 'file_name': 'PRIVATE_NAME.jpg'}

    def fetch(self):
        return self.reader.download(self.file, threading.Event())

    def test_exact_https_public_pinned_connection_no_extra_headers(self):
        self.assertEqual(self.fetch(), b'JPEG_FIXTURE')
        self.assertEqual(self.connect.call_args.args[:2], ('attachments.example', '1.1.1.1'))
        self.conn.request.assert_called_once_with('GET', '/photo?signature=PRIVATE_QUERY',
            headers={'Accept': 'image/jpeg', 'Accept-Encoding': 'identity'})
        self.conn.close.assert_called_once()

    def test_unapproved_urls_rejected_before_dns_and_errors_never_echo_values(self):
        urls = ['file:///private/key', 'http://attachments.example/a', 'https://127.0.0.1/a',
                'https://[::1]/a', 'https://169.254.169.254/metadata', 'https://attachments.example.evil/a',
                'https://attachments.example@evil.example/a', 'https://evil.example@attachments.example/a',
                'https://attachments.example:8443/a', 'https://attachments.example/a#secret',
                'https://attachments.example./a', 'https://ATTACHMENTS.example/a',
                'https://attachments.example\\@evil.example/a', 'https://attachments.example/\r\nsecret',
                'https://attachments.example/圖', 'data:image/jpeg;base64,AA']
        for url in urls:
            self.file['download_url'] = url
            with self.subTest(url=url), self.assertRaises(a.DownloadError) as caught:
                self.fetch()
            self.assertNotIn(url, str(caught.exception))
        self.resolve.assert_not_called()
        self.connect.assert_not_called()

    def test_private_mixed_mapped_and_rebinding_addresses_fail_closed(self):
        for addresses in [['10.0.0.1'], ['127.0.0.1'], ['::1'], ['169.254.169.254'],
                          ['1.1.1.1', '192.168.1.1'], ['::ffff:1.1.1.1'], ['2002:7f00:1::'],
                          ['224.0.0.1'], [], ['not-an-ip']]:
            self.resolve.return_value = addresses
            with self.subTest(addresses=addresses), self.assertRaises(a.DownloadError):
                self.fetch()
        self.connect.assert_not_called()

    def test_tls_uses_numeric_address_and_original_server_name(self):
        context = Mock()
        conn = a.PinnedHTTPS('attachments.example', '1.1.1.1', 10, context)
        with patch.object(socket, 'socket') as factory, patch.object(socket, 'getaddrinfo', side_effect=AssertionError('Rebinding')):
            raw = factory.return_value
            conn.connect()
            raw.connect.assert_called_once_with(('1.1.1.1', 443))
            context.wrap_socket.assert_called_once_with(raw, server_hostname='attachments.example')

    def test_status_redirect_mime_encoding_truncation_and_oversize(self):
        cases = [Response(status=302), Response(status=403), Response(status=404),
                 Response(headers=[('Content-Type', 'text/html')]),
                 Response(headers=[('Content-Type', 'image/jpeg'), ('Content-Encoding', 'gzip')]),
                 Response(headers=[('Content-Type', 'image/jpeg'), ('Content-Length', '1000001')]),
                 Response(headers=[('Content-Type', 'image/jpeg'), ('Content-Length', '4'), ('Content-Length', '4')]),
                 Response(headers=[('Content-Type', 'image/jpeg'), ('Content-Length', '15')]),
                 Response(headers=[('Content-Type', 'image/jpeg'), ('Transfer-Encoding', 'chunked'), ('Content-Length', '12')]),
                 Response(b'x' * (a.MAX_BYTES+1), headers=[('Content-Type', 'image/jpeg')])]
        for case in cases:
            self.conn.getresponse.return_value = case
            with self.subTest(status=case.status), self.assertRaises(a.DownloadError):
                self.fetch()
        self.assertEqual(self.connect.call_count, len(cases))  # No redirect/retry.

    def test_cancel_before_dns_and_total_deadline(self):
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(a.DownloadError):
            self.reader.download(self.file, cancelled)
        self.resolve.assert_not_called()
        def slow_dns(*_):
            self.now += 16
            return ['1.1.1.1']
        self.resolve.side_effect = slow_dns
        with self.assertRaises(a.DownloadError):
            self.fetch()
        self.connect.assert_not_called()

    def test_no_permissive_tls_and_no_download_by_default(self):
        with self.assertRaises(a.DownloadError):
            a.SafeHTTPS(context=ssl._create_unverified_context())
        with patch.object(socket, 'getaddrinfo', side_effect=AssertionError('No network')):
            with self.assertRaises(a.DownloadError):
                a.SafeHTTPS().download(self.file, threading.Event())

    def test_descriptor_shape_control_chars_and_mime(self):
        cases = [{}, {**self.file, 'path': '/tmp/x'}, {**self.file, 'file_id': 3},
                 {**self.file, 'file_name': 'x\x00PRIVATE_NAME'}, {**self.file, 'mime_type': 'image/png'},
                 {**self.file, 'download_url': 'x'*8193}]
        for case in cases:
            with self.assertRaises(a.DownloadError):
                self.reader.download(case, threading.Event())
        self.resolve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
