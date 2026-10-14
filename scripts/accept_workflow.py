"""One offline acceptance entry point. No install, public listener, key or model call.

Run with the project-local Python environment described in the root README.
The complete dev check includes short-lived loopback tests and real pinned
metadata verification; generated workflow artifacts contain synthetic data only.
"""
import base64
import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'plugins/photo-traits-local'


def module(name):
    spec = importlib.util.spec_from_file_location('acceptance_' + name, PACKAGE / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def main():
    if sys.version_info < (3, 12):
        raise RuntimeError('Python 3.12+ required.')
    dependencies = {name: importlib.metadata.version(name) for name in ('PyJWT', 'cryptography', 'cffi', 'pycparser')}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    evidence = ROOT / '.local' / ('workflow-acceptance-' + stamp)
    evidence.mkdir(mode=0o700, parents=True)
    for command in ('init', 'check'):
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/dev.py'), command], cwd=ROOT,
                             text=True, capture_output=True)
        (evidence / (command + '.log')).write_text(run.stdout + run.stderr)
        print(run.stdout.strip())
        if run.returncode or 'skipped=' in run.stderr:
            raise RuntimeError('Acceptance incomplete; inspect the local check log (all tests, including OAuth, are required).')
        if command == 'check':
            match = re.search(r'Ran (\d+) tests', run.stderr)
            if not match:
                raise RuntimeError('Missing complete test evidence.')
            test_count = int(match[1])
            print(f'Synthetic/unit regression tests: {test_count} passed, no skips.')
    w, fixtures = module('workflow'), module('attachment_mock')
    w.p.bind_workspace(ROOT)
    class FixtureReader:
        def check_descriptor(self, file):
            w.attachments.target(w.attachments.descriptor(file), {'synthetic.invalid'})

        def download(self, _file, _cancelled):
            return fixtures.fixture_jpeg()
    engine = w.WorkflowEngine(ROOT / 'data/derived' / ('workflow-acceptance-' + stamp), reader=FixtureReader())
    def forbidden(*_):
        raise AssertionError('Acceptance cannot read a key or call a model.')
    engine.load_key = engine.post_decisions = forbidden
    principal = w.Principal('synthetic-acceptance-user', frozenset(w.SCOPES))
    def call(name, args):
        return w.call_tool(engine, principal, name, args)
    state = call('new_session', {})['structuredContent']
    sid = state['session_id']
    fields = [{'name': 'dark_mark', 'definition': 'A dark mark is visibly present in the central region.',
        'type': 'choice', 'unit': 'category', 'unknown_rule': 'choice_unknown', 'choices': [
            {'value': 'present', 'description': 'A distinct dark mark is visible.'},
            {'value': 'absent', 'description': 'The visible region contains no distinct dark mark.'},
            {'value': 'unknown', 'description': 'The region cannot be assessed.'}]},
        {'name': 'reflection', 'definition': 'A bright reflection obscures the central region.',
         'type': 'predicate', 'unit': 'model_probability', 'unknown_rule': 'human_review_required'}]
    state = call('plan_fields', {'session_id': sid, 'revision': state['revision'],
        'purpose': 'Software acceptance on a generated gray fixture; no biological evidence.',
        'species_scope': 'Synthetic fixture for a user-selected single-species workflow', 'fields': fields})['structuredContent']
    call('attach_photo', {'session_id': sid, 'revision': state['revision'],
        'file': {'download_url': 'https://synthetic.invalid/gray.jpg', 'file_id': 'synthetic-only', 'mime_type': 'image/jpeg'}})
    offer = call('preview_plan', {'session_id': sid})['structuredContent']
    args = {'session_id': sid, 'plan_sha256': offer['plan']['plan_sha256'],
            'confirmation_id': offer['confirmation_id'], 'human_approved_photo_and_fields': True}
    call('analyze_photo', args)
    session = engine.sessions[sid]
    session.thread.join(3)
    call('analyze_photo', args)
    if session.status != 'completed' or engine.reserved or list(session.path.glob('*.jpg')):
        raise RuntimeError('Mock lifecycle or cleanup assertion failed.')
    call('review_values', {'session_id': sid, 'plan_sha256': session.digest(),
                          'values': {'dark_mark': 'absent', 'reflection': 'unknown'}})
    exported = call('export_csv', {'session_id': sid})
    csv_bytes = exported['content'][1]['resource']['text'].encode()
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode('utf-8-sig'))))
    if len(rows) != 2 or any(r['evidence_kind'] != 'MOCK_NOT_MODEL_OUTPUT' for r in rows):
        raise RuntimeError('Synthetic CSV assertion failed.')
    csv_path = engine.root / 'synthetic-results.csv'
    w.p.t.write_new(csv_path, csv_bytes)
    sources = [PACKAGE / name for name in ('server.py', 'attachments.py', 'workflow.py', 'workflow_http.py',
        'requirements-http.txt', 'capabilities_http.py', 'capabilities_server.py', 'descriptor_probe.py')]
    sources += [ROOT / 'scripts/traits.py']
    report = {'timestamp_utc': stamp, 'status': 'PASS_LOCAL_SYNTHETIC_ONLY', 'test_count': test_count,
              'dependencies': dependencies, 'model_api_calls': 0, 'real_credentials_read': False,
              'real_photo_downloads': 0, 'public_service_started': False, 'host_acceptance': 'NOT_RUN',
              'photos_remaining': 0, 'csv_sha256': hashlib.sha256(csv_bytes).hexdigest(),
              'csv_path': str(csv_path), 'review_values_are_scripted_fixtures': True,
              'sources_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}
    (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Report: ' + str(evidence / 'report.json'))
    print('Synthetic CSV: ' + str(csv_path))
    print('True ChatGPT web/desktop/iOS single-photo analysis remains unverified. Live cloud mode is disabled.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit('Acceptance incomplete. Check Python/dependencies and .local/workflow-acceptance-*/check.log; no deployment or model request was authorized.')
