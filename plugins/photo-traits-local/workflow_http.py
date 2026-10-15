"""OAuth-protected, stateless MCP transport for the single-photo workflow.

Deployment and live credentials require separate approval. CLI starts MOCK only.
Auth0 signs tokens; PyJWT/cryptography verify RS256. There is no OAuth issuer,
registration endpoint, client secret handler or refresh-token store in this app.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit

_spec = importlib.util.spec_from_file_location('phototraits_workflow', Path(__file__).with_name('workflow.py'))
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)
MAX_BODY = 100_000
# A 1 MB JPEG needs at most 1,333,336 base64 characters plus a small RPC envelope.
# Only attach_photo_bytes may exceed MAX_BODY, after authentication and parsing.
MAX_UPLOAD_BODY = 1_350_000
PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26')


class AuthorizationError(ValueError):
    pass


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON key.')
        value[key] = item
    return value


def json_bytes(data):
    def invalid(_):
        raise ValueError('Non-finite JSON.')
    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid)


class TokenVerifier:
    def __init__(self, *, issuer, resource, allowed_subject, fetch_jwks=None, clock=time.monotonic):
        import jwt  # Optional only for this HTTP deployment; offline core stays stdlib.
        self.jwt = jwt
        issuer_parts, resource_parts = urlsplit(issuer), urlsplit(resource)
        for value, parts in ((issuer, issuer_parts), (resource, resource_parts)):
            w.attachments.target(value, {w.attachments.host_name(parts.hostname)})
        if (issuer_parts.path != '/' or issuer_parts.query or resource_parts.path != '/mcp'
                or resource_parts.query or not isinstance(allowed_subject, str) or not 1 <= len(allowed_subject) <= 256):
            raise AuthorizationError('Explicit issuer, resource and immutable user subject required.')
        self.issuer, self.resource, self.allowed_subject = issuer, resource, allowed_subject
        self.clock, self.lock = clock, threading.Lock()
        self.keys, self.expires, self.next_refresh = {}, 0, 0
        reader = w.attachments.SafeHTTPS([issuer_parts.hostname])
        self.fetch_jwks = fetch_jwks or (lambda: reader.read(issuer + '.well-known/jwks.json',
            limit=65536, mime_types=('application/json', 'application/jwk-set+json')))

    def signing_key(self, kid):
        with self.lock:
            now = self.clock()
            if now >= self.expires or (kid not in self.keys and now >= self.next_refresh):
                if now < self.next_refresh:
                    raise AuthorizationError('Authorization unavailable.')
                self.next_refresh = now + 30
                rows = json_bytes(self.fetch_jwks())['keys']
                if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
                    raise AuthorizationError('Authorization unavailable.')
                keys = {}
                for row in rows:
                    if (not isinstance(row, dict) or row.get('kty') != 'RSA' or row.get('use') not in (None, 'sig')
                            or row.get('alg') not in (None, 'RS256')):
                        continue
                    name = row.get('kid')
                    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name) or name in keys:
                        raise AuthorizationError('Authorization unavailable.')
                    key = self.jwt.PyJWK.from_dict(row, algorithm='RS256').key
                    if not 2048 <= key.key_size <= 4096:
                        raise AuthorizationError('Authorization unavailable.')
                    keys[name] = key
                self.keys, self.expires = keys, now + 300
            if self.clock() >= self.expires or kid not in self.keys:
                raise AuthorizationError('Authorization unavailable.')
            return self.keys[kid]

    def verify(self, token):
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 16384:
                raise ValueError()
            header = self.jwt.get_unverified_header(token)
            if (header.get('alg') != 'RS256' or header.get('typ') not in (None, 'JWT', 'at+jwt')
                    or any(k in header for k in ('jku', 'jwk', 'x5u', 'x5c', 'crit', 'b64'))
                    or not isinstance(header.get('kid'), str)
                    or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', header['kid'])):
                raise ValueError()
            claims = self.jwt.decode(token, self.signing_key(header['kid']), algorithms=['RS256'],
                issuer=self.issuer, audience=self.resource, leeway=0,
                options={'require': ['exp', 'iat', 'iss', 'aud', 'sub'], 'verify_signature': True})
            if (claims['sub'] != self.allowed_subject or type(claims['exp']) is not int
                    or type(claims['iat']) is not int or not isinstance(claims.get('scope'), str)
                    or len(claims['scope']) > 500):
                raise ValueError()
            return w.Principal(claims['sub'], frozenset(claims['scope'].split()))
        except Exception:
            raise AuthorizationError('A valid token for the approved user and resource is required.') from None


def error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


class TemporaryResourceDiagnostics:
    """Remove after the approved UI check: at most 12 request pairs / 30 minutes.

    Never retain raw input. HTTP/RPC values describe dispatch, not client receipt.
    Only authenticated, bounded resource requests reach this observer.
    """
    MAX_REQUESTS, MAX_SECONDS, MAX_LINE_BYTES = 12, 1800, 256

    def __init__(self, *, clock=time.monotonic, sink=None):
        self.clock = clock
        self.expires = clock() + self.MAX_SECONDS
        self.remaining = self.MAX_REQUESTS
        self.lock = threading.Lock()
        self.sink = sink or (lambda line: print(line, file=sys.stderr, flush=True))

    def begin(self, raw, started):
        if len(raw) > MAX_BODY:
            return None
        try:
            request = json_bytes(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        if not isinstance(request, dict) or request.get('method') not in ('resources/read', 'resources/list'):
            return None
        params = request.get('params', {})
        record = {'method': request['method'],
                  'uri': w.PICKER_URI if isinstance(params, dict) and params.get('uri') == w.PICKER_URI else '[redacted]',
                  'meta_present': isinstance(params, dict) and '_meta' in params}
        with self.lock:
            if self.remaining == 0 or self.clock() >= self.expires:
                return None
            self.remaining -= 1
        ticket = (record, started)
        self.emit(ticket, None, 'pending')
        return ticket

    def emit(self, ticket, status, rpc_status):
        if ticket is None:
            return
        record, started = ticket
        # All output values are enums, bounded numbers, a boolean, or the fixed URI.
        row = {**record, 'elapsed_ms': min(3_600_000, max(0, int((self.clock() - started) * 1000))),
               'http_status': status if type(status) is int and status in (200, 400, 403, 500) else None,
               'rpc_status': rpc_status if rpc_status in ('pending', 'ok', -32700, -32600, -32601, -32602, -32603, -32001) else 'unknown'}
        line = 'PT_RESOURCE_DIAG ' + json.dumps(row, separators=(',', ':'), ensure_ascii=True)
        if len(line.encode('ascii')) <= self.MAX_LINE_BYTES:
            try:
                self.sink(line)
            except Exception:
                pass  # A diagnostic sink must not affect the request or leak exceptions.


def dispatch(engine, principal, raw):
    if len(raw) > MAX_UPLOAD_BODY:
        return 400, error(None, -32600, 'Request too large.')
    try:
        request = json_bytes(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return 400, error(None, -32700, 'Invalid JSON.')
    if not isinstance(request, dict):
        return 400, error(None, -32600, 'Invalid request.')
    request_id = request.get('id')
    good_id = ((type(request_id) is int and 0 <= request_id <= 2**31-1)
               or (isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id)))
    if request.get('jsonrpc') != '2.0' or request.keys() - {'jsonrpc', 'id', 'method', 'params'}:
        return 400, error(None, -32600, 'Invalid request.')
    method, params = request.get('method'), request.get('params', {})
    if len(raw) > MAX_BODY and not (method == 'tools/call' and isinstance(params, dict)
                                     and params.get('name') == 'attach_photo_bytes'):
        return 400, error(None, -32600, 'Request too large.')
    if method == 'notifications/initialized' and 'id' not in request and params == {}:
        return 202, None
    if not good_id or not isinstance(params, dict):
        return 400, error(None, -32600, 'Invalid request.')
    if method == 'initialize':
        result = {'protocolVersion': params.get('protocolVersion') if params.get('protocolVersion') in PROTOCOLS else PROTOCOLS[0],
                  'capabilities': {'tools': {'listChanged': False},
                                   'resources': {'subscribe': False, 'listChanged': False},
                                   'extensions': {'io.modelcontextprotocol/ui': {'mimeTypes': [w.PICKER_MIME]}}},
                  'serverInfo': {'name': 'PhotoTraits', 'version': '0.2.0'},
                  'instructions': 'One photo and one or two user-confirmed visible traits. Show preview and rubric; require human approval before analysis and corrections. Do not invent approval. MOCK is never model output.'}
    elif method == 'ping' and params == {}:
        result = {}
    elif method == 'tools/list' and params == {}:
        result = {'tools': w.tool_definitions()}
    elif method in ('resources/list', 'resources/read'):
        if not isinstance(principal, w.Principal) or not set(w.SCOPES) <= principal.scopes:
            return 403, error(request_id, -32001, 'Required authorization scope is missing.')
        allowed = {'_meta'} if method == 'resources/list' else {'uri', '_meta'}
        if params.keys() - allowed or ('_meta' in params and not isinstance(params['_meta'], dict)):
            return 400, error(request_id, -32602, 'Invalid UI resource parameters.')
        resource = {'uri': w.PICKER_URI, 'name': 'PhotoTraits photo picker', 'mimeType': w.PICKER_MIME}
        if method == 'resources/list':
            result = {'resources': [resource]}
        elif params.get('uri') == w.PICKER_URI:
            # Only a static packaged resource; never turn a URI into a filesystem path.
            result = {'contents': [{'uri': w.PICKER_URI, 'mimeType': w.PICKER_MIME,
                'text': Path(__file__).with_name('photo_picker.html').read_text(encoding='utf-8'),
                '_meta': {'ui': {'prefersBorder': True,
                                'csp': {'connectDomains': [], 'resourceDomains': [], 'frameDomains': []}}}}]}
        else:
            return 400, error(request_id, -32602, 'Unknown UI resource.')
    elif method == 'tools/call':
        try:
            # MCP permits omitted arguments and request metadata. Metadata is not
            # a tool input or an authority signal and is never forwarded or stored.
            w.p.exact(params, ['name'], ['arguments', '_meta'])
            if '_meta' in params and not isinstance(params['_meta'], dict):
                w.p.reject('Request metadata must be an object.')
            if not isinstance(params['name'], str):
                raise ValueError()
            definition = next((d for d in w.tool_definitions() if d['name'] == params['name']), None)
            if definition and not set(definition['securitySchemes'][0]['scopes']) <= principal.scopes:
                return 403, error(request_id, -32001, 'Required authorization scope is missing.')
            result = w.call_tool(engine, principal, params['name'], params.get('arguments', {}))
        except w.attachments.HostNotAllowed as exc:
            details = {'candidate_host': exc.candidate_host, 'download_performed': False}
            result = {'isError': True, 'content': [{'type': 'text', 'text': w.p.t.encoded(details).decode()}],
                      'structuredContent': details}
        except (w.p.Invalid, w.attachments.DownloadError) as exc:
            result = {'isError': True, 'content': [{'type': 'text', 'text': str(exc)}]}
        except Exception:
            result = {'isError': True, 'content': [{'type': 'text', 'text': 'Operation failed safely. No automatic retry.'}]}
    else:
        return 200, error(request_id, -32601, 'Unsupported method.')
    return 200, {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def public_discovery(issuer, resource):
    """Validate explicit public URLs without fetching keys or enabling authorization."""
    issuer_parts, resource_parts = urlsplit(issuer), urlsplit(resource)
    for value, parts in ((issuer, issuer_parts), (resource, resource_parts)):
        w.attachments.target(value, {w.attachments.host_name(parts.hostname)})
    if (issuer_parts.path != '/' or issuer_parts.query or resource_parts.path != '/mcp'
            or resource_parts.query):
        raise ValueError('Discovery requires an exact HTTPS issuer and /mcp resource.')
    return {'resource': resource, 'authorization_servers': [issuer],
            'scopes_supported': list(w.SCOPES), 'bearer_methods_supported': ['header']}


def make_server(engine, verifier, address=('127.0.0.1', 0), *, discovery=None):
    locked = verifier is None
    if locked and engine is not None:
        raise ValueError('Locked transport must not have a workflow engine.')
    if not locked and engine is None:
        raise ValueError('Authenticated transport requires a workflow engine.')
    if discovery is not None and not locked:
        raise ValueError('Public-only discovery is restricted to locked transport.')
    metadata = public_discovery(*discovery) if discovery is not None else (
        {'resource': verifier.resource, 'authorization_servers': [verifier.issuer],
         'scopes_supported': list(w.SCOPES), 'bearer_methods_supported': ['header']} if not locked else None)
    resource = urlsplit(verifier.resource) if not locked else None
    origin = resource.scheme + '://' + resource.netloc if resource else None
    public_resource = urlsplit(metadata['resource']) if metadata else None
    metadata_url = (public_resource.scheme + '://' + public_resource.netloc
                    + '/.well-known/oauth-protected-resource') if public_resource else None
    diagnostics = TemporaryResourceDiagnostics() if not locked and engine.mode == 'mock' else None
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version, sys_version = 'PhotoTraits/0.2', ''

        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, *_):
            pass

        def reply(self, status, value=None):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() if value is not None else b''
            self.close_connection = True
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Connection', 'close')
            if status in (401, 403):
                reason = 'invalid_token' if status == 401 else 'insufficient_scope'
                if metadata_url is None:
                    challenge = 'Bearer realm="PhotoTraits", error="invalid_token"'
                else:
                    challenge = ('Bearer resource_metadata="' + metadata_url + '", scope="photo:analyze results:read", '
                                 'error="' + reason + '"')
                    if not locked:
                        challenge += ', error_description="Sign in with the approved user and scopes"'
                self.send_header('WWW-Authenticate', challenge)
            if status == 405:
                self.send_header('Allow', 'POST')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def send_error(self, code, message=None, explain=None):
            self.reply(code, {'error': 'Request rejected.'})

        def handle_expect_100(self):
            self.send_error(417)
            return False

        def target_ok(self):
            if locked:
                # Only explicit public metadata/health/denials; never reflect request hosts.
                return True
            hosts, origins = self.headers.get_all('Host', []), self.headers.get_all('Origin', [])
            if hosts != [resource.netloc] or len(origins) > 1 or (origins and origins[0] not in (origin, 'https://chatgpt.com')):
                self.send_error(403)
                return False
            return True

        def do_GET(self):
            if not self.target_ok():
                return
            if self.path in ('/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp'):
                if metadata is None:
                    return self.reply(503, {'error': 'Authorization is not configured.'})
                self.reply(200, metadata)
            elif self.path == '/healthz':
                self.reply(200, {'status': 'ok', 'mode': 'mock' if locked else engine.mode,
                                 'authorization': 'locked' if locked else 'configured'})
            else:
                self.send_error((401 if locked else 405) if self.path == '/mcp' else 404)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            started = time.monotonic()
            if not self.target_ok():
                return
            if self.path != '/mcp':
                return self.send_error(404)
            if locked:
                # Reject before reading the body or parsing any supplied token.
                return self.send_error(401)
            auth = self.headers.get_all('Authorization', [])
            try:
                if len(auth) != 1 or not auth[0].startswith('Bearer '):
                    raise AuthorizationError()
                principal = verifier.verify(auth[0][7:])  # Every request, including list/initialize.
            except AuthorizationError:
                return self.send_error(401)
            lengths = self.headers.get_all('Content-Length', [])
            if (self.headers.get_all('Transfer-Encoding') or self.headers.get_all('Content-Encoding')
                    or self.headers.get_all('Content-Type', []) != ['application/json']
                    or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,7}', lengths[0])
                    or not 0 < int(lengths[0]) <= MAX_UPLOAD_BODY):
                return self.send_error(400)
            if self.headers.get('MCP-Protocol-Version', PROTOCOLS[0]) not in PROTOCOLS:
                return self.send_error(400)
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                return self.send_error(400)
            ticket = diagnostics.begin(raw, started) if diagnostics is not None else None
            try:
                status, result = dispatch(engine, principal, raw)
            except Exception:
                if diagnostics is not None:
                    diagnostics.emit(ticket, 500, -32603)
                raise
            if diagnostics is not None:
                diagnostics.emit(ticket, status, result.get('error', {}).get('code', 'ok') if isinstance(result, dict) else 'ok')
            if status == 403 and isinstance(result, dict) and result.get('error', {}).get('code') == -32001:
                result = {'jsonrpc': '2.0', 'id': result['id'], 'result': {'isError': True,
                    'content': [{'type': 'text', 'text': 'Required authorization scope is missing.'}],
                    '_meta': {'mcp/www_authenticate': ['Bearer resource_metadata="' + metadata_url + '", '
                        'error="insufficient_scope", error_description="Sign in with the approved scopes", '
                        'scope="photo:analyze results:read"']}}}
            self.reply(status, result)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        slots = threading.BoundedSemaphore(8)
        def service_actions(self):
            if engine is not None:
                engine.periodic_cleanup()

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

        def handle_error(self, *_):
            pass  # No traceback containing tokens/arguments/results.
    server = Server(address, Handler)
    return server


def startup(args):
    discovery = None
    if args.discovery_issuer is not None or args.discovery_resource is not None:
        if not args.locked or not args.discovery_issuer or not args.discovery_resource:
            raise ValueError('Both discovery URLs require locked startup.')
        discovery = (args.discovery_issuer, args.discovery_resource)
    if args.locked:
        if args.attachment_host:
            raise ValueError('Locked startup cannot accept attachment hosts.')
        # Public URLs only; no workspace binding, environment/key lookup, reader or engine.
        return make_server(None, None, (args.bind, args.port), discovery=discovery), None
    w.p.bind_workspace(args.workspace)
    # Public configuration only. Never look up OPENAI_API_KEY or .env.local.
    verifier = TokenVerifier(issuer=os.environ['PHOTOTRAITS_OAUTH_ISSUER'],
        resource=os.environ['PHOTOTRAITS_RESOURCE'], allowed_subject=os.environ['PHOTOTRAITS_ALLOWED_SUB'])
    engine = w.WorkflowEngine(args.workspace / w.p.TRANSIENT_ROOT,
        reader=w.attachments.SafeHTTPS(args.attachment_host, context=w.p.t.tls_context()))
    return make_server(engine, verifier, (args.bind, args.port)), engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--attachment-host', action='append', default=[])
    parser.add_argument('--locked', action='store_true',
                        help='Serve health, optional public discovery and denials; all workflow tools stay disabled.')
    parser.add_argument('--discovery-issuer', help='Public HTTPS issuer, including its trailing slash; requires --locked.')
    parser.add_argument('--discovery-resource', help='Public canonical HTTPS /mcp URL; requires --locked.')
    args = parser.parse_args()
    try:
        server, engine = startup(args)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            if engine is not None:
                engine.close()
    except Exception:
        parser.exit(1, 'Workflow unavailable: check approved configuration and dependencies locally. No credentials were read.\n')


if __name__ == '__main__':
    main()
