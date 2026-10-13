"""Isolated Gate 1 MCP endpoint: one read-only, zero-argument capability tool.

No imports from the photo/model server, files, environment configuration,
network, subprocesses or UI. Use stdio; this file never opens a listener.
The receipt on stderr can be matched to a real host result after connection.
"""
import json
import sys
import uuid

VERSION = '0.1.0'
PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
TOOL_NAME = 'photo_traits_capabilities'
MAX_MESSAGE_BYTES = 16_384
CAPABILITIES = {
    'product': 'PhotoTraits',
    'adapter_version': VERSION,
    'mode': 'capabilities_only',
    'available_tools': [TOOL_NAME],
    'photo_input_enabled': False,
    'filesystem_tools_enabled': False,
    'model_api_calls_enabled': False,
    'research_data_access_enabled': False,
    'credential_access_enabled': False,
}
TOOL = {
    'name': TOOL_NAME,
    'title': 'PhotoTraits version and capabilities',
    'description': (
        'Use this to check the PhotoTraits connection and adapter version. '
        'Takes no arguments. Returns capability flags and a response receipt. '
        'Cannot read photos, files or credentials, or call model APIs. '
        'This endpoint does not perform trait analysis.'
    ),
    'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    'outputSchema': {
        'type': 'object',
        'properties': {
            **{key: {'const': value} for key, value in CAPABILITIES.items()},
            'receipt_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'},
        },
        'required': [*CAPABILITIES, 'receipt_id'],
        'additionalProperties': False,
    },
    'annotations': {
        'readOnlyHint': True,
        'destructiveHint': False,
        'idempotentHint': True,
        'openWorldHint': False,
    },
    'securitySchemes': [{'type': 'noauth'}],
    '_meta': {'securitySchemes': [{'type': 'noauth'}]},
}


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def valid_id(value):
    return type(value) is int or (isinstance(value, str) and len(value) <= 128)


class Server:
    def __init__(self, audit):
        self.audit = audit
        self.initializing = False
        self.ready = False

    def dispatch(self, request):
        if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
                or not isinstance(request.get('method'), str)
                or set(request) - {'jsonrpc', 'id', 'method', 'params'}):
            return error(None, -32600, 'Invalid request.')
        method, params = request['method'], request.get('params', {})
        if 'id' not in request:
            # Notifications never execute tools and never receive a response.
            if (method == 'notifications/initialized' and self.initializing
                    and isinstance(params, dict)):
                self.ready = True
            return None
        request_id = request['id']
        if not valid_id(request_id):
            return error(None, -32600, 'Invalid request identifier.')
        if not isinstance(params, dict) or ('_meta' in params and not isinstance(params['_meta'], dict)):
            return error(request_id, -32602, 'Expected object parameters.')
        if method == 'ping':
            result = {}
        elif method == 'initialize':
            if self.initializing:
                return error(request_id, -32600, 'Already initialized.')
            if (not isinstance(params.get('protocolVersion'), str)
                    or not isinstance(params.get('capabilities'), dict)
                    or not isinstance(params.get('clientInfo'), dict)):
                return error(request_id, -32602, 'Invalid initialize parameters.')
            version = params['protocolVersion']
            self.initializing = True
            result = {
                'protocolVersion': version if version in PROTOCOLS else PROTOCOLS[0],
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': 'photo-traits-capabilities', 'version': VERSION},
                'instructions': (
                    'This endpoint only reports PhotoTraits version and capabilities. '
                    'Call photo_traits_capabilities with {}. Do not send files, photos, '
                    'paths, URLs, credentials or research data. Trait analysis is unavailable.'
                ),
            }
        elif not self.ready:
            return error(request_id, -32000, 'Complete MCP initialization first.')
        elif method == 'tools/list':
            if set(params) - {'_meta'}:
                return error(request_id, -32602, 'This tool list has no cursor or filters.')
            result = {'tools': [TOOL]}
        elif method == 'tools/call':
            if (set(params) - {'name', 'arguments', '_meta'}
                    or params.get('name') != TOOL_NAME):
                return error(request_id, -32602, 'Unknown tool or unsupported parameters.')
            arguments = params.get('arguments', {})
            if not isinstance(arguments, dict) or arguments:
                return error(request_id, -32602, 'This tool accepts only an empty object.')
            receipt_id = uuid.uuid4().hex
            payload = {**CAPABILITIES, 'receipt_id': receipt_id}
            # Never log request arguments, client metadata, request IDs or errors.
            self.audit.write(encode({
                'event': 'capabilities_response_prepared',
                'tool': TOOL_NAME,
                'adapter_version': VERSION,
                'receipt_id': receipt_id,
            }) + '\n')
            self.audit.flush()
            result = {
                'content': [{'type': 'text', 'text': encode(payload)}],
                'structuredContent': payload,
                'isError': False,
            }
        else:
            return error(request_id, -32601, 'Method unavailable on this capabilities-only endpoint.')
        return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate key.')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('Invalid JSON constant.')


def serve(reader, writer, audit):
    server = Server(audit)
    while raw := reader.readline(MAX_MESSAGE_BYTES + 1):
        if len(raw) > MAX_MESSAGE_BYTES:
            writer.write(encode(error(None, -32600, 'Message limit exceeded; closing stream.')) + '\n')
            writer.flush()
            return 1
        try:
            request = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                                 parse_constant=reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            response = error(None, -32700, 'Invalid JSON.')
        else:
            response = server.dispatch(request)
        if response is not None:
            writer.write(encode(response) + '\n')
            writer.flush()
    return 0


def main():
    if sys.version_info < (3, 12) or len(sys.argv) != 1:
        print('Requires Python 3.12+ with no script arguments; stdio only.', file=sys.stderr)
        return 2
    return serve(sys.stdin.buffer, sys.stdout, sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main())
