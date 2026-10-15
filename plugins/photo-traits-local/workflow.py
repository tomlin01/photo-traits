"""One photo / one confirmed attempt, using the existing Decisions/review core.

The optional picker accepts bounded JPEG bytes, never paths or new download hosts.
The HTTP layer supplies the authenticated principal, never tool arguments.
Live requires an injected, verified durable budget store. CLI remains MOCK only.
"""
import base64
import binascii
import copy
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import secrets
import time
import uuid


def sibling(name):
    spec = importlib.util.spec_from_file_location('phototraits_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p, attachments = sibling('server'), sibling('attachments')
SESSION_TTL = p.SESSION_TTL
EXPORT_TTL = p.EXPORT_TTL
CONFIRM_TTL = 300
SCOPES = ('photo:analyze', 'results:read')
MAX_BASE64_CHARS = 4 * ((attachments.MAX_BYTES + 2) // 3)
PICKER_URI = 'ui://photo-traits/photo-picker-v1.html'
PICKER_MIME = 'text/html;profile=mcp-app'
PICKER_SCHEMA = {'type': 'object', 'properties': {
    'session_id': {'type': 'string'}, 'revision': {'type': 'integer'},
    'mode': {'type': 'string', 'const': 'mock'}, 'jpeg_max_bytes': {'type': 'integer'},
    'has_photo': {'type': 'boolean'}, 'expires_at_unix': {'type': 'number'}},
    'required': ['session_id', 'revision', 'mode', 'jpeg_max_bytes', 'has_photo', 'expires_at_unix'],
    'additionalProperties': False}


def decode_photo_bytes(value):
    """Strict, canonical base64; never interpret a URL, filename or data URI."""
    if not isinstance(value, str) or not 4 <= len(value) <= MAX_BASE64_CHARS or len(value) % 4:
        p.reject('Select one JPEG at most 1,000,000 bytes; invalid byte encoding.')
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        p.reject('Select one JPEG at most 1,000,000 bytes; invalid byte encoding.')
    if not 0 < len(data) <= attachments.MAX_BYTES or base64.b64encode(data).decode('ascii') != value:
        p.reject('Select one JPEG at most 1,000,000 bytes; invalid byte encoding.')
    return data


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset


class PhotoSession(p.Session):
    def __init__(self, engine, owner):
        super().__init__(engine)
        self.owner = owner
        self.offer = None
        self.staging = False
        self.last_attachment = None

    def editable(self, revision):
        super().editable(revision)
        if self.run_id or self.cancelled.is_set() or self.staging:
            p.reject('This attempt is closed or busy. Start a new session only for a newly approved analysis.')

    def invalidate(self):
        self.offer = None
        super().invalidate()

    def edit(self, purpose, species_scope, fields, revision):
        if not isinstance(fields, list) or not 1 <= len(fields) <= 2:
            p.reject('Confirm one or two visible-appearance fields for this single-photo workflow.')
        super().edit(purpose, species_scope, fields, revision)

    def attach(self, file, revision):
        attachments.descriptor(file)
        fingerprint = p.t.sha(p.t.encoded(file))
        self._stage_photo(revision, fingerprint,
                          lambda: self.engine.reader.check_descriptor(file),
                          lambda: self.engine.reader.download(file, self.cancelled))

    def picker_state(self, revision):
        with self.lock:
            self.editable(revision)
            if self.engine.mode != 'mock' or not self.fields:
                p.reject('The photo picker requires a planned MOCK session.')
            return {'session_id': self.id, 'revision': self.revision, 'mode': 'mock',
                    'jpeg_max_bytes': attachments.MAX_BYTES, 'has_photo': bool(self.photos),
                    'expires_at_unix': self.retention()['expires_at_unix']}

    def attach_bytes(self, value, revision):
        # Check owner (in call_tool), mode and revision before decoding a large value.
        with self.lock:
            self.picker_state(revision)
            data = decode_photo_bytes(value)
        return self._stage_photo(revision, 'bytes:' + p.t.sha(data), lambda: None, lambda: data)

    def _stage_photo(self, revision, fingerprint, preflight, read):
        """Stage safely and return the revision captured under this write's lock."""
        with self.lock:
            self.editable(revision)
            if not self.fields:
                p.reject('Discuss and define one or two fields before attaching a photo.')
            if self.last_attachment == (fingerprint, self.revision):
                return self.revision
            preflight()  # No DNS, download or session changes on an unlisted host.
            self.staging = True
            self.offer = None
        try:
            data = read()
            cleaned, dimensions = p.strip_metadata(data)
            with self.lock:
                self.available()
                if self.cancelled.is_set() or self.revision != revision:
                    p.reject('Attachment cancelled or plan changed. No photo was staged.')
                original_hash = p.t.sha(data)
                if self.photos and self.photos[0]['original_sha256'] == original_hash:
                    self.last_attachment = (fingerprint, self.revision)
                    return self.revision
                handle = uuid.uuid4().hex
                p.t.write_new(self.path / (handle + '.jpg'), cleaned)
                old = list(self.uploaded)
                photo = {'handle': handle, 'original_sha256': original_hash, 'sha256': p.t.sha(cleaned),
                         'dimensions': dimensions, 'individual_label': '', 'observation_label': '', 'side': 'unknown'}
                self.uploaded, self.photos = {handle: copy.deepcopy(photo)}, [photo]
                self.invalidate()
                self.last_attachment = (fingerprint, self.revision)
                self.engine.storage.purge_photos(self.id, self.lease, old)
                return self.revision
        finally:
            with self.lock:
                self.staging = False
            self.engine.cleanup()

    def prepare(self):
        with self.lock:
            self.editable(self.revision)
            if not self.fields or len(self.photos) != 1:
                p.reject('Define one or two fields and attach exactly one JPEG first.')
            data = self.image(self.photos[0]['handle'])
            nonce = secrets.token_urlsafe(24)
            self.offer = (self.digest(), nonce, self.engine.clock() + CONFIRM_TTL)
            return {'plan': self.state(), 'confirmation_id': nonce,
                    'confirmation_expires_in_seconds': CONFIRM_TTL,
                    'requires_human': 'Show the sanitized preview and complete field rubric; ask the user to approve this exact analysis.',
                    'preview_validation': 'JPEG markers and dimensions checked; human must confirm that the displayed image decodes and is the intended photo.',
                    'notice': 'Appearance estimates only. No physical size, identity, species classification or biological verification.',
                    'human_consent_enforcement': 'Host tool approval and conversation; the server cannot prove intent from model-supplied arguments.'}, data

    def analyze(self, digest, confirmation_id, approved):
        with self.lock:
            self.available()
            if digest != self.digest():
                p.reject('Photo or schema changed. Preview and confirm the current plan again.')
            if self.run_id:
                return self.run_id  # Includes cancelled/failed: never creates a second paid attempt.
            self.editable(self.revision)
            if (approved is not True or not self.offer or self.offer[0] != digest
                    or not isinstance(confirmation_id, str) or not secrets.compare_digest(self.offer[1], confirmation_id)
                    or self.engine.clock() >= self.offer[2]):
                p.reject('Current photo and field definitions require fresh human confirmation.')
            self.confirm(digest, [self.photos[0]['handle']], approved, via='chatgpt_host_approval_required')
            self.offer = None
            return self.execute(digest)

    def purge_photos(self):
        try:
            self.engine.storage.purge_photos(self.id, self.lease, self.uploaded)
        except (OSError, p.transient.UnsafeStorage):
            self.clear_requested = True
            self.cancelled.set()

    def work(self):
        try:
            super().work()
        finally:
            with self.lock:
                self.purge_photos()

    def cancel(self):
        super().cancel()
        with self.lock:
            self.offer = None
            if self.status != 'running':
                self.purge_photos()


class WorkflowEngine(p.Engine):
    def __init__(self, root, *, reader=None, clock=time.monotonic, budget='0', authorization=None,
                 budget_store=None, synthetic_transport=None, per_call_maximum_usd='0.25'):
        super().__init__(root, budget=budget, authorization=authorization, budget_store=budget_store,
                         synthetic_transport=synthetic_transport, per_call_maximum_usd=per_call_maximum_usd,
                         clock=clock)
        self.reader = reader or attachments.SafeHTTPS()

    def new(self, principal):
        with self.sessions_lock:
            self.cleanup()
            if self.closed or len(self.sessions) >= 8:
                p.reject('Session limit reached or process closing; wait for expiry.')
            session = PhotoSession(self, principal.subject)
            self.sessions[session.id] = session
            return session

    def owned(self, principal, sid, *, allow_clearing=False):
        session = self.get(sid, allow_clearing=allow_clearing)
        if session.owner != principal.subject:
            p.reject('Unknown or expired session for this signed-in user.')
        return session


READ_TOOLS = {'workflow_status', 'session_status', 'preview_plan', 'export_csv'}


def tool_definitions():
    original = {t['name']: copy.deepcopy(t) for t in p.tool_definitions()}
    sid = {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}
    digest = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
    revision = {'type': 'integer', 'minimum': 0}
    def tool(name, description, properties):
        scopes = ['results:read'] if name in READ_TOOLS else list(SCOPES)
        return {'name': name, 'description': description, 'inputSchema': {'type': 'object', 'properties': properties,
                'required': list(properties), 'additionalProperties': False},
                'annotations': {'readOnlyHint': name in READ_TOOLS and name != 'export_csv',
                                'destructiveHint': name in ('cancel_session', 'clear_session', 'export_csv'),
                                'openWorldHint': name in ('attach_photo', 'analyze_photo'),
                                'idempotentHint': name in ('analyze_photo', 'cancel_session', 'clear_session')},
                'securitySchemes': [{'type': 'oauth2', 'scopes': scopes}]}
    fields = original['plan_fields']['inputSchema']['properties']['fields']
    fields['maxItems'] = 2
    definitions = [
        tool('workflow_status', 'Read workflow limits and runtime mode. Does not read keys or photos.', {}),
        tool('new_session', 'Start one private temporary single-photo attempt; no model call.', {}),
        tool('plan_fields', 'Discuss and define exactly one or two visible traits within the user-selected single species. Edits invalidate confirmation; never invent human consent.',
             {'session_id': sid, 'revision': revision, 'purpose': {'type': 'string', 'maxLength': 1000},
              'species_scope': {'type': 'string', 'maxLength': 120}, 'fields': fields}),
        tool('attach_photo', 'Stage the user-selected JPEG attachment after discussing fields. HTTPS download and metadata removal only, no model call. A replacement invalidates confirmation. Never invent a file reference.',
             {'session_id': sid, 'revision': revision, 'file': copy.deepcopy(attachments.FILE_SCHEMA)}),
        tool('open_photo_picker', 'Open the optional local-file picker after planning fields in a MOCK session. The human selects one JPEG; picking a file does not approve analysis. Actual ChatGPT host support is not yet verified.',
             {'session_id': sid, 'revision': revision}),
        tool('attach_photo_bytes', 'App-only: stage one user-selected JPEG as canonical base64. No URLs, filenames, downloads, model calls or automatic retries. Replacement invalidates confirmation.',
             {'session_id': sid, 'revision': revision,
              'jpeg_base64': {'type': 'string', 'minLength': 4, 'maxLength': MAX_BASE64_CHARS}}),
        tool('preview_plan', 'Show sanitized photo and complete rubric to the human before requesting approval. Never treat model arguments as independent proof of consent.', {'session_id': sid}),
        tool('analyze_photo', 'Only after the human confirms the displayed photo and every field/rubric, execute this exact plan once. Requires host approval. No automatic retry, extra fields or budget override.',
             {'session_id': sid, 'plan_sha256': digest, 'confirmation_id': {'type': 'string', 'maxLength': 64},
              'human_approved_photo_and_fields': {'type': 'boolean', 'const': True}}),
        tool('session_status', 'Read candidates and progress. Unknown/error/refusal stays unknown; model confidence is not biological validation.', {'session_id': sid}),
        tool('review_values', 'Record only corrections explicitly supplied by the human, using the displayed rubric. Keep candidates separate. Never invent human observations.',
             {'session_id': sid, 'plan_sha256': digest, 'values': {'type': 'object', 'minProperties': 1, 'maxProperties': 2,
                 'additionalProperties': {'type': 'string', 'maxLength': 64}}}),
        tool('export_csv', 'Generate allowlisted CSV bytes as an embedded text/csv resource. First generation leaves at most five minutes to review/save; repeated exports never extend expiry. Preserve exact values in a downloadable file when supported. Generation does not confirm delivery or saving.', {'session_id': sid}),
        tool('cancel_session', 'Cancel and delete temporary photo. An in-flight request may finish; it remains reserved and is never automatically retried.', {'session_id': sid}),
        tool('clear_session', 'Clear this signed-in user’s temporary session, including plans, results and human corrections. Returns pending while an attachment or model attempt stops. Does not erase budget accounting or external copies.', {'session_id': sid}),
    ]
    for definition in definitions:
        definition['_meta'] = {'securitySchemes': copy.deepcopy(definition['securitySchemes'])}
    next(v for v in definitions if v['name'] == 'attach_photo')['_meta']['openai/fileParams'] = ['file']
    picker = next(v for v in definitions if v['name'] == 'open_photo_picker')
    picker['_meta']['ui'] = {'resourceUri': PICKER_URI}
    picker['annotations']['readOnlyHint'] = True
    upload = next(v for v in definitions if v['name'] == 'attach_photo_bytes')
    upload['_meta']['ui'] = {'visibility': ['app']}
    upload['annotations']['destructiveHint'] = True  # Replaces the temporary photo.
    for definition in (picker, upload):
        definition['outputSchema'] = copy.deepcopy(PICKER_SCHEMA)
    return definitions


def call_tool(engine, principal, name, args):
    definitions = {d['name']: d for d in tool_definitions()}
    if name not in definitions:
        p.reject('Unknown tool.')
    required_scopes = set(definitions[name]['securitySchemes'][0]['scopes'])
    if not isinstance(principal, Principal) or not required_scopes <= principal.scopes:
        p.reject('Required authorization scope is missing.')
    p.exact(args, definitions[name]['inputSchema']['required'])
    content = []
    if name == 'workflow_status':
        result = {'mode': engine.mode, 'model': p.t.MODEL, 'max_fields': 2, 'max_photos': 1,
                  'jpeg_max_bytes': attachments.MAX_BYTES, 'automatic_retries': 0,
                  'retention_seconds': SESSION_TTL, 'photo_deleted_after_attempt': True,
                  'after_first_export_seconds': EXPORT_TTL, 'cleanup_interval_seconds': p.SWEEP_INTERVAL,
                  'storage': 'temporary_server_session_only',
                  'live_host_acceptance': False, 'cost_reserved_usd': str(engine.reserved),
                  'project_budget': engine.budget_status(),
                  'per_call_maximum_usd': str(p.t.REQUEST_RESERVE_USD)}
    elif name == 'new_session':
        result = engine.new(principal).state()
    else:
        s = engine.owned(principal, args['session_id'], allow_clearing=name in ('clear_session', 'session_status'))
        if name == 'plan_fields':
            s.edit(args['purpose'], args['species_scope'], args['fields'], args['revision'])
        elif name == 'attach_photo':
            s.attach(args['file'], args['revision'])
        elif name == 'open_photo_picker':
            result = s.picker_state(args['revision'])
        elif name == 'attach_photo_bytes':
            committed_revision = s.attach_bytes(args['jpeg_base64'], args['revision'])
            # Reject a stale completion instead of acknowledging another upload.
            result = s.picker_state(committed_revision)
        elif name == 'preview_plan':
            result, data = s.prepare()
            content = [{'type': 'image', 'data': base64.b64encode(data).decode(), 'mimeType': 'image/jpeg'}]
        elif name == 'analyze_photo':
            s.analyze(args['plan_sha256'], args['confirmation_id'], args['human_approved_photo_and_fields'])
        elif name == 'review_values':
            with s.lock:
                if args['plan_sha256'] != s.digest():
                    p.reject('Review belongs to a different plan.')
                s.review(s.photos[0]['handle'] if s.photos else '', args['values'], 'authenticated-user', 'Explicit human correction in ChatGPT.')
        elif name == 'cancel_session':
            s.cancel()
        elif name == 'clear_session':
            result = engine.clear(s)
        elif name == 'export_csv':
            data = s.csv_bytes()
            result = {'filename': 'phototraits-results.csv', 'mime_type': 'text/csv', 'sha256': p.t.sha(data),
                      'status': 'generated', 'user_save_confirmed': False, 'retention': s.retention(),
                      'bytes': len(data), 'evidence_kind': 'MOCK_NOT_MODEL_OUTPUT' if engine.mode == 'mock' else 'human_review_required'}
            content = [{'type': 'resource', 'resource': {'uri': 'phototraits://' + s.id + '/results.csv',
                         'mimeType': 'text/csv', 'text': data.decode('utf-8')}}]
        if name not in ('preview_plan', 'export_csv', 'clear_session', 'open_photo_picker', 'attach_photo_bytes'):
            result = s.state()
    return {'content': [{'type': 'text', 'text': p.t.encoded(result).decode()}] + content,
            'structuredContent': result, 'isError': False}
