"""Local-only fileParams rehearsal: generated JPEGs, no remote downloader or live mode.

The file object follows the documented ChatGPT protocol. Its .invalid URL is an
in-memory fixture reference, NOT an OpenAI download endpoint or host credential.
This module is deliberately absent from the installed Gate 1 capability server.
"""
import argparse
import copy
import importlib.util
from pathlib import Path
import secrets
import sys
import threading
import time
import uuid

_spec = importlib.util.spec_from_file_location('photo_traits_local', Path(__file__).with_name('server.py'))
p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p)

FIXTURE_TTL_SECONDS = 300  # Local policy, not a claim about ChatGPT URL lifetime.
FILE_SCHEMA = {
    'type': 'object',
    'properties': {'download_url': {'type': 'string', 'maxLength': 8192},
                   'file_id': {'type': 'string', 'maxLength': 256},
                   'mime_type': {'type': 'string', 'maxLength': 100},
                   'file_name': {'type': 'string', 'maxLength': 256}},
    'required': ['download_url', 'file_id'], 'additionalProperties': False,
}


def fixture_jpeg(alternate=False):
    """Encode a complete 8x8 constant gray baseline JPEG using only stdlib.

    One DC coefficient plus EOB, no research pixels. APP1/COM are deliberately
    synthetic metadata markers for verifying removal, not real geolocation.
    """
    def segment(marker, value):
        return bytes([255, marker]) + (len(value) + 2).to_bytes(2, 'big') + value
    dc = bytes([0, 1, 1] + [0] * 14 + [0, 4])
    ac = bytes([16, 1] + [0] * 15 + [0])
    return (b'\xff\xd8'
            + segment(0xE1, b'Exif\0\0SYNTHETIC_GPS_TEST_ONLY')
            + segment(0xFE, b'ARTIFICIAL_NON_SENSITIVE_FIXTURE')
            + segment(0xDB, bytes([0] + [1] * 64))
            + segment(0xC0, bytes([8, 0, 8, 0, 8, 1, 1, 0x11, 0]))
            + segment(0xC4, dc + ac)
            + segment(0xDA, bytes([1, 1, 0, 0, 63, 0]))
            + (b'\xa1' if alternate else b'\x3f') + b'\xff\xd9')


class FixtureFiles:
    """Exact session/id/url grants; never resolves DNS, redirects or filesystem paths."""
    def __init__(self, clock=time.monotonic):
        self.clock, self.grants = clock, {}

    def issue(self, session):
        files = []
        for alternate in (False, True):
            file_id = 'fixture_' + uuid.uuid4().hex
            url = 'https://phototraits-fixture.invalid/' + secrets.token_urlsafe(24)
            self.grants[(session.id, file_id)] = {
                'url': url, 'data': fixture_jpeg(alternate), 'mime': 'image/jpeg',
                'expires': self.clock() + FIXTURE_TTL_SECONDS,
            }
            files.append({'file_id': file_id, 'download_url': url, 'mime_type': 'image/jpeg',
                          'file_name': 'synthetic-gray-' + str(int(alternate)) + '.jpg'})
        return files

    def resolve(self, session, file):
        p.exact(file, ['download_url', 'file_id'], ['mime_type', 'file_name'])
        for key, value in file.items():
            limit = FILE_SCHEMA['properties'][key]['maxLength']
            if (not isinstance(value, str) or not value or len(value) > limit
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                p.reject('Invalid file reference. Values are never logged.')
        if file.get('mime_type', 'image/jpeg') != 'image/jpeg':
            p.reject('Only JPEG file inputs are supported in this rehearsal.')
        key = (session.id, file['file_id'])
        grant = self.grants.get(key)
        if (not grant or not secrets.compare_digest(grant['url'].encode(), file['download_url'].encode())
                or session.cancelled.is_set()):
            p.reject('Only this session\'s generated fixture is available; remote downloads are disabled.')
        if self.clock() >= grant['expires']:
            self.grants.pop(key, None)
            p.reject('Fixture reference expired; start a new session. No URL is refreshed or retried.')
        if grant['mime'] != 'image/jpeg' or not 0 < len(grant['data']) <= p.MAX_BYTES:
            p.reject('Fixture content failed its MIME or size limit.')
        # Neither the supplied name nor the URL is interpreted as a local path.
        return grant['data'], grant['mime']

    def revoke(self, session_id):
        for key in list(self.grants):
            if key[0] == session_id:
                del self.grants[key]


class MockSession(p.Session):
    def cancel(self):
        with self.lock:
            self.engine.files.revoke(self.id)
            super().cancel()


class MockEngine(p.Engine):
    def __init__(self, root, *, clock=time.monotonic):
        super().__init__(root)  # No budget, authorization or live argument accepted.
        self.files = FixtureFiles(clock)

    def new(self):
        if len(self.sessions) >= 8:
            p.reject('At most eight sessions per local process.')
        session = MockSession(self)
        self.sessions[session.id] = session
        return session


def tool_definitions():
    tools = p.tool_definitions()
    for tool in tools:
        tool['annotations']['openWorldHint'] = False
        if tool['name'] == 'new_session':
            tool['description'] = 'Create a local mock session with two generated JPEG fileParams fixtures and the existing local review UI. No host files or network.'
        elif tool['name'] == 'execute_confirmed':
            tool['description'] = 'Execute only a current plan explicitly confirmed in the local UI. MOCK ONLY: never calls a model or loads a credential.'
    tools.append({
        'name': 'stage_chatgpt_file', 'title': 'Stage a synthetic fileParams fixture',
        'description': 'Local mock rehearsal only. Accept the file object issued by new_session; real ChatGPT URLs and arbitrary files are rejected. Staging invalidates plan confirmation and never confirms or executes it.',
        'inputSchema': {'type': 'object', 'properties': {
            'session_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'},
            'revision': {'type': 'integer', 'minimum': 0}, 'file': copy.deepcopy(FILE_SCHEMA)},
            'required': ['session_id', 'revision', 'file'], 'additionalProperties': False},
        'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False},
        '_meta': {'openai/fileParams': ['file']},
    })
    return tools


def call_tool(engine, name, args):
    if not isinstance(engine, MockEngine) or engine.mode != 'mock' or engine.budget != 0:
        p.reject('Attachment rehearsal is mock-only; live mode is unavailable.')
    if name == 'stage_chatgpt_file':
        p.exact(args, ['session_id', 'revision', 'file'])
        session = engine.get(args['session_id'])
        with session.lock:
            session.editable(args['revision'])
            data, mime = engine.files.resolve(session, args['file'])
            session.upload(data, mime, args['revision'])
            engine.files.grants.pop((session.id, args['file']['file_id']))
            return session.state()
    result = p.call_tool(engine, name, args)
    if name == 'status':
        result.update({'photo_input': 'generated_fileparams_fixtures_or_local_picker',
                       'chat_attachment_bridge': 'local_mock_contract_only',
                       'real_host_attachment_verified': False, 'remote_downloads_enabled': False})
    elif name == 'new_session':
        result.update({'fixture_files': engine.files.issue(engine.get(result['session_id'])),
                       'fixture_reference_ttl_seconds': FIXTURE_TTL_SECONDS})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--stdio', action='store_true')
    mode.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    try:
        workspace = p.bind_workspace(args.workspace)
        engine = MockEngine(workspace / 'data/derived/attachment-mock-sessions')
        server = p.serve_http(engine)
        try:
            if args.stdio:
                p.stdio(engine, definitions=tool_definitions, dispatch=call_tool)
            else:
                print(p.t.encoded(call_tool(engine, 'new_session', {})).decode(), flush=True)
                threading.Event().wait()
        finally:
            server.shutdown()
            server.server_close()
    except KeyboardInterrupt:
        pass
    except Exception:
        print('Local attachment rehearsal failed; no inputs or raw exception details were logged.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
