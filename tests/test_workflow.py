"""Single-photo acceptance and cost/error regressions using synthetic inputs only."""
import base64
import copy
import csv
from datetime import date
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from scripts import traits as t
from tests.test_attachments import a as mock_files
from tests.test_plugin import fields
from tests.test_traits import response, FAKE_KEY

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('single_photo_workflow', ROOT / 'plugins/photo-traits-local/workflow.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
w.p.t = t


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'data/derived').mkdir(parents=True)
        for patcher in (patch.object(t, 'ROOT', self.root), patch.object(t, 'ensure_ignored'),
                        patch.object(t, 'PRICE_DATE', date.today().isoformat())):
            patcher.start(); self.addCleanup(patcher.stop)
        self.now = 100.0
        self.reader = Mock()
        self.reader.download.return_value = mock_files.fixture_jpeg()
        self.engine = w.WorkflowEngine(self.root / 'data/derived/workflow', reader=self.reader, clock=lambda: self.now)
        self.principal = w.Principal('synthetic-user', frozenset(w.SCOPES))
        self.s = self.engine.new(self.principal)
        self.file = {'download_url': 'https://fixture.invalid/photo?SIGNED_SECRET', 'file_id': 'PRIVATE_FILE_ID',
                     'file_name': 'PRECISE_LOCATION.jpg', 'mime_type': 'image/jpeg'}

    def call(self, name, args=None):
        return w.call_tool(self.engine, self.principal, name, {'session_id': self.s.id, **(args or {})})

    def prepare(self, count=2):
        self.call('plan_fields', {'revision': self.s.revision, 'purpose': 'Synthetic software acceptance only.',
                                'species_scope': 'User selected single-species scope; synthetic fixture', 'fields': fields()[:count]})
        self.call('attach_photo', {'revision': self.s.revision, 'file': self.file})
        return self.call('preview_plan')['structuredContent']

    def analyze(self, offer):
        return self.call('analyze_photo', {'plan_sha256': offer['plan']['plan_sha256'],
            'confirmation_id': offer['confirmation_id'], 'human_approved_photo_and_fields': True})

    def finish(self):
        self.s.thread.join(3)
        self.assertFalse(self.s.thread.is_alive())

    def live_fixture(self, budget='.25'):
        self.store = w.p.spending.SQLiteBudgetStore.provision(self.root / 'data/derived/budget.sqlite3',
            opening_spent_upper_usd=str(Decimal('5') - Decimal(budget)), opening_evidence_sha256='a' * 64)
        transport = Mock(side_effect=lambda body: response({'questions': body['questions']}))
        self.engine = w.WorkflowEngine(self.root / 'data/derived/live', reader=self.reader,
            budget='5', authorization='SYNTHETIC-TRANSPORT-ONLY', budget_store=self.store,
            synthetic_transport=transport)
        self.s = self.engine.new(self.principal)
        self.engine.load_key = Mock(return_value=FAKE_KEY)
        self.engine.post_decisions = Mock(side_effect=AssertionError('No real transport'))

    def test_end_to_end_plan_attach_confirm_mock_review_csv(self):
        with patch.object(t, 'load_key', side_effect=AssertionError('No key')), \
             patch.object(t, 'post_decisions', side_effect=AssertionError('No model API')):
            offer = self.prepare()
            clean = self.s.image(self.s.photos[0]['handle'])
            self.assertNotIn(b'Exif', clean)
            self.assertNotIn(b'SYNTHETIC_GPS', clean)
            self.assertEqual(len(self.s.photos), 1)
            self.analyze(offer); self.finish()
            self.assertEqual(self.s.status, 'completed')
            self.assertFalse(list(self.s.path.glob('*.jpg')))
            self.assertEqual(set(self.s.results[0]['candidate']), {f['name'] for f in fields()[:2]})
            self.assertTrue(all(v['value'] is None for v in self.s.results[0]['candidate'].values()))
            self.call('review_values', {'plan_sha256': self.s.digest(), 'values': {'surface_mark': 'absent', 'reflection': 'unknown'}})
            result = self.call('export_csv')
            data = result['content'][1]['resource']['text'].encode()
            self.assertEqual(t.sha(data), result['structuredContent']['sha256'])
            rows = list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['human_value'], 'absent')
            self.assertEqual(rows[0]['candidate_status'], 'mock_unknown')
            self.assertEqual(rows[0]['evidence_kind'], 'MOCK_NOT_MODEL_OUTPUT')
            self.assertEqual(rows[0]['identity_status'], 'unknown_do_not_merge')
            self.assertEqual(rows[1]['human_value'], 'unknown')
            self.assertFalse({'lat', 'lon', 'trapID', 'area', 'file_name', 'download_url'} & rows[0].keys())
            self.assertEqual(self.engine.reserved, 0)
        for path in self.s.path.rglob('*'):
            if path.is_file():
                for private in ('SIGNED_SECRET', 'PRIVATE_FILE_ID', 'PRECISE_LOCATION', FAKE_KEY):
                    self.assertNotIn(private.encode(), path.read_bytes())

    def test_unconfirmed_stale_photo_or_schema_never_execute(self):
        offer = self.prepare()
        with self.assertRaises(w.p.Invalid): self.s.execute(self.s.digest())
        with self.assertRaises(w.p.Invalid): self.s.analyze(self.s.digest(), '', True)
        with self.assertRaises(w.p.Invalid): self.s.analyze(self.s.digest(), offer['confirmation_id'], False)
        self.s.edit('Changed definition scope.', 'Synthetic single-species fixture', fields()[:1], self.s.revision)
        with self.assertRaises(w.p.Invalid): self.analyze(offer)
        offer = self.call('preview_plan')['structuredContent']
        self.reader.download.return_value = mock_files.fixture_jpeg(True)
        self.call('attach_photo', {'revision': self.s.revision, 'file': {**self.file, 'file_id': 'OTHER'}})
        self.assertEqual(len(self.s.photos), 1)
        self.assertEqual(len(list(self.s.path.glob('*.jpg'))), 1)
        with self.assertRaises(w.p.Invalid): self.analyze(offer)
        self.assertIsNone(self.s.thread)

    def test_field_count_and_nonappearance_fields_rejected(self):
        for selection in ([], fields(), [{**fields()[0], 'name': 'length_mm'}], [{**fields()[0], 'name': 'species'}]):
            with self.assertRaises(w.p.Invalid):
                self.s.edit('Purpose', 'Single species', selection, self.s.revision)

    def test_expired_confirmation_and_session_cleanup(self):
        offer = self.prepare()
        self.now += w.CONFIRM_TTL
        with self.assertRaises(w.p.Invalid): self.analyze(offer)
        self.now += w.SESSION_TTL
        with self.assertRaises(w.p.Invalid): self.call('session_status')
        self.assertFalse(self.s.path.exists())

    def test_user_scope_and_session_ownership_are_not_model_arguments(self):
        self.prepare()
        stranger = w.Principal('other-user', frozenset(w.SCOPES))
        with self.assertRaises(w.p.Invalid): w.call_tool(self.engine, stranger, 'session_status', {'session_id': self.s.id})
        readonly = w.Principal(self.principal.subject, frozenset({'results:read'}))
        with self.assertRaises(w.p.Invalid): w.call_tool(self.engine, readonly, 'cancel_session', {'session_id': self.s.id})
        with self.assertRaises(w.p.Invalid): self.call('session_status', {'subject': self.principal.subject})
        with self.assertRaises(w.p.Invalid): self.call('read_file', {'path': '.env.local'})

    def test_duplicate_attachment_is_idempotent_and_never_stores_descriptor(self):
        self.prepare()
        revision = self.s.revision
        self.call('attach_photo', {'revision': revision, 'file': self.file})
        self.assertEqual(self.reader.download.call_count, 1)
        self.assertEqual(self.s.revision, revision)

    def test_cancel_during_download_prevents_stage_confirm_or_fee(self):
        self.s.edit('Purpose', 'Fixture', fields()[:1], 0)
        def cancelled(file, event):
            self.s.cancel()
            return mock_files.fixture_jpeg()
        self.reader.download.side_effect = cancelled
        with self.assertRaises(w.p.Invalid): self.s.attach(self.file, self.s.revision)
        self.assertFalse(self.s.photos)
        self.assertFalse(list(self.s.path.glob('*.jpg')))
        with self.assertRaises(w.p.Invalid): self.s.edit('Changed', 'Fixture', fields()[:1], self.s.revision)
        self.assertEqual(self.engine.reserved, 0)

    def test_unlisted_host_preserves_session_photo_and_preview_without_storing_descriptor(self):
        self.s.edit('Purpose', 'Fixture', fields()[:1], 0)
        resolver, connection = Mock(), Mock()
        rejecting_reader = w.attachments.SafeHTTPS(resolver=resolver, connection=connection)
        rejected = {**self.file, 'download_url': 'https://new.example/PRIVATE_PATH?PRIVATE_QUERY',
                    'file_id': 'REJECTED_FILE_ID', 'file_name': 'REJECTED_FILE_NAME.jpg'}
        def snapshot():
            return copy.deepcopy((self.s.state(), self.s.offer, self.s.last_attachment,
                                  self.s.uploaded, self.s.staging, self.engine.reserved)), {
                str(path.relative_to(self.s.path)): path.read_bytes()
                for path in self.s.path.rglob('*') if path.is_file()}
        for existing_photo in (False, True):
            if existing_photo:
                self.engine.reader = self.reader
                self.call('attach_photo', {'revision': self.s.revision, 'file': self.file})
                self.call('preview_plan')
            self.engine.reader = rejecting_reader
            before = snapshot()
            with self.subTest(existing_photo=existing_photo), \
                 patch.object(rejecting_reader, 'download', wraps=rejecting_reader.download) as download, \
                 self.assertRaises(w.attachments.HostNotAllowed):
                self.call('attach_photo', {'revision': self.s.revision, 'file': rejected})
            download.assert_not_called()
            self.assertEqual(snapshot(), before)
            for data in snapshot()[1].values():
                for private in ('new.example', 'PRIVATE_PATH', 'PRIVATE_QUERY', 'REJECTED_FILE_ID', 'REJECTED_FILE_NAME'):
                    self.assertNotIn(private.encode(), data)
        self.assertEqual(rejecting_reader.hosts, frozenset())
        resolver.assert_not_called()
        connection.assert_not_called()

    def test_live_requests_have_only_confirmed_fields_and_sanitized_photo(self):
        self.live_fixture()
        offer = self.prepare(count=1)
        clean = self.s.image(self.s.photos[0]['handle'])
        self.analyze(offer)
        self.analyze(offer)
        self.finish()
        self.analyze(offer)
        self.engine.synthetic_transport.assert_called_once()
        body = self.engine.synthetic_transport.call_args.args[0]
        self.assertEqual([q['name'] for q in body['questions']], ['surface_mark'])
        self.assertEqual(body['model'], 'gpt-6-luna')
        encoded_image = body['input'][0]['content'][1]['image_url'].split(',', 1)[1]
        self.assertEqual(base64.b64decode(encoded_image), clean)
        for private in ('SIGNED_SECRET', 'PRIVATE_FILE_ID', 'PRECISE_LOCATION', 'species_scope', 'individual_label'):
            self.assertNotIn(private, json.dumps(body))
        self.assertEqual(self.store.snapshot()['settled_upper_usd'], '0.00022')
        self.assertFalse(list(self.s.path.glob('*.jpg')))

    def test_failure_unknown_reserved_once_no_automatic_retry(self):
        self.live_fixture()
        offer = self.prepare()
        self.engine.synthetic_transport.side_effect = RuntimeError('SIGNED_SECRET ' + FAKE_KEY)
        self.analyze(offer); self.finish(); self.analyze(offer)
        self.engine.synthetic_transport.assert_called_once()
        self.assertEqual(self.s.status, 'failed')
        self.assertTrue(all(v == {'value': None, 'confidence': None, 'status': 'unknown_error'}
                            for v in self.s.results[0]['candidate'].values()))
        self.assertNotIn(FAKE_KEY, json.dumps(self.s.state()))
        self.assertEqual(self.store.snapshot()['pending_reserved_usd'], '0.25')
        self.assertFalse(list(self.s.path.glob('*.jpg')))

    def test_cancel_before_key_after_reservation_never_sends(self):
        self.live_fixture()
        offer = self.prepare()
        reserve = self.engine.reserve
        def reserve_then_cancel(*args):
            receipt = reserve(*args)
            self.s.cancel()
            return receipt
        self.engine.reserve = reserve_then_cancel
        self.analyze(offer); self.finish(); self.analyze(offer)
        self.engine.load_key.assert_not_called()
        self.engine.post_decisions.assert_not_called()
        self.assertEqual(self.s.status, 'cancelled')
        self.assertEqual(self.store.snapshot()['pending_reserved_usd'], '0.25')

    def test_cancel_in_flight_does_not_duplicate_attempt(self):
        self.live_fixture()
        offer = self.prepare()
        entered, release = threading.Event(), threading.Event()
        def in_flight(body):
            entered.set(); release.wait(2)
            return response({'questions': body['questions']})
        self.engine.synthetic_transport.side_effect = in_flight
        self.analyze(offer)
        self.assertTrue(entered.wait(2))
        self.s.cancel(); self.analyze(offer)
        release.set(); self.finish(); self.analyze(offer)
        self.engine.synthetic_transport.assert_called_once()
        self.assertEqual(self.s.status, 'cancelled')

    def test_ephemeral_live_gate_budget_exhaustion_and_restarts(self):
        with self.assertRaises(w.p.Invalid):
            w.WorkflowEngine(self.root / 'data/derived/ephemeral', budget='.25')
        with self.assertRaises(w.p.Invalid):
            w.WorkflowEngine(self.root / 'data/derived/excess', budget='5.01')
        self.live_fixture()
        offer = self.prepare(); self.analyze(offer); self.finish()
        self.s = self.engine.new(self.principal)
        offer = self.prepare(); self.analyze(offer); self.finish()
        self.engine.synthetic_transport.assert_called_once()
        self.engine.load_key.assert_not_called()
        self.assertEqual(self.s.status, 'failed')
        self.assertTrue(all(v['value'] is None for v in self.s.results[0]['candidate'].values()))
        restarted = w.WorkflowEngine(self.engine.root, budget='5', authorization='SYNTHETIC-TRANSPORT-ONLY',
            budget_store=self.store, synthetic_transport=self.engine.synthetic_transport)
        self.assertEqual(restarted.budget_status(), self.engine.budget_status())

    def test_boolean_durability_flag_cannot_unlock_any_live_path(self):
        with self.assertRaises(TypeError):
            w.WorkflowEngine(self.root / 'data/derived/not-live', budget='5',
                             authorization='scope', durable_cost_ledger=True)
        self.live_fixture()
        with self.assertRaises(w.p.Invalid), patch.object(t, 'load_key') as key:
            w.WorkflowEngine(self.engine.root, budget='5', authorization='scope', budget_store=self.store)
        key.assert_not_called()

    def test_commit_acknowledgment_loss_keeps_reservation_without_sending(self):
        self.live_fixture()
        offer = self.prepare()
        reserve = self.store.reserve
        def lost_ack(*args):
            reserve(*args)
            raise w.p.spending.BudgetError('Synthetic lost commit acknowledgment.')
        with patch.object(self.store, 'reserve', side_effect=lost_ack):
            self.analyze(offer); self.finish(); self.analyze(offer)
        self.engine.load_key.assert_not_called()
        self.engine.synthetic_transport.assert_not_called()
        self.engine.post_decisions.assert_not_called()
        self.assertEqual(self.store.snapshot()['pending_reserved_usd'], '0.25')
        self.assertEqual(self.s.status, 'failed')
        self.assertFalse(list(self.s.path.glob('*.jpg')))

    def test_storage_lost_between_reserve_and_send_blocks_transport(self):
        self.live_fixture()
        offer = self.prepare()
        reserve = self.engine.reserve
        def remove_after_reserve(*args):
            receipt = reserve(*args)
            self.store.path.rename(self.store.path.with_suffix('.unavailable'))
            return receipt
        with patch.object(self.engine, 'reserve', side_effect=remove_after_reserve):
            self.analyze(offer); self.finish()
        self.engine.synthetic_transport.assert_not_called()
        self.engine.load_key.assert_not_called()
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.s.status, 'failed')

    def test_settlement_failure_keeps_hold_and_safe_response_without_retry(self):
        self.live_fixture()
        offer = self.prepare()
        with patch.object(self.store, 'settle', side_effect=w.p.spending.BudgetError('Synthetic outage')):
            self.analyze(offer); self.finish(); self.analyze(offer)
        self.assertEqual(self.store.snapshot()['pending_reserved_usd'], '0.25')
        self.engine.synthetic_transport.assert_called_once()
        self.assertEqual(len(list(self.s.path.rglob('*-response.json'))), 1)
        self.assertTrue(all(c['value'] is None for c in self.s.results[0]['candidate'].values()))

    def test_errors_missing_usage_refusal_and_unknown_keep_safe_values(self):
        self.live_fixture(budget='5')
        for kind in ('timeout', 'http429', 'missing_usage', 'bad_model', 'refusal', 'unknown'):
            with self.subTest(kind=kind):
                self.s = self.engine.new(self.principal)
                offer = self.prepare(count=1)
                raw = response({'questions': w.p.questions(fields()[:1])})
                if kind == 'missing_usage': raw.pop('usage')
                if kind == 'bad_model': raw['model'] = 'other-model'
                if kind == 'http429': raw = {'transport_error': 'http_error', 'http_status': 429}
                if kind == 'refusal': raw['answers'] = [{'name': 'surface_mark', 'type': 'refusal'}]
                if kind == 'unknown': raw = response({'questions': w.p.questions(fields()[:1])}, {'surface_mark': 'unknown'})
                self.engine.synthetic_transport.reset_mock()
                self.engine.synthetic_transport.side_effect = TimeoutError('PRIVATE_TRANSPORT_DETAIL') if kind == 'timeout' else lambda body: raw
                self.analyze(offer); self.finish(); self.analyze(offer)
                self.engine.synthetic_transport.assert_called_once()
                self.engine.post_decisions.assert_not_called()
                self.engine.load_key.assert_not_called()
                value = self.s.results[0]['candidate']['surface_mark']['value']
                self.assertEqual(value, 'unknown' if kind == 'unknown' else None)
                self.assertFalse(list(self.s.path.glob('*.jpg')))
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['pending_attempts'], 4)
        self.assertEqual(snapshot['settled_upper_usd'], '0.00044')

    def test_model_candidate_and_human_override_remain_distinct_in_csv(self):
        self.live_fixture()
        offer = self.prepare(count=1)
        self.analyze(offer); self.finish()
        safe_response = next(self.s.path.rglob('*-response.json')).read_bytes()
        self.call('review_values', {'plan_sha256': self.s.digest(), 'values': {'surface_mark': 'absent'}})
        rows = list(csv.DictReader(io.StringIO(self.s.csv_bytes().decode('utf-8-sig'))))
        self.assertEqual(rows[0]['model_candidate'], 'present')
        self.assertEqual(rows[0]['human_value'], 'absent')
        self.assertEqual(rows[0]['evidence_kind'], 'SYNTHETIC_NOT_MODEL_OUTPUT')
        self.assertEqual(next(self.s.path.rglob('*-response.json')).read_bytes(), safe_response)
        self.assertFalse(self.s.results[0]['model_called'])

    def test_malformed_metadata_jpeg_and_unknown_review_values_rejected(self):
        self.reader.download.return_value = b'not a JPEG'
        with self.assertRaises(w.p.Invalid): self.prepare()
        self.assertFalse(self.s.photos)
        self.reader.download.return_value = mock_files.fixture_jpeg()
        offer = self.prepare(); self.analyze(offer); self.finish()
        with self.assertRaises(w.p.Invalid):
            self.call('review_values', {'plan_sha256': self.s.digest(), 'values': {'surface_mark': 'invented', 'reflection': 'unknown'}})


if __name__ == '__main__':
    unittest.main()
