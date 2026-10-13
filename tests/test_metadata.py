"""Synthetic contract tests; these are not evidence about downloaded Melops data."""
import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import metadata as m


def fixture():
    base = dict(filename_year='img1_2024', ID='00123', date='30.08.2024',
                year='2024', sightingnumb='1', sightings='2', length='120',
                sex='f', validated_sex='0', side='left', head_w='600', head_h='500',
                lat='12.3456789', lon='98.7654321', trapID='private-trap')
    return [base, dict(base, filename_year='img2_2024', side='right'),
            dict(base, filename_year='img3_2024', date='31.08.2024', sightingnumb='2', length='121'),
            dict(base, filename_year='img4_2024', ID='untagged1', validated_sex='1', sex='s')]


def encode(rows, delimiter='\t'):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=delimiter)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


class MetadataTests(unittest.TestCase):
    def test_same_day_ordinals_preserve_measurements_and_flag_review(self):
        rows = fixture()
        rows[2]['date'] = rows[0]['date']
        report, _, tables = m.inspect(encode(rows), 10)
        self.assertFalse(report['errors'])
        self.assertEqual(report['warnings']['same_day_multiple_ordinals_review'], 1)
        observations = [o for o in tables[1] if o['individual_id'] == '00123']
        self.assertEqual({o['length_source'] for o in observations}, {'120', '121'})
        self.assertTrue(all(o['same_day_multiple_ordinals_review'] for o in observations))
        self.assertEqual(len(observations), 2)

    def test_repeated_side_is_retained_as_separate_images(self):
        rows = fixture()
        rows.append(dict(rows[0], filename_year='repeat_left'))
        report, _, tables = m.inspect(encode(rows), 10)
        self.assertFalse(report['errors'])
        self.assertEqual(report['warnings']['observations_with_repeated_side'], 1)
        self.assertEqual(len(tables[2]), 5)

    def test_missing_length_and_untagged_identity_remain_explicit(self):
        rows = fixture()
        for row in rows:
            row['tagged'] = '0' if row['ID'] == 'untagged1' else '1'
        rows[-1]['length'] = 'NA'
        report, _, tables = m.inspect(encode(rows), 10)
        self.assertEqual((report['tagged_individual_ids'], report['untagged_id_labels']), (1, 1))
        self.assertEqual(report['missing_length_rows'], 1)
        self.assertEqual(next(o for o in tables[1] if o['individual_id'] == 'untagged1')['length_source'], '')
        self.assertEqual(next(i for i in tables[0] if i['individual_id'] == 'untagged1')['identity_basis'], 'source_label_unverified')

    def test_grouping_and_units(self):
        report, dictionary, (individuals, observations, images) = m.inspect(encode(fixture()), 10)
        self.assertFalse(report['errors'])
        self.assertEqual((report['image_rows'], report['individuals'], report['observations']), (4, 2, 3))
        self.assertEqual(len(images), 4)
        self.assertIn('00123', {r['individual_id'] for r in individuals})
        self.assertEqual(observations[0]['length_unit'], 'mm')
        self.assertTrue(all(r['head_dimensions_unit'] == 'pixels' for r in images))
        self.assertTrue(all(r['observed_in_file'] for r in dictionary))

    def test_sampling_keeps_all_photos_and_is_order_invariant(self):
        result = m.build(fixture(), 1)
        self.assertEqual(result, m.build(list(reversed(fixture())), 1))
        selected = result[0][0]['individual_id']
        self.assertEqual(len(result[2]), sum(r['ID'] == selected for r in fixture()))

    def test_unknown_sex_and_no_ai_claim(self):
        self.assertEqual(m.sex_verified('f', '0'), 'unknown')
        self.assertEqual(m.sex_verified('new_code', '1'), 'unknown')
        self.assertEqual(m.sex_verified('', ''), 'unknown')
        self.assertEqual(m.sex_verified('s', '1'), 'sneaker_male')
        self.assertTrue(all(o['ai_model_id'] == '' for o in m.build(fixture(), 10)[1]))

    def test_coordinate_values_never_exported(self):
        result = json.dumps(m.inspect(encode(fixture()), 10))
        for secret in ['12.3456789', '98.7654321', 'private-trap']:
            self.assertNotIn(secret, result)
        for table in m.build(fixture(), 10):
            self.assertTrue(all(not {'lat', 'lon', 'trapID', 'area'} & r.keys() for r in table))

    def test_duplicates_and_invalid_measurement(self):
        rows = fixture()
        rows[1]['filename_year'] = rows[0]['filename_year']
        rows[0]['length'] = 'inf'
        errors = m.validate(rows)
        self.assertEqual(errors['duplicate_image_ids'], 1)
        self.assertEqual(errors['invalid_length'], 1)

    def test_observation_conflicts_are_not_silently_resolved(self):
        rows = fixture()
        rows[1]['sex'] = 'm'
        report, _, tables = m.inspect(encode(rows), 10)
        self.assertTrue(report['errors'])
        conflicted = next(o for o in tables[1] if o['conflicting_fields'])
        self.assertEqual(conflicted['sex_source'], '')
        self.assertEqual(conflicted['sex_verified'], 'unknown')

    def test_observation_identity_ambiguity(self):
        rows = fixture()
        rows[2]['sightingnumb'] = '1'
        self.assertIn('observation_ordinal_multiple_dates', m.validate(rows))

    def test_missing_values_and_bad_flags(self):
        rows = fixture()
        rows[0]['length'] = 'NA'
        self.assertNotIn('invalid_length', m.validate(rows))
        rows[0]['validated_sex'] = '2'
        self.assertIn('invalid_validated_sex', m.validate(rows))

    def test_parse_delimiters_and_reject_bad_schema(self):
        for delimiter in ['\t', ',', ';']:
            self.assertEqual(len(m.parse(encode(fixture(), delimiter))[1]), 4)
        with self.assertRaises(ValueError):
            m.parse(b'foo,bar\n1,2\n')
        with self.assertRaises(ValueError):
            m.build(fixture(), 0)

    def test_size_cap_with_and_without_content_length(self):
        for headers in [{'Content-Length': '20'}, {}]:
            response = io.BytesIO(b'x' * 20)
            response.headers = headers
            with self.assertRaises(ValueError):
                m.bounded_read(response, 10)

    def test_checksum_failure_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'metadata.txt'
            path.write_bytes(b'not Melops')
            with self.assertRaises(ValueError):
                m.fetch(path)
            self.assertEqual(path.read_bytes(), b'not Melops')

    def test_fetch_verifies_before_writing(self):
        response = io.BytesIO(b'wrong checksum')
        response.headers = {}
        with tempfile.TemporaryDirectory() as temp, patch.object(m, 'urlopen', return_value=response):
            path = Path(temp) / 'metadata.txt'
            with self.assertRaises(ValueError):
                m.fetch(path)
            self.assertFalse(path.exists())

    def test_cli_real_data_guard_and_error_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'synthetic.tsv'
            source.write_bytes(encode(fixture()))
            output = Path(temp) / 'result'
            command = [sys.executable, str(m.ROOT / 'scripts/metadata.py'), 'inspect',
                       '--input', str(source), '--output', str(output)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertFalse(output.exists())
            self.assertEqual(subprocess.run(command + ['--allow-unverified-input'], capture_output=True).returncode, 0)
            self.assertTrue((output / 'observations.csv').exists())
            self.assertFalse(json.loads((output / 'report.json').read_text())['matches_pinned_source'])
            self.assertEqual(subprocess.run(command + ['--allow-unverified-input'], capture_output=True).returncode, 2)
            rows = fixture()
            rows[1]['length'] = '999'
            source.write_bytes(encode(rows))
            command[command.index('--output') + 1] = str(Path(temp) / 'failed')
            self.assertEqual(subprocess.run(command + ['--allow-unverified-input'], capture_output=True).returncode, 1)
            self.assertFalse((Path(temp) / 'failed/observations.csv').exists())


if __name__ == '__main__':
    unittest.main()
