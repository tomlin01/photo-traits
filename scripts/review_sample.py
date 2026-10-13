"""Prepare an offline review queue; never imply that image pixels were acquired."""
import argparse
import csv
import json
from pathlib import Path

from metadata import ROOT, SOURCE, build, digest, inspect, parse


def prepare(data, count=15):
    if not 10 <= count <= 20:
        raise ValueError('Choose 10–20 tagged individuals')
    if len(data) > SOURCE['metadata']['max_bytes'] or digest(data, 'md5') != SOURCE['metadata']['md5']:
        raise ValueError('Pinned metadata checksum/size verification failed')
    report, _, _ = inspect(data, count)
    if report['errors']:
        raise ValueError('Metadata validation failed')
    _, rows, _ = parse(data)
    # Exclude an entire identity if any row lacks a PIT flag; keep all visits/sides.
    excluded = {r['ID'] for r in rows if r['tagged'] != '1'}
    eligible = [r for r in rows if r['ID'] not in excluded]
    individuals, observations, images = build(eligible, count)
    source = {r['filename_year']: r for r in eligible}
    review = []
    for image in images:
        r = source[image['filename_year']]
        review.append({
            'individual_id': r['ID'], 'split_group': r['ID'], 'split': 'pilot_review_only',
            'observation_id': image['observation_id'], 'filename_year': r['filename_year'],
            'side_source': r['side'], 'blurry_source': r['blurry'],
            'sex_source': r['sex'], 'validated_sex_source': r['validated_sex'],
            'length_source_mm': r['length'],
            'image_status': 'not_acquired', 'image_path': '', 'image_sha256': '',
            'pixel_review_status': 'not_reviewed', 'side_pixel_check': 'unknown',
            'scale_visibility': 'unknown', 'head_completeness': 'unknown',
            'eye_visibility': 'unknown', 'operculum_pattern_visibility': 'unknown',
            'missing_reason': 'image_not_acquired', 'evidence_notes': '',
            'reviewer': '', 'reviewed_at': '', 'expert_review_status': 'pending',
        })
    summary = {'status': 'metadata_only_pending_pixels', 'individuals': len(individuals),
               'observations': len(observations), 'candidate_image_rows': len(review),
               'images_acquired': 0, 'pixels_reviewed': 0, 'downloaded_image_bytes': 0,
               'download_budget_bytes': 100_000_000, 'metadata_sha256': digest(data),
               'sampling': 'SHA256-ranked PIT IDs; all linked visits and sides retained',
               'split_policy': 'pilot only; these individuals and all crops/visits must stay together',
               'traits_status': 'draft, not validated against pixels or expert annotations'}
    return summary, review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'data/raw/Melops_metadata.txt')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/derived/review-pilot')
    parser.add_argument('--individuals', type=int, default=15)
    args = parser.parse_args()
    try:
        with args.input.open('rb') as stream:
            data = stream.read(SOURCE['metadata']['max_bytes'] + 1)
        summary, rows = prepare(data, args.individuals)
        args.output.mkdir(parents=True, exist_ok=False)
        with (args.output / 'review_queue.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
        print(json.dumps(summary, indent=2))
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(2, f'Review queue failed: {error}\n')


if __name__ == '__main__':
    main()
