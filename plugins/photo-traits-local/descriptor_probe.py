"""One-file descriptor-only MCP probe; never fetch, retain or echo file values.

Only fixed sibling transport sources are loaded at startup. No photo/workspace,
model, credential or downloader module is imported. Run behind a separately
approved, time-bounded relay; this listener itself is loopback-only.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import uuid

_spec = importlib.util.spec_from_file_location(
    'probe_transport', Path(__file__).with_name('capabilities_http.py'))
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)

VERSION = '0.1.0'
NAME = 'probe_file_descriptor'
FILE_FIELDS = ('download_url', 'file_id', 'mime_type', 'file_name')
FIXED = {'mode': 'descriptor_only', 'download_performed': False,
         'photo_transfer_verified': False, 'source_authenticated': False,
         'model_api_calls': 0}
CHECK_SCHEMA = {'type': 'object', 'properties': {
    'present': {'type': 'boolean'}, 'type_is_string': {'type': 'boolean'}},
    'required': ['present', 'type_is_string'], 'additionalProperties': False}
TOOL = {
    'name': NAME, 'title': 'Check a synthetic attachment descriptor only',
    'description': ('Use once, only for the explicitly approved public synthetic gray JPEG. '
                    'Checks file descriptor property presence and string types only. '
                    'Never downloads or opens the file, retains or returns its URL, ID or name, '
                    'or calls a model. Not a photo-transfer or authentication test.'),
    'inputSchema': {'type': 'object', 'properties': {'file': {
        'type': 'object', 'properties': {k: {'type': 'string'} for k in FILE_FIELDS},
        'required': ['download_url', 'file_id'], 'additionalProperties': False}},
        'required': ['file'], 'additionalProperties': False},
    'outputSchema': {'type': 'object', 'properties': {
        **{k: {'const': v} for k, v in FIXED.items()},
        'schema_valid': {'type': 'boolean'},
        'checks': {'type': 'object', 'properties': {k: CHECK_SCHEMA for k in FILE_FIELDS},
                   'required': list(FILE_FIELDS), 'additionalProperties': False},
        'receipt_id': {'type': ['string', 'null']}},
        'required': [*FIXED, 'schema_valid', 'checks', 'receipt_id'],
        'additionalProperties': False},
    'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                    'idempotentHint': False, 'openWorldHint': False},
    'securitySchemes': [{'type': 'noauth'}],
    '_meta': {'openai/fileParams': ['file'], 'securitySchemes': [{'type': 'noauth'}]},
}


class Probe:
    def __init__(self, audit):
        self.audit, self.consumed = audit, False  # Never retains a descriptor.

    def dispatch(self, request):
        if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
                or not isinstance(request.get('method'), str)
                or set(request) - {'jsonrpc', 'id', 'method', 'params'}):
            return h.cap.error(None, -32600, 'Invalid request.')
        if 'id' not in request:
            return None  # Notifications cannot execute the probe.
        rid, method, params = request['id'], request['method'], request.get('params', {})
        if not h.cap.valid_id(rid) or not isinstance(params, dict):
            return h.cap.error(None, -32600, 'Invalid request.')
        args = params.get('arguments', {})
        file = args.get('file', {}) if isinstance(args, dict) else {}
        # A deliberately copied file value must not escape as a JSON-RPC id.
        if isinstance(rid, str) and isinstance(file, dict) and any(
                isinstance(v, str) and v and v in rid for v in file.values()):
            return h.cap.error(None, -32600, 'Invalid request identifier.')
        if '_meta' in params and not isinstance(params['_meta'], dict):
            return h.cap.error(rid, -32602, 'Invalid parameters.')
        if method == 'initialize':
            if (not isinstance(params.get('protocolVersion'), str)
                    or not isinstance(params.get('capabilities'), dict)
                    or not isinstance(params.get('clientInfo'), dict)):
                return h.cap.error(rid, -32602, 'Invalid initialization.')
            version = params['protocolVersion']
            result = {'protocolVersion': version if version in h.HTTP_PROTOCOLS else h.HTTP_PROTOCOLS[0],
                      'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': 'photo-traits-fileparams-probe', 'version': VERSION},
                      'instructions': 'Only probe_file_descriptor exists. Use once with the explicitly approved synthetic gray image. No other files. Never print or save file URLs, IDs or names. No downloads, image analysis or model calls.'}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            if set(params) - {'_meta'}:
                return h.cap.error(rid, -32602, 'Unsupported parameters.')
            result = {'tools': [TOOL]}
        elif method == 'tools/call':
            if set(params) - {'name', 'arguments', '_meta'} or params.get('name') != NAME:
                return h.cap.error(rid, -32602, 'Only the descriptor probe is available.')
            if self.consumed:
                return h.cap.error(rid, -32000, 'The single valid probe has already completed.')
            checks = {k: {'present': isinstance(file, dict) and k in file,
                          'type_is_string': isinstance(file, dict) and isinstance(file.get(k), str)}
                      for k in FILE_FIELDS}
            valid = (isinstance(args, dict) and set(args) == {'file'} and isinstance(file, dict)
                     and {'download_url', 'file_id'} <= set(file) <= set(FILE_FIELDS)
                     and all(isinstance(v, str) for v in file.values()))
            receipt = uuid.uuid4().hex if valid else None
            payload = {**FIXED, 'schema_valid': valid, 'checks': checks, 'receipt_id': receipt}
            if valid:
                self.consumed = True
                self.audit.write(h.cap.encode({'event': 'descriptor_probe_response_prepared',
                    'tool': NAME, **payload}) + '\n')
                self.audit.flush()
            result = {'content': [{'type': 'text', 'text': h.cap.encode(payload)}],
                      'structuredContent': payload, 'isError': not valid}
        else:
            return h.cap.error(rid, -32601, 'Method unavailable.')
        return {'jsonrpc': '2.0', 'id': rid, 'result': result}

    def dispatch_json(self, raw):
        if len(raw) > h.cap.MAX_MESSAGE_BYTES:
            return 413, h.cap.error(None, -32600, 'Request too large.')
        try:
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=h.cap.unique_object,
                               parse_constant=h.cap.reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return 400, h.cap.error(None, -32700, 'Invalid JSON.')
        result = self.dispatch(value)
        return (202, None) if result is None else (200, result)


class Handler(h.CapabilityHandler):
    server_version = 'PhotoTraitsDescriptorProbe/' + VERSION

    def do_POST(self):
        if not self.valid_target():
            return
        versions = self.headers.get_all('MCP-Protocol-Version', [])
        if len(versions) > 1 or (versions and versions[0] not in h.HTTP_PROTOCOLS):
            self.reject(400)
            return
        accept = h.accepted_types(','.join(self.headers.get_all('Accept', [])))
        if not {'application/json', 'text/event-stream'} <= accept:
            self.reject(406)
            return
        types = self.headers.get_all('Content-Type', [])
        if (len(types) != 1 or types[0].split(';', 1)[0].strip().lower() != 'application/json'
                or self.headers.get_all('Content-Encoding')):
            self.reject(415)
            return
        lengths = self.headers.get_all('Content-Length', [])
        if (self.headers.get_all('Transfer-Encoding') or len(lengths) != 1
                or not lengths[0].isascii() or not lengths[0].isdecimal()):
            self.reject(400)
            return
        if len(lengths[0]) > 5 or int(lengths[0]) > h.cap.MAX_MESSAGE_BYTES:
            self.reject(413)
            return
        size = int(lengths[0])
        try:
            raw = self.rfile.read(size)
        except TimeoutError:
            self.reject(408)
            return
        if len(raw) != size:
            self.reject(400)
            return
        status, response = self.server.probe.dispatch_json(raw)
        self.respond(status, response)


class HTTPServer(h.CapabilityHTTPServer):
    def handle_error(self, request, client_address):
        self.audit.write('{"event":"descriptor_probe_transport_error"}\n')
        self.audit.flush()


def make_server(port=0, audit=None):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('Invalid port.')
    server = HTTPServer(('127.0.0.1', port), Handler)
    server.audit = sys.stderr if audit is None else audit
    server.probe = Probe(server.audit)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error('Python 3.12+ required.')
    try:
        server = make_server(args.port)
        print(h.cap.encode({'transport': 'streamable-http', 'response_mode': 'json',
                           'stateless': True, 'one_valid_probe_only': True,
                           'url': f'http://127.0.0.1:{server.server_port}/mcp'}), flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    except KeyboardInterrupt:
        pass
    except Exception:
        print('{"event":"descriptor_probe_startup_error"}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
