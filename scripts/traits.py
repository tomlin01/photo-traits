"""Small, offline-first photo review workflow; network requires the explicit run --live gate."""
import argparse
import base64
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
DATASET = Path('data/derived/review-12pit')
QUESTIONS_FILE = ROOT / 'docs/trait_questions.json'
ENDPOINT = 'https://api.openai.com/v1/decisions'
MODEL = 'gpt-6-luna'
PRICE_DATE = '2026-10-12'
INPUT_USD_PER_MILLION = Decimal('0.10')
# Cover the documented 2x long-context rate AND 10% regional premium.
UPPER_USD_PER_MILLION = Decimal('0.22')
MAX_INPUT_TOKENS = 922_000
# Full model input ceiling costs $0.20284 at the upper rate; round up further.
REQUEST_RESERVE_USD = Decimal('0.25')
MAX_IMAGES = 12
MAX_JPEG_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 262_144
SOURCE_FIELDS = ['source_record', 'source_archive_member', 'source_filename_year',
                 'source_date', 'source_sightingnumb', 'source_tagged',
                 'source_suspected_tagloss', 'source_length', 'source_sex',
                 'source_validated_sex', 'source_side', 'source_blurry',
                 'source_head_w', 'source_head_h', 'source_member_sha256']
HUMAN_FIELDS = ['human_review_status', 'human_reviewer', 'human_review_date',
                'human_photo_side_confirmed', 'human_cheek_visibility',
                'human_eye_ring_visibility', 'human_quality_flags',
                'human_pattern_topology', 'human_eye_ring_apparent_hue', 'human_notes']
VISIBILITY = {'clear', 'partial', 'unassessable', 'unknown'}
PATTERNS = {'curving_lines', 'branching_reticulated_lines', 'mixed', 'other', 'unknown'}
HUES = {'orange_red', 'yellow_pale_gold', 'mixed', 'other', 'unknown'}
QUALITY = {'glare', 'soft_focus', 'uneven_exposure', 'crop_truncation', 'none_observed', 'unknown'}


class SafeError(Exception):
    """Only constant, credential-free messages may reach the CLI."""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def read_regular(path, limit):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise SafeError('Symlink or redirected path refused.')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SafeError('Only regular files are supported.')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise SafeError('Local file exceeds its size limit.')
    return data


def local_path(path):
    path = Path(os.path.abspath(path))
    if path.resolve() != path or not path.is_relative_to(ROOT / 'data/derived'):
        raise SafeError('Output must be a non-symlink path under data/derived.')
    return path


def ensure_ignored(path):
    relative = str(path.relative_to(ROOT))
    tracked = subprocess.run(['git', 'ls-files', '-z', '--', relative], cwd=ROOT, capture_output=True)
    ignored = subprocess.run(['git', 'check-ignore', '-q', '--', relative], cwd=ROOT, capture_output=True)
    if tracked.returncode or tracked.stdout or ignored.returncode:
        raise SafeError('Local data/credential target must be Git-ignored and untracked.')


def jpeg_dimensions(data):
    """Validate marker structure and reject APP1-15/COM, including between scans.

    This is a metadata/size guard, not an independent full JPEG decoder.
    """
    if not data.startswith(b'\xff\xd8'):
        raise SafeError('Expected JPEG input.')
    i, dimensions, scan = 2, None, False
    while i < len(data):
        if scan:
            i = data.find(b'\xff', i)
            if i < 0:
                break
        if data[i] != 255:
            raise SafeError('Malformed JPEG marker.')
        while i < len(data) and data[i] == 255:
            i += 1
        if i >= len(data):
            break
        marker = data[i]
        i += 1
        if scan and (marker == 0 or 0xD0 <= marker <= 0xD7):
            continue
        scan = False
        if marker == 0xD9:
            if dimensions and i == len(data):
                return dimensions
            raise SafeError('JPEG missing dimensions or has trailing data.')
        if 0xE1 <= marker <= 0xEF or marker == 0xFE:
            raise SafeError('JPEG contains prohibited metadata; no upload attempted.')
        if i + 2 > len(data):
            break
        size = int.from_bytes(data[i:i + 2], 'big')
        if size < 2 or i + size > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2):
            if size < 8:
                break
            height = int.from_bytes(data[i + 3:i + 5], 'big')
            width = int.from_bytes(data[i + 5:i + 7], 'big')
            if not 1 <= width <= 2048 or not 1 <= height <= 2048:
                raise SafeError('Image dimensions outside this pilot scope.')
            dimensions = [width, height]
        i += size
        scan = marker == 0xDA
    raise SafeError('Incomplete JPEG structure.')


def load_questions():
    config = json.loads(read_regular(QUESTIONS_FILE, 50_000))
    if config['model'] != MODEL or config['endpoint'] != ENDPOINT:
        raise SafeError('Only Decisions API / gpt-6-luna is authorized; no fallback.')
    questions = config['questions']
    if len(questions) != 9 or len({q['name'] for q in questions}) != len(questions):
        raise SafeError('Unexpected question contract.')
    return config


def image_bytes(row):
    path = ROOT / DATASET / row['photo_ref']
    if not path.is_relative_to(ROOT / DATASET / 'images'):
        raise SafeError('Image path outside the approved dataset.')
    data = read_regular(path, MAX_JPEG_BYTES)
    if sha(data) != row['image_sha256'] or jpeg_dimensions(data) != row['pixel_dimensions']:
        raise SafeError('Image changed since preparation; no upload attempted.')
    return data


def source_rows():
    root = ROOT / DATASET
    csv_data = read_regular(root / 'individual-review.csv', 200_000)
    manifest_data = read_regular(root / 'source-manifest.json', 200_000)
    source_manifest = json.loads(manifest_data)
    manifest = {entry['photo_ref']: entry for entry in source_manifest}
    if len(manifest) != len(source_manifest):
        raise SafeError('Duplicate source image entries.')
    rows = list(csv.DictReader(io.StringIO(csv_data.decode('utf-8-sig'))))
    if not 1 <= len(rows) <= MAX_IMAGES or len(rows) != len(manifest):
        raise SafeError('Dataset must contain at most 12 matched photos.')
    items = []
    for row in rows:
        image_id, ref = row['source_filename_year'], row['photo_ref']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', image_id) or ref != f'images/{image_id}.jpg':
            raise SafeError('Invalid image identity or path.')
        if ref not in manifest or row['delivered_sha256'] != manifest[ref]['sha256']:
            raise SafeError('Source CSV and manifest image hashes disagree.')
        raw = manifest[ref]['raw_metadata']
        for field, key in [('pit_id', 'ID'), ('source_filename_year', 'filename_year'),
                           ('source_date', 'date'), ('source_sightingnumb', 'sightingnumb')]:
            if str(row[field]) != str(raw[key]):
                raise SafeError('Source identity/observation relation is ambiguous.')
        item = {'image_id': image_id, 'individual_id': row['pit_id'],
                'split_group': row['pit_id'],
                'identity_basis': 'source_PIT_label' if row['source_tagged'] == '1' else 'source_label_unverified',
                'observation_id': '|'.join(row[k] for k in ('pit_id', 'source_date', 'source_sightingnumb')),
                'photo_ref': ref, 'image_sha256': row['delivered_sha256'],
                'pixel_dimensions': manifest[ref]['pixel_dimensions'],
                'source': {k: row[k] for k in SOURCE_FIELDS},
                'human_original': {k: row[k] for k in HUMAN_FIELDS}}
        image_bytes(item)
        items.append(item)
    if len({r['image_id'] for r in items}) != len(items):
        raise SafeError('Duplicate image IDs; no automatic merge.')
    for item in items:
        siblings = [r for r in items if r['observation_id'] == item['observation_id']]
        fields = ['source_length', 'source_sex', 'source_validated_sex']
        item['source_observation_ambiguous'] = any(len({r['source'][k] for r in siblings}) > 1 for k in fields)
    return items, {'source_csv_sha256': sha(csv_data), 'source_manifest_sha256': sha(manifest_data)}


def request_body(row, config, include_image=True):
    # No PIT ID, source sex, dates, filenames or old AI/human labels enter the API input.
    image = 'data:image/jpeg;base64,' + base64.b64encode(image_bytes(row)).decode('ascii') if include_image else '<verified-local-JPEG-omitted>'
    return {'model': MODEL,
            'input': [{'role': 'user', 'content': [
                {'type': 'input_text', 'text': config['instructions']},
                {'type': 'input_image', 'image_url': image, 'detail': 'high'}]}],
            'questions': config['questions']}


def prepare(run, prior_upper=None, evidence=None):
    run = local_path(run)
    ensure_ignored(run / 'manifest.json')
    config = load_questions()
    rows, hashes = source_rows()
    prior = None
    evidence_hash = None
    if prior_upper is not None:
        prior = Decimal(prior_upper)
        if not prior.is_finite() or not 0 <= prior <= 1 or not evidence:
            raise SafeError('Known prior cost requires a finite upper bound and an audit file.')
        evidence_hash = sha(read_regular(local_path(evidence), 100_000))
    manifest = {'version': 1, 'created_at': now(), 'dataset': str(DATASET),
                'model': MODEL, 'endpoint': ENDPOINT, 'question_sha256': sha(encoded(config)),
                'price_checked_at': PRICE_DATE, 'input_usd_per_million': str(INPUT_USD_PER_MILLION),
                'conservative_input_usd_per_million': str(UPPER_USD_PER_MILLION),
                'per_request_reserved_usd': str(REQUEST_RESERVE_USD),
                'budget_usd': '1.00', 'prior_cost_upper_usd': str(prior) if prior is not None else None,
                'prior_audit_sha256': evidence_hash, 'prior_audit_path': str(local_path(evidence).relative_to(ROOT)) if evidence else None,
                'max_attempts': MAX_IMAGES, 'automatic_retries': 0, 'images': rows, **hashes}
    run.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name in ['attempts', 'raw', 'results', 'reviews']:
        (run / name).mkdir(mode=0o700)
    write_new(run / 'manifest.json', encoded(manifest))
    write_new(run / 'request-preview.json', encoded({'endpoint': ENDPOINT, 'body': request_body(rows[0], config, False)}))
    write_new(run / 'human-review-draft.json', encoded([
        {'image_id': r['image_id'], **r['human_original']} for r in rows]))
    return {'status': 'prepared_offline', 'photos': len(rows), 'individuals': len({r['individual_id'] for r in rows}),
            'live_calls': 0, 'prior_cost_known': prior is not None}


def load_run(run):
    run = local_path(run)
    manifest = json.loads(read_regular(run / 'manifest.json', 500_000))
    config = load_questions()
    if (manifest['model'], manifest['endpoint'], manifest['dataset'], manifest['question_sha256']) != (
            MODEL, ENDPOINT, str(DATASET), sha(encoded(config))):
        raise SafeError('Run/model/endpoint/question contract mismatch; no fallback.')
    return manifest, config


def load_key():
    path = ROOT / '.env.local'
    ensure_ignored(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise SafeError('Credential file must be owner-only, regular and owned by this user.')
    text = read_regular(path, 16_384).decode('utf-8-sig')
    matches = re.findall(r'^\s*(?:export\s+)?OPENAI_API_KEY\s*=\s*(.*?)\s*$', text, re.M)
    if len(matches) != 1:
        raise SafeError('Credential unavailable or ambiguous; value not displayed.')
    value = matches[0].strip()
    if value.startswith(('"', "'")):
        if len(value) < 2 or value[-1] != value[0]:
            raise SafeError('Credential quoting is malformed; value not displayed.')
        value = value[1:-1]
    if not re.fullmatch(r'sk-[A-Za-z0-9_-]{20,}', value):
        raise SafeError('Credential absent or malformed; value not displayed.')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def tls_context():
    defaults = ssl.get_default_verify_paths()
    if defaults.cafile or defaults.capath:
        return ssl.create_default_context()
    if sys.platform == 'darwin' and Path('/etc/ssl/cert.pem').is_file():
        return ssl.create_default_context(cafile='/etc/ssl/cert.pem')
    raise SafeError('TLS trust store unavailable; do not disable certificate verification.')


def post_decisions(body, key):
    if body.get('model') != MODEL or set(body) != {'model', 'input', 'questions'}:
        raise SafeError('Wrong model or non-Decisions request contract.')
    # Fixed origin, verified TLS, no environment proxy/base URL, redirects or retries.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=tls_context()))
    request = urllib.request.Request(ENDPOINT, data=encoded(body), method='POST',
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with opener.open(request, timeout=45) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            return {'transport_error': 'response_size_limit'}
        return json.loads(data)
    except urllib.error.HTTPError as error:
        # Never persist API error text: authentication errors may echo credentials.
        code = error.code
        error.close()
        return {'transport_error': 'http_error', 'http_status': code}
    except urllib.error.URLError as error:
        return {'transport_error': 'tls_certificate_verification_failed' if isinstance(error.reason, ssl.SSLCertVerificationError) else 'network_error'}
    except (OSError, ValueError):
        return {'transport_error': 'network_or_decode_error'}


def probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def parse_answers(raw, config):
    if raw.get('model') != MODEL or not isinstance(raw.get('answers'), list):
        raise SafeError('Unexpected Decisions response model or envelope.')
    answers = raw['answers']
    expected = {q['name']: q for q in config['questions']}
    if len(answers) != len(expected) or {a.get('name') for a in answers} != set(expected):
        raise SafeError('Missing, duplicated or unexpected answers.')
    normalized, refused = {}, []
    for answer in answers:
        name, kind = answer['name'], answer.get('type')
        question = expected[name]
        if kind == 'refusal':
            normalized[name] = None
            refused.append(name)
            continue
        if kind != question['type']:
            raise SafeError('Answer type differs from requested question.')
        if kind == 'predicate':
            if not probability(answer.get('probability')):
                raise SafeError('Invalid predicate probability.')
            normalized[name] = answer['probability']
            continue
        options = [o['value'] for o in question['choices']] if kind == 'choice' else list(range(len(question['levels'])))
        distribution = answer.get('probabilities', [])
        if (len(distribution) != len(options) or {p.get('value') for p in distribution} != set(options)
                or not all(probability(p.get('probability')) for p in distribution)
                or abs(sum(p['probability'] for p in distribution) - 1) > 0.01
                or not probability(answer.get('confidence'))):
            raise SafeError('Invalid answer probability distribution.')
        if kind == 'choice':
            if answer.get('choice') not in options:
                raise SafeError('Answer choice outside the allowed categories.')
            normalized[name] = answer['choice']
        else:
            score = answer.get('score')
            weighted = sum(p['value'] * p['probability'] for p in distribution)
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= len(options) - 1 or abs(score - weighted) > 0.02:
                raise SafeError('Invalid probability-weighted rubric score.')
            normalized[name] = score
    suppressed = []
    for visibility, trait in [('cheek_visibility', 'cheek_pattern'), ('eye_ring_visibility', 'eye_ring_hue')]:
        if normalized[visibility] not in {'clear', 'partial'}:
            if normalized[trait] not in (None, 'unknown'):
                suppressed.append(trait)
            normalized[trait] = None
    return {'status': 'refusal' if len(refused) == len(expected) else 'partial_refusal' if refused else 'completed',
            'candidate': normalized, 'refused_questions': refused, 'suppressed_due_to_visibility': suppressed}


def usage_cost(raw):
    usage = raw.get('usage', {})
    count = usage.get('input_tokens')
    if type(count) is not int or not 0 < count <= MAX_INPUT_TOKENS:
        raise SafeError('Missing or out-of-bounds input usage; reserved cost remains charged.')
    # Input total is conservative even when some cached input is not charged.
    return {'input_tokens': count, 'estimated_base_usd': str(Decimal(count) * INPUT_USD_PER_MILLION / 1_000_000),
            'cost_upper_usd': str(Decimal(count) * UPPER_USD_PER_MILLION / 1_000_000)}


def read_attempt(run, attempt_id):
    path = run / 'results' / f'{attempt_id}.json'
    if not path.exists():
        return None
    result = json.loads(read_regular(path, 500_000))
    if sha(read_regular(run / 'raw' / f'{attempt_id}.json', MAX_RESPONSE_BYTES * 2)) != result['raw_sha256']:
        raise SafeError('Raw output changed; review/export stopped.')
    return result


def read_result(run, image_id):
    suffix = '.retry1' if (run / 'attempts' / f'{image_id}.retry1.json').exists() else ''
    return read_attempt(run, image_id + suffix)


def accounting(run, manifest):
    prior = manifest['prior_cost_upper_usd']
    if prior is None:
        raise SafeError('Prior call cost is unknown; complete an audit before live work.')
    total = Decimal(prior)
    attempts = list((run / 'attempts').glob('*.json'))
    for path in attempts:
        result = read_attempt(run, path.stem)
        total += Decimal(result['cost']['cost_upper_usd']) if result and result.get('cost') else REQUEST_RESERVE_USD
    return total, {p.stem for p in attempts}


def run_live(run, *, live=False, approve_upload=False, max_new=1, retry_failed_once=False, transport=None):
    if not live or not approve_upload:
        raise SafeError('Live work requires both --live and --approve-upload; no request sent.')
    run = local_path(run)
    manifest, config = load_run(run)
    if not 1 <= max_new <= MAX_IMAGES or not 1 <= len(manifest['images']) <= MAX_IMAGES:
        raise SafeError('Image count exceeds the approved scope.')
    if not 0 <= (date.today() - date.fromisoformat(PRICE_DATE)).days <= 7:
        raise SafeError('Pricing verification is stale; verify official Decisions pricing again.')
    audit = local_path(ROOT / manifest['prior_audit_path']) if manifest.get('prior_audit_path') else None
    if not audit or sha(read_regular(audit, 100_000)) != manifest['prior_audit_sha256']:
        raise SafeError('Prior-cost audit missing or changed.')
    current_rows, current_hashes = source_rows()
    if current_rows != manifest['images'] or any(manifest[k] != v for k, v in current_hashes.items()):
        raise SafeError('Source dataset changed since preparation; audit the scope again.')
    for row in manifest['images']:
        image_bytes(row)
    ensure_ignored(run / 'manifest.json')
    lock = run / '.run-lock'
    write_new(lock, b'Exclusive live-run lock. A stale lock requires local audit.\n')
    completed = 0
    try:
        total, attempted = accounting(run, manifest)
        if len(attempted) > MAX_IMAGES + 1:
            raise SafeError('Attempt limit exceeded.')
        attempted_images = {a.removesuffix('.retry1') for a in attempted}
        failed = []
        for image_id in attempted_images:
            previous = read_result(run, image_id)
            if not previous or previous['status'] in {'transport_error', 'invalid_response'}:
                failed.append(image_id)
        if retry_failed_once:
            if len(failed) != 1 or max_new != 1 or any(a.endswith('.retry1') for a in attempted) or not read_result(run, failed[0]):
                raise SafeError('One explicit retry is allowed for one recorded failure, only once per round.')
            pending = [r for r in manifest['images'] if r['image_id'] == failed[0]]
        else:
            if failed:
                raise SafeError('An earlier attempt is unresolved/failed; audit it before further live work.')
            pending = [r for r in manifest['images'] if r['image_id'] not in attempted_images][:max_new]
        key = load_key() if pending else None
        for row in pending:
            if total + REQUEST_RESERVE_USD > min(Decimal(manifest['budget_usd']), Decimal('1')):
                return {'status': 'budget_stop', 'new_attempts': completed, 'cost_upper_usd': str(total)}
            body = request_body(row, config)
            attempt_id = row['image_id'] + ('.retry1' if retry_failed_once else '')
            receipt = {'started_at': now(), 'image_id': row['image_id'], 'image_sha256': row['image_sha256'],
                       'endpoint': ENDPOINT, 'model': MODEL, 'reserved_usd': str(REQUEST_RESERVE_USD),
                       'transport_kind': 'synthetic_fixture' if transport else 'live',
                       'attempt_id': attempt_id, 'explicit_retry': retry_failed_once, 'automatic_retries': 0}
            write_new(run / 'attempts' / f'{attempt_id}.json', encoded(receipt))
            completed += 1
            try:
                raw = (transport or post_decisions)(body, key)
            except Exception:
                raw = {'transport_error': 'transport_exception'}
            # Only response evidence is retained, never request bodies/headers/images.
            if not isinstance(raw, dict):
                raw = {'transport_error': 'invalid_response_envelope'}
            raw = {k: raw[k] for k in ('model', 'answers', 'usage', 'transport_error', 'http_status') if k in raw}
            raw_bytes = encoded(raw).replace(key.encode(), b'[REDACTED]')
            raw_bytes = re.sub(rb'data:image/[^"\s]+', b'[IMAGE_DATA_REDACTED]', raw_bytes)
            raw = json.loads(raw_bytes)
            write_new(run / 'raw' / f'{attempt_id}.json', raw_bytes)
            result = {'image_id': row['image_id'], 'finished_at': now(), 'raw_sha256': sha(raw_bytes),
                      'status': 'transport_error', 'candidate': None, 'cost': None,
                      'transport_kind': receipt['transport_kind'], 'attempt_id': attempt_id}
            try:
                result.update(parse_answers(raw, config))
                result['cost'] = usage_cost(raw)
            except (SafeError, TypeError, KeyError, ValueError):
                result['status'] = 'transport_error' if 'transport_error' in raw else 'invalid_response'
                result['candidate'] = None
            write_new(run / 'results' / f'{attempt_id}.json', encoded(result))
            total, attempted = accounting(run, manifest)
            if result['status'] not in ('completed', 'partial_refusal', 'refusal'):
                return {'status': result['status'], 'new_attempts': completed,
                        'http_status': raw.get('http_status'), 'cost_upper_usd': str(total), 'automatic_retries': 0}
        return {'status': 'batch_finished', 'new_attempts': completed, 'total_attempts': len(attempted),
                'cost_upper_usd': str(total)}
    finally:
        lock.unlink()


def latest_human(run, row):
    human = dict(row['human_original'])
    for path in sorted((run / 'reviews').glob('*.json')):
        revision = json.loads(read_regular(path, 100_000))
        if revision['image_id'] == row['image_id']:
            human = revision['human']
    return human


def import_review(run, draft):
    run = local_path(run)
    manifest, _ = load_run(run)
    rows = {r['image_id']: r for r in manifest['images']}
    entries = json.loads(read_regular(local_path(draft), 200_000))
    if not isinstance(entries, list) or len({e['image_id'] for e in entries}) != len(entries):
        raise SafeError('Review draft must be a list without duplicate image IDs.')
    changes = []
    for entry in entries:
        if set(entry) != {'image_id', *HUMAN_FIELDS} or entry['image_id'] not in rows:
            raise SafeError('Review draft has unknown fields or image IDs.')
        human = {k: entry[k] for k in HUMAN_FIELDS}
        if not all(isinstance(v, str) for v in human.values()):
            raise SafeError('Human fields must be strings; unknown is allowed.')
        previous = latest_human(run, rows[entry['image_id']])
        if human == previous or (human == rows[entry['image_id']]['human_original'] and human['human_review_status'] == 'unconfirmed'):
            continue
        if human['human_review_status'] not in {'reviewed', 'needs_adjudication'}:
            raise SafeError('Changed human rows need an explicit review status.')
        if not human['human_reviewer'].strip() or not human['human_notes'].strip():
            raise SafeError('Human review requires reviewer and evidence/uncertainty notes.')
        date.fromisoformat(human['human_review_date'])
        for key in ['human_cheek_visibility', 'human_eye_ring_visibility']:
            if human[key] not in VISIBILITY:
                raise SafeError('Invalid human visibility; use unknown when uncertain.')
        if human['human_pattern_topology'] not in PATTERNS or human['human_eye_ring_apparent_hue'] not in HUES:
            raise SafeError('Invalid human candidate category.')
        if human['human_photo_side_confirmed'] not in {'match', 'mismatch', 'uncertain', 'unknown'}:
            raise SafeError('Side confirmation must be match/mismatch/uncertain/unknown.')
        flags = set(human['human_quality_flags'].split(';'))
        if not flags <= QUALITY or not flags or ('none_observed' in flags and len(flags) > 1):
            raise SafeError('Invalid human quality flags.')
        for visibility, trait in [('human_cheek_visibility', 'human_pattern_topology'),
                                  ('human_eye_ring_visibility', 'human_eye_ring_apparent_hue')]:
            if human[visibility] in {'unknown', 'unassessable'} and human[trait] != 'unknown':
                raise SafeError('Unassessable human regions must retain unknown traits.')
        compared = ['human_cheek_visibility', 'human_eye_ring_visibility', 'human_pattern_topology', 'human_eye_ring_apparent_hue']
        if (previous['human_review_status'] != 'unconfirmed'
                and previous['human_reviewer'] != human['human_reviewer']
                and any(previous[k] != human[k] for k in compared)
                and human['human_review_status'] != 'needs_adjudication'):
            raise SafeError('Reviewer disagreement must be saved as needs_adjudication.')
        changes.append({'image_id': entry['image_id'], 'saved_at': now(), 'human': human})
    for revision in changes:
        filename = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '-' + uuid.uuid4().hex + '.json'
        write_new(run / 'reviews' / filename, encoded(revision))
    return {'status': 'human_revisions_saved', 'new_revisions': len(changes)}


def csv_safe(value):
    value = '' if value is None else str(value)
    return "'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value


def export(run, output):
    run, output = local_path(run), local_path(output)
    ensure_ignored(output)
    manifest, config = load_run(run)
    records, cards = [], []
    for row in manifest['images']:
        result = read_result(run, row['image_id'])
        human = latest_human(run, row)
        source = row['source']
        verified_sex = {'f': 'female', 'm': 'male', 's': 'sneaker_male'}.get(source['source_sex'], 'unknown') if source['source_validated_sex'] == '1' else 'unknown'
        record = {k: row[k] for k in ['image_id', 'individual_id', 'split_group', 'identity_basis', 'observation_id', 'photo_ref', 'image_sha256', 'source_observation_ambiguous']}
        record.update(source)
        record['source_length_mm'] = record.pop('source_length')
        record['source_head_w_px'] = record.pop('source_head_w')
        record['source_head_h_px'] = record.pop('source_head_h')
        record.update(source_sex_verified=verified_sex, model=MODEL,
                      ai_status=result['status'] if result else 'attempted_unknown' if (run / 'attempts' / f"{row['image_id']}.json").exists() else 'not_run',
                      ai_evidence_kind=result['transport_kind'] if result else 'none',
                      raw_sha256=result['raw_sha256'] if result else '')
        candidate = result.get('candidate') or {} if result else {}
        for question in config['questions']:
            record['ai_' + question['name']] = candidate.get(question['name'])
        record.update(human)
        records.append(record)
        photo = urllib.parse.quote(os.path.relpath(ROOT / DATASET / row['photo_ref'], output.parent))
        cards.append('<article><h2>' + html.escape(row['image_id']) + '</h2><a href="' + photo + '"><img src="' + photo + '" alt="Fish head crop"></a><p>Human: ' + html.escape(human['human_review_status']) + '; evidence: ' + html.escape(record['ai_evidence_kind']) + '</p><details><summary>Show model candidates after your independent review</summary><pre>' + html.escape(json.dumps(candidate, ensure_ascii=False, indent=2)) + '</pre></details></article>')
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(records[0]))
    writer.writeheader()
    writer.writerows({k: csv_safe(v) for k, v in r.items()} for r in records)
    page = '<!doctype html><meta charset="utf-8"><title>Fish head review</title><style>body{font:16px system-ui;max-width:1000px;margin:32px auto;padding:16px}article{border-top:1px solid #ccc;padding:20px 0}img{max-width:100%;max-height:650px}pre{white-space:pre-wrap}summary{cursor:pointer}</style><h1>Head-photo review</h1><p>Photograph appearance only. Human review remains pending until explicitly imported. Review photos before revealing model candidates. Edit human-review-draft.json, then use the review command. Sources and model answers are preserved separately.</p>' + ''.join(cards)
    page_path = output.with_suffix('.html')
    if output.exists() or page_path.exists():
        raise SafeError('Export already exists; choose a new output name.')
    write_new(output, stream.getvalue().encode('utf-8-sig'))
    write_new(page_path, page.encode())
    return {'status': 'exported', 'rows': len(records), 'human_reviewed': sum(r['human_review_status'] == 'reviewed' for r in records)}


def summary(run):
    run = local_path(run)
    manifest, _ = load_run(run)
    counts, input_tokens, estimate = {}, 0, Decimal('0')
    for row in manifest['images']:
        result = read_result(run, row['image_id'])
        status = result['status'] if result else 'attempted_unknown' if (run / 'attempts' / f"{row['image_id']}.json").exists() else 'not_run'
        counts[status] = counts.get(status, 0) + 1
        if result and result.get('cost'):
            input_tokens += result['cost']['input_tokens']
            estimate += Decimal(result['cost']['estimated_base_usd'])
    total, attempted = accounting(run, manifest)
    return {'model': MODEL, 'endpoint': ENDPOINT, 'statuses': counts, 'attempts': len(attempted),
            'input_tokens_with_verified_usage': input_tokens, 'base_cost_estimate_usd': str(estimate),
            'cost_upper_including_unresolved_attempts_and_prior_usd': str(total),
            'human_reviewed': sum(latest_human(run, r)['human_review_status'] == 'reviewed' for r in manifest['images']),
            'accuracy_evaluated': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare', help='Verify local inputs and prepare a new run; no credentials/network.')
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--prior-cost-upper-usd')
    prep.add_argument('--prior-audit', type=Path)
    run = sub.add_parser('run', help='Explicit paid upload to Decisions API; default one new image, zero retries.')
    run.add_argument('--run', type=Path, required=True)
    run.add_argument('--live', action='store_true')
    run.add_argument('--approve-upload', action='store_true')
    run.add_argument('--max-new', type=int, default=1)
    run.add_argument('--retry-failed-once', action='store_true', help='Explicitly retry one audited failure once; preserve the old attempt and cost reserve.')
    review = sub.add_parser('review', help='Import human edits as append-only revisions.')
    review.add_argument('--run', type=Path, required=True)
    review.add_argument('--input', type=Path, required=True)
    out = sub.add_parser('export', help='Export allowlisted CSV and local HTML; never overwrite.')
    out.add_argument('--run', type=Path, required=True)
    out.add_argument('--output', type=Path, required=True)
    report = sub.add_parser('summary')
    report.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            result = prepare(args.output, args.prior_cost_upper_usd, args.prior_audit)
        elif args.command == 'run':
            result = run_live(args.run, live=args.live, approve_upload=args.approve_upload, max_new=args.max_new, retry_failed_once=args.retry_failed_once)
        elif args.command == 'review':
            result = import_review(args.run, args.input)
        elif args.command == 'export':
            result = export(args.run, args.output)
        else:
            result = summary(args.run)
        print(json.dumps(result, ensure_ascii=False))
        if result.get('status') in {'budget_stop', 'transport_error', 'invalid_response'}:
            raise SystemExit(2)
    except SafeError as error:
        parser.exit(2, f'Traits stopped: {error}\n')
    except (OSError, ValueError, KeyError, TypeError, csv.Error):
        parser.exit(2, 'Traits stopped: local data or output validation failed; no secret/error body displayed.\n')


if __name__ == '__main__':
    main()
