"""Synthetic transport/schema tests only; no API requests and no biological accuracy claims."""
import copy
import csv
from datetime import date
import io
import json
import ssl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import traits as t


FAKE_KEY = 'sk-unit-test-not-a-real-key-000000000000'


def jpeg():
    # Minimal marker fixture, not a real photograph/full decoder fixture.
    return b'\xff\xd8\xff\xc0\x00\x0b\x08\x00\x20\x00\x20\x01\x01\x11\x00\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x10\xff\x00\x20\xff\xd9'


def response(config, choices=None):
    choices = choices or {}
    answers = []
    for q in config['questions']:
        a = {'name': q['name'], 'type': q['type']}
        if q['type'] == 'predicate':
            a['probability'] = 0.2
        elif q['type'] == 'choice':
            options = [v['value'] for v in q['choices']]
            selected = choices.get(q['name'], options[0])
            a.update(choice=selected, confidence=0.9, probabilities=[
                {'value': v, 'probability': 1.0 if v == selected else 0.0} for v in options])
        else:
            a.update(score=1.0, confidence=0.8, probabilities=[
                {'value': i, 'label': level['label'], 'probability': 1.0 if i == 1 else 0.0}
                for i, level in enumerate(q['levels'])])
        answers.append(a)
    return {'model': t.MODEL, 'answers': answers,
            'usage': {'input_tokens': 1000, 'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
                      'output_tokens': 0, 'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': 1000}}


class TraitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.patches = [patch.object(t, 'ROOT', self.root), patch.object(t, 'ensure_ignored'),
                        patch.object(t, 'load_key', return_value=FAKE_KEY),
                        patch.object(t, 'PRICE_DATE', date.today().isoformat())]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.dataset = self.root / t.DATASET
        (self.dataset / 'images').mkdir(parents=True)
        self.config = t.load_questions()
        self.rows = []
        manifest = []
        for n in range(2):
            image_id = f'fixture{n}_2026'
            data = jpeg()
            ref = f'images/{image_id}.jpg'
            (self.dataset / ref).write_bytes(data)
            row = {k: '' for k in t.SOURCE_FIELDS + t.HUMAN_FIELDS}
            row.update(pit_id='00123', source_filename_year=image_id, photo_ref=ref,
                       source_date='12.10.2026', source_sightingnumb=str(n + 1), source_tagged='1',
                       source_length='100', source_sex='f', source_validated_sex='0',
                       source_side='left', source_head_w='32', source_head_h='32',
                       delivered_sha256=t.sha(data), human_review_status='unconfirmed',
                       area='DO_NOT_EXPORT_LOCATION', lat='12.3456789', trapID='PRIVATE_TRAP')
            self.rows.append(row)
            manifest.append({'photo_ref': ref, 'sha256': t.sha(data), 'pixel_dimensions': [32, 32],
                             'raw_metadata': {'ID': row['pit_id'], 'filename_year': image_id,
                                              'date': row['source_date'], 'sightingnumb': row['source_sightingnumb']}})
        with (self.dataset / 'individual-review.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(self.rows[0]))
            writer.writeheader()
            writer.writerows(self.rows)
        (self.dataset / 'source-manifest.json').write_bytes(t.encoded(manifest))
        self.audit = self.dataset.parent / 'audit.json'
        self.audit.write_bytes(t.encoded({'fixture': True, 'prior_cost_upper_usd': '0'}))
        self.run = self.dataset.parent / 'run'
        t.prepare(self.run, '0', self.audit)

    def live(self, **kwargs):
        return t.run_live(self.run, live=True, approve_upload=True,
                          transport=kwargs.pop('transport', lambda body, key: response(self.config)), **kwargs)

    def draft(self):
        entries = json.loads((self.run / 'human-review-draft.json').read_text())
        entries[0].update(human_review_status='reviewed', human_reviewer='Fixture reviewer',
                          human_review_date='2026-10-12', human_photo_side_confirmed='uncertain',
                          human_cheek_visibility='unknown', human_eye_ring_visibility='partial',
                          human_quality_flags='soft_focus', human_pattern_topology='unknown',
                          human_eye_ring_apparent_hue='yellow_pale_gold', human_notes='Synthetic review test only.')
        return entries

    def test_prepare_preserves_relations_and_original_humans(self):
        manifest, _ = t.load_run(self.run)
        self.assertEqual(len({r['individual_id'] for r in manifest['images']}), 1)
        self.assertEqual(len({r['observation_id'] for r in manifest['images']}), 2)
        self.assertTrue(all(r['split_group'] == '00123' for r in manifest['images']))
        self.assertTrue(all(r['human_original']['human_review_status'] == 'unconfirmed' for r in manifest['images']))
        for private in ['DO_NOT_EXPORT_LOCATION', '12.3456789', 'PRIVATE_TRAP']:
            self.assertNotIn(private, json.dumps(manifest))
        with self.assertRaises(FileExistsError):
            t.prepare(self.run, '0', self.audit)

    def test_no_network_or_credentials_without_both_live_flags(self):
        with patch.object(t, 'post_decisions') as post, patch.object(t, 'load_key') as key:
            for flags in [{}, {'live': True}, {'approve_upload': True}]:
                with self.assertRaises(t.SafeError):
                    t.run_live(self.run, **flags)
            post.assert_not_called()
            key.assert_not_called()

    def test_request_is_decisions_only_and_contains_no_source_labels(self):
        manifest, config = t.load_run(self.run)
        body = t.request_body(manifest['images'][0], config)
        self.assertEqual(set(body), {'model', 'input', 'questions'})
        self.assertEqual(body['model'], 'gpt-6-luna')
        self.assertEqual(t.ENDPOINT, 'https://api.openai.com/v1/decisions')
        self.assertTrue(body['input'][0]['content'][1]['image_url'].startswith('data:image/jpeg;base64,'))
        text = json.dumps(body)
        for absent in ['source_sex', 'source_length', '00123', 'fixture0', 'Authorization', FAKE_KEY, 'max_output_tokens', 'json_schema']:
            self.assertNotIn(absent, text)
        for wrong in ['gpt-4.1-mini', 'gpt-4.1-mini-2025-04-14']:
            with self.assertRaises(t.SafeError):
                t.post_decisions(dict(body, model=wrong), FAKE_KEY)
        for field, value in [('endpoint', 'https://api.openai.com/v1/responses'), ('model', 'gpt-4.1-mini')]:
            changed = copy.deepcopy(manifest)
            changed[field] = value
            (self.run / 'manifest.json').write_bytes(t.encoded(changed))
            with self.assertRaises(t.SafeError):
                self.live()
        (self.run / 'manifest.json').write_bytes(t.encoded(manifest))

    def test_unknown_and_visibility_suppression_preserve_raw(self):
        raw = response(self.config, {'cheek_visibility': 'unassessable', 'eye_ring_hue': 'unknown'})
        original = copy.deepcopy(raw)
        result = t.parse_answers(raw, self.config)
        self.assertIsNone(result['candidate']['cheek_pattern'])
        self.assertEqual(result['candidate']['eye_ring_hue'], 'unknown')
        self.assertEqual(result['suppressed_due_to_visibility'], ['cheek_pattern'])
        self.assertEqual(raw, original)

    def test_refusal_invalid_probabilities_and_wrong_score(self):
        raw = response(self.config)
        raw['answers'][0] = {'type': 'refusal', 'name': 'glare'}
        parsed = t.parse_answers(raw, self.config)
        self.assertEqual(parsed['status'], 'partial_refusal')
        self.assertIsNone(parsed['candidate']['glare'])
        raw['answers'][-1]['score'] = 100
        with self.assertRaises(t.SafeError):
            t.parse_answers(raw, self.config)
        raw = response(self.config)
        raw['answers'][0]['probability'] = float('nan')
        with self.assertRaises(t.SafeError):
            t.parse_answers(raw, self.config)
        raw = response(self.config)
        raw['answers'][-1] = raw['answers'][0]
        with self.assertRaises(t.SafeError):
            t.parse_answers(raw, self.config)

    def test_run_once_resume_and_budget_accounting(self):
        calls = []
        def transport(body, key):
            calls.append(body)
            return response(self.config)
        self.live(max_new=1, transport=transport)
        self.live(max_new=12, transport=transport)
        self.live(max_new=12, transport=transport)
        self.assertEqual(len(calls), 2)
        summary = t.summary(self.run)
        self.assertEqual(summary['attempts'], 2)
        self.assertEqual(summary['human_reviewed'], 0)
        self.assertEqual(summary['cost_upper_including_unresolved_attempts_and_prior_usd'], '0.00044')
        result = t.read_result(self.run, 'fixture0_2026')
        self.assertEqual(result['transport_kind'], 'synthetic_fixture')

    def test_unknown_prior_cost_and_budget_stop(self):
        manifest, _ = t.load_run(self.run)
        for prior in [None, '0.90']:
            manifest['prior_cost_upper_usd'] = prior
            (self.run / 'manifest.json').write_bytes(t.encoded(manifest))
            with patch.object(t, 'post_decisions') as post:
                if prior is None:
                    with self.assertRaises(t.SafeError): self.live()
                else:
                    self.assertEqual(self.live()['status'], 'budget_stop')
                post.assert_not_called()
        self.assertFalse(list((self.run / 'attempts').glob('*.json')))

    def test_transport_failure_reserved_no_retry_or_secret_output(self):
        calls = []
        def failed(body, key):
            calls.append(1)
            raise RuntimeError('Authorization: Bearer ' + key)
        result = self.live(transport=failed, max_new=12)
        self.assertEqual(result['status'], 'transport_error')
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['cost_upper_usd'], '0.25')
        with self.assertRaises(t.SafeError):
            self.live(transport=failed, max_new=12)
        self.assertEqual(len(calls), 1)
        for path in self.run.rglob('*.json'):
            self.assertNotIn(FAKE_KEY, path.read_text())

    def test_redaction_missing_usage_and_jpeg_metadata_guard(self):
        raw = response(self.config)
        raw['usage'] = {'bad': FAKE_KEY, 'image': 'data:image/jpeg;base64,NEVER_LOG_THIS'}
        result = self.live(transport=lambda body, key: raw)
        self.assertEqual(result['status'], 'invalid_response')
        raw_text = (self.run / 'raw/fixture0_2026.json').read_text()
        self.assertNotIn(FAKE_KEY, raw_text)
        self.assertNotIn('data:image/', raw_text)
        self.assertEqual(result['cost_upper_usd'], '0.25')
        for marker in [b'\xe1', b'\xef', b'\xfe']:
            with self.assertRaises(t.SafeError):
                t.jpeg_dimensions(jpeg()[:2] + b'\xff' + marker + b'\x00\x04xx' + jpeg()[2:])

    def test_human_revision_and_export_do_not_change_raw_or_source(self):
        self.live()
        raw_path = self.run / 'raw/fixture0_2026.json'
        raw_before = raw_path.read_bytes()
        source_before = (self.dataset / 'individual-review.csv').read_bytes()
        draft = self.run / 'edited-review.json'
        draft.write_bytes(t.encoded(self.draft()))
        self.assertEqual(t.import_review(self.run, draft)['new_revisions'], 1)
        self.assertEqual(t.import_review(self.run, draft)['new_revisions'], 0)
        self.assertEqual(raw_before, raw_path.read_bytes())
        self.assertEqual(source_before, (self.dataset / 'individual-review.csv').read_bytes())
        output = self.run / 'traits.csv'
        t.export(self.run, output)
        with output.open(encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]['source_sex_verified'], 'unknown')
        self.assertEqual(rows[0]['human_pattern_topology'], 'unknown')
        self.assertEqual(rows[0]['ai_cheek_pattern'], 'curving_lines')
        self.assertEqual(rows[1]['human_review_status'], 'unconfirmed')
        self.assertEqual(rows[0]['ai_evidence_kind'], 'synthetic_fixture')
        self.assertIn('source_length_mm', rows[0])
        self.assertIn('source_head_w_px', rows[0])
        for private in ['DO_NOT_EXPORT_LOCATION', '12.3456789', 'PRIVATE_TRAP', FAKE_KEY]:
            self.assertNotIn(private, output.read_text())
        with self.assertRaises(t.SafeError): t.export(self.run, output)

    def test_reviewer_disagreement_and_raw_tampering_are_explicit(self):
        self.live()
        draft = self.run / 'edited-review.json'
        data = self.draft()
        draft.write_bytes(t.encoded(data))
        t.import_review(self.run, draft)
        data[0].update(human_reviewer='Second reviewer', human_eye_ring_apparent_hue='orange_red')
        draft.write_bytes(t.encoded(data))
        with self.assertRaises(t.SafeError): t.import_review(self.run, draft)
        data[0]['human_review_status'] = 'needs_adjudication'
        draft.write_bytes(t.encoded(data))
        t.import_review(self.run, draft)
        self.assertEqual(len(list((self.run / 'reviews').glob('*.json'))), 2)
        (self.run / 'raw/fixture0_2026.json').write_text('{}')
        with self.assertRaises(t.SafeError): t.export(self.run, self.run / 'bad.csv')

    def test_changed_input_symlink_and_formula_safety(self):
        image = self.dataset / 'images/fixture0_2026.jpg'
        original = image.read_bytes()
        image.write_bytes(original + b'x')
        with self.assertRaises(t.SafeError): self.live()
        image.unlink()
        external = self.root / 'external.jpg'
        external.write_bytes(original)
        image.symlink_to(external)
        with self.assertRaises(t.SafeError): self.live()
        self.assertEqual(t.csv_safe('=HYPERLINK("bad")'), '\'=HYPERLINK("bad")')

    def test_key_loader_rejects_symlink_duplicates_and_exposure(self):
        self.patches[2].stop()
        keyfile = self.root / '.env.local'
        t.write_new(keyfile, ('OPENAI_API_KEY=' + FAKE_KEY + '\n').encode())
        self.assertEqual(t.load_key(), FAKE_KEY)
        keyfile.write_text('OPENAI_API_KEY=' + FAKE_KEY + '\nOPENAI_API_KEY=\n')
        with self.assertRaises(t.SafeError) as caught:
            t.load_key()
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        keyfile.unlink()
        target = self.root / 'key-target'
        t.write_new(target, ('OPENAI_API_KEY=' + FAKE_KEY + '\n').encode())
        keyfile.symlink_to(target)
        with self.assertRaises(t.SafeError): t.load_key()

    def test_explicit_retry_preserves_failure_and_cost_reserve(self):
        self.live(transport=lambda body, key: {'transport_error': 'test_failure'})
        failed_raw = (self.run / 'raw/fixture0_2026.json').read_bytes()
        self.live(retry_failed_once=True)
        self.assertEqual((self.run / 'raw/fixture0_2026.json').read_bytes(), failed_raw)
        self.assertEqual(t.read_result(self.run, 'fixture0_2026')['status'], 'completed')
        self.assertEqual(t.accounting(self.run, t.load_run(self.run)[0])[0], t.Decimal('0.25022'))
        with self.assertRaises(t.SafeError): self.live(retry_failed_once=True)
        self.live(max_new=12)
        self.assertEqual(len(list((self.run / 'attempts').glob('*.json'))), 3)

    def test_tls_always_verifies_certificates_and_hostnames(self):
        context = t.tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


if __name__ == '__main__':
    unittest.main()
