"""Bounded Melops acquisition and coordinate-free, individual-grouped inspection."""
import argparse
import csv
import hashlib
import io
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SOURCE = json.loads((ROOT / 'docs/source.json').read_text())
FIELDS = json.loads((ROOT / 'docs/fields.json').read_text())
MISSING = {'', 'NA', 'N/A', 'NaN', 'nan', 'NULL', 'null'}
REQUIRED = {'filename_year', 'ID', 'date', 'sightingnumb', 'length', 'sex', 'validated_sex', 'side'}
OBS_FIELDS = ['length', 'sex', 'validated_sex', 'spawning', 'cryptocotyle', 'scaleloss', 'zombie']
IMAGE_FIELDS = ['filename_year', 'side', 'head_w', 'head_h', 'blurry']


def digest(data, algorithm='sha256'):
    return hashlib.new(algorithm, data).hexdigest()


def bounded_read(response, limit):
    size = response.headers.get('Content-Length')
    if size is not None and int(size) > limit:
        raise ValueError('Download exceeds configured byte limit')
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Download exceeds configured byte limit')
    return data


def fetch(destination):
    spec = SOURCE['metadata']
    if destination.exists():
        if destination.stat().st_size > spec['max_bytes']:
            raise ValueError('Existing file exceeds configured byte limit')
        data = destination.read_bytes()
    else:
        with urlopen(spec['url'], timeout=45) as response:
            data = bounded_read(response, spec['max_bytes'])
    if digest(data, 'md5') != spec['md5']:
        raise ValueError('Metadata checksum mismatch; existing file left unchanged')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_bytes(data)
    return {'bytes': len(data), 'md5': digest(data, 'md5'), 'sha256': digest(data)}


def present(value):
    return value.strip() not in MISSING


def parse(data):
    text = data.decode('utf-8-sig')
    dialect = csv.Sniffer().sniff(text[:65536], delimiters='\t,;')
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    header = reader.fieldnames or []
    if len(header) != len(set(header)):
        raise ValueError('Duplicate column names')
    if not REQUIRED.issubset(header):
        raise ValueError('Required columns missing: ' + ', '.join(sorted(REQUIRED - set(header))))
    rows = list(reader)
    if not rows:
        raise ValueError('Metadata contains no rows')
    if any(None in row or None in row.values() for row in rows):
        raise ValueError('Inconsistent row width')
    return header, rows, dialect.delimiter


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Non-finite number')
    return result


def observation_key(row):
    # Length-delimited JSON avoids collisions from punctuation in source IDs.
    return json.dumps([row['ID'], row['date'], row['sightingnumb']], separators=(',', ':'))


def validate(rows):
    problems = Counter()
    names = Counter(r['filename_year'] for r in rows)
    problems['duplicate_image_ids'] = sum(n - 1 for n in names.values())
    ordinal_dates = defaultdict(set)
    for row in rows:
        for key in ['ID', 'filename_year', 'date', 'sightingnumb']:
            if not present(row[key]):
                problems['missing_' + key] += 1
        try:
            date = datetime.strptime(row['date'], '%d.%m.%Y')
            if present(row.get('year', '')) and int(row['year']) != date.year:
                problems['year_date_mismatch'] += 1
        except ValueError:
            problems['invalid_date'] += 1
        for key in ['length', 'head_w', 'head_h', 'sightings', 'sightingnumb']:
            if present(row.get(key, '')):
                try:
                    n = number(row[key])
                    if n <= 0 or (key in {'sightings', 'sightingnumb'} and not n.is_integer()):
                        raise ValueError()
                except ValueError:
                    problems['invalid_' + key] += 1
        for key, allowed in {
            'validated_sex': {'0', '1'}, 'tagged': {'0', '1'},
            'suspected_tagloss': {'0', '1'}, 'blurry': {'0', '1'},
            'spawning': {'0', '1', '2'}, 'cryptocotyle': {'0', '1', '2', '3'},
            'scaleloss': {'0', '1', '2', '3'}, 'zombie': {'0', '1', '2', '3'},
            'side': {'left', 'right'},
        }.items():
            if present(row.get(key, '')) and row[key] not in allowed:
                problems['invalid_' + key] += 1
        ordinal_dates[(row['ID'], row['sightingnumb'])].add(row['date'])
    problems['observation_ordinal_multiple_dates'] = sum(len(v) > 1 for v in ordinal_dates.values())
    return {k: v for k, v in sorted(problems.items()) if v}


def sex_verified(label, flag):
    return {'f': 'female', 'm': 'nesting_male', 's': 'sneaker_male'}.get(label, 'unknown') if flag == '1' else 'unknown'


def build(rows, sample_size):
    if sample_size < 1:
        raise ValueError('sample-individuals must be positive')
    groups = defaultdict(list)
    for row in rows:
        groups[row['ID']].append(row)
    # Stable sampling by individual, retaining every observation/image of selected fish.
    ids = sorted(groups, key=lambda key: (digest(key.encode()), key))[:sample_size]
    individuals, observations, images = [], [], []
    for individual in ids:
        encounters = defaultdict(list)
        for row in groups[individual]:
            encounters[observation_key(row)].append(row)
        date_ordinals = defaultdict(set)
        for row in groups[individual]:
            date_ordinals[row['date']].add(row['sightingnumb'])
        individuals.append({'individual_id': individual, 'species': SOURCE['species'],
                            'identity_basis': 'source_PIT' if all(r.get('tagged') == '1' for r in groups[individual]) else 'source_label_unverified',
                            'observation_count': len(encounters), 'image_count': len(groups[individual]),
                            'split_group': individual})
        for key, members in sorted(encounters.items()):
            first = members[0]
            observation = {'observation_id': key, 'individual_id': individual,
                           'date': first['date'], 'sightingnumb': first['sightingnumb'],
                           'same_day_multiple_ordinals_review': len(date_ordinals[first['date']]) > 1}
            conflicts = []
            for field in OBS_FIELDS:
                values = {r.get(field, '') for r in members if present(r.get(field, ''))}
                if len(values) > 1:
                    conflicts.append(field)
                observation[field + '_source'] = next(iter(values)) if len(values) == 1 else ''
            observation.update({
                'sex_verified': sex_verified(observation['sex_source'], observation['validated_sex_source']),
                'length_unit': 'mm', 'length_provenance': 'source_physical_measurement',
                'validation_provenance': 'source_human_validation_flag',
                'conflicting_fields': ','.join(conflicts), 'image_count': len(members),
                'ai_sex_inference': '', 'ai_length_estimate_mm': '', 'ai_model_id': '',
            })
            observations.append(observation)
            for row in sorted(members, key=lambda r: r['filename_year']):
                image = {'observation_id': key, 'individual_id': individual}
                image.update({field: row.get(field, '') for field in IMAGE_FIELDS})
                image['head_dimensions_unit'] = 'pixels'
                images.append(image)
    return individuals, observations, images


def inspect(data, sample_size):
    header, rows, delimiter = parse(data)
    errors = validate(rows)
    tables = build(rows, sample_size)
    all_observations = defaultdict(list)
    for row in rows:
        all_observations[observation_key(row)].append(row)
    conflict_counts = Counter()
    for members in all_observations.values():
        for field in OBS_FIELDS:
            if len({r.get(field, '') for r in members if present(r.get(field, ''))}) > 1:
                conflict_counts[field] += 1
    if conflict_counts:
        errors['observation_trait_conflict_occurrences'] = sum(conflict_counts.values())
    date_ordinals = defaultdict(set)
    for row in rows:
        date_ordinals[(row['ID'], row['date'])].add(row['sightingnumb'])
    warnings = {
        'same_day_multiple_ordinals_review': sum(len(v) > 1 for v in date_ordinals.values()),
        'observations_with_repeated_side': sum(len(members) > len({r['side'] for r in members}) for members in all_observations.values()),
    }
    dictionary = [{
        'column': key, 'declared_type': FIELDS.get(key, ['undocumented'])[0],
        'provenance': FIELDS.get(key, ['', 'undocumented'])[1],
        'description': FIELDS.get(key, ['', '', 'Undocumented source column; do not infer meaning.'])[2],
        'present_rows': sum(present(r[key]) for r in rows),
        'missing_rows': sum(not present(r[key]) for r in rows),
        'observed_in_file': True,
    } for key in header]
    report = {
        'input_sha256': digest(data), 'input_md5': digest(data, 'md5'), 'input_bytes': len(data),
        'matches_pinned_source': digest(data, 'md5') == SOURCE['metadata']['md5'],
        'delimiter': delimiter, 'image_rows': len(rows), 'individuals': len({r['ID'] for r in rows}),
        'observations': len(all_observations), 'errors': errors,
        'warnings': {k: v for k, v in warnings.items() if v},
        'identity_count_basis': 'Distinct source ID labels; untagged labels are not PIT-confirmed identities',
        'tagged_individual_ids': len({r['ID'] for r in rows if r.get('tagged') == '1'}),
        'untagged_id_labels': len({r['ID'] for r in rows if r.get('tagged') == '0'}),
        'missing_length_rows': sum(not present(r['length']) for r in rows),
        'validated_sex_rows': sum(r['validated_sex'] == '1' for r in rows),
        'observation_key': ['ID', 'date', 'sightingnumb'],
        'same_day_policy': 'Retain distinct source ordinals and measured traits; flag for review, do not assert independent recaptures',
        'trait_conflicts_by_field': dict(conflict_counts),
        'unknown_verified_sex_rows': sum(sex_verified(r['sex'], r['validated_sex']) == 'unknown' for r in rows),
        'selected_individuals': len(tables[0]),
        'sampling': 'SHA256-ranked individual IDs; all linked images and observations retained',
        'dictionary_basis': 'Actual file header/counts; field semantics from record documentation',
        'privacy': 'Allowlisted sample columns; no coordinates, trap identifiers or location values',
    }
    return report, dictionary, tables


def write_csv(path, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    download = sub.add_parser('fetch')
    download.add_argument('--output', type=Path, default=ROOT / 'data/raw/Melops_metadata.txt')
    check = sub.add_parser('inspect')
    check.add_argument('--input', type=Path, default=ROOT / 'data/raw/Melops_metadata.txt')
    check.add_argument('--output', type=Path, default=ROOT / 'data/derived/melops')
    check.add_argument('--sample-individuals', type=int, default=10)
    check.add_argument('--allow-unverified-input', action='store_true', help='For explicit local/fixture inspection only')
    args = parser.parse_args()
    try:
        if args.command == 'fetch':
            print(json.dumps(fetch(args.output), indent=2))
            return 0
        with args.input.open('rb') as stream:
            data = stream.read(SOURCE['metadata']['max_bytes'] + 1)
        if len(data) > SOURCE['metadata']['max_bytes']:
            raise ValueError('Input exceeds configured byte limit')
        if not args.allow_unverified_input and digest(data, 'md5') != SOURCE['metadata']['md5']:
            raise ValueError('Input checksum does not match pinned Melops; use explicit unverified mode for fixtures')
        report, dictionary, tables = inspect(data, args.sample_individuals)
        # Never replace an earlier run or leave stale sample exports after a failed run.
        args.output.mkdir(parents=True, exist_ok=False)
        for name, payload in [('report.json', report), ('field_dictionary.json', dictionary)]:
            (args.output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        if not report['errors']:
            for name, table in zip(['individuals.csv', 'observations.csv', 'images.csv'], tables):
                write_csv(args.output / name, table)
        print(json.dumps({'output': str(args.output), 'image_rows': report['image_rows'],
                          'individuals': report['individuals'], 'errors': report['errors']}, indent=2))
        return 1 if report['errors'] else 0
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(2, f'Metadata workflow failed: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
