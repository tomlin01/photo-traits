"""Loopback-only, stateless Streamable HTTP for the one capability tool.

POST /mcp returns JSON (never SSE); GET /mcp returns 405. No static files,
credentials, model client, photo server or network forwarding are loaded.
The only source loaded at startup is the adjacent capabilities_server.py.
"""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
from pathlib import Path
from socketserver import TCPServer
import sys


# Resolve one fixed sibling even with Python -I -S; never add cwd to sys.path.
_spec = importlib.util.spec_from_file_location(
    'photo_traits_capabilities', Path(__file__).with_name('capabilities_server.py'))
cap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cap)

HTTP_PROTOCOLS = tuple(version for version in cap.PROTOCOLS if version >= '2025-03-26')
ENDPOINT = '/mcp'
DEFAULT_PORT = 8766


def dispatch_json(raw, audit):
    """No session state is retained; initialization is not an access token."""
    try:
        request = json.loads(raw.decode('utf-8'), object_pairs_hook=cap.unique_object,
                             parse_constant=cap.reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return 400, cap.error(None, -32700, 'Invalid JSON.')
    server = cap.Server(audit)
    if isinstance(request, dict) and request.get('method') != 'initialize':
        # Streamable HTTP permits stateless operation without session IDs.
        server.initializing = server.ready = True
    response = server.dispatch(request)
    if response is None:
        return 202, None
    if (isinstance(request, dict) and request.get('method') == 'initialize'
            and 'result' in response
            and response['result']['protocolVersion'] not in HTTP_PROTOCOLS):
        response['result']['protocolVersion'] = HTTP_PROTOCOLS[0]
    status = 400 if response.get('error', {}).get('code') == -32600 else 200
    return status, response


def accepted_types(value):
    result = set()
    for item in value.split(','):
        media, *parameters = (part.strip().lower() for part in item.split(';'))
        quality = 1.0
        for parameter in parameters:
            if parameter.startswith('q='):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        if 0 < quality <= 1:
            result.add(media)
    return result


class CapabilityHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'PhotoTraitsCapabilities/' + cap.VERSION
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_args):
        pass  # Never log request paths, headers, submitted bodies or credentials.

    def respond(self, status, payload=None):
        body = cap.encode(payload).encode('utf-8') if payload is not None else b''
        self.close_connection = True
        self.send_response(status)
        if payload is not None:
            self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if status == 405:
            self.send_header('Allow', 'POST')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def reject(self, status):
        self.respond(status, {'error': 'HTTP request rejected.'})

    def send_error(self, code, message=None, explain=None):
        self.reject(code)  # Base-handler errors must not reflect request text.

    def handle_expect_100(self):
        self.reject(417)
        return False

    def valid_target(self):
        port = self.server.server_port
        allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        hosts = self.headers.get_all('Host', [])
        origins = self.headers.get_all('Origin', [])
        if len(hosts) != 1 or hosts[0].lower() not in allowed_hosts:
            self.reject(403)
            return False
        if (len(origins) > 1 or (origins and origins[0] not in
                               {f'http://{host}' for host in allowed_hosts})):
            self.reject(403)
            return False
        if self.path != ENDPOINT:
            self.reject(404)
            return False
        return True

    def do_POST(self):
        if not self.valid_target():
            return
        versions = self.headers.get_all('MCP-Protocol-Version', [])
        if len(versions) > 1 or (versions and versions[0] not in HTTP_PROTOCOLS):
            self.reject(400)
            return
        # MCP clients list both types; this server chooses JSON exclusively.
        accept = accepted_types(','.join(self.headers.get_all('Accept', [])))
        if not {'application/json', 'text/event-stream'} <= accept:
            self.reject(406)
            return
        content_types = self.headers.get_all('Content-Type', [])
        if (len(content_types) != 1 or
                content_types[0].split(';', 1)[0].strip().lower() != 'application/json'
                or self.headers.get_all('Content-Encoding')):
            self.reject(415)
            return
        if self.headers.get_all('Transfer-Encoding'):
            self.reject(400)
            return
        lengths = self.headers.get_all('Content-Length', [])
        if not lengths:
            self.reject(411)
            return
        if (len(lengths) != 1 or not lengths[0].isascii()
                or not lengths[0].isdecimal()):
            self.reject(400)
            return
        if len(lengths[0]) > len(str(cap.MAX_MESSAGE_BYTES)):
            self.reject(413)
            return
        length = int(lengths[0])
        if length > cap.MAX_MESSAGE_BYTES:
            self.reject(413)
            return
        try:
            raw = self.rfile.read(length)
        except TimeoutError:
            self.reject(408)
            return
        if len(raw) != length:
            self.reject(400)
            return
        status, response = dispatch_json(raw, self.server.audit)
        self.respond(status, response)

    def unsupported(self):
        if self.valid_target():
            self.reject(405)

    do_GET = do_HEAD = do_DELETE = do_OPTIONS = do_PUT = do_PATCH = unsupported


class CapabilityHTTPServer(HTTPServer):
    def server_bind(self):
        # HTTPServer normally does a hostname lookup; no DNS is needed here.
        TCPServer.server_bind(self)
        self.server_name = '127.0.0.1'
        self.server_port = self.server_address[1]

    def handle_error(self, request, client_address):
        # No traceback containing submitted data or local file paths.
        self.audit.write('{"event":"capabilities_http_transport_error"}\n')
        self.audit.flush()


def make_server(port=DEFAULT_PORT, audit=None):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('Port must be between 0 and 65535.')
    server = CapabilityHTTPServer(('127.0.0.1', port), CapabilityHandler)
    server.audit = sys.stderr if audit is None else audit
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help='Loopback port (default: 8766; 0 chooses a free port).')
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error('Requires Python 3.12 or newer.')
    try:
        server = make_server(args.port)
    except (OSError, ValueError):
        parser.exit(1, 'Could not start the loopback capabilities server.\n')
    print(cap.encode({'transport': 'streamable-http', 'response_mode': 'json',
                      'stateless': True,
                      'url': f'http://127.0.0.1:{server.server_port}{ENDPOINT}'}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
