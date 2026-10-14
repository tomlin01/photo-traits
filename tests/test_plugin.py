"""Offline contract/security tests. All transport outputs and photos here are synthetic."""
import base64
import copy
import csv
from datetime import date
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from scripts import traits as t
from tests.test_traits import jpeg, response, FAKE_KEY

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'plugins/photo-traits-local'
spec = importlib.util.spec_from_file_location('photo_traits_plugin', PACKAGE / 'server.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
p.t = t


def fields():
    return [
        {'name': 'surface_mark', 'definition': 'Classify the visible dark mark in the central image region.',
         'type': 'choice', 'unit': 'category', 'unknown_rule': 'choice_unknown',
         'choices': [{'value': 'present', 'description': 'A dark mark is visibly present.'},
                     {'value': 'absent', 'description': 'The visible region has no dark mark.'},
                     {'value': 'unknown', 'description': 'The region cannot be assessed.'}]},
        {'name': 'reflection', 'definition': 'A bright reflection obscures the central region.',
         'type': 'predicate', 'unit': 'model_probability', 'unknown_rule': 'human_review_required'},
        {'name': 'visible_extent', 'definition': 'Rate how much of the central region is visibly present.',
         'type': 'score', 'unit': 'ordinal_index', 'unknown_rule': 'human_review_required',
         'levels': [{'label': 'none', 'description': 'No region visible.'},
                    {'label': 'some', 'description': 'Part of region visible.'},
                    {'label': 'all', 'description': 'Whole region visible.'}]},
    ]


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'data/derived').mkdir(parents=True)
        for patcher in [patch.object(t, 'ROOT', self.root), patch.object(t, 'ensure_ignored'),
                        patch.object(t, 'PRICE_DATE', date.today().isoformat())]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.engine = p.Engine(self.root / 'data/derived/plugin-tests')
        self.engine.origin = 'http://127.0.0.1:1'
        self.s = self.engine.new()

    def prepare(self, selection=None):
        self.s.edit('Synthetic software QA, not biological evidence.', 'Synthetic fixture; single-species workflow', selection or fields(), self.s.revision)
        self.s.upload(jpeg(), 'image/jpeg', self.s.revision)
        self.s.confirm(self.s.digest(), [r['handle'] for r in self.s.photos], False)

    def finish(self):
        self.s.thread.join(3)
        self.assertFalse(self.s.thread.is_alive())

    def test_dynamic_questions_only_confirmed_names_and_types(self):
        qs = p.questions(fields())
        self.assertEqual([q['name'] for q in qs], ['surface_mark', 'reflection', 'visible_extent'])
        self.assertEqual([q['type'] for q in qs], ['choice', 'predicate', 'score'])
        self.assertEqual([q['name'] for q in p.questions(fields()[:1])], ['surface_mark'])
        for absent in ['cheek', 'eye_ring', 'sex', 'length_mm']:
            self.assertNotIn(absent, json.dumps(qs))

    def test_unknown_unit_duplicates_and_unsupported_fields(self):
        for mutated in [[], fields() * 2, [{**fields()[0], 'name': 'length_mm'}],
                        [{**fields()[2], 'unit': 'mm'}], [{**fields()[1], 'unknown_rule': 'null'}],
                        [{**fields()[0], 'choices': fields()[0]['choices'][:-1]}],
                        [{**fields()[0], 'name': 'location'}],
                        [{**fields()[0], 'definition': 'Determine the fish sex.'}],
                        [{**fields()[0], 'url': 'https://example.com/image.jpg'}]]:
            with self.assertRaises(p.Invalid): p.validate_fields(mutated)

    def test_edit_and_photo_change_invalidate_confirmation_and_stale_revision(self):
        self.prepare()
        digest, revision = self.s.digest(), self.s.revision
        self.s.edit('Changed purpose.', 'Synthetic fixture', fields()[:1], revision)
        self.assertIsNone(self.s.confirmed)
        with self.assertRaises(p.Invalid): self.s.execute(digest)
        with self.assertRaises(p.Invalid): self.s.edit('Stale', 'Fixture', fields(), revision)
        self.s.confirm(self.s.digest(), [r['handle'] for r in self.s.photos], False)
        rows = [{'handle': self.s.photos[0]['handle'], 'individual_label': 'i1', 'observation_label': 'o1', 'side': 'left'}]
        self.s.select(rows, self.s.revision)
        self.assertIsNone(self.s.confirmed)
        self.assertEqual(self.s.photos[0]['individual_label'], 'i1')
        with self.assertRaises(p.Invalid): self.s.confirm(self.s.digest(), [], False)

    def test_mock_zero_key_network_and_duplicate_execution_is_idempotent(self):
        self.prepare()
        with patch.object(t, 'load_key', side_effect=AssertionError('Forbidden key read')) as key, \
             patch.object(t, 'post_decisions', side_effect=AssertionError('Forbidden network')) as post:
            run_id = self.s.execute(self.s.digest())
            self.assertEqual(self.s.execute(self.s.digest()), run_id)
            self.finish()
            self.assertEqual(self.s.execute(self.s.digest()), run_id)
            key.assert_not_called()
            post.assert_not_called()
        self.assertEqual(self.s.status, 'completed')
        self.assertEqual(len(self.s.results), 1)
        self.assertEqual(self.s.results[0]['evidence_kind'], 'MOCK_NOT_MODEL_OUTPUT')
        self.assertTrue(all(c['value'] is None for c in self.s.results[0]['candidate'].values()))
        self.assertEqual(self.engine.reserved, 0)

    def test_deselected_photos_can_be_restored_and_relations_are_flagged(self):
        self.prepare()
        h = self.s.photos[0]['handle']
        self.assertTrue(self.s.state()['relation_warnings'])
        self.s.select([{'handle': h, 'individual_label': 'i1', 'observation_label': 'o1', 'side': 'left'}], self.s.revision)
        self.s.select([], self.s.revision)
        self.assertEqual(self.s.state()['available_photos'][0]['individual_label'], 'i1')
        self.s.select([{'handle': h, 'individual_label': 'i1', 'observation_label': 'o1', 'side': 'left'}], self.s.revision)
        self.assertEqual(len(self.s.photos), 1)
        self.assertIsNone(self.s.confirmed)

    def test_price_guard_is_rechecked_before_each_reservation(self):
        engine = p.Engine(self.root / 'data/derived/stale-price', budget='1', authorization='new-price-fixture')
        with patch.object(t, 'PRICE_DATE', '2000-01-01'), patch.object(t, 'load_key') as key:
            with self.assertRaises(p.Invalid): engine.reserve(self.s.path, 'x')
            key.assert_not_called()
        self.assertEqual(engine.reserved, 0)

    def test_no_execution_without_confirmation_or_after_cancel(self):
        with self.assertRaises(p.Invalid): self.s.execute(self.s.digest())
        self.prepare()
        self.s.cancel()
        with self.assertRaises(p.Invalid): self.s.execute(self.s.digest())
        for name in ('confirm', 'read_file', 'set_api_key', 'enable_live'):
            with self.assertRaises(p.Invalid): p.call_tool(self.engine, name, {})
        with self.assertRaises(p.Invalid):
            p.call_tool(self.engine, 'execute_confirmed', {'session_id': self.s.id, 'plan_sha256': self.s.digest(), 'confirmed': True})

    def test_jpeg_metadata_removed_even_between_scans_and_no_original_saved(self):
        metadata = b'GPS_PRIVATE_TEXT'
        segment = b'\xff\xe1' + (len(metadata)+2).to_bytes(2, 'big') + metadata
        original = jpeg()[:2] + segment + jpeg()[2:-2] + segment + jpeg()[-2:]
        clean, dimensions = p.strip_metadata(original)
        self.assertEqual(clean, jpeg())
        self.assertEqual(dimensions, [32, 32])
        self.s.upload(original, 'image/jpeg', self.s.revision)
        photo = self.s.photos[0]
        self.assertNotEqual(photo['original_sha256'], photo['sha256'])
        self.assertEqual(self.s.image(photo['handle']), clean)
        for path in self.s.path.iterdir():
            self.assertNotIn(metadata, path.read_bytes())
        with self.assertRaises(p.Invalid): self.s.upload(original, 'image/jpeg', self.s.revision)

    def test_path_url_mime_size_count_and_tampering_guards(self):
        for data, mime in [(jpeg(), 'image/png'), (b'https://example.com/x.jpg', 'image/jpeg'),
                           (b'file:///tmp/foo', 'image/jpeg'), (b'x' * (p.MAX_BYTES+1), 'image/jpeg'),
                           (jpeg()+b'TRAILER', 'image/jpeg')]:
            with self.assertRaises(p.Invalid): self.s.upload(data, mime, self.s.revision)
        with self.assertRaises(p.Invalid): self.s.image('../../.env.local')
        self.s.upload(jpeg(), 'image/jpeg', 0)
        image_path = self.s.path / (self.s.photos[0]['handle'] + '.jpg')
        image_path.write_bytes(jpeg() + b'changed')
        with self.assertRaises(p.Invalid): self.s.image(self.s.photos[0]['handle'])
        image_path.unlink()
        external = self.root / 'outside.jpg'
        external.write_bytes(jpeg())
        image_path.symlink_to(external)
        with self.assertRaises(t.SafeError): self.s.image(self.s.photos[0]['handle'])
        with patch.object(p, 'MAX_PHOTOS', 1):
            with self.assertRaises(p.Invalid): self.s.upload(jpeg().replace(b'\x10\xff', b'\x11\xff'), 'image/jpeg', self.s.revision)

    def test_live_needs_separate_operator_budget_user_approval_and_reservation(self):
        with self.assertRaises(p.Invalid): self.engine.reserve(self.s.path, 'x')
        with self.assertRaises(p.Invalid): p.Engine(self.root / 'data/derived/live-no-auth', budget='1')
        engine = p.Engine(self.root / 'data/derived/live', budget='.25', authorization='new-fixture-only')
        self.engine = engine
        self.s = engine.new()
        self.s.edit('Synthetic QA only', 'Fixture', fields(), 0)
        self.s.upload(jpeg(), 'image/jpeg', self.s.revision)
        with self.assertRaises(p.Invalid): self.s.confirm(self.s.digest(), [self.s.photos[0]['handle']], False)
        self.s.confirm(self.s.digest(), [self.s.photos[0]['handle']], True)
        bodies = []
        def fake_post(body, key):
            self.assertEqual(engine.reserved, Decimal('.25'))
            self.assertTrue(list(self.s.path.rglob('*-attempt.json')))
            self.assertEqual(key, FAKE_KEY)
            bodies.append(body)
            return response({'questions': p.questions(fields())})
        with patch.object(t, 'load_key', return_value=FAKE_KEY) as key, patch.object(t, 'post_decisions', side_effect=fake_post):
            self.s.execute(self.s.digest()); self.finish()
            key.assert_called_once()
        self.assertEqual(self.s.status, 'completed')
        self.assertEqual(set(bodies[0]), {'model', 'input', 'questions'})
        self.assertEqual(bodies[0]['model'], 'gpt-6-luna')
        self.assertEqual([q['name'] for q in bodies[0]['questions']], [f['name'] for f in fields()])
        for secret in [FAKE_KEY, 'individual_label', 'source_sex', 'source_length', 'filename']:
            self.assertNotIn(secret, json.dumps(bodies[0]))
        with self.assertRaises(p.Invalid): engine.reserve(self.s.path, 'excess')
        with self.assertRaises(FileExistsError): p.Engine(engine.root, budget='.25', authorization='new-fixture-only')
        for path in engine.root.rglob('*.json'):
            self.assertNotIn(FAKE_KEY, path.read_text())
            self.assertNotIn('data:image/', path.read_text())

    def test_transport_error_is_sanitized_reserved_and_not_retried(self):
        engine = p.Engine(self.root / 'data/derived/live-error', budget='.50', authorization='new-error-fixture')
        self.engine, self.s = engine, engine.new()
        self.s.edit('Synthetic QA only', 'Fixture', fields(), 0)
        self.s.upload(jpeg(), 'image/jpeg', self.s.revision)
        self.s.confirm(self.s.digest(), [self.s.photos[0]['handle']], True)
        with patch.object(t, 'load_key', return_value=FAKE_KEY), \
             patch.object(t, 'post_decisions', side_effect=RuntimeError(FAKE_KEY)) as post:
            run_id = self.s.execute(self.s.digest()); self.finish()
            self.assertEqual(self.s.execute(self.s.digest()), run_id)
            self.assertEqual(post.call_count, 1)
        self.assertEqual(self.s.status, 'failed')
        self.assertEqual(engine.reserved, Decimal('.25'))
        for path in engine.root.rglob('*.json'):
            self.assertNotIn(FAKE_KEY, path.read_text())

    def test_cancel_during_attempt_stops_following_photo(self):
        engine = p.Engine(self.root / 'data/derived/live-cancel', budget='1', authorization='new-cancel-fixture')
        self.engine, self.s = engine, engine.new()
        self.s.edit('Synthetic QA only', 'Fixture', fields(), 0)
        self.s.upload(jpeg(), 'image/jpeg', self.s.revision)
        self.s.upload(jpeg().replace(b'\x10\xff', b'\x11\xff'), 'image/jpeg', self.s.revision)
        self.s.confirm(self.s.digest(), [r['handle'] for r in self.s.photos], True)
        entered, release = threading.Event(), threading.Event()
        def fake_post(body, key):
            entered.set()
            release.wait(2)
            return response({'questions': body['questions']})
        with patch.object(t, 'load_key', return_value=FAKE_KEY), patch.object(t, 'post_decisions', side_effect=fake_post) as post:
            self.s.execute(self.s.digest())
            self.assertTrue(entered.wait(2))
            self.s.cancel()
            with self.assertRaises(p.Invalid): self.s.edit('Changed', 'Fixture', fields(), self.s.revision)
            release.set(); self.finish()
            self.assertEqual(post.call_count, 1)
        self.assertEqual(self.s.status, 'cancelled')
        self.assertEqual(engine.reserved, Decimal('.25'))
        self.assertEqual(len(self.s.results), 1)

    def test_response_refusal_unknown_and_invalid_values(self):
        raw = response({'questions': p.questions(fields())})
        raw['answers'][0] = {'name': 'surface_mark', 'type': 'refusal'}
        safe, candidates, cost = p.parse_response(raw, fields())
        self.assertIsNone(candidates['surface_mark']['value'])
        self.assertEqual(candidates['surface_mark']['status'], 'unknown_refusal')
        self.assertEqual(cost['input_tokens'], 1000)
        raw['answers'][1]['probability'] = float('nan')
        with self.assertRaises(p.Invalid): p.parse_response(raw, fields())
        raw = response({'questions': p.questions(fields())})
        raw['answers'][-1]['score'] = 80
        with self.assertRaises(p.Invalid): p.parse_response(raw, fields())

    def test_human_review_append_only_export_allowlist_mock_and_formula_safety(self):
        self.prepare(); self.s.execute(self.s.digest()); self.finish()
        h = self.s.photos[0]['handle']
        values = {'surface_mark': 'unknown', 'reflection': 'unknown', 'visible_extent': 'unknown'}
        self.s.review(h, values, '=reviewer', 'Synthetic human review, no biological claim.')
        self.s.review(h, {**values, 'surface_mark': 'present'}, 'Second reviewer', 'Synthetic disagreement.')
        self.assertEqual(len(self.s.reviews), 2)
        self.assertEqual(self.s.reviews[-1]['status'], 'needs_adjudication')
        self.s.review(h, values, 'Second reviewer', 'Unresolved disagreement stays flagged.')
        self.assertEqual(self.s.reviews[-1]['status'], 'needs_adjudication')
        csv_data = self.s.csv_bytes().decode('utf-8-sig')
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        self.assertEqual(len(rows), 3)
        self.assertEqual({r['evidence_kind'] for r in rows}, {'MOCK_NOT_MODEL_OUTPUT'})
        self.assertTrue(all(r['model_candidate'] == '' for r in rows))
        self.assertEqual(rows[0]['human_value'], 'unknown')
        self.assertEqual(rows[0]['identity_status'], 'unknown_do_not_merge')
        for forbidden in ['lat', 'lon', 'area', 'trapID', 'human_note', 'filename', 'api_key']:
            self.assertNotIn(forbidden, rows[0])
        self.assertEqual(t.csv_safe('=reviewer'), "'=reviewer")

    def test_http_origin_token_traversal_and_response_headers(self):
        server = p.serve_http(self.engine)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = self.engine.origin + '/s/' + self.s.id
        with opener.open(base, timeout=2) as response:
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertNotIn(self.s.token, response.read().decode())
        for suffix, headers, body in [('/state', {}, None), ('/state', {'Authorization': 'Bearer bad'}, None),
                ('/state', {'Authorization': 'Bearer '+self.s.token, 'Origin': 'https://evil.example'}, None),
                ('/../../.env.local', {'Authorization': 'Bearer '+self.s.token}, None),
                ('/cancel', {'Authorization': 'Bearer '+self.s.token, 'Content-Type': 'application/json'}, b'{}')]:
            with self.assertRaises(urllib.error.HTTPError):
                opener.open(urllib.request.Request(base+suffix, data=body, headers=headers), timeout=2)
        with opener.open(urllib.request.Request(base+'/state', headers={'Authorization': 'Bearer '+self.s.token}), timeout=2) as response:
            self.assertEqual(json.load(response)['mode'], 'mock')

    def test_stdio_lifecycle_tools_and_harmless_call(self):
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': p.PROTOCOL, 'capabilities': {}, 'clientInfo': {'name': 'offline-test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'status', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'status', 'arguments': {'api_key': FAKE_KEY}}},
        ]
        output = io.StringIO()
        with patch.object(t, 'load_key') as key, patch.object(t, 'post_decisions') as post:
            p.stdio(self.engine, io.BytesIO(('\n'.join(json.dumps(m) for m in messages)+'\n').encode()), output)
            key.assert_not_called(); post.assert_not_called()
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r['id'] for r in responses], [1, 2, 3, 4])
        self.assertEqual(responses[0]['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(len(responses[1]['result']['tools']), 6)
        self.assertEqual(responses[2]['result']['structuredContent']['mode'], 'mock')
        self.assertTrue(responses[3]['result']['isError'])
        self.assertNotIn(FAKE_KEY, output.getvalue())


class PackageTests(unittest.TestCase):
    def test_manifest_paths_identity_and_safe_default(self):
        manifest = json.loads((PACKAGE / 'plugin.json').read_text())
        self.assertEqual(manifest['name'], PACKAGE.name)
        self.assertEqual(manifest['version'], p.VERSION)
        self.assertEqual(set(manifest), {'$schema', 'name', 'version', 'description', 'author', 'extensions'})
        self.assertLessEqual(len(manifest['extensions']['com.openai']['interface']['shortDescription']), 30)
        mcp = json.loads((PACKAGE / 'mcp.json').read_text())
        config = mcp['mcpServers']['photo-traits']
        self.assertEqual(config['type'], 'stdio')
        self.assertEqual(config['cwd'], '${PLUGIN_ROOT}')
        self.assertNotIn('--allow-live', config['args'])
        self.assertNotIn('env', config)
        for path in PACKAGE.rglob('*'):
            self.assertFalse(path.is_symlink())
        skill = (PACKAGE / 'skills/plan-photo-traits/SKILL.md').read_text()
        self.assertIn('name: plan-photo-traits', skill)
        self.assertIn('description:', skill)


if __name__ == '__main__':
    unittest.main()
